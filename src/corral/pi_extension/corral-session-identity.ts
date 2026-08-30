/**
 * Corral 会话身份桥（corral-session-identity）
 *
 * 职责单一：把「当前 Pi TUI 进程正在展示哪条会话」写成本机 claim 文件，
 * 供 Corral 把分屏精确绑定到这条会话。不注册模型/工具/命令，不读取对话
 * 正文，不联网。只在 `ctx.mode === "tui"` 时写入——SDK/RPC/JSON/print 与
 * subagent（SDK 子会话，不加载全局扩展）都不参与，避免抢占主分屏身份。
 *
 * Claim 协议 v1 字段见 Corral 设计文档
 * docs/design/PI_SESSION_IDENTITY_EXTENSION_DESIGN.md。
 */
import {
	getAgentDir,
	type ExtensionAPI,
	type ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import {
	closeSync,
	fsyncSync,
	mkdirSync,
	openSync,
	readFileSync,
	realpathSync,
	renameSync,
	unlinkSync,
	writeFileSync,
} from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { createHash, randomUUID } from "node:crypto";

const PROTOCOL_VERSION = 1;
const EXTENSION_VERSION = "1.0.0";
const IDENTITY_DIRNAME = "corral-session-identity";
/** 心跳间隔；Corral 读取侧按 4 个周期（60s）判定 claim 过期。 */
const HEARTBEAT_MS = 15_000;

interface ClaimIdentity {
	sessionId: string;
	sessionFile: string | null;
	cwd: string;
	parentSession: string | null;
}

export default function (pi: ExtensionAPI): void {
	// 扩展 reload（/new、/resume、/fork、/reload 后 Pi 会重载扩展实例）时
	// globalThis 存续：nonce 保持不变，配合 env 里的 instanceId 让 Corral 仍认得
	// 这是同一个进程。
	const globalCache = globalThis as {
		__corralPiIdentity?: { nonce: string; nativeInstanceId: string };
	};
	const processIdentity = (globalCache.__corralPiIdentity ??= {
		nonce: randomUUID(),
		nativeInstanceId: `native-${randomUUID()}`,
	});
	const nonce = processIdentity.nonce;
	const processStartedAt = new Date(Date.now() - process.uptime() * 1000).toISOString();

	// 托管 Pi 的 instance 来自 Corral env；裸 Pi 使用进程级缓存，保证
	// /new、/resume、/fork、/reload 重载扩展后仍写同一 claim 文件。
	const instanceId = process.env.CORRAL_PI_INSTANCE_ID || processIdentity.nativeInstanceId;
	let claimPath = process.env.CORRAL_PI_CLAIM_PATH || "";
	let sequence = 0;
	let lastIdentity: ClaimIdentity | null = null;
	let lastState: "" | "active" | "switching" | "shutdown" = "";
	let heartbeat: ReturnType<typeof setInterval> | undefined;
	let lastCtx: ExtensionContext | undefined;
	let ownerPath = "";
	let ownershipToken = "";
	let ownershipHeld = false;
	let blockedByOwner = false;

	function resolveClaimPath(): string {
		if (!claimPath) {
			// 裸 Pi（未经 Corral 托管）也写 claim：Corral 能精确识别它，
			// 但只认这个 namespace，不读会话正文。
			claimPath = join(
				getAgentDir(),
				"corral-session-identity",
				"claims",
				"v1",
				`${instanceId}.json`,
			);
		}
		return claimPath;
	}

	function canonicalSessionFile(sessionFile: string): string {
		const absolute = resolve(sessionFile);
		try {
			return realpathSync(absolute);
		} catch {
			let parent = dirname(absolute);
			try {
				parent = realpathSync(parent);
			} catch {
				// 文件和父目录都尚未创建时，绝对规范路径仍可稳定参与 hash。
			}
			return join(parent, basename(absolute));
		}
	}

	function ownerFile(sessionFile: string): { canonical: string; path: string } {
		const canonical = canonicalSessionFile(sessionFile);
		const hash = createHash("sha256").update(canonical, "utf-8").digest("hex");
		return {
			canonical,
			path: join(getAgentDir(), IDENTITY_DIRNAME, "owners", "v1", `${hash}.lock`),
		};
	}

	function processAlive(pid: number): boolean {
		if (!Number.isInteger(pid) || pid <= 0) return false;
		try {
			process.kill(pid, 0);
			return true;
		} catch (error) {
			// EPERM 表示进程存在但无权发信号；同样视为存活。
			return (error as { code?: string }).code === "EPERM";
		}
	}

	function readOwner(path: string): Record<string, unknown> | null {
		try {
			const value = JSON.parse(readFileSync(path, "utf-8"));
			return value && typeof value === "object" ? value as Record<string, unknown> : null;
		} catch {
			return null;
		}
	}

	function removeDeadOwner(path: string): boolean {
		const owner = readOwner(path);
		// 损坏 owner 且无法证明原进程已死时保守冲突，不擅自删除。
		if (!owner) return false;
		const pid = Number(owner.pid ?? 0);
		if (processAlive(pid)) return false;
		try {
			// 删除前再回读 token，避免 stale 检查后锁已被后来者替换。
			const current = readOwner(path);
			if (!current || current.ownershipToken !== owner.ownershipToken) return false;
			unlinkSync(path);
			return true;
		} catch {
			return false;
		}
	}

	function acquireOwnership(identity: ClaimIdentity): boolean {
		if (!identity.sessionFile) return true;
		const location = ownerFile(identity.sessionFile);
		mkdirSync(dirname(location.path), { recursive: true });
		for (let attempt = 0; attempt < 2; attempt += 1) {
			const token = randomUUID();
			const owner = {
				protocolVersion: PROTOCOL_VERSION,
				canonicalSessionFile: location.canonical,
				instanceId,
				pid: process.pid,
				processStartedAt,
				instanceNonce: nonce,
				claimPath: resolveClaimPath(),
				ownershipToken: token,
				acquiredAt: new Date().toISOString(),
			};
			let fd: number | undefined;
			try {
				fd = openSync(location.path, "wx", 0o600);
				writeFileSync(fd, JSON.stringify(owner), "utf-8");
				fsyncSync(fd);
				closeSync(fd);
				fd = undefined;
				ownerPath = location.path;
				ownershipToken = token;
				ownershipHeld = true;
				return true;
			} catch (error) {
				if (fd !== undefined) {
					try { closeSync(fd); } catch { /* 已关闭 */ }
				}
				if ((error as { code?: string }).code !== "EEXIST") return false;
				const current = readOwner(location.path);
				if (
					current?.instanceId === instanceId &&
					current?.instanceNonce === nonce &&
					current?.pid === process.pid
				) {
					ownerPath = location.path;
					ownershipToken = String(current.ownershipToken ?? "");
					ownershipHeld = Boolean(ownershipToken);
					return ownershipHeld;
				}
				if (!removeDeadOwner(location.path)) return false;
			}
		}
		return false;
	}

	function ownershipConflict(sessionFile: string | null | undefined): boolean {
		if (!sessionFile) return false;
		const path = ownerFile(sessionFile).path;
		if (ownershipHeld && path === ownerPath) return false;
		const current = readOwner(path);
		if (!current) return false;
		if (!processAlive(Number(current.pid ?? 0)) && removeDeadOwner(path)) return false;
		return true;
	}

	function releaseOwnership(): void {
		if (!ownershipHeld || !ownerPath || !ownershipToken) return;
		try {
			const current = readOwner(ownerPath);
			if (
				current?.instanceId === instanceId &&
				current?.instanceNonce === nonce &&
				current?.ownershipToken === ownershipToken
			) {
				unlinkSync(ownerPath);
			}
		} catch {
			// 退出清理失败由下一位 writer 的死进程回收处理。
		} finally {
			ownerPath = "";
			ownershipToken = "";
			ownershipHeld = false;
		}
	}

	function notifyOwnerConflict(ctx: ExtensionContext): void {
		ctx.ui.notify(
			"该 Pi 会话已在另一个窗口运行，已阻止重复写入。/ This Pi session is already open in another window; duplicate writing was blocked.",
			"error",
		);
	}

	function writeClaim(
		state: "active" | "switching" | "shutdown",
		reason: string,
		identity: ClaimIdentity | null,
		targetSessionFile: string | null = null,
	): void {
		const ctx = lastCtx;
		if (!ctx || !identity || !identity.sessionId) return;
		const claim = {
			protocolVersion: PROTOCOL_VERSION,
			extensionVersion: EXTENSION_VERSION,
			instanceId,
			pid: process.pid,
			processStartedAt,
			instanceNonce: nonce,
			state,
			sessionId: identity.sessionId,
			sessionFile: identity.sessionFile,
			cwd: identity.cwd,
			parentSession: identity.parentSession,
			reason,
			targetSessionFile,
			updatedAt: new Date().toISOString(),
			sequence: ++sequence,
		};
		const path = resolveClaimPath();
		try {
			mkdirSync(dirname(path), { recursive: true });
			// 同目录临时文件 + 原子替换，读侧永远看不到半份 JSON。
			const tmp = `${path}.tmp-${process.pid}-${sequence}`;
			writeFileSync(tmp, JSON.stringify(claim), "utf-8");
			renameSync(tmp, path);
		} catch {
			// claim 写失败只影响 Corral 精确关联，绝不干扰 Pi 本体。
		}
	}

	function currentIdentity(ctx: ExtensionContext): ClaimIdentity | null {
		const sm = ctx.sessionManager;
		try {
			const header = sm.getHeader() as { parentSession?: string } | undefined;
			return {
				// getSessionFile() 对尚未落盘的会话返回 undefined：照实写 null，
				// Corral 保持 provisional，等文件出现后转正。
				sessionId: sm.getSessionId() ?? "",
				sessionFile: sm.getSessionFile() ?? null,
				cwd: sm.getCwd() ?? ctx.cwd,
				parentSession: header?.parentSession ?? null,
			};
		} catch {
			return null;
		}
	}

	function stopHeartbeat(): void {
		if (heartbeat !== undefined) {
			clearInterval(heartbeat);
			heartbeat = undefined;
		}
	}

	function startHeartbeat(): void {
		if (heartbeat !== undefined) return;
		heartbeat = setInterval(() => {
			if (lastState === "active" && lastIdentity) {
				writeClaim("active", "heartbeat", lastIdentity);
			}
		}, HEARTBEAT_MS);
		// 心跳只服务 Corral 读取侧的时效校验，不得阻止进程退出。
		if (typeof heartbeat.unref === "function") heartbeat.unref();
	}

	/** 跨扩展 reload 续接 sequence：新实例不得把序号写回比磁盘更小的值。 */
	function resumeSequence(): void {
		try {
			const raw = readFileSync(resolveClaimPath(), "utf-8");
			const parsed = JSON.parse(raw) as { sequence?: number; instanceId?: string };
			if (
				parsed &&
				typeof parsed.sequence === "number" &&
				parsed.instanceId === instanceId &&
				parsed.sequence > sequence
			) {
				sequence = parsed.sequence;
			}
		} catch {
			// 首次运行或旧 claim 缺失/损坏：从 0 起步即可。
		}
	}

	pi.on("session_start", async (event, ctx) => {
		if (ctx.mode !== "tui") return;
		lastCtx = ctx;
		resumeSequence();
		const identity = currentIdentity(ctx);
		if (!identity) return;
		lastIdentity = identity;
		if (!acquireOwnership(identity)) {
			blockedByOwner = true;
			lastState = "shutdown";
			notifyOwnerConflict(ctx);
			ctx.shutdown();
			return;
		}
		blockedByOwner = false;
		lastState = "active";
		writeClaim("active", event.reason ?? "startup", identity);
		startHeartbeat();
	});

	pi.on("session_before_switch", async (event, ctx) => {
		if (ctx.mode !== "tui") return;
		lastCtx = ctx;
		if (event.targetSessionFile && ownershipConflict(event.targetSessionFile)) {
			notifyOwnerConflict(ctx);
			return { cancel: true };
		}
		if (!lastIdentity) return;
		// /new、/resume 切换中：保留旧身份写 switching，targetSessionFile 只作
		// 诊断；Corral 维持原 pane 归属，等新 session_start 覆盖成 active。
		lastState = "switching";
		writeClaim(
			"switching",
			event.reason ?? "switch",
			lastIdentity,
			event.targetSessionFile ?? null,
		);
	});

	pi.on("input", async (_event, ctx) => {
		if (!blockedByOwner) return { action: "continue" as const };
		notifyOwnerConflict(ctx);
		return { action: "handled" as const };
	});

	pi.on("session_shutdown", async (event, ctx) => {
		if (ctx.mode !== "tui") return;
		stopHeartbeat();
		releaseOwnership();
		lastCtx = ctx;
		if (!lastIdentity) return;
		const reason = event.reason ?? "quit";
		if (reason === "quit") {
			// 真正退出：写 shutdown，Corral 把这个 pane 转为静态历史。
			lastState = "shutdown";
			writeClaim("shutdown", reason, lastIdentity);
		} else {
			// new/resume/fork/reload：进程还在换会话，扩展实例即将被重载。
			// 写 switching 而不是 shutdown，避免读取侧在替换窗口把 pane 判成
			// 已结束或改绑别人；新实例的 session_start 会覆盖成 active。
			lastState = "switching";
			writeClaim(
				"switching",
				reason,
				lastIdentity,
				event.targetSessionFile ?? null,
			);
		}
	});
}
