# Harness-native spool lifecycle remediation plan

Status: partly implemented; kept as the design record. Of the implementation
order below, step 1's supervisor compatibility bridge landed (dd516aa,
2026-08-10; the schema-2 store is named in code but never activated), step 2's
namespace-safe owners and watchdog landed (4814006, a48aef4, 25922fc, 25bcb4f,
2026-08-11; durable owner-episode convergence 8d022c0, 2026-08-14), step 3's
lifecycle reducer landed without the separate journal tier (d94e998,
2026-08-23; see `spindle/lifecycle.py`), and steps 5 and 6 landed (Claude
lifecycle telemetry a2ab382, 2026-08-24; stream-driver default 5c0ea3e,
2026-08-19). Step 4 (moving live writers to the schema-2 store) and steps 7 to
11 (the Codex app-server driver) have not been started; step 12 was not
audited. Checked against master 138e07e on 2026-11-03.

Date: 2026-08-01

Scope: make Spindle's Claude Code and Codex spools durable and observable by
using their supported programmatic lifecycle events instead of inferring task
state from namespace-local PID visibility. Preserve the current detached
supervisor, permission boundaries, shard behavior, and legacy spool readers.
Gemini and Kimi remain later work.

This plan supersedes the lifecycle direction in
`docs/CLAUDE_SPOOL_REPAIR_PLAN.md` where the two differ. That plan remains
useful evidence for the durable supervisor and Claude stream-driver work
already completed.

## Evidence

The plan follows current official documentation and source, the installed
harnesses, the current Spindle implementation, and two independent web-enabled
research passes.

- Codex app-server lifecycle and schemas:
  https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md
- Claude Code headless lifecycle:
  https://code.claude.com/docs/en/headless
- Claude Agent SDK message types:
  https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/types.py
- Claude Agent SDK process transport:
  https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/transport/subprocess_cli.py
- Claude Agent SDK control protocol:
  https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/query.py
- Agent Client Protocol v1:
  https://agentclientprotocol.com/protocol/v1/overview
- Gemini ACP mode:
  https://geminicli.com/docs/cli/acp-mode/
- Prior SKEIN finding: `finding-20260801-dj9j`
- External-research correction and synthesis: `finding-20260801-68ml`

The installed versions examined were Claude Code 2.1.220, Codex CLI 0.146.0,
and Gemini CLI 0.39.1. Kimi findings are deliberately not used to set current
implementation requirements.

## Diagnosis

The recent false finalizations were machinery failures.

Spindle has three different kinds of state but currently lets them collapse
into one another:

1. **Job state**: whether the requested work is pending, active, waiting,
   complete, failed, cancelled, or indeterminate.
2. **Protocol state**: whether a provider accepted a thread/turn, reported
   progress, emitted a terminal result, or acknowledged cancellation.
3. **Transport state**: whether Spindle can still communicate with the local
   harness process and whether its process group has been reaped.

`_check_and_finalize_spool` currently uses namespace-local PID visibility as a
transport probe and can turn that observation into a job terminal transition.
A supervisor in a different PID namespace cannot meaningfully interpret the
recorded host PID. In the observed failures it finalized healthy work in
synchronized batches before the detached wrappers wrote their real exit
sidecars.

The harnesses already expose better job and protocol facts:

- Codex app-server emits a typed terminal `turn/completed` whose turn status is
  `completed`, `interrupted`, or `failed`; supports `turn/interrupt`; and
  exposes persisted thread reads.
- Claude Code stream-json emits `system/init`, task lifecycle events, and a
  terminal `result`. Current first-party SDK types confirm the exact task
  status shapes, including terminal `task_updated` events that may have no
  matching `task_notification`.
- ACP provides a later shared transport vocabulary for Gemini and Kimi, but it
  does not provide durable in-flight request recovery and does not erase
  provider differences.

The missing component is a Spindle-owned durable state machine that consumes
those facts without confusing them with OS process visibility.

## Decisions

### Keep Spindle as the durable owner

The store supervisor, spool locks, atomic records, shard metadata, concurrency
accounting, capture retention, and process cleanup remain Spindle-owned.
Provider session persistence is conversation durability, not spool durability.

Every new protocol driver is a detached per-spool process in its own session,
launched through the existing durable machinery. Start with one driver and one
provider process per spool. Do not introduce a shared provider daemon until
isolation, approval routing, and cross-spool failure behavior have independent
evidence.

### Protocol terminals own job completion

A spool may become `complete`, provider-failed, or provider-interrupted only
from a matching terminal protocol event, a successfully reconciled persisted
terminal record, or a pre-launch failure where no provider work was accepted.

OS exit is transport evidence. A transport that exits without terminal
protocol evidence produces `transport_lost` or `indeterminate`, never a
fabricated provider success or failure.

Timeout and explicit cancellation remain Spindle authorities, but an observer
does not make the public spool terminal merely by deciding that work should
stop. It durably records the requested outcome and moves the spool to a
slot-counted `stopping` state. The per-spool owner must acknowledge the request
and obtain a provider terminal or prove child cleanup before the requested
terminal state is published. A provider terminal that races with the request
is preserved with its ordering.

### Do not host Claude directly in the Agent SDK

The Python Agent SDK starts the CLI as an in-process child, registers an
`atexit` handler that terminates active children, and prefers its bundled CLI
unless `cli_path` is set. Hosting it in the short-lived `spindle spin` caller
would violate detached ownership and could silently change the selected CLI.

Use Claude's supported stream-json wire events now. Treat the official Agent
SDK types and parser as the reference schema. A later SDK refactor may live
inside the detached logical owner `spindle_claude_driver.py`, with an explicit
CLI path, but it is not a prerequisite and is not part of this implementation.

### Use only the Codex app-server stable subset

The first Codex driver uses app-server stdio and the schema subset generated
without `--experimental` by the supported installed Codex version. The
generator itself is still labeled experimental, so rollout remains pinned and
canaried. Do not use `thread/shellCommand`, `process/spawn`,
background-terminal methods, or collaboration-agent behavior in the first
production cut.

Retain `codex exec --json` as a recorded per-spool rollback protocol while
app-server is canaried.

### Keep the future ACP core thin

Gemini and Kimi may later share ACP framing, initialization, capability
negotiation, request routing, updates, and stop-reason handling. They still
require provider adapters for launch commands, authentication, modes, errors,
persistence, shell location, and containment. No ACP implementation is in the
current scope.

## Invariants

1. A launching CLI may exit immediately after returning a spool ID without
   ending or orphaning the work.
2. A process observer must never interpret a PID created in a different PID
   namespace.
3. A held per-spool process-ownership lock means the transport is still owned,
   regardless of namespace-local PID probes.
4. Job completion requires terminal protocol evidence, a pre-acceptance launch
   failure, or a Spindle stop decision followed by owner-confirmed cleanup.
5. A transport exit without terminal evidence is distinguishable from
   provider failure.
6. Cancellation records who requested it, whether the provider acknowledged
   it, whether a terminal event was observed, and whether process cleanup was
   forced.
7. Reconnection never blindly resends a prompt whose acceptance is ambiguous.
8. Permission, sandbox, working directory, executable, provider home, model,
   and profile semantics remain equal to or narrower than the existing launch.
9. Legacy spools remain readable under their recorded protocols during
   rollout. A legacy spool that lacks cross-namespace ownership evidence is
   never guessed dead by a foreign observer.
10. Unknown additive protocol fields and notifications are preserved or
    ignored safely; unknown terminal discriminators fail closed.
11. `pending`, `running`, and `stopping` work all consume concurrency until a
    terminal transition is durably committed.

## Durable lifecycle model

Add a provider-neutral lifecycle record alongside the existing compatibility
fields. Introduce it through the dual-reader migration described below rather
than immediately making old and new supervisors mutually incompatible.

Required fields:

- `lifecycle_protocol`: for example `claude-stream-v2`,
  `codex-app-server-v1`, or `codex-exec-json-v1`
- `adapter_version`
- `provider_executable` and `provider_version`
- `provider_schema_digest` or capability snapshot where applicable
- `transport_state`: `starting`, `connected`, `lost`, `exited`, or `reaped`
- `connection_epoch`: a monotonic local integer
- provider conversation identifiers: session/thread ID and turn/prompt ID
- `protocol_state`: `starting`, `accepted`, `active`, `waiting`, or `terminal`
- `desired_terminal_kind` and public stop state: `cancel_requested`,
  `timeout_requested`, or `stopping`
- normalized terminal kind: `completed`, `failed`, `refused`,
  `interrupted`, `cancelled`, `indeterminate`, or `orphaned`
- raw provider terminal status, stop reason, and structured error
- `last_activity_at`, `last_event_type`, and bounded active-work summaries
- cancellation request, acknowledgement, terminal-observation, and forced-kill
  timestamps
- process owner PID, child process-group ID, birth token, and PID namespace
  identity as device plus inode
- process ownership state: `held`, `released`, `absent_legacy`, `unreadable`,
  or `identity_mismatch`
- process and journal lock device/inode identity
- control request ID, acknowledgement, and owner generation
- cleanup outcome and recovery history

Keep the public `status` field during migration. Derive it monotonically from
the lifecycle record rather than letting individual parsers assign it. During
the compatibility bridge, normalized `stopping` maps to legacy
`status=running` for existing API consumers while CLI/MCP detail reports
`stopping`; it remains active for slot accounting.

Add an append-only per-spool protocol journal with local monotonic sequence
numbers. The materialized spool JSON remains the query-optimized snapshot.
The journal is the crash-recovery and late-event record.

All journal producers use one store primitive under the per-spool lock:

1. validate and repair the journal tail as described below, replaying it when
   the snapshot trails
2. allocate the next sequence from the last valid journal record, never from
   the materialized snapshot
3. append exactly one canonical JSON object plus newline using append mode
4. flush and `fsync` the journal
5. reduce that event into a new snapshot, write and `fsync` a temporary file,
   rename it over the snapshot, then `fsync` the spool directory
6. release the lock

Recovery accepts only complete newline-terminated records with contiguous
sequences. Under the lock it truncates a torn final record back to the last
valid boundary before any new append, rebuilds a snapshot that trails the
journal, and quarantines a sequence gap, duplicate, or snapshot that claims
events absent from the journal. Cancellation commands use the same primitive;
they are not a second unsynchronized writer.

The per-spool journal lock is also a stable inode. It is never unlinked or
replaced while a spool, journal, or producer can exist. Retirement first
acquires that exact recorded inode exclusively and verifies no spool artifact
can be reopened before removing the whole retired set.

Publish public `running` after the detached owner and its control/identity
artifacts exist. Record provider acceptance identifiers with the journal
primitive before exposing `protocol_state=accepted`; do not overload `pending`
with the provider handshake.

Do not duplicate prompts, model prose, tool inputs, or unrestricted tool
output into spool metadata. Those remain in the existing protected capture or
transcript. Metadata contains identifiers, statuses, timestamps, bounded
summaries, and errors.

## Workstream 1: namespace-safe ownership and containment

This is the immediate containment and lands before either provider migration.

1. Replace the inline detached wrapper with one logical Spindle owner per
   spool: the packaged protocol driver for Claude/Codex or a packaged legacy
   shim. Shared owner machinery holds the process-identity lock, polls the
   durable control mailbox, starts the provider child, records its process
   group, enforces the wall timeout, owns the exit sidecar, and journals
   cleanup. Control records are atomic, carry a unique request ID and owner
   generation, and are acknowledged through the journal. There is no separate
   generic parent whose pipe EOF changes provider behavior.
   The provider and its descendants never inherit the ownership lock. Before
   provider exec, the owner sets `FD_CLOEXEC` and installs a platform
   containment primitive—Linux parent-death signaling plus process-group
   cleanup, or an equivalent packaged watchdog—so the provider group cannot
   continue unowned if the logical owner crashes. Owner crash is therefore a
   truthful containment failure: terminate/reap the provider, preserve
   partial protocol evidence, and settle as `indeterminate`; it is not normal
   cancellation or provider failure. The short-lived launching CLI remains
   unrelated and may exit immediately without affecting the owner.
   While the bridge still writes schema-1 spool JSON, place its mailbox,
   journal, identity, and exit artifacts beside that record using suffixes
   matched by neither of the old root sweeps: not `*.json` and not `*.lock`.
   In particular, use a journal guard such as `<id>.journal-guard`, not
   `<id>.journal.lock`; the old 24-hour orphan-lock sweep would unlink the
   latter. Schema 2 later puts the complete artifact set in its sibling live
   store. After activation, a
   provider-protocol rollback is still a schema-2 spool and uses versioned
   sidecars; schema-1 root writes resume only after the explicit drain and
   unseal procedure.
2. Record the launcher's PID namespace identity with the process identity. On
   Linux use the device and inode identity of `/proc/self/ns/pid`; on platforms
   without that facility record an explicit unsupported value.
3. Replace boolean remote liveness with a tri-state result:
   `alive`, `dead`, or `unverifiable`.
4. If the current namespace differs from the recorded namespace, PID,
   process-group, and `/proc` birth-token checks return `unverifiable`. They
   must not send signals or authorize terminalization.
5. Model process-lock evidence explicitly. At creation, record the lock's
   device and inode. `held` means that exact inode is locked. `released` is
   valid only when this spool recorded successful lock creation, the pathname
   still resolves to the recorded inode, and that inode can now be acquired.
   A missing lock on an older spool is `absent_legacy`, never `released`;
   missing current locks and replaced inodes are `identity_mismatch`; open or
   permission errors are `unreadable`.
6. Never unlink or replace a process-identity or journal-lock pathname during
   finalization, timeout, capture cleanup, shard cleanup, or ordinary
   retention. Retire it only with the complete terminal spool artifact set,
   after acquiring the exact recorded inode exclusively. Process-group probes
   never authorize lock or capture deletion.
7. Timeout, drop, and cancellation append a control request rather than
   signaling a recorded foreign PID. The owner first uses provider cancellation
   when the driver supports it, then terminates and, after a grace, kills the
   recorded child process group. Only the owner, or a same-namespace process
   that verifies namespace and birth identity, may signal it.
8. The logical owner enforces the spool wall timeout itself, so a foreign
   observer is not the only timeout executor. It releases custody only after
   its provider group is reaped and cannot outlive that cleanup merely because
   a provider descendant detached.
9. Keep the spool `stopping` and slot-counted until the owner journals
   acknowledgement plus protocol settlement or forced cleanup and child exit.
   A held, unreadable, or identity-mismatched ownership lock blocks automatic
   terminalization and replacement. `unreadable` or `identity_mismatch` marks
   the store unhealthy, rejects new launches with a repair diagnostic, and has
   an explicit operator repair procedure; it is never silently converted to
   released to reclaim capacity.
10. Record the observer namespace on explicit cancellation and recovery
   decisions.
11. Remove direct `_is_pid_alive` terminal decisions from harness-specific
   `unspool`, `peek`, cleanup, and recovery paths. Route all of them through
   the same namespace-aware reconciliation result.
12. For pre-migration spools with `absent_legacy` ownership evidence, allow
    only the recorded same-namespace owner/service or explicit manual recovery
    to finalize or signal them. A foreign observer reports `unverifiable`
    rather than freeing the slot.

Tests:

- unit tests for same-namespace alive/dead and cross-namespace unverifiable
- a Linux bwrap integration test where the observer cannot see the host PID
  but can see the shared ownership lock
- regression tests proving no finalization or signal occurs while the lock is
  held across a namespace mismatch
- cross-namespace cancel and timeout tests proving the owner receives the
  request, terminates the child group, and only then publishes terminal state
- owner crash before acknowledgement and child refusal to terminate, both
  remaining slot-counted until ownership is resolved
- logical-owner crash during a healthy protocol turn, proving provider
  containment, partial-evidence preservation, indeterminate classification,
  and no effect from mere launcher exit
- exact regression in which finalization and timeout run while the identity
  lock is held: neither may unlink the pathname, change its inode, delete live
  captures, or permit a replacement lock
- missing legacy lock, missing current lock, replaced inode, released created
  lock, and unreadable lock as distinct cases
- a legacy harness that starts a detached grandchild, proving neither provider
  nor descendant inherits custody and the shim releases it after group cleanup
- owner/shim self-timeout when the observer is in a foreign PID namespace
- cross-namespace cancel and timeout for a pre-activation bridge schema-1
  launch and for a post-activation schema-2 spool using a legacy provider
  protocol
- an old cleanup import more than 24 hours after bridge launch, proving it
  cannot enumerate or unlink any new schema-1 owner/journal sidecar
- permission repair returning an unhealthy store to service without guessing
  the owner dead
- real exit-sidecar and released-lock finalization
- PID reuse and namespace mismatch as distinct cases

## Workstream 2: common driver and reconciliation primitives

Create one internal driver event envelope without forcing providers into one
wire protocol:

- `transport.started`
- `protocol.connected`
- `conversation.accepted`
- `turn.started`
- `activity`
- `work.started`, `work.updated`, `work.finished`
- `approval.requested`, `approval.resolved`
- `turn.terminal`
- `cancel.acknowledged`
- `transport.lost`, `transport.exited`

Each event carries the spool ID, adapter version, connection epoch, local
sequence, provider identifiers, provider event name, and sanitized payload.

Do not `fsync` unbounded token-rate activity. Coalesce non-authoritative
`activity` observations into a bounded summary interval before journaling.
Never coalesce acceptance, work transitions, approvals, errors, control
requests, terminal events, cleanup, or ownership changes.

Implement a single transition reducer. It owns monotonic state changes,
terminal precedence, cancellation races, and snapshot writes. Provider drivers
translate; they do not write final spool status directly.

Late events from an older connection epoch remain in the journal but cannot
mutate the current snapshot. A provider terminal that was durably observed
before an explicit cancellation wins; a cancellation that became authoritative
first remains the job terminal while preserving the late provider event as
evidence.

Add fake-driver fixtures that can script connection loss and reordered events
without launching a model.

Add crash injection at journal append, journal `fsync`, snapshot temporary
write, rename, and directory `fsync`. Cover torn tails, duplicate and skipped
sequences, snapshot-behind replay, impossible snapshot-ahead quarantine, and a
cancel request racing a provider event. These are store tests, not provider
canaries. The snapshot-behind case must resume at journal-tail sequence plus
one; a duplicate is corruption to detect, never the expected result of an
ordinary crash. Add a sustained activity test that bounds journal write rate
without losing authoritative transitions.

## Workstream 3: Codex app-server driver

Add a packaged detached `spindle_codex_driver.py` using the shared owner
machinery.

### Launch and compatibility

1. Resolve and pin the absolute Codex executable exactly as today. Run its
   version query and every other compatibility probe inside a filesystem-
   contained disposable `HOME`/`CODEX_HOME`; Codex 0.146.0 attempts helper
   alias installation even for diagnostic commands, so probes never receive
   the user's auth, config, or writable home.
2. Initially support the installed/canaried Codex version 0.146.0.
3. Generate and package the app-server schema with `--experimental` omitted.
   The generator command is itself labeled experimental, so call this the
   supported stable API subset, not a stability promise for the command.
4. Before reserving a slot, attest the resolved binary with a no-model schema
   generation in that disposable, filesystem-contained probe environment. Give
   it only a writable output directory and disposable homes; mount or expose
   the resolved executable and its runtime dependencies read-only, disable
   network, and do not copy or link user authentication/configuration. Sort
   relative output paths; for every JSON file parse and reserialize with sorted
   object keys and fixed separators, then hash a length-delimited path/content
   stream. This removes the installed generator's nondeterministic bundle key
   order. Compare that canonical digest with the packaged parser contract.
   Cache lookup uses executable realpath, device/inode, size, mtime, and
   version; the resulting attestation records the digest but does not put that
   unknown result in its own lookup key. `doctor` performs the same
   non-user-mutating check. Never infer a digest from version alone or run this
   after provider work is accepted.
5. If the executable version is unsupported or its schema digest differs,
   either use the explicitly configured exec rollback protocol or reject before
   reserving work. Never silently parse it as a known app-server version.
   If the platform cannot provide the probe containment above, report
   app-server attestation unavailable and retain the explicit rollback/reject
   behavior; do not probe against the user's writable home.
6. Preserve the current behavioral sandbox refusal for every restrictive
   launch. Run the no-model enforcement probe in the exact child environment
   before slot reservation and cache it by resolved binary plus effective
   `HOME`/`CODEX_HOME` and `config.toml` signature. A config edit, home change,
   or binary change must re-probe and failure remains a hard refusal. This is a
   separate behavior check: unlike schema/version attestation, it deliberately
   evaluates the effective user configuration but executes only the existing
   local sandbox probe, never a model or trust-changing app-server request.
7. Start one `codex app-server` over stdio per spool with a command-line config
   override pinning the requested sandbox mode, and send the matching explicit
   policy again on the turn. This is required so a user config value cannot
   silently widen the structured request. Send one `initialize`, then
   `initialized`. Record the returned `codexHome` and runtime metadata.
8. Preserve `HOME`, `CODEX_HOME`, profile environment, cwd, model, permission,
   sandbox, and additional writable-root behavior from the existing exec
   launcher.

### Thread and turn lifecycle

For a new spool:

1. `thread/start` with persistence enabled (`ephemeral: false`), no cwd, and
   the stable 0.146.0 fields `sandbox: <SandboxMode>` plus the mapped
   `approvalPolicy`; never send experimental-only `permissions`
2. durably record the returned thread ID
3. `turn/start` with a caller-generated correlation ID, real cwd, mapped
   `approvalPolicy`, and the stable tagged `sandboxPolicy` object. For
   workspace-write, explicitly reconstruct every writable root currently
   supplied by `--add-dir`, including shard worktree and `.git` requirements;
   do not rely on its empty default.
4. durably record the turn ID
5. journal `turn/started`, `item/*`, approval requests, errors, and
   `turn/completed`
6. after terminalization, use `thread/read` with `includeTurns: true`, match the
   recorded thread and turn IDs, and materialize a final assistant result that
   was absent from the terminal notification

For continuation, `thread/read` first, then `thread/resume`, then start a new
turn only after the prior turn is settled. Preserve the exact working directory
and permission inputs. Never resume merely because a session ID exists on a
still-running spool.

Treat only the matching `turn/completed` as provider terminal. Preserve
mid-turn `error` notifications but await the terminal turn state. Treat the
known `inProgress` status as active; it is not an unknown terminal
discriminator.

Generate strict outbound request models from the packaged stable subset rather
than trusting the upstream schemas' permissive `additionalProperties`.
Tests fail if any emitted field is absent from the pinned stable contract, has
the wrong wire type, or appears only with `--experimental`. Pin the distinction
between thread-level `sandbox` as a string and turn-level `sandboxPolicy` as a
tagged object.

### Approvals

The first cut must explicitly map every existing Spindle permission profile to
an app-server sandbox and approval policy with no authority widening.

Unexpected server approval or user-input requests fail closed and enter a
durable waiting state. If Spindle cannot answer a request under the stored
policy, it declines or cancels it explicitly and records the outcome; it must
not hang invisibly.

Codex currently documents that `thread/start` can mark a project trusted when
given a writable cwd. Therefore app-server remains disabled until a live
no-mutation launch shape is proven. The first hypothesis to canary is:
initialize, start a persistent thread without cwd under read-only policy, then
put the real cwd, `approvalPolicy`, and correctly typed `sandboxPolicy` plus
writable roots on `turn/start`. Snapshot the relevant configuration before and
after. Run this canary with disposable configuration and an ephemeral
authentication mechanism that does not copy persistent credentials; never
probe by mutating the user's real Codex home. If permission parity or
no-mutation cannot be demonstrated, block app-server rollout and retain
`exec-json`. A dedicated production `CODEX_HOME` is not an automatic fallback
because it changes authentication, session, and configuration semantics.

Interactive approval response APIs are a separate public feature unless they
are required to preserve current behavior. The driver and lifecycle schema
must nevertheless represent waiting requests durably.

### Cancellation

1. Write the durable cancel request and notify the driver.
2. If thread and turn IDs are known, send `turn/interrupt`.
3. Await the matching `turn/completed` with `interrupted` for a bounded grace.
4. If the protocol cannot settle, ask the verified owning process to terminate
   its process group.
5. Record requested, protocol-acknowledged, terminal-observed, and forced-kill
   facts separately.

Turn interruption does not prove background terminals are gone. Because
background-terminal methods are experimental, the first production adapter
does not enable work that intentionally outlives the turn. Canary and design
that policy separately.

### Recovery

After transport loss:

1. never resend `turn/start` automatically
2. preserve the journal and last accepted identifiers
3. do not start a replacement app-server while the prior ownership lock is
   held, unreadable, identity-mismatched, or stopping; require a released
   recorded lock on the same inode plus exit evidence or an explicit owner
   handoff token
4. restart the same attested Codex version with a new connection epoch
5. initialize, then call `thread/read` with `includeTurns: true`
6. match the recorded thread ID and exact accepted turn ID before using any
   persisted result; never treat the most recent turn as the same turn
7. finalize from a persisted matching terminal turn when present
8. if the turn is active, partial, absent, or ambiguous, record
   `indeterminate` with an explicit recovery action
9. allow a later new turn only after the prior state is settled

`clientUserMessageId` is correlation, not assumed idempotency.

### Codex canaries

- initialize ordering and unknown additive notifications
- successful, failed, refused, interrupted, quota, auth, sandbox, and stream
  disconnect turns
- connection loss before and after thread acceptance, turn acceptance,
  side effects, and terminal notification
- final result absent from `turn/completed`
- cancellation during model streaming, command execution, pending approval,
  and immediately after turn acceptance
- approval decline/cancel and connection loss while waiting
- distinct `HOME` and `CODEX_HOME`, including trust/config mutation checks
- sandbox and writable-root parity with current `codex exec`, including a
  hostile config override and a config edit that must invalidate the
  behavioral-probe cache
- strict stable-schema validation of every emitted `thread/start` and
  `turn/start` field and wire type; reject experimental-only `permissions` and
  prove shard/worktree writable roots are reconstructed
- restrictive app-server launch refusal when the no-model sandbox probe is
  inconclusive or fails
- two schema generations from the same binary producing the same canonical
  digest despite different raw JSON object order
- version and schema probes with a byte-for-byte snapshot of the real
  `HOME`/`CODEX_HOME`, proving helper-alias startup and every other write stay
  inside the disposable probe tree
- restart plus `thread/read` reconciliation
- no experimental app-server calls on the stable path

## Workstream 4: Claude stream-json driver

Keep `spindle_claude_driver.py` as the detached logical owner and provider
stdio owner using the shared owner machinery. Make its first-party structured
path the supported integration rather than replacing its lifecycle with the
Agent SDK.

### Structured events

Consume and journal:

- `system/init`, including session ID and the open-ended `capabilities` array
- assistant, tool use, and tool result activity
- `system/task_started`
- `system/task_progress`
- `system/task_notification`
- `system/task_updated`, taking status from `patch.status`
- terminal `result`, including `stop_reason`, `terminal_reason`,
  `permission_denials`, cost, usage, and session ID

Use the official Agent SDK types/parser as the compatibility reference. Treat
terminal task statuses as the first-party set spanning both event families:
`completed`, `failed`, `stopped`, and `killed`.

Use capabilities rather than CLI version comparisons for protocol behavior.
Ignore unknown capabilities. Keep version checks only for requirements that
have no capability advertisement.

Do not enable token-delta partial messages. Complete message and task-progress
events provide the required activity without multiplying capture volume.

### Background work

Preserve the existing driver rule that a result is not job-complete while
Spindle-tracked work remains unresolved. Preserve the rule that a background
task resolved after a result requires a later result that accounts for it.

Current Claude Code documentation says headless mode waits for background
subagents with a bounded ceiling and terminates background Bash about five
seconds after the final result and stdin closure. Spindle's parked stream
driver deliberately keeps stdin open, so that Bash rule is not its bound; the
driver's own wall deadline and owner cleanup are. Monitor and ScheduleWakeup
remain undocumented. Continue to disallow ScheduleWakeup and retain the
existing Monitor policy until live canaries establish their exact 2.1.220
behavior.

The official documentation says `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` bounds
background subagents after the final turn, while installed-binary inspection
suggested the 2.1.220 sweep may wait for stdin closure. Treat that conflict as
an empirical question, not a rollout premise. Canary a parked stream with
stdin held open, a short positive ceiling, and an unresolved background
subagent:

- If the ceiling fires with stdin open, record it as a provider background-work
  policy distinct from Spindle's wall timeout. For an explicit finite spool
  timeout, set a positive provider ceiling no later than the owner deadline
  minus cleanup margin. For `timeout=None`, leave Claude's documented ten-minute
  default and surface that effective background-subagent cap.
- If the ceiling is inert until stdin closes, leave it unset in the parked
  stream path and use the generic owner's wall timeout and cleanup as the only
  finite bound.

Neither outcome changes job-terminal authority: a provider ceiling event or
transport exit is journaled and reconciled, not fabricated into success.
Leave the variable unset for one-shot rollback so Claude's documented default
applies after stdin closure. Never set `0`; it means unbounded.

Do not remove the hardened park/sentinel state machine or its structured-event
tests merely because equivalent SDK types exist.

### Cancellation

When `system/init.capabilities` advertises interrupt receipts:

1. record the durable cancel request
2. after the streaming-input control channel has completed its `initialize`
   request/response, send the official SDK-shaped envelope
   `control_request(request={subtype: interrupt})` with a unique request ID
3. wait briefly for a terminal result whose `terminal_reason` records
   `aborted_streaming` or `aborted_tools`
4. escalate through verified process-group termination if the protocol does
   not settle

Treat `terminal_reason` as open-ended. Preserve unknown values.

The Claude driver is the logical owner introduced in Workstream 1: it receives
the mailbox request, remains the sole stdin owner, and owns escalation. The
observing process never signals a foreign-namespace PID.

Canary the exact initialize-before-interrupt ordering, response request-ID
matching, structured receipt capability, cancellation queued before a turn is
ready, and an unknown control response. Do not infer the wire shape from prose
or treat a write to stdin as an acknowledgement.

### Rollout

1. rerun the existing stream-driver canary on installed Claude Code 2.1.220
2. add canaries for task-progress, terminal task-updated without notification,
   background subagent, background Bash, Monitor, interrupt receipt, and
   timeout-ceiling interaction
3. make the stream driver default for new Claude spools
4. retain an explicit legacy one-shot rollback switch and recorded per-spool
   protocol
5. remove prose/XML fallbacks only after supported-version canaries prove the
   structured events cover every required case

Do not add `claude-agent-sdk` as a dependency in this work. Reconsider it later
only inside the detached driver, after checking wheel/platform cost, pinning
the installed CLI path, and resolving the background-task turn-boundary
canary.

## Workstream 5: user-visible state and operations

Update `spools`, `peek`, `unspool`, `wait`, `drop`, HTTP responses, and MCP
responses to use the normalized lifecycle.

For active work expose:

- provider protocol and version
- accepted session/thread and turn identifiers
- `protocol_state`, including owner-started but not yet accepted
- last protocol activity
- bounded active work
- waiting approval/input state
- requested stop reason, owner acknowledgement, and cleanup progress
- transport state, including `unverifiable` rather than `dead`
- ownership corruption/unreadable diagnostics and the exact repair command or
  file-permission action, without offering an unsafe "assume dead" shortcut

For terminal work expose:

- normalized terminal kind
- provider terminal status and stop reason
- whether Spindle imposed timeout or cancellation
- protocol cancellation and forced-cleanup outcomes
- whether recovery was attempted and why a state is indeterminate

PIDs remain diagnostics and are labeled with their namespace identity. No
command tells a caller that a spool is dead solely from a PID probe.

`wait` terminates on the normalized job terminal, not on wrapper exit.
`unspool` never invokes a second harness-specific PID finalizer.

Keep `pending` only until the detached owner and its identity/control artifacts
are published. Thereafter the spool is `running` with
`protocol_state=starting`. Apply a separate handshake deadline to initialize
and acceptance, while the existing wall timeout continues from spool creation.
A handshake failure requests owner cleanup and reaches terminal state only
after that cleanup is confirmed.

## Compatibility and migration

- First ship a bridge release that still writes schema 1 but advertises
  `readable_spool_schemas=[1,2]`, its writable schema, supported lifecycle
  capabilities, and supervisor protocol range. It must contain the schema-2
  reader/reducer before any process writes schema 2 and must preserve unknown
  additive lifecycle fields.
- Make schema 2 a complete sibling live store, for example
  `~/.spindle/spools-v2`, not a child of the existing
  `~/.spindle/spools`. Move every live writer there: spool JSON, journals,
  owner controls and identity locks, captures, prompts, exit sidecars,
  transcripts, worktree locks, supervisor and concurrency state, atomic-write
  temporaries, retention bookkeeping, doctor probes, and default exports. An
  audit must account for every path derived from the current `SPINDLE_DIR`;
  after activation no compatible live operation writes beneath the schema-1
  root.
- Treat the existing `~/.spindle/spools` as a schema-1 archive. The bridge may
  read terminal history and captures there without acquiring or creating root
  locks, but never reconciles, finalizes, rewrites, or automatically retires
  archived records while schema 2 is active. Archive retention is an explicit
  offline maintenance operation, not a silently failing background unlink.
  Old import-time recovery, `peek`, `drop`, and `unspool` cannot enumerate the
  sibling v2 store.
- Replace exact version equality with capability/range negotiation. A
  pre-bridge live supervisor may continue legacy schema-1 launches, but no
  schema-2 or new-driver spool is created until a bridge-capable supervisor
  owns the store.
- Activation first prepares and `fsync`s the complete sibling v2 layout and a
  phase journal there. It then acquires the existing schema-1 supervisor
  control lock that every old reservation path already takes. Hold that lock
  continuously across a durable `closing` phase, the final drain scan, and the
  schema-1 directory seal. Refuse activation if the scan finds any active,
  stopping, indeterminate, unreadable, or lock-owning schema-1 record.
- The phase journal and directory mode make every crash interval decidable:

  1. `closing` is durable before archive mutation. On recovery, reacquire the
     control lock and rescan. If old work appeared before the lock was
     reacquired, abort cleanly back to bridge mode without changed v1 modes;
     otherwise continue.
  2. After the final drain succeeds under the lock, write and `fsync`
     `seal_committing`. The first archive mutation is one atomic chmod of the
     schema-1 directory that blocks create, unlink, rename, and atomic
     replacement. Its observed mode is the commit oracle: writable means
     return to `closing` and rescan; sealed means resume forward.
  3. Only after that directory commit, make archived records and sidecars
     nonwritable. Record resumable progress in the writable v2 phase journal.
     A crash during this sweep always resumes sealing; it never reopens
     schema-1 launches.
  4. Verify the complete seal with a separate process, write and `fsync`
     `seal_verified`, publish the v2 active marker, and only then release the
     old control lock.

  Do not activate on a platform/filesystem where this exclusion, commit oracle,
  and seal cannot be demonstrated.
- The seal, not a live supervisor lease, is the stale-client exclusion
  primitive. It survives supervisor crash and reboot. A pre-bridge client may
  acquire its old lifetime lock, but cannot replace the incompatible root
  supervisor record or create/update a schema-1 spool. The writable sibling v2
  store remains outside every old root glob and path derivation.
- New `doctor` checks the v2 live store for write health and reports the v1
  archive as intentionally sealed. Default export, shard creation, transcript
  persistence, cleanup, and every other positive compatible surface must work
  without an archive write. An old `doctor` reporting the archive unwritable is
  an incompatible-client diagnostic; operator documentation says to upgrade,
  not chmod the archive.
- Unsealing is an explicit bridge maintenance operation allowed only after all
  schema-2 work drains and the v2 supervisor stops. It is required for schema-1
  archive deletion or rollback below the bridge and is never performed by
  ordinary retention.
- The bridge release is then the minimum rollback target: it can read and
  preserve schema 2 and can choose legacy provider protocols for new schema-2
  spools, but it does not resume root schema-1 writes until the explicit
  drain/unseal. Rolling back below the bridge requires draining schema-2 work
  and stopping the store; it is not an in-place supported rollback.
- Leave existing Claude/Codex parsers in place during rollout.
- Record the protocol on every new spool and dispatch finalization by that
  recorded protocol.
- Do not reinterpret old terminal records under new semantics.
- Do not rewrite existing spool files in bulk.
- Reject an incompatible active store before accepting new work, with the
  incompatible owner and capability range in the diagnostic.
- Test this with an actual pre-bridge process imported before activation, not
  only two new supervisor objects. After activation it must be unable to list,
  sweep, wait, drop, or finalize schema-2 work, and its attempt to launch
  schema-1 work must fail without changing either store.
- Repeat that stale-process test after killing the versioned supervisor while
  a detached schema-2 owner remains, and after a simulated reboot with no live
  supervisor. Both must remain sealed; the compatible bridge must be able to
  restart from the persistent marker.
- Pause a resident pre-bridge launcher immediately before schema-1 reservation
  and immediately after it acquires the supervisor control lock. Activation
  must wait, then include or reject that spool in the final drain; no pending
  record, shard, provider process, or sidecar may land between final scan and
  directory seal.
- Mount or permission-seal the v1 archive during a positive compatible-surface
  test, then exercise `doctor`, default export, shard launch, transcript write,
  capture cleanup, retention, supervisor restart, `spools`, `peek`, `unspool`,
  and `wait`. Any attempted v1 write fails the test. Separately prove terminal
  v1 history remains readable.
- Keep current public status strings during the first migration and add
  structured fields. A later API version may expose the richer state directly.

## Implementation order

1. Bridge supervisor compatibility, add the sibling schema-2 live-store
   resolver and read-only schema-1 archive reader, and keep writing schema 1.
2. Namespace-safe shared owner machinery and per-harness logical owners,
   durable control path, and
   cross-namespace cancellation/timeout tests.
3. Crash-consistent journal, lifecycle reducer, and fake-driver test harness.
4. Audit and move every live writer to the sibling v2 store; drain schema-1
   work, execute the crash-safe archive seal, then activate schema-2 writes and
   v2 supervisor state.
5. Claude structured telemetry additions and canaries.
6. Claude stream driver default with legacy rollback.
7. Package and canonically attest the Codex stable-subset schema; retain the
   behavior-based sandbox refusal and config-sensitive cache.
8. Implement the Codex detached app-server driver against fakes.
9. Prove the no-trust-mutation launch shape and exact permission parity; if it
   fails, keep app-server disabled.
10. Codex lifecycle, result extraction, cancellation, and owner-gated recovery.
11. Canary rollout with per-spool protocol selection.
12. Remove remaining direct PID-based terminal decisions.
13. Design ACP support later from observed Gemini and Kimi capability
    handshakes; do not infer it from this plan.

The bridge is a compatibility prerequisite, not the behavioral remedy. The
generic owner and namespace correction are the first functional change;
protocol migration cannot repair a foreign observer that still misinterprets a
PID.

## Verification

Every workstream receives focused unit tests, subprocess tests, and the
relevant existing suites. The final change runs:

- namespace and durable-supervisor tests
- bridge-to-schema-2 mixed-version, drain, and rollback-target tests
- a resident pre-bridge importer attempting every read/finalize/launch path
- the same stale importer after versioned-supervisor death and simulated
  reboot, plus compatible restart and explicit drain/unseal
- a sealed v1 archive plus writable sibling v2 store exercising every
  compatible positive surface: doctor, default export, shard/worktree locks,
  transcripts, captures, retention, supervisor/concurrency state, CLI/MCP
  reads, and atomic temporaries
- activation crash injection in `closing`, before and after
  `seal_committing`, on both sides of the directory-mode commit, during the
  content-mode sweep, after `seal_verified`, and before/after v2 active
  publication
- a pre-bridge launcher paused on both sides of the shared reservation/control
  lock while activation attempts its final drain and seal
- journal crash-injection and recovery tests
- fake Claude and Codex protocol-driver suites
- real pinned-version canaries, isolated from user configuration and
  disposable worktrees
- Codex schema-attestation, no-trust-mutation, and permission-parity canaries
- Codex config-sensitive behavioral sandbox refusal tests
- handshake timeout, owner-acknowledged cancel, forced cleanup, and
  slot-accounting tests
- bridge schema-1 and versioned legacy-provider control delivery from a foreign
  PID namespace
- descriptor-inheritance, owner self-timeout, and stable lock-inode tests
- bounded activity-journal throughput test
- parked Claude streams measuring both possible background-wait ceiling
  outcomes with stdin open, and proving the selected branch plus Spindle owner
  timeout cleans the group without fabricating a terminal result
- full pytest with bytecode and cache writes disabled
- shell suites and diff checks already required by the repository

Canaries must not spend user work twice. Compare providers only with controlled
read-only prompts or disposable worktrees. Never shadow a side-effecting user
spool through both exec and app-server.

Because this work changes concurrency, cancellation, recovery, and permission
handling, each implementation slice receives fragile-change Fell with two
different base-model families before merge.

## Rollback

- The bridge release is the minimum in-place rollback target after schema 2 is
  activated. It continues using the sibling v2 store even when selecting
  legacy provider protocols. Older code requires a full v2 drain, stopped v2
  supervisor, and explicit archive unseal; it must never open the active v2
  store.
- Namespace-safe ownership and the generic control owner are not feature
  flagged. New launches may select a legacy provider protocol, but they still
  use this owner so cancellation remains cross-namespace safe.
- Claude records its protocol per spool and retains the one-shot launcher as an
  explicit rollback path.
- Codex records `app-server` or `exec-json` per spool and retains exec as the
  rollback path until app-server has completed the canary window.
- A provider rollback changes only new launches. Existing spools continue under
  the driver and schema recorded when they started; the bridge supervisor
  remains able to read them.
- An incompatible provider version falls back only when the caller or
  deployment explicitly allows the legacy protocol. It never silently widens
  permissions or replays work.

## Out of scope

- Gemini or Kimi ACP implementation
- Codex experimental background-terminal, raw-process, or collaboration APIs
- a shared multi-spool provider daemon
- replacing Spindle's supervisor with a provider SDK
- adding the Claude Agent SDK dependency
- changing the concurrency limit
- changing shard creation, merge, or cleanup policy
- changing permission authority
- touching or adopting the untracked `uv.lock`

## Acceptance criteria

The remediation is complete when:

1. A foreign PID namespace cannot falsely terminalize or signal an owned spool.
2. Cross-namespace timeout and cancel reach the owning process, clean up the
   child group, and remain slot-counted until cleanup is confirmed.
3. Every new Claude and app-server Codex spool records protocol acceptance,
   activity, terminal evidence, and transport outcome.
4. Logical-owner death contains the provider group and produces an
   indeterminate transport failure with preserved evidence, not fabricated
   provider failure/completion; launching-client exit has no effect.
5. Claude background-task completion is based on structured first-party events
   and the stream driver is the default with a working rollback.
6. Codex completion, failure, and interruption come from the matching
   `turn/completed`; reconciliation uses persistent threads and
   `thread/read(includeTurns=true)` for the exact accepted turn.
7. Cancellation provenance distinguishes request, owner and protocol
   acknowledgement, terminal observation, and forced cleanup.
8. Recovery never starts a second provider while the previous owner is held
   and never duplicates an ambiguously accepted turn.
9. App-server preserves existing sandbox, cwd, home, model, profile, and
   permission behavior without mutating unrelated trust/configuration; every
   restrictive launch retains the config-sensitive behavioral sandbox refusal.
10. Every emitted Codex request field and wire type is present in the packaged
    stable 0.146.0 contract; `thread/start` uses stable `sandbox` and
    `approvalPolicy`, and `turn/start` reconstructs the typed sandbox policy
    plus every writable root without experimental `permissions`.
11. Installed Codex schema attestation matches the packaged parser contract
    before an app-server slot is reserved, and repeated generation with
    reordered raw JSON yields the same canonical digest. Version/schema probes
    cannot write outside disposable homes and leave the user's real
    `HOME`/`CODEX_HOME` byte-identical.
12. Journal crash injection proves torn-write recovery and rejects impossible
    snapshots or sequence histories; an ordinary snapshot-behind crash resumes
    at journal-tail sequence plus one.
13. A held lock pathname and inode survive all finalization, timeout, and
    cleanup paths; provider descendants never inherit custody.
14. A resident pre-bridge process cannot enumerate or mutate the sibling
    schema-2 store or launch schema-1 work, even when the v2 supervisor has
    died or the machine has rebooted; the persistent archive seal remains.
15. Activation holds the old reservation/control lock across final drain and
    directory seal, excludes an in-flight old launcher, and has a deterministic
    forward-or-abort result at every crash phase.
16. With the schema-1 archive mounted or permission-sealed read-only, every
    compatible writer and public operation succeeds solely through the sibling
    v2 store, while terminal v1 history remains readable.
17. The Claude background-ceiling canary records which 2.1.220 behavior is
    present and the selected branch preserves truthful timeout and terminal
    semantics.
18. Bridge and schema-2 supervisors can coexist only in the advertised safe
    combinations, and rollback below the bridge is rejected while schema-2
    work exists.
19. Legacy spools remain readable; unverifiable legacy ownership is surfaced
    instead of guessed.
20. All focused and full tests pass and the complete implementation diff is
    Fell clean.
