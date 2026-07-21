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
- `install-service` refuses to overwrite a service file spindle did not write,
  even with `--force`, instead of clobbering a hand-written unit.
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
