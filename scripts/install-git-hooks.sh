#!/usr/bin/env bash
# 把仓库内 .githooks 接到 Git 实际会执行的 hooks 目录。
#
# 本机若配置了全局 core.hooksPath（如 ~/.git-hooks），Git 会忽略各仓 .git/hooks。
# 此时绝不能把「只服务本仓」的脚本直接盖到全局 hooks 上——否则其它仓库推送也会跑
# pickup 的检查。做法：在 hooksPath 放一个通用分发器，仅当当前仓库有
# `.githooks/pre-push` 时才转调。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

git rev-parse --git-dir >/dev/null || {
  echo "错误：当前不在 git 仓库内" >&2
  exit 1
}

chmod +x "$ROOT/.githooks/pre-push"

HOOKS_DIR="$(git rev-parse --git-path hooks)"
# 解析成绝对路径，便于判断是否落在本仓外
HOOKS_ABS="$(cd "$HOOKS_DIR" 2>/dev/null && pwd -P || python3 -c "import pathlib; print(pathlib.Path(r'''$HOOKS_DIR''').resolve())")"
ROOT_ABS="$(pwd -P)"

is_shared=0
case "$HOOKS_ABS" in
  "$ROOT_ABS"/*|"$ROOT_ABS"/.git/*) ;;
  *) is_shared=1 ;;
esac

# 通用分发器：任意带 .githooks/pre-push 的仓库都能用，不绑死 pickup 路径
write_dispatcher() {
  local dest="$1"
  # 若 dest 是指向本仓脚本的软链，cat > 会顺着链把真脚本盖掉——先拆链再写
  if [ -L "$dest" ]; then
    rm -f "$dest"
  fi
  cat >"$dest" <<'EOF'
#!/usr/bin/env bash
# 由 pickup scripts/install-git-hooks.sh 安装的通用 pre-push 分发器。
# 仅当当前仓库存在可执行的 .githooks/pre-push 时转调；其它仓库直接放行。
set -euo pipefail
root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
hook="$root/.githooks/pre-push"
if [ -x "$hook" ]; then
  exec "$hook" "$@"
fi
exit 0
EOF
  chmod +x "$dest"
}

mkdir -p "$HOOKS_ABS"

if [ "$is_shared" -eq 1 ]; then
  dest="$HOOKS_ABS/pre-push"
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    # 已有实体文件：若已是我们的分发器则覆盖更新；否则备份后写入
    if ! grep -q '由 pickup scripts/install-git-hooks.sh 安装的通用 pre-push 分发器' "$dest" 2>/dev/null; then
      bak="$dest.backup-$(date +%Y%m%d%H%M%S)"
      cp -p "$dest" "$bak"
      echo "已备份原有 $dest → $bak"
    fi
  fi
  # 若当前是指向本仓专用脚本的错误软链（旧版安装），改成通用分发器
  write_dispatcher "$dest"
  echo "已安装通用分发器: $dest"
  echo "检测到共享 hooksPath: $HOOKS_ABS"
  echo "本仓检查脚本: $ROOT/.githooks/pre-push"
else
  mkdir -p "$HOOKS_ABS"
  rel="$(python3 -c "
import os, pathlib
src = pathlib.Path(r'''$ROOT/.githooks/pre-push''').resolve()
dst_dir = pathlib.Path(r'''$HOOKS_ABS''').resolve()
print(os.path.relpath(src, dst_dir))
")"
  ln -sfn "$rel" "$HOOKS_ABS/pre-push"
  echo "已安装 $HOOKS_ABS/pre-push → $rel"
fi

echo "==> 日常推送只拦 ruff；release: 提交或 v* 标签推送跑全量。"
