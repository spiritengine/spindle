# Slice 2 owner decisions

These decisions implement the 21 open questions in
`finding-20260811-igbj`. They are intentionally conservative and limited to
the namespace-safe shared owner primitive.

1. Primary containment: the owner is a Linux child subreaper, opens pidfds for
   direct children immediately after spawn, and arms PDEATHSIG only as a
   secondary net from the owner main thread with an immediate `getppid()`
   re-check. A packaged watchdog will be added only if that combination cannot
   pass S2-C-OWN-01 and S2-C-OWN-03. Ownership and terminal authority never
   depend on PDEATHSIG.
2. Descendant guarantee: the owner must reap all descendants which become its
   children and must not release custody while tracked children survive.
   Session/process-group escape is not accepted as cleanup. If subreaping and
   pidfd tracking cannot contain an escaped descendant after owner crash, the
   watchdog becomes mandatory. Adversarial nested subreapers are not promised
   without that watchdog.
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
12. Owner generation: allocate a positive monotonic generation under the
    stable ownership lock and persist it in owner identity before provider
    launch. A replacement increments the last durable generation; it never
    reuses a generation after restart.
13. Owner wakeup: bounded mailbox polling is authoritative. A future inotify or
    pipe wakeup may reduce latency but may not replace polling or correctness.
14. Wall time: persist an absolute UTC deadline. A live owner measures elapsed
    execution with monotonic time. Restart/reboot recovery treats an overdue
    deadline as a durable timeout request, but still requires ownership and
    cleanup evidence before terminalization.
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
    prompt, transcript, and provider captures are one set. Retirement acquires
    and verifies the recorded released inodes before deleting the set.
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
    durable requests while holding that lock; only the owner publishes a stop
    terminal after cleanup. This prevents a stale observer write from replacing
    an owner terminal record.
24. Generation-scoped settlement: owner exit and cleanup evidence authorizes
    settlement only when its `owner_generation` matches the current durable
    identity. A same-ID replacement removes prior exit sidecars before launch
    while retaining the identity needed to allocate the next generation.
25. Legacy recovery retirement: the uncalled `_recover_orphans` path is
    deleted. Production recovery uses unified reconciliation exclusively;
    maintenance suppresses only lock-acquisition `OSError`, and an explicit
    supervisor store root always refreshes `_STORE_LAYOUT` with `SPINDLE_DIR`.
