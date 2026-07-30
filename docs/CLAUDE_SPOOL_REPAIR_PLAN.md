# Claude spool ownership and observability repair plan

Status: proposed; plan only, not yet implemented

Primary evidence:

- `finding-20260729-dszg`
- `issue-20260729-jmcq`
- `issue-20260729-9lge`, corrected by the auto-mode analysis below
- stream-driver canary `finding-20260724-jbnh`
- plan Fell round-1 triage `finding-20260729-xzou` and `finding-20260729-u5lc`
- plan Fell round-2 triage `finding-20260729-xsnq`
- plan final-gate triage `finding-20260729-k8o2`

## Goal

Make headless Claude spools self-finalizing and observable enough that callers can
distinguish active work, terminal work, permission decisions, and explicit cancellation
without inspecting host PIDs or guessing from an empty capture.

The repair must preserve the current permission model:

- `careful` remains Claude Code `auto`.
- Sharding remains an independent workspace choice.
- `shard` and `careful+shard` retain their existing bwrap-contained bypass behavior.
- `auto+shard` retains `auto`.
- Spindle never escalates a denied or unsupported launch into a broader permission mode.

## Corrected diagnosis

Three failures occurred near each other but have different causes.

### Explicit cancellation was mistaken for agent failure

The recent Opus spools were alive when an external coordinator called `spindle drop`.
One-shot `claude -p --output-format json` had not emitted stdout, and raw PID inspection
from another process context appeared to show no live worker. The coordinator inferred
death and explicitly cancelled the spools. Claude did not cancel itself, and the observed
Opus test run was not waiting for approval.

### CLI monitor ownership is not durable

Every launch starts a detached harness process and an in-process daemon monitor thread.
When `spindle spin` is invoked as a CLI command, it returns the spool ID and exits, taking
the daemon thread with it. The harness survives and writes its exit-status file, but the
spool can remain `running` until another Spindle command or service startup performs
orphan recovery.

This is a shared lifecycle defect in the launch machinery, not a Claude permission defect.
The repair should cover all harnesses that use the shared monitor path, while Claude is
the only harness whose live event telemetry changes in this work.

### The approval probe used an unsupported auto-mode model

The controlled denial spool used Claude Code 2.1.220, `careful`/`auto`, and Haiku.
Claude Code's current auto-mode requirements exclude Haiku. The successful Opus spools
are consistent with those requirements.

The probe therefore does not justify changing `careful`, changing shard policy, or
classifying every auto-mode denial as task failure. It does reveal two narrower needs:

1. reject built-in Claude model/mode combinations Spindle knows cannot support `auto`, before
   reserving a slot or launching Claude; and
2. retain structured permission-denial information instead of discarding it.

In supported auto mode, classifier denials are part of normal execution: Claude can
adapt and still complete the task. A nonempty `permission_denials` list alone is not
proof that the final result is incomplete.

## Invariants

1. A CLI caller may exit immediately after `spin`; a separate durable owner must still
   finalize the spool.
2. Spool state, not caller-visible process state, is authoritative.
3. A running streamed Claude spool records recent activity and its session ID as soon as
   those events arrive.
4. An explicit `drop` remains an explicit cancellation. It must never be rewritten as a
   generic agent error, and it must preserve the evidence available at cancellation.
5. Terminal parsing never silently widens permission authority.
6. Legacy one-shot spools remain readable and finalizable during rollout.
7. The repair does not change which profiles create shards or which permission mode each
   existing profile selects.

## Proposed implementation

### 1. Replace ephemeral finalizer ownership with one store supervisor

Keep the detached harness process and exit-status file. Replace the daemon-only finalizer
with one Spindle-owned supervisor for the resolved spool store. It survives the launching
CLI and owns reconciliation for every `pending` and `running` spool in that store.

The supervisor will:

- repeatedly run one bounded reconciliation step per active spool instead of entering one
  spool's current infinite monitor loop;
- inherit the exact `SPINDLE_HOME`, record its package identity, and enforce the supervisor
  protocol and spool-schema compatibility rules below;
- receive the launcher's resolved `SPINDLE_DIR` explicitly, so subprocess ownership does
  not depend on re-resolving environment or an in-process monkeypatch;
- remain alive while any spool is pending or running, then exit after a bounded idle grace;
  and
- use the existing per-spool lock for every spool-record write, not only terminal
  transitions.

Use a store-wide `flock` held for the supervisor's entire lifetime as the ownership claim.
The kernel releases the lock when the supervisor dies, so claiming does not depend on PID
reuse checks or `/proc` start times. The recorded supervisor PID, package path, and start
time are diagnostic only.

Starting or finding the supervisor must be idempotent across CLI, MCP/stdio, HTTP service,
and recovery processes:

- a separate short-held store control lock serializes supervisor compatibility checks,
  minimal pending-spool reservation, supervisor startup, and idle supervisor retirement;
- a launcher that cannot acquire the lifetime lock leaves the existing owner alone;
- a launcher that acquires it starts the supervisor and passes the locked descriptor to the
  child, then closes its copy only after successful process creation;
- concurrent candidates that lose the lock exit without touching another supervisor; and
- a failed supervisor spawn releases the lock and fails the spool launch loudly.

The supervisor record carries a `supervisor_protocol_version`, `spool_schema_version`,
package path, and package version. Under the control lock, a launcher first verifies that
an existing owner supports its protocol and schema, or starts a compatible owner. A
different package path or version is diagnostic when those versions are compatible. If
the protocol or schema is incompatible, reject the launch before reserving a slot or
creating work, and let the active owner drain and idle-retire; an old supervisor must
never be allowed to parse records from a schema it does not understand.

Still under the control lock, create the minimal pending record with
`_try_reserve_slot_and_create` before releasing the launcher to do shard setup. The
already-running supervisor treats that minimal reservation as legitimate and waits up to
the existing `PENDING_SPAWN_TIMEOUT`. As shard creation succeeds, the shard helper writes
`working_dir`, `shard`, `shard_created_by_spool`, `shard_source_dir`, and `base_branch`
under the spool lock before returning. The launcher then publishes the remaining pending
metadata and spawns the harness.

If the launcher dies at any of those points, the supervisor terminalizes the abandoned
reservation after the timeout. Before doing so, it searches the deterministic spool-owned
shard name and branch for the spool ID; if found, it attaches that recovery metadata to
the terminal record so the shard remains discoverable and preserved. If harness
publication wins, the same supervisor begins running-state reconciliation.

To retire after its idle grace, the supervisor takes the same control lock, rescans for
pending/running spools, and exits only when the rescan is empty. This prevents a launcher
from observing an owner that retires without seeing its newly reserved spool.

The existing in-process harness `Popen` handle may remain as a fast exit-code and reaping
aid, but correctness must depend only on durable files and recorded process identity.
Likewise, a long-lived CLI/service parent that launches the supervisor retains its
`Popen` handle and runs a small waiter to reap the supervisor when it idle-exits. A
short-lived standalone CLI may exit and let the operating system adopt the child. Keep
the existing local reaper for service-owned harness children as well, so neither child
type accumulates zombies under a surviving parent.

Add a top-level supervisor entry point packaged and located like
`spindle_claude_driver.py`. Launch it with `start_new_session=True`, closed stdin, and
stdout/stderr directed to a dedicated supervisor log rather than inherited CLI or MCP
descriptors. Set an internal environment guard before it imports `spindle`; the guard
skips module-load cleanup/recovery, which otherwise runs before an entry-point body can
intervene. The supervisor imports the full package once per store, not once per spool.

Refactor the current monitor loop into one reconciliation step. A same-spool process
replacement, including expired-session transcript fallback, must update the spool's
capture generation and remain owned; it must not interpret successful replacement as a
reason to stop supervising the still-running spool.

Tests may replace the supervisor launcher through an internal injected helper. Subprocess
tests pass an explicit temporary store path. Do not add a public production switch that
silently disables durable ownership.

This preserves the documented standalone CLI: `spindle spin` must not require or silently
route through the HTTP service.

### 2. Make the existing Claude stream driver the default, with rollback

The existing `stream-driver-v1` has already passed its isolated behavioral canary. Before
changing the default, rerun that canary against the installed Claude Code version and the
supported Opus model used by production reviewers.

After the canary passes:

- use `stream-driver-v1` by default for new Claude spools;
- keep an explicit environment opt-out to the legacy one-shot path for rollback;
- continue reading the recorded `claude_protocol` per spool so old and new records coexist;
  and
- do not remove the one-shot parser or migration compatibility in this repair.

The driver remains responsible for parked-turn correctness. This plan does not redesign
its background-task state machine.

### 3. Incrementally index live Claude telemetry

Extend the monitor to fold complete new NDJSON lines from the capture without reparsing the
entire file on every poll. Persist:

- `session_id`, from the earliest authoritative init or result event;
- `last_activity_at`, set when a new valid event is observed;
- `last_event_type`;
- `active_tools`, keyed by tool-use ID and containing only tool name and start time; and
- an internal capture generation and complete-line byte offset used for the next
  incremental read.

Do not copy prompts, tool inputs, tool output, or model prose into spool metadata. The raw
event stream remains in the capture/transcript.

Remove only the matching entry from `active_tools` on a tool result. Parallel tool calls
remain visible until each one resolves. A progress event for an outstanding tool advances
`last_activity_at` without changing its identity. Advance the byte offset only through the
last complete newline and ignore an incomplete final line until the next poll.

Every telemetry update must:

1. read capture data outside the spool lock;
2. acquire the spool lock and re-read the record;
3. abort if the spool is no longer `running` or its capture generation changed; and
4. merge only the new telemetry fields into that current record.

This prevents a stale telemetry write from restoring `status: running` over cancellation,
timeout, or finalization. Terminal finalization must preserve an already indexed
`session_id` when a later result omits or nulls it.

Every same-spool relaunch atomically increments `capture_generation`, resets the byte
offset and event/tool state, moves the old session ID to `prior_session_id`, and clears the
current `session_id` until the replacement stream identifies itself. A defensive file-size
check also resets the reader when a capture is truncated unexpectedly. The supervisor
continues reconciling the replacement instead of exiting.

Expose the fields through `spools` and `peek`. When a running spool has not emitted a
displayable line, `peek` should still report its protocol, session ID if known, and most
recent Spindle-owned activity. PID data remains diagnostic only and is not presented as a
caller liveness decision.

Legacy one-shot spools will have no live event telemetry; report that fact rather than
representing an empty capture as inactivity or death.

### 4. Make cancellation truthful and evidence-preserving

Extend `drop` with an optional human reason. Record:

- `error_kind: "cancelled"`;
- `cancel_requested_at`;
- `cancel_source` (`cli`, `mcp`, or the named internal operation that explicitly invoked
  cancellation);
- `cancel_reason`, when supplied;
- the last indexed activity and active tools already present in the spool;
- whether process-group termination was verified.

Split finalization into a lock-owning wrapper and a lock-free body, following the existing
locked-helper pattern used by expired-session recovery. While `drop` holds the spool lock,
call the lock-free body to reconcile a spool that has already published a terminal stream
sentinel or whose recorded process and exit-status file are terminal. If finalization
wins, return the terminal result and do not relabel it as cancelled. Do not recursively
call the current non-reentrant `_check_and_finalize_spool` while holding the lock.

For a genuinely running spool, terminate it under the existing process-identity checks.
After termination, preserve stdout, stderr, exit status, and any parseable partial Claude
session ID in the transcript/capture paths. Do not unlink cancellation captures in
`drop`. Set `capture_retain_until` to `cancel_requested_at` plus the existing capture
retention interval; age-based cleanup must not remove those captures before that timestamp,
even when the spool itself was created more than the normal retention interval ago.

Extend ordinary cleanup to the transcript and capture artifacts, not only the spool
record. For completed spools, apply the normal retention age from `completed_at`, with the
existing creation-time fallback for legacy records. For cancelled spools, use
`capture_retain_until`. Once the applicable age has passed, remove the transcript,
stdout/stderr, exit-status, prompt, and lock artifacts along with the spool record so
bounded-age diagnostics do not become permanent orphan files.

Do not add heuristics that refuse a deliberate cancellation merely because recent activity
exists. Callers may intentionally stop active work. The repair makes that decision informed
and auditable; it does not take the authority away from `drop`.

Timeout remains `status: timeout`, not cancellation. Recovery remains recovery unless it
explicitly invokes the cancellation path.

Expose `error_kind`, cancellation source/reason/time, last activity, and active tool names
through `spools`, so a coordinator does not need to call `peek` or inspect the spool file
to distinguish cancellation from failure.

### 5. Validate known-incompatible auto-mode models without redesigning permissions

Add one launch validation shared by fresh built-in Claude spins, retries, and respins. It
runs before slot reservation, after model alias resolution, and keys off
`_claude_permission_mode(permission) == "auto"` rather than literal permission strings.
This includes the omitted default and legacy stored values that currently fall back to
`auto`.

For the built-in Anthropic Claude harness in resolved auto mode:

- reject only a closed list of model spellings that current Claude Code documentation
  explicitly excludes, initially the resolved Haiku family and other exact legacy aliases
  already known to Spindle;
- allow supported Sonnet and Opus families;
- pass unknown future model IDs through rather than guessing from their spelling;
- leave account/admin/provider eligibility to Claude Code, because Spindle cannot infer it
  reliably; and
- classify a structured auto-mode-unavailable terminal error distinctly if Claude rejects
  an otherwise supported model for account, provider, or admin reasons.

Lodged profiles may use Claude Code against alternate providers whose model names and
auto-mode policies Spindle cannot infer. Do not apply the built-in-provider deny-list to
them, guess from their spelling, or silently substitute a model. Preserve their provider's
structured failure.

Other permission profiles may continue to use Haiku. In particular, this validation must
not reject Haiku with `readonly`, `manual`, `research`, `full`, or the existing bypass
shard profiles.

There is no force override for a known denied built-in combination in this repair. The
caller can select a compatible existing permission profile; when Claude Code's published
support changes, update the small deny-list and its source-version test.

Update every user-facing built-in Claude example or help string that selects Haiku while
leaving permission at the default `careful`/`auto`, including CLI examples, README API
examples, and the public `spin()` docstring. Use a supported Sonnet or Opus model for
default auto examples, or explicitly name a compatible non-auto permission where Haiku is
the point of the example. Keep Haiku listed as an available model where that remains true.

### 6. Preserve permission-denial telemetry without treating every denial as failure

When Claude's terminal result contains `permission_denials`, store at most 20 normalized
entries containing tool name, tool-use ID, and a provider reason bounded to 512 characters.
Do not copy denied tool input into spool metadata. The full provider event remains in the
bounded-age raw transcript for diagnosis.

Classification remains structural:

- an explicit Claude/CLI error remains `status: error` and carries the denial summary;
- an auto-mode-unavailable result receives its distinct error kind;
- a successful supported-auto result may remain complete with denial diagnostics, because
  the classifier can deny one action and Claude can complete via another path; and
- Spindle does not infer task completeness by matching phrases such as “approval needed.”

This supersedes the earlier proposal to make every nonempty `permission_denials` list a
terminal error.

## Test plan

### Durable ownership

- Launch a fake harness through the real `spindle spin` CLI in a temporary
  `SPINDLE_HOME`; let the CLI process exit; assert the spool becomes terminal without
  invoking `wait`, `spools`, `peek`, or starting a service.
- Cover successful exit, nonzero exit, timeout, and a process with a descendant that
  outlives its leader.
- Race two supervisor candidates and assert one lifetime-lock owner and one terminal
  transition.
- Reject an incompatible supervisor protocol or spool schema before slot reservation;
  permit a compatible launcher from a different package path and record the diagnostic
  identity.
- Kill the supervisor, start another Spindle operation against the same store, reclaim the
  released lifetime lock, and finalize the original harness without relaunching the task.
- Kill the launching CLI process group after it returns the spool ID and assert the
  detached supervisor and harness continue.
- Kill a launcher after the minimal reservation and before shard creation; assert bounded
  stale-pending finalization.
- Kill a launcher after deterministic shard creation but before full metadata or harness
  publication; assert the supervisor discovers and preserves the shard in the terminal
  record.
- Verify service-launched and stdio-MCP-launched spools still finalize and leave no zombie
  wrapper.
- Keep the launching service alive through supervisor idle retirement and assert the
  supervisor child is reaped rather than left as a zombie.
- Verify the supervisor uses the explicit temporary store path and never touches the real
  user store.

### Streaming and telemetry

- Replay init, assistant tool-use, tool progress, matching tool result, and terminal
  sentinel events in partial writes.
- Assert early `session_id`, monotonic activity updates, `active_tools` set/cleared, and
  incomplete-line handling.
- Assert the incremental reader does not duplicate events after restart or offset
  recovery.
- Replay two parallel tool calls and resolve them independently.
- Relaunch one spool after a nonzero offset; assert atomic generation reset, replacement
  session discovery, no stale active tools, and continued supervisor ownership.
- Race telemetry with drop, timeout, and finalization; assert no terminal state can return
  to `running`.
- Assert a terminal result without a session ID does not erase the earlier indexed ID.
- Keep all existing parked-turn driver tests green.
- Assert legacy one-shot records still finalize and explicitly report unavailable live
  telemetry.

### Cancellation

- Cancel an active streamed spool and assert provenance plus retained partial captures and
  session ID.
- Race `drop` with a terminal sentinel and assert exactly one truthful terminal outcome.
- Assert PID reuse and unverifiable-group protections remain fail-safe.
- Cancel a spool older than the ordinary retention interval and assert its evidence
  survives until `capture_retain_until`; assert cleanup removes it afterward.
- Assert normal completed-spool cleanup removes its transcript and capture artifacts after
  the ordinary retention age, without leaving orphan files.

### Auto-mode compatibility and denials

- Reject `careful`/`auto` plus explicit Haiku before slot reservation.
- Reject omitted-permission plus explicit Haiku before slot reservation.
- Permit supported Opus/Sonnet aliases with `careful`/`auto`.
- Permit Haiku under non-auto profiles, including the existing shard/bypass combinations.
- Pass opaque lodged-profile and unknown future model IDs through unchanged.
- Preserve a classifier denial followed by successful alternate work as a completed result
  with diagnostics.
- Preserve an explicit terminal permission error as an error with structured denial data.
- Preserve an account/provider auto-mode-unavailable error without changing model or
  permission mode.
- Assert denial metadata caps entry count and reason length and contains no tool input.

### Verification

- Run focused lifecycle, stream-driver, permission, and cancellation tests.
- Run the complete test suite with cache and bytecode writes disabled.
- Run Ruff, formatting checks, and the repository diff check.
- Run a live current-version Opus stream canary in an isolated fixture repository before
  enabling the default.

## Rollout

1. Land durable supervisor ownership, cancellation preservation, model validation, and
   denial telemetry while the legacy stream opt-in remains available.
2. Verify standalone CLI completion and service/MCP completion in the installed
   environment.
3. Rerun the existing stream-driver canary against the installed Claude Code and Opus.
4. Flip new Claude spools to the stream driver by default, retaining the explicit
   one-shot rollback switch.
5. Observe spool finalization latency, monitor reclamation, stream parse errors, and
   auto-mode-unavailable errors. Removing legacy compatibility is a separate future
   decision.

Each rollout step is separately reversible. The implementation remains one bounded repair
campaign and receives a two-family Fell before merge.

## Explicitly out of scope

- Redesigning permission profiles or their names.
- Making shards mandatory for code work.
- Treating a shard alone as permission to widen Claude's authority.
- Automatically retrying with `full`, `shard`, or bypass permissions.
- Replacing the HTTP or MCP transport.
- Rewriting the stream driver's parked-task algorithm.
- Updating Claude Code as the repair; 2.1.220 is already the current installed and
  published release at the time of this plan.
- Changing external coordinators beyond documenting that they must consume Spindle-owned
  activity and terminal state rather than raw PIDs.
- General spool-schema cleanup unrelated to the fields required above.

## Acceptance criteria

- A standalone CLI spin reaches a terminal spool record after its launcher exits, without
  another user command or service process.
- A running streamed Opus spool exposes its session ID and advances activity while a tool
  is working.
- Empty stdout on a running legacy one-shot spool is reported as unavailable telemetry,
  never as proof of death.
- Explicit cancellation remains possible, preserves its evidence, and names its source and
  reason.
- Known unsupported Haiku-plus-auto launches fail before consuming a spool slot; Haiku
  remains usable under compatible profiles.
- Supported auto-mode classifier denials are visible without being automatically
  misclassified as incomplete.
- No telemetry or cancellation race can restore a terminal spool to `running`.
- Same-spool fallback resets capture telemetry and remains supervised through the
  replacement process.
- No existing permission profile, shard decision, or authority level changes.
