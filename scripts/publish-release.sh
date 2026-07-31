#!/usr/bin/env bash
# 发版收尾：不等 GitHub Actions 排队，直接把新版本送到用户手上。
#
# 背景：`.github/workflows/release.yml` 负责构建各平台安装包、补 Release 附件、
# 更新 Homebrew 配方；但 GitHub 免费额度的并发上限（尤其 macOS 只有 5 个并发槽，
# 且按账号而非按仓库计）经常让整批任务排队几十分钟。真实发生过：v0.24.25 的
# release 任务排了 45 分钟仍未开始，用户 `brew upgrade` 拿到的还是三个版本前的
# 配方。本脚本把「用户能不能升级」这件事从 CI 的排队里摘出来，本机几十秒做完：
#
#   1. 确保 tag 对应的 GitHub Release 存在；
#   2. 构建**本机平台**能出的安装包 + 源码包，上传到该 Release；
#   3. 把 Homebrew 配方指到这个 tag 的源码归档（配方本身从源码编译，不依赖上面
#      那些附件，所以 macOS 用户即使没有预编译包也能立刻升级）。
#
# CI 之后跑完会补齐本机出不了的那部分附件（在 Linux 上发版就是 macOS 预编译包，
# 反之亦然），并对同名附件做覆盖上传，两边不冲突。
#
# 用法（在 cli/ 目录，或用绝对路径）：
#   bash scripts/publish-release.sh            # 版本号取 pyproject.toml
#   bash scripts/publish-release.sh v0.24.26   # 显式指定
#
# 可选环境变量：
#   PICKUP_SKIP_WHEELS=1   跳过构建/上传安装包
#   PICKUP_SKIP_TAP=1      跳过更新 Homebrew 配方
#   HOMEBREW_TAP_TOKEN     写 tap 仓库用的令牌（默认取 `gh auth token`）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

TAP_REPO="${PICKUP_TAP_REPO:-x0c/homebrew-tap}"
SOURCE_REPO="${PICKUP_REPO:-x0c/pickup}"

die() { echo "错误：$*" >&2; exit 1; }

command -v gh >/dev/null || die "未找到 gh 命令，无法操作 GitHub Release"
gh auth status >/dev/null 2>&1 || die "gh 未登录，先执行 gh auth login"

TAG="${1:-}"
if [ -z "$TAG" ]; then
  TAG="v$(python3 -c '
import re, pathlib
text = pathlib.Path("pyproject.toml").read_text(encoding="utf-8")
print(re.search(r"^version = \"([^\"]+)\"", text, re.M).group(1))
')"
fi
VERSION="${TAG#v}"
echo "==> 发布 ${TAG}"

git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null \
  || die "本地没有 ${TAG} 标签，先打好标签再跑本脚本"
git ls-remote --exit-code --tags github "refs/tags/${TAG}" >/dev/null 2>&1 \
  || die "${TAG} 还没推到 github 远端，先 git push github --tags"

# ---- 1. Release 本体 ----
if gh release view "$TAG" >/dev/null 2>&1; then
  echo "==> Release ${TAG} 已存在"
else
  echo "==> 创建 Release ${TAG}"
  gh release create "$TAG" --title "$TAG" --generate-notes
fi

# ---- 2. 安装包 ----
if [ "${PICKUP_SKIP_WHEELS:-0}" = "1" ]; then
  echo "==> 跳过安装包构建（PICKUP_SKIP_WHEELS=1）"
else
  command -v maturin >/dev/null || die "未找到 maturin，先执行 pip install 'maturin>=1.9,<2'"
  DIST="$(mktemp -d)"
  trap 'rm -rf "$DIST"' EXIT
  case "$(uname -s)" in
    Darwin)
      # macOS 只出 universal2：Intel / Apple Silicon 共用一个包。
      maturin build --release --out "$DIST" --target universal2-apple-darwin
      SKIPPED="Linux 各架构预编译包（需在 Linux 上发版或等 CI 补齐）"
      ;;
    Linux)
      # 交叉编译靠 zig；缺了就只出本机架构，不静默少传。
      ZIG_ARG=()
      if python3 -c "import ziglang" >/dev/null 2>&1; then
        ZIG_ARG=(--zig)
      else
        echo "提示：未安装 ziglang（pip install ziglang），本轮只出本机架构" >&2
      fi
      maturin build --release --out "$DIST" --compatibility manylinux_2_17 "${ZIG_ARG[@]}"
      if [ ${#ZIG_ARG[@]} -gt 0 ]; then
        for spec in \
          "aarch64-unknown-linux-gnu manylinux_2_17" \
          "x86_64-unknown-linux-musl musllinux_1_2" \
          "aarch64-unknown-linux-musl musllinux_1_2"
        do
          set -- $spec
          rustup target add "$1" >/dev/null 2>&1 || true
          maturin build --release --out "$DIST" --target "$1" --compatibility "$2" --zig
        done
      fi
      SKIPPED="macOS 预编译包（需在 Mac 上发版或等 CI 补齐）"
      ;;
    *) die "不支持的发版平台：$(uname -s)" ;;
  esac
  maturin sdist --out "$DIST"

  # 附件里的 SHA256SUMS 只覆盖本机这批；CI 补齐后会整体覆盖成全量清单。
  python3 - "$DIST" <<'PY'
import hashlib, pathlib, sys
dist = pathlib.Path(sys.argv[1])
lines = []
for item in sorted(dist.iterdir()):
    if item.name == "SHA256SUMS":
        continue
    digest = hashlib.sha256(item.read_bytes()).hexdigest()
    lines.append(f"{digest}  {item.name}")
(dist / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

  echo "==> 上传安装包"
  # 打完 tag 之后 CI 的 release 工作流也在传同名附件，而 --clobber 是「先删再传」：
  # 两边交错时本机这次会拿到 404（附件 ID 在上传途中被对方删掉了）。这里绝不能让
  # 上传失败中断整个脚本——后面的 Homebrew 配方才是决定"用户能不能升级"的那一步，
  # 附件没传成最多是少个预编译包，配方停在旧版本才是真事故。
  # 2026-07-31 v0.24.29 实测踩到：脚本在这里退出，配方全靠 CI 恰好跑赢才没停在旧版。
  if ! gh release upload "$TAG" "$DIST"/* --clobber; then
    echo "!!  上传失败，等 8 秒重试一次（多半是和 CI 的 release 工作流抢同名附件）"
    sleep 8
    if ! gh release upload "$TAG" "$DIST"/* --clobber; then
      UPLOAD_FAILED=1
      echo "!!  安装包上传仍未成功；继续往下走，收尾核对会列出 Release 实际有几个附件"
    fi
  fi
  echo "==> 本轮未覆盖：${SKIPPED}"
fi

# ---- 3. Homebrew 配方 ----
if [ "${PICKUP_SKIP_TAP:-0}" = "1" ]; then
  echo "==> 跳过 Homebrew 配方（PICKUP_SKIP_TAP=1）"
else
  TOKEN="${HOMEBREW_TAP_TOKEN:-$(gh auth token)}"
  [ -n "$TOKEN" ] || die "拿不到可写 ${TAP_REPO} 的令牌"
  ARCHIVE="https://github.com/${SOURCE_REPO}/archive/refs/tags/${TAG}.tar.gz"
  echo "==> 计算源码归档校验和"
  SHA="$(curl -fsSL "$ARCHIVE" | python3 -c '
import hashlib, sys
print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())
')"
  WORK="$(mktemp -d)"
  git clone -q "https://x-access-token:${TOKEN}@github.com/${TAP_REPO}.git" "$WORK/tap"
  # 用 python 改写而不是 sed -i：BSD sed（macOS）与 GNU sed 的 -i 参数不兼容，
  # 而发版机大概率就是 Mac。
  # 退出码 3 = 配方已是更新的版本，属正常跳过，不能让 set -e 把脚本带走。
  rc=0
  ARCHIVE="$ARCHIVE" SHA="$SHA" VERSION="$VERSION" python3 - "$WORK/tap/Formula/pickup.rb" <<'PY' || rc=$?
import os, pathlib, re, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
current = re.search(r'^  url ".*/tags/v([^"]+)\.tar\.gz"', text, re.M)
def parts(v): return tuple(int(x) for x in re.findall(r"\d+", v))
if current and parts(current.group(1)) > parts(os.environ["VERSION"]):
    # 配方已经比本次要发的版本更新（例如补发旧 tag），不要往回退。
    print(f"配方已是更新的 {current.group(1)}，跳过")
    sys.exit(3)
text = re.sub(r'^  url ".*"', f'  url "{os.environ["ARCHIVE"]}"', text, count=1, flags=re.M)
text = re.sub(r'^  sha256 ".*"', f'  sha256 "{os.environ["SHA"]}"', text, count=1, flags=re.M)
path.write_text(text, encoding="utf-8")
PY
  if [ "$rc" -eq 0 ]; then
    if git -C "$WORK/tap" diff --quiet -- Formula/pickup.rb; then
      echo "==> 配方已经指向 ${TAG}，无需改动"
    else
      git -C "$WORK/tap" -c user.name="x0c" -c user.email="x0c@users.noreply.github.com" \
        commit -q -am "pickup ${VERSION}"
      git -C "$WORK/tap" push -q origin main
      echo "==> 配方已更新到 ${VERSION}"
    fi
  elif [ "$rc" -ne 3 ]; then
    rm -rf "$WORK"; die "改写配方失败"
  fi
  rm -rf "$WORK"
fi

echo
echo "==> 收尾核对"
gh release view "$TAG" --json tagName,assets \
  --jq '"Release \(.tagName)：\(.assets | length) 个附件"'
curl -fsSL "https://raw.githubusercontent.com/${TAP_REPO}/main/Formula/pickup.rb" \
  | grep -E '^  url ' | sed 's/^/配方 /'
curl -fsSL "https://api.github.com/repos/${SOURCE_REPO}/releases/latest" \
  | python3 -c 'import json,sys; print("最新 Release：" + json.load(sys.stdin)["tag_name"])'
if [ "${UPLOAD_FAILED:-0}" = "1" ]; then
  echo "!!  注意：本机这轮安装包上传失败过，请对照上面的附件数量确认是否需要重跑本脚本"
  exit 1
fi
