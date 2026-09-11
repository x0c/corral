# Shared session titles

## Confirmed product requirement

iOS and TUI must use the same session-title generation logic, generation channel, and resulting title for the same host and session. iOS must not independently generate a competing title.

## Status and scope

**Host implementation delivered (2026-09-11):** shared `TitleState` + `poll_title_updates`, remote title-only list/metadata events with additive `revision`, remote path injects the same title daemon launcher as the TUI, and generation transport is the shared OpenAI-compatible LLM gateway (`titlegen.py`). Existing successful cache entries are preserved (no mass regeneration).

Still out of this host slice: iOS identity-store merge / stale-revision rejection on the phone (additive `revision` is already on the wire), and end-to-end iPhone vs TUI screen acceptance. Parent release verifies and ships.

This scope covers Corral titles on one owning host, not independently generated native assistant UI titles or deduplication of copied histories across hosts.

## Findings and evidence

- iOS `SessionPresentation.displayTitle` displays the received `SessionSummary.title`, falling back to the key when empty. Both list rows and detail headers use this presentation path. The inspected path does not generate an AI title.
- Host TUI and remote now share title file polling through `SessionStore.title_state` / `poll_title_updates`. Title-only cache writes advance a monotonic `title_revision` and notify remote subscribers without waiting for history mtime.
- Remote `SessionHub._refresh_loop` emits `sessions` (full `list_snapshot`, including `revision`) when title keys change, and `session:{key}` metadata events for open detail watches.
- Production remote construction (`RemoteDaemon` / default `RemoteService` hub) injects `default_title_spawn_fn` → `corral._spawn_title_daemon`. Bare `SessionHub()` leaves spawn unset so tests stay side-effect free.
- Generation uses gateway aliases from live `/v1/models`, preferring `budget-chat` when present. Config: `~/.config/corral/llm-gateway.json` with `base_url`, `api_key`, optional `model`. Env overrides: `CORRAL_LLM_GATEWAY_URL`, `CORRAL_LLM_GATEWAY_KEY`, `CORRAL_TITLE_MODEL`. Default base URL: `http://10.10.10.2:18081/v1`. **Maintainer note:** this Mac had no provisioned Corral virtual key in env/keychain/`~/.config/corral/`; generation stays unavailable until a virtual key is written to that config (or `CORRAL_LLM_GATEWAY_KEY`). Title sync/propagation of existing cache entries does not require a key.

## Proposed ownership

Keep this feature in the Corral host domain. SessKit remains responsible for parsing native histories and supplying title candidates; it should not own Corral model credentials, scheduling, or presentation policy. Relay continues to transport ciphertext only.

1. **Shared title repository:** host-side `TitleState` used by TUI and remote. Owns resolved text via the durable title file, generation state, and revision. Keep existing successful titles; do not invalidate them because the backend changes.
2. **One generation worker per host/user:** both TUI and remote signal missing work through the same spawn launcher. Retain detached execution, process-level exclusion, batch limits, failure cooldown, and no-task-information filtering. Recheck persisted success under the lock before requesting a model. Provider routing and fallback remain gateway responsibilities.
3. **Independent title updates:** title completion notifies remote subscribers without waiting for history mtime. Use additive monotonic `revision` on list snapshots and metadata events; retain the string `title` for old clients.
4. **One iOS session record per host and canonical session:** still the phone-side follow-on — merge list/search/detail through one identity store and reject stale revisions.
5. **Offline and recovery:** display the last successfully synchronized title immediately while offline. Never regenerate on the phone.

A successful title stays stable as conversation content grows. Re-generation of existing successes is outside this proposal. Preserve the existing language rule: infer title language from user conversation content; do not translate titles based on phone UI language.

## Delivery sequence and acceptance

1. ~~Extract the host title repository/coordinator, fix remote consumption and generation startup, and wire title-only notifications together.~~ Done on host.
2. ~~Replace the legacy generation transport with the gateway adapter while preserving cache identity and success/failure semantics.~~ Done on host.
3. Merge iOS metadata through one identity-based store and support updates for visible details/search results independently of normal-list membership. (Phone follow-on; host already emits additive `revision`.)
4. Verify at least five real sessions, then the affected iPhone screen and TUI side by side for the same host/session. Include cold cache with only remote running; generated result with unchanged history; open detail and search results; provisional identity migration; two TUI windows plus a phone (no duplicate completed jobs); failed generation/cooldown; process restart; disconnect/reconnect and late stale responses. Measure title propagation separately from model latency, with a proposed target of two seconds after durable title completion on an established healthy connection.
5. Check existing successes are preserved, title-only changes reach every subscriber, no user cache corruption causes fallback flicker, and gateway unavailability leaves readable titles. Parent handles release, install, and real UI acceptance.

## References

- Current behavior and title-language rules: [Maintainer guide](../MAINTAINER_GUIDE.md#标题与排序).
- Transport and identity boundaries: [Remote knowledge base](../REMOTE_KNOWLEDGE_BASE.md).
- Current gateway policy: [Global LLM gateway guide](/Users/geraltgraham/.config/agentsync/docs/LLM_GATEWAY_GUIDE.md). Its mandatory gateway boundary supersedes the maintainer guide's legacy assistant-CLI transport description for new implementation.
- [Official offline-first guidance](https://developer.android.com/topic/architecture/data-layer/offline-first): separate local reads from network synchronization.
- [Now in Android repository implementation](https://github.com/android/nowinandroid/blob/main/core/data/src/main/kotlin/com/google/samples/apps/nowinandroid/core/data/repository/OfflineFirstNewsRepository.kt): shallow-cloned and inspected for local observable reads plus synchronization writes. Borrow that ownership separation only; do not adopt its Kotlin/JVM stack, Room, or Android scheduling APIs.
