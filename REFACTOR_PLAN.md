# LCNC Suite — Architecture Refactor Plan

**Branch:** `refactor/architecture-authority` (off `development`)
**Source:** derived from `ARCHITECTURE_HANDOFF.md` after the 2026-06-03 reviewer re-baseline.
**Status:** planning artifact — no behavior changes yet.

## Status — 2026-06-03 (tracker reconciliation)

Most underlying issues have since closed; the remaining surface is small.
**⚠ = flagged as only *partially* done — verify before treating as complete.**

**Closed (resolved):**
- §8 security — #17 auth, #18 queue-drop, #20 symlink-realpath ✅
- §5 cache-to-INI keying — #29 ✅
- §6 renumber transaction — #30 ✅
- silent fallbacks — #21 ✅ · misc — #22, #23, #25, #32 ✅

**Reopened — shipped partially, residual tracked on the original issue:**
- **#27** (validation) — bounded-error boundary shipped; ~20 raw `int()`/`float()` casts remain, incl. a live Inf/NaN-into-tool-table hole at `gateway.py:3698`.
- **#24** (persistence locks) — settings lock shipped; tool-write lock missing + `threading.Lock`-in-asyncio unresolved.

**Still open — remaining work, in priority order:**
1. **#19** backend permission gates + **#31** frontend command-policy path → **WS1** (highest value)
2. **#27** finish payload validation (WS1 validation half — reopened)
3. **#26** lint/test/tooling coverage → **WS2** (must precede #33)
4. **#24** tool-persistence lock + store classes → **WS3** (reopened; #30 done)
5. **#33** gateway modularization → **WS4** (gated on #26)
6. **#28** request IDs → deferred (WS5)

Net: WS1 (#19/#27/#31) is the top open cluster; WS3 partially done (#30) with #24 reopened
for the remainder; WS2 (#26) still gates WS4 (#33); the deferred set is just #28.

## Purpose

Make machine-control authority **explicit** without a rewrite. The handoff doc's thesis is
correct (*backend owns permission · schemas own validation · stores own persistence · frontend
owns interaction, not trust*), but its "Current Shape" sections predate the hardening commits.
This plan acts only on what is **actually still open**, in an order that is safe for a
~6800-line safety-critical gateway.

## Guiding principles (from project memory)

- **Authorization, not deadman.** Backend gates reject *new* commands; they never abort motion.
  Motion-abort safety stays in the HAL chain. (`feedback_armed_is_authorization_not_deadman`)
- **No silent fallbacks.** A rejected command returns a bounded `{ok: false, reason}`; a skipped
  branch emits a trace event. Never mask absent data with a synthetic default.
  (`feedback_no_silent_fallbacks`)
- **The gate is the fix.** Enforce state in the proper layer so the UI reflects it — do not work
  around permission gaps in the backend with ad-hoc checks.
- **Tests before the knife.** Characterization tests must exist before any module is extracted
  from `gateway.py`.

## What is explicitly OUT of scope (already done or deferred)

- **§8 security** — token + origin gate on WS, `require_token` on HTTP mutations, restricted CORS:
  **done**. Only two documented residuals remain (served-token weakness → `project_stronger_token_mode`;
  CORS `["*"]` fallback → document it). Not a workstream here.
- **§2 / §7 requestIds + typed frontend dispatcher** — **deferred.** Low value for a single-operator
  UI. Revisit only if the multi-client / pendant / mobile goal is pursued, or a concurrent
  command/reply bug is observed.
- **§5 runtime epochs** — **deferred.** Version counters + INI-change invalidation already exist.
  Revisit only when a concrete stale-cache / INI-switch symptom appears.

---

## Workstream 1 — Backend safety-invariant command policy  *(highest value — now top open item)*

> **Status (2026-06-04):**
> - **#27 validation** — items 1–2 shipped (`b7acf0b`); item 3 (range/enum) deferred to WS2.
> - **#19 policy engine** — pure `command_policy.py` built + tested, committed **inert** (`ee678fc`).
> - **Architecture decided (2026-06-04): UNIFY** — see below. This supersedes the original
>   "backend mirrors the frontend policy" framing (which kept the rules in two places).

### Architecture decision (2026-06-04) — one policy definition, consumed everywhere

Three layers (frontend / backend / LinuxCNC), but two *different* things flow through them:

- **State** (homed/idle/estop/eoffset/…) originates in LinuxCNC + HAL and already flows one-way:
  LCNC → gateway (poll) → broadcast → frontend. **LinuxCNC is the state authority; nobody invents
  state.** Unchanged.
- **Policy** (which state permits which command) does **not** exist in LinuxCNC — it is permissive
  by design (`cycle_start` unhomed, touch-off with eoffset active, the whole `armed` concept: none
  are enforced by LCNC). So *we* must define policy — but only **once**.

**Decision:** the policy *formulas* live once, in `command_policy.py` (Python, backend). The
backend (a) **enforces** them on every incoming command and (b) **broadcasts** the evaluated
permission classes in the status payload. The frontend stops re-deriving them — `permissions.ts`
drops `evaluatePermissions` and becomes a thin **consumer**. This *removes* the
`permissions.ts` ↔ `command_policy.py` duplication rather than adding a third copy.

**Why the frontend still applies two terms (not duplication).** The status payload is a single
shared broadcast (encoded once, sent verbatim to all clients — `gateway.py:315`); per-client
`armed` lives in the `clients` envelope, not in the payload. So the backend broadcasts permissions
computed as if `armed=True`, and the frontend ANDs the only two genuinely client-local inputs it
owns: `armed` (per-client authorization) + `busy` (per-tab debounce), with `always` exempt from
the `armed` AND. The drift-prone combinatorics (homed/idle/running/paused/eoffset/estop/enabled)
live solely in the backend.

**Server-side move — estop/enabled "merged truth."** The frontend merges `STAT.estop`/`enabled`
with the HAL chain (`emc_enable_in`) at its `isEstop`/`isEnabled` computeds (issue #14 edge-detect
guard). That merge must move into the backend `MachineState` builder so the broadcast permissions
are correct. `emc_enable_in` is already in `StatusPayload`.

### Implementation sequence

1. **Backend broadcast** *(zero machine-risk, display-only)* — add `_policy_state(armed)` building
   `command_policy.MachineState` from polled values (incl. the estop/enabled merge); compute
   `evaluate_permissions(state(armed=True))`; ship it in the broadcast. No command is rejected.
2. **Frontend consume** — `permissions.ts` drops `evaluatePermissions`, adds a consumer reading
   `status.permissions` + overlay (`armed` on all but `always`; `!busy` on the busy-subset; all
   false when no status). App.vue keeps providing `PERMISSIONS_KEY` — every MachineBtn/Gate
   consumer is untouched. `npm run build` must stay green.
3. **Enforcement (#19 wiring — needs COMMAND_GATES mapping sign-off)** — central
   `check_command(cmd, _policy_state(client_armed))` in `_handle_command_impl` → bounded deny.
4. **#31** folds in once the frontend has a single permission source.

Land **1+2 together** (they're a matched pair); **3 after** the mapping is signed off. The
"safety-invariant scope" table below still describes what step 3 enforces.

**Goal:** a direct WS client (even with the LAN token) cannot drive the machine into states the UI
forbids — *and* the policy is defined exactly once.

**Scope — enforce only these invariants** (not all 14 frontend classes):

| Invariant | Applies to (examples) |
|---|---|
| homed-before-motion | `cycle_start`, `mdi`, `run_from_line`, jog-in-world |
| idle-before-mode-change | `set_mode`, MDI entry |
| not-running | MDI, touch-off, tool change, WCS edit |
| no-eoffset-contamination | touch-off, tool change, probe ops, WCS edit |
| armed (existing) | every mutation |

**Tasks:**
1. Add `lcnc-gateway/command_policy.py` — a **pure** module: `evaluate(cmd: str, state: MachineState) -> Optional[str]` returning a deny-reason or `None`. `MachineState` is a small dataclass built from already-polled STAT fields (interp state, task_mode, homed mask, eoffset-enable).
2. Wire it into the WS command dispatch in `gateway.py` **before** execution, after `require_armed()`. On deny: emit a trace event and return `{ok: false, reason}` — no exception, no disconnect.
3. Keep `require_armed()` and `reject_if_auto_running()`; the policy layers on top, it does not replace them.
4. Map each existing command to its invariant set in one table, co-located with the frontend
   permission names so drift is visible (comment cross-referencing `permissions.ts`).

**Acceptance:** unit tests assert `(cmd, state) → allow/deny` for each invariant: cycle_start
un-homed → deny; MDI while running → deny; touch-off with eoffset active → deny; jog_stop from
any state → allow (authorization, not safety). No regression in legitimate idle/homed flows.

**Risk:** medium — could reject flows that currently pass. Mitigate with WS2 characterization
tests landing alongside. **Effort:** 2–4 days first pass.

---

## Workstream 2 — Characterization tests for command handlers  *(must precede WS4)*

**Goal:** pin current command-dispatch behavior so the policy gate (WS1) and later extraction
(WS4) have a net. Targets the ~1% → meaningful coverage gap.

**Tasks:**
1. pytest harness with a **fake `linuxcnc`** (`stat`/`command`/`error_channel` stubs) so handlers
   run without a real LinuxCNC. Put stubs in `conftest.py`.
2. `test_command_policy.py` — the `(cmd, state)` matrix from WS1.
3. `test_command_dispatch.py` — characterize representative handlers across states: `jog`,
   `mdi`, `cycle_start`, `load_file`, `save_tool`, feed/spindle/rapid override.
4. Extend `test_gateway_util.py` — assert `finite_float()` coverage on every numeric command
   field (drives the §3 coverage closure).

**Acceptance:** `pytest` green; tests fail if a handler's state-gating or numeric validation
changes silently. **Effort:** ~1 week for a useful baseline.

---

## Workstream 3 — Stores with real transaction locks  *(contained win)*

> **Status (2026-06-03): PARTIAL — #30 (renumber transaction) done; #24 REOPENED.**
> Settings lock shipped, but the tool-write paths (`write_tool_table`, `save_tool_library`)
> have no lock, and the `threading.Lock`-in-asyncio decision (Task 3 below) is unresolved.

**Goal:** kill lost-update / multi-tab races on settings and tool table; resolve the
threading-vs-asyncio lock question.

**Tasks:**
1. `settings_store.py` — `SettingsStore` owning the settings file + its lock; `load()` /
   `save_section()`. Migrate inline `save_settings_section()` (`gateway.py:2483`) to it.
2. `tool_store.py` — `ToolStore` with transactional `save_tool` / `renumber_tool` /
   `import_tools` (read-modify-write under one lock).
3. **Resolve the lock model:** the existing `threading.Lock` (`gateway.py:2429`) blocks the event
   loop if held in an async handler. Either switch to `asyncio.Lock` + `run_in_executor` for the
   blocking file IO, or guarantee writes run off-loop. Document the choice.

**Acceptance:** concurrency test issues overlapping `save_section` / `renumber_tool` and asserts
no lost update and a consistent final file. **Effort:** settings 1–2 days; tool store 3–6 days.

---

## Workstream 4 — Extract 3–4 gateway seams  *(after WS2 is green)*

**Goal:** shrink the monolith along the seams the earlier workstreams already created — **not**
the speculative 15-file layout.

**Extract, in order:** `command_policy.py` (WS1, already out) → `command_dispatch` → `settings_store` /
`tool_store` (WS3, already out) → optionally `status_poller`. Stop there.

**Acceptance:** characterization tests (WS2) stay green across every extraction; no change to
LinuxCNC runtime behavior. **Effort:** 2–4 weeks incremental. **Risk:** high without WS2 — do not
start before it.

---

## Workstream 5 — Deferred (triggers, not dates)

- requestIds / typed protocol (§2), typed frontend dispatcher (§7): trigger = multi-client/pendant
  goal **or** an observed concurrent reply-routing bug.
- runtime epochs (§5): trigger = an observed stale-cache / INI-switch corruption.
- stronger token mode (`project_stronger_token_mode`): trigger = a deployment beyond trusted-LAN.

---

## Sequencing

```
WS1 (policy)  ─┐
               ├─►  WS3 (stores)  ─►  WS4 (extraction)
WS2 (tests)  ─┘        (parallel-safe with WS1; both feed WS2)
```

WS1 and WS2 land together (policy needs the net; tests need something to characterize). WS3 is
independent and can run in parallel. WS4 is gated on WS2 being green. WS5 stays parked until a
trigger fires.

## Definition of done (per workstream)

- New backend code has pytest coverage; `npm run build` (frontend) stays at **zero** TS errors.
- Every reject path emits a trace event and returns a bounded reply — no silent fallback.
- No change to HAL / motion-abort safety behavior.
