# Changelog

## 1.2.0

Release-readiness pass: an installed spindle can now be identified, diagnosed,
and run alongside another install without confusing the two.

### Added

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

- `spindle spin --harness <name>` now resolves the same names the `spin` MCP
  tool does. A lodged profile name used to fall through to plain Claude Code
  with none of the profile's model, endpoint, or extra arguments applied, and an
  unknown name used to spawn a Claude Code spool instead of erroring.
- `spindle status` asked a hardcoded `127.0.0.1:8002` with `curl` and printed
  whatever answered, so a fresh install reported another install's service as
  its own. It now uses the resolved host/port, needs no `curl`, and says when
  the responding service is a different install or a skewed version.
- `install-service` will not silently overwrite a service file that does not
  carry spindle's marker: without `--force` it refuses, and with `--force` it
  writes a timestamped backup beside the file first. Ownership cannot be
  inferred — the shape spindle generates is also the shape users copy out of
  `examples/` and then edit — so it keeps a copy rather than guess.
- Generated units quote their `Environment=` values and escape `%`. systemd
  splits an unquoted assignment on whitespace and expands `%` specifiers, so a
  `PATH` containing `/mnt/c/Program Files/...` (the WSL default) silently
  truncated at the space, and any `%` in a path made systemd drop the whole
  assignment and fall back to its minimal `PATH`. Both start cleanly and answer
  `/health`; only the spawned agents die. `ExecStart` and `SPINDLE_HOME` are
  quoted for the same reason.
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
