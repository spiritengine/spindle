# Changelog

## 1.2.0

Release-readiness pass: an installed spindle can now be identified, diagnosed,
and run alongside another install without confusing the two.

### Added

- Claude Opus 5 model selection via `model="opus-5"`, resolved to the canonical
  `claude-opus-5` model ID and advertised by `spin_harnesses`.
- `spindle doctor` — one command that reports the CLI's version and path,
  whether the service answering the port is *this* install, whether the spool
  store is writable, which harness CLIs are on PATH (with versions), and whether
  git/bwrap are available for shards. `--smoke` spawns one harmless read-only
  headless spool per harness (claude-code and codex, the two with an enforced
  read-only tier) and checks the answer comes back. `--json` for machines.
  Exits 1 when a check fails.
- `spindle --version`.
- Identity fields on `GET /health`: `version`, `package`, `pid`, `python`,
  `spool_dir`, `port`.
- `SPINDLE_PORT` and `SPINDLE_HOST`, honored by `serve`, `status`, and `doctor`.
- `install-service --name/--port/--home`, so a second install can run its own
  service alongside an existing one.
- `python -m spindle` entry point.
- README sections for the command line, running as a service, the environment
  variables, and running two installs on one machine.

### Fixed

- Kimi no longer runs auto-approved headless tools against the entire host for
  ordinary spools. Every launch except `full` without shard intent now runs
  inside Spindle's own bwrap boundary with a read-only root, private `/tmp`,
  hidden filesystem-path `/run` sockets, isolated host process/IPC state, and an
  explicit write set; the shared network still exposes localhost and abstract
  Unix sockets. Missing bwrap refuses before reserving a slot or creating a
  shard. Spool records store the actual
  boundary, and respins and retries preserve the shard/research boundary without
  accepting substituted paths. External Git metadata is never writable, so Kimi
  leaves shard changes uncommitted for the caller to inspect and commit before
  `shard_merge`. Shard intent always stays contained; `full` without a shard is
  the explicit uncontained mode.
- `spindle spin --harness <name>` now resolves the same names the `spin` MCP
  tool does. A lodged profile name used to fall through to plain Claude Code
  with none of the profile's model, endpoint, or extra arguments applied, and an
  unknown name used to spawn a Claude Code spool instead of erroring.
- `spindle status` asked a hardcoded `127.0.0.1:8002` with `curl` and printed
  whatever answered, so a fresh install reported another install's service as
  its own. It now uses the resolved host/port, needs no `curl`, and says when
  the responding service is a different install or a skewed version.
- `install-service` writes a timestamped backup before replacing any existing
  service file, and refuses without `--force`. A regenerated unit is built from
  the command line, so hand-added directives are dropped by `--force` whether or
  not spindle wrote the file; and ownership cannot be inferred anyway, since the
  shape spindle generates is the shape users copy out of `examples/` and edit.
  Regeneration follows one rule on both platforms: an explicit argument beats
  what spindle recorded installing the service with, which beats the default.
  Without the middle term, re-running `install-service --name X --force` — which
  is what doctor's own remedies tell you to do — rebuilt the service from the
  command line alone, moving it off its port onto 8002 (colliding with the
  default install) and off its spool store onto `~/.spindle`, stranding every
  spool there.
- Spindle keeps its own record of what it installed each service with, in
  `$XDG_CONFIG_HOME/spindle/services/<name>.json`. Regeneration reads that,
  not the unit file. Reading settings back out of a systemd unit means
  reimplementing systemd's `Environment=` syntax — quoting, escapes, resets,
  and `%h`-style specifiers whose value depends on the service's runtime
  context and cannot be known from the file at all — and a value read slightly
  wrong is worse than one not read, because it is written straight back.
  A service file spindle has no record of is never regenerated from a guess:
  `install-service` stops and asks for the port and store explicitly, offering
  its best reading of the file as a hint to check rather than to trust.
- A launchd agent whose new plist fails to load is restored from its backup and
  reloaded, rather than leaving the machine with the old agent unloaded and a
  rejected plist in place.
- Generated units record `SPINDLE_SERVICE_NAME`, so the `spindle_reload` MCP
  tool restarts the unit its own service was installed as. It hardcoded
  `spindle.service`, so an agent calling it from a second install restarted the
  first one — interrupting that service's in-flight spools while leaving the
  caller's unreloaded.
- `spindle reload` refuses (rather than warning and proceeding) when the service
  it would restart uses a different spool store: a drain that cannot see the
  queue it promised to protect is worse than no drain. `--force` still restarts.
- `install-service` checks `systemctl daemon-reload` and `systemctl enable` exit
  codes instead of printing "Reloaded"/"Enabled" and exiting 0 underneath
  systemd's own error, and writes units to `$XDG_CONFIG_HOME/systemd/user` when
  that is set, rather than a hardcoded `~/.config` systemd may never read.
- Generated units quote their `Environment=` values and escape `%` and `\`. systemd
  splits an unquoted assignment on whitespace and expands `%` specifiers, so a
  `PATH` containing `/mnt/c/Program Files/...` (the WSL default) silently
  truncated at the space, and any `%` in a path made systemd drop the whole
  assignment and fall back to its minimal `PATH`. Both start cleanly and answer
  `/health`; only the spawned agents die. A value ending in a backslash escaped
  its own closing quote and dropped the assignment the same way. `ExecStart` and
  `SPINDLE_HOME` are quoted for the same reasons.
- The launchd plist XML-escapes every interpolated value. One `&` in a directory
  name produced a plist that would not parse, which launchd reports only by
  never starting the service. `install-service` now also checks `launchctl
  load`'s exit code instead of announcing success unconditionally.
- `spindle start`'s background fallback runs from the home directory. `-m` puts
  the working directory at the front of `sys.path`, so starting it from a
  spindle checkout served that checkout's code while the console script was the
  installed one — the exact confusion this release exists to remove.
- `spindle reload --name X` reads the port from that unit instead of assuming
  the default, so it no longer probes a different service (whose store happens
  to match) and reports the store check as clean.
- `spindle doctor` keeps checking the service's PATH when that service is
  confirmed to be this install but uses a different spool store — the gate was
  on the check's overall status, which switched the check off in exactly the
  two-install setup the README documents.
- `spindle doctor` treats an unrelated application answering the port as a
  warning, not a failure, and no longer accepts a response that reports a
  version but not which install it is.
- `spindle doctor --smoke --harness <typo>` fails instead of skipping and
  exiting 0 having smoked nothing; harness names are matched case-insensitively;
  a full spool queue is a skip, not a failure; and a hung smoke keeps its
  working directory rather than deleting it under a process that may be alive.
- `spindle doctor` reports when the running service cannot find a harness this
  shell can — a unit's `PATH` is baked at install time, and when it goes stale
  (a new node version relocates `codex`) the failure otherwise surfaces much
  later as a spool that dies at spawn. `/health` reports the service's `PATH`
  to make this checkable.
- `spindle reload` warns when the service it is restarting stores spools
  somewhere other than the store this command drains — the drain would report an
  idle queue while that service has agents mid-flight.
- The launchd log is named for the service, so two installs no longer interleave
  output in one `~/.spindle/spindle.log`.
- Generated units bake in `PATH` (de-duplicated, with non-existent entries
  dropped), `SPINDLE_PORT`, and `SPINDLE_HOME`. A systemd user unit otherwise
  starts with a minimal `PATH` and cannot find `claude`/`codex`/`gemini`.
- `spindle start`'s no-systemd fallback launches `python -m spindle` rather than
  executing the installed package's `__init__.py` as a loose script.
- The codex sandbox enforcement probe logged its *success* as a warning ("could
  not stat target"), so a clean install's first codex spin printed what looked
  like a failure. A blocked write — the outcome the probe wants — is now a debug
  line; a genuine stat error still warns. No change to the probe's verdict.
- Importing spindle no longer prints authlib's deprecation warning (three lines
  of stderr ahead of every CLI command's output).
- Codex spools launch with `--skip-git-repo-check`, so a working directory that
  is not a git repository still runs. codex 0.145.0 refuses to start outside a
  repo (`Not inside a trusted directory and --skip-git-repo-check was not
  specified`) unless that exact path is listed under `[projects]` in
  `~/.codex/config.toml`, which made whether a spool could run depend on the
  operator's personal config — `spindle doctor --smoke`, which runs in a temp
  dir, failed its codex leg on every machine. Both the fresh spin and the
  `resume` path pass it. Containment is unchanged: the sandbox tier is still
  pinned by `--sandbox` plus a matching `-c sandbox_mode`, and the git check
  only ever gated startup.
- `docs/CODEX_SETUP.md`'s "Verify Sandbox Enforcement" recipe no longer passes
  itself when nothing was enforced. It ran `codex exec --sandbox read-only` from
  `/tmp` and called "no file appeared" a pass — but on codex 0.145.0 that
  invocation exits before the first model turn (`/tmp` is not a repo), and a
  binary enforcing nothing leaves no file either. The only check is now the
  deterministic no-model `codex sandbox` probe, run twice against one fresh
  directory — refuse-the-write leg and allow-the-write control on the same
  target, the shell command Spindle's own probe uses, with the marker emitted
  after the write so it proves the attempt completed. The doc documents no
  `codex exec` pass condition at all: on 0.145.0 the model
  will not perform a write it expects to fail (one execution in 14 attempts),
  so such a check can only return "cannot tell" at an API call apiece, and the
  evidence it would need is not the marker but the `command_execution` item's
  `command` plus a non-zero `exit_code` — a model asked to `echo` the refusal
  string satisfies any marker-based criterion with nothing sandboxed. What the
  section keeps is the reasoning: narration is never proof, and
  `spindle doctor --smoke --harness codex` is the end-to-end check of the spool
  path.
- The other `codex exec` snippets in `docs/CODEX_SETUP.md` pass
  `--skip-git-repo-check` too, so they no longer die outside a git repo. The
  post-install "Verify authentication" step was the worst of these: it failed
  with a trusted-directory error that reads as broken credentials. Those
  snippets also now say `--sandbox workspace-write` instead of `--full-auto`,
  which the same page warns silently overrides `--sandbox`.

### Changed

- The version has one source, `spindle/_version.py`; the wheel metadata reads it
  with setuptools' `attr:` directive, so package metadata, `--version`,
  `/health`, and `doctor` cannot disagree.
- Formatting is `ruff format`, which is what CI has always checked. CONTRIBUTING
  told contributors to run black, whose output CI rejects; the black config and
  dev dependency are gone.

## 1.1.0

Multi-harness support (Claude Code, Codex, Gemini, Kimi), lodged profiles,
permission profiles, shard isolation, and the spool query tools.
