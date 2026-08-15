# Slice 2 owner decisions

These decisions implement the 21 open questions in
`finding-20260811-igbj`. They are intentionally conservative and limited to
the namespace-safe shared owner primitive.

1. Primary containment: S2-C-OWN-01 and S2-C-OWN-03 prove that owner-local
   subreaping, pidfds, and PDEATHSIG cannot contain work after the owner itself
   dies, so the conditional fallback is resolved: the packaged watchdog is
   mandatory. It is the owner's longer-lived parent and a Linux child
   subreaper. The owner still opens provider pidfds immediately and arms
   PDEATHSIG with an immediate `getppid()` re-check as a secondary net.
   Ownership and terminal authority never depend on PDEATHSIG. A watchdog-held
   lease pipe lets the owner detect watchdog-parent loss, take over descendant
   containment, publish cleanup proof, and exit. This covers ordered watchdog
   loss followed by owner loss after that detection; simultaneous unobservable
   double `SIGKILL` is not claimed.
2. Descendant guarantee: the owner reaps descendants while healthy. If it
   crashes, the watchdog adopts, kills, and reaps the provider plus descendants
   reparented through it, including a `setsid` escape, before publishing crash
   containment evidence. Session/process-group escape is not accepted as
   cleanup. Adversarial nested subreapers remain outside the promise.
3. pidfd acquisition: accept the narrow `fork` to `pidfd_open` window for a
   direct child under enforced zombie retention: the owner alone reaps, does
   not use `SIG_IGN`/`SA_NOCLDWAIT`, opens the pidfd immediately, and polls
   it once before registration. No native `clone(CLONE_PIDFD)` launcher is
   added unless the immediate-exit test disproves this boundary.
4. Namespace placement: the logical owner stays in the ambient ancestor PID
   namespace, outside provider bwrap PID unshares. Persisted owner PID,
   provider PID, and PGID use the owner's coordinate space and always carry the
   owner's namespace device/inode.
5. Legacy authority: namespace equality is insufficient. Automatic legacy
   action requires the recorded service/owner identity and its generation;
   otherwise only an explicit manual-recovery authority may act.
6. Physical names: `<id>.process-owner`, `<id>.owner-identity`,
   `<id>.process-identity`, `<id>.owner-exit`,
   `<id>.journal-guard`, and `<id>.control-mailbox/`. Requests and receipts
   are unique `<request-id>.request` and `<request-id>.receipt` entries.
   `OWNER_ARTIFACT_SUFFIXES` is the mandatory registry.
7. Slice-2 durability: request, receipt, and identity publications use a
   same-directory temporary file, file fsync, atomic replace, and directory
   fsync. Torn-record recovery and journal replay remain slice 3.
8. Owner acknowledgement: it means the current generation durably accepted
   responsibility. It says nothing about provider acknowledgement, signals,
   cleanup, child exit, or terminalization. Stale generations receive a durable
   `rejected_stale_generation` receipt with no acknowledgement timestamp.
9. Owner death during stop: before acknowledgement and after acknowledgement
   but before cleanup both become indeterminate while preserving the exact
   facts. Durable cleanup plus child-exit evidence and the released exact inode
   allow recovery to publish the requested terminal once.
10. Concurrent requests: request IDs are idempotent and never overwrite one
    another. The first current-generation request durably accepted wins; later
    requests receive a superseded receipt and remain provenance. Timeout has no
    hidden priority over an already accepted explicit request.
11. FLAG FOR PATRICK — drop versus cancel public semantics are not settled.
    The compatibility implementation preserves `kind=drop` as provenance and
    maps its desired process terminal to `cancelled`; it must not delete live
    state. Whether the later retained public record differs from cancel remains
    a product decision.
12. Owner generation: the launcher reserves a positive monotonic generation in
    the authoritative spool episode under the record guard before watchdog
    publication. The owner later binds the exact ownership inode and accepts
    provider custody. A replacement increments the last durable generation; it
    never reuses a generation after restart.
13. Owner wakeup: bounded mailbox polling is authoritative. A future inotify or
    pipe wakeup may reduce latency but may not replace polling or correctness.
14. Wall time: persist an absolute UTC deadline. A live owner measures elapsed
    execution with monotonic time. Restart/reboot recovery treats an overdue
    deadline as a durable timeout request, but still requires ownership and
    cleanup evidence before terminalization. If cleanup already accepted a stop
    request, that first request retains its terminal provenance rather than
    being rewritten. An unavailable or expired upstream provider session is not
    replaced automatically: its respin remains terminal with the provider's
    failure, and its saved transcript remains available for manual reconstruction
    in a separate conversation (finding-20260814-5ckx).
15. Store repair: unreadable/replaced/missing-current ownership paths make the
    store unhealthy and reject launches. Diagnosis belongs in `doctor`;
    repair is an explicit operator action which validates the complete artifact
    set and recorded inode. No automatic death guess or inode replacement.
16. Filesystem scope: stable flock ownership is supported only on local
    filesystems in slice 2. Network filesystem support is deferred and must not
    be claimed implicitly.
17. Live inode reverification: verify after every acquisition, before accepting
    control, before publishing stopping or terminal state, before cleanup
    receipt publication, and before release. The owner loop also verifies once
    per poll interval.
18. Retirement unit: spool record, stable owner and journal-guard inodes,
    identities, mailbox requests/receipts, exit evidence, stdout, stderr,
    prompt, transcript, and provider captures are one set. A bound episode
    requires acquisition and verification of its recorded released inode before
    deletion. A valid aborted episode that never bound an inode instead acquires
    and revalidates any harmless physical lock pathname without trusting a stale
    identity mirror, then retires the same complete set. The presence of an
    episode always selects this authority path; a missing optional
    `.owner-identity` mirror never permits legacy or partial cleanup.
19. Provider cancellation interface: one narrow callback returns attempted,
    acknowledged, terminal-observed, or unsupported with timestamps. It neither
    parses provider events nor reduces lifecycle state.
20. FLAG FOR PATRICK — support for setuid/setgid/file-capability provider
    executables is not settled. The owner design must remain safe when
    PDEATHSIG is cleared because PDEATHSIG is secondary. No silent claim of
    support and no rejection policy is added until Patrick chooses the public
    executable policy.
21. Test injection: checkpoint and monotonic-clock dependencies use explicit
    inherited socket FDs supplied on direct owner startup. Hooks are disabled
    when those FDs are absent; production behavior never uses sleeps or
    environment phase-marker files.

## Stage 3 refinements

22. Public launch ownership: every harness launch enters through the packaged
    logical owner behind the existing pre-exec barrier. The compatibility
    `pid` field names the owner; `owner_pid`, `provider_pid`, and
    `provider_process_group_id` make the two process roles explicit.
23. Record serialization: logical-owner read-modify-write transitions acquire
    the same per-spool record lock as launchers and observers. Observers publish
    durable requests while holding that lock. The owner may publish stopping and
    cleanup facts, but only a reconciler may persist release and then project a
    terminal status, error, and normalized terminal kind. This prevents a stale
    observer write from replacing authoritative episode progress.
24. Generation-scoped settlement: owner exit and cleanup evidence authorizes
    settlement only when its `owner_generation` matches the current durable
    identity. A same-ID replacement removes prior exit sidecars before launch
    while retaining the identity needed to allocate the next generation.
25. Legacy recovery retirement: the uncalled `_recover_orphans` path is
    deleted. Production recovery uses unified reconciliation exclusively;
    maintenance suppresses only lock-acquisition `OSError`, and an explicit
    supervisor store root always refreshes `_STORE_LAYOUT` with `SPINDLE_DIR`.

## Stage 4 refinements

26. Watchdog authority: the watchdog records containment and cleanup facts but
    does not bind an ownership inode, invent acceptance, interpret provider
    output, persist release, or publish terminals. Owner crash before inode
    binding aborts the current reservation without fabricating current owner
    identity or exit evidence. Otherwise status remains running through
    `cleanup_proven` until reconciliation proves release.
27. Crash settlement: owner loss before durable cleanup normalizes to
    `indeterminate` only after reconciler release, preserving request and
    acknowledgement facts without rewriting intent as outcome. Owner loss after
    an acknowledged cleanup receipt and child-exit evidence recovers the
    requested terminal exactly once and records `owner_crashed_after_cleanup`.
28. Foreign dead-owner observation: a namespace-mismatched observer treats a
    retireable released cleanup episode as `unverifiable`; it cannot settle,
    mutate, retire, or free capacity. This refusal belongs to reconciliation,
    finalization, and capacity consumers rather than the pure episode
    classifier. A same-namespace reconciler may prove release from the same
    preserved episode and publish its terminal, so foreign refusal is not a
    permanent property of the record.

## Generation-episode refinements

29. Episode authority: `spindle.owner-episode/1` in the spool record is the
    authority for a current live owner. Its generation, revision, phase,
    identities with PID namespaces, exact lock inode, deadline, winning request,
    acknowledgement, cleanup, failure, and release facts govern classification
    and mutation. Flat fields and sidecars are compatibility or diagnostic
    mirrors and cannot override it.
30. Episode completion: `cleanup_proven` is active while its exact inode is
    held and retireable after the inode is observed released. The reconciler
    alone persists `released` with its proof. Only after that transition does it
    publish terminal status and error, set the normalized terminal kind, and
    remove `public_stop_state`; requested stops retain `stopping` until then.
31. Capacity: every record reaches capacity and destructive consumers through
    the same episode classification. Active and unhealthy records consume or
    block capacity. Retireable cleanup and valid unbound aborts do not, except
    that foreign namespace refusal retains capacity until an authorized
    same-namespace observer reconciles the record.
32. Control admission: a public drop, cancel, timeout, or shard abandonment may
    publish a request only for an `accepted` current
    episode whose exact recorded inode is held. Admission takes the mailbox
    guard before the spool lock. A refusal creates no request or receipt.
33. Pre-bind abort: an aborted reservation with no authoritative lock fact may
    coexist with a released physical lock pathname and a stale predecessor
    identity mirror. Maintenance validates the current aborted episode and
    acquires the unbound pathname itself; it never uses the stale mirror as
    custody authority or fabricates current owner identity or exit facts.
34. Mixed versions: current supervisors advertise `owner-episode-v1` and do
    not migrate a live pre-episode owner in place. Unknown or mixed live formats
    are unhealthy. Schema-1 terminal history remains readable, and current
    startup may proceed only after the incompatible lifetime owner drains.
