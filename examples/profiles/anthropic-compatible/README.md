# Example profile: `anthropic-compatible`

A **profile** is a named, lodged configuration: a base harness plus a set of
overrides. The most common use is pointing the Claude Code harness at any
Anthropic-compatible endpoint by injecting env vars into the spawned child —
without changing how spindle parses output, unspools, or resumes sessions.

A profile is a **folder** (the folder name is the profile name, i.e. the value
you pass as `harness`) containing a single `profile.json`. The folder-per-profile
layout leaves room for future additions (a seed config dir, notes, binaries) to
sit alongside the JSON.

This directory is the committed example. JSON has no comments, so this README is
the schema reference.

## Schema

Every field is optional except that the file must parse as a JSON object.

| Field | Type | Meaning |
|-------|------|---------|
| `description` | string | One-liner shown to humans/agents in `spin_harnesses()`. |
| `harness` | string | Base harness. Default `"claude-code"`. v1 only supports `"claude-code"` as a base. |
| `model` | string | Default model for this profile (a caller-passed `model` still wins). |
| `model_aliases` | object | Profile-scoped alias map. A caller `model` matching a key is mapped before falling back to the literal. |
| `base_url` | string | Sets `ANTHROPIC_BASE_URL` in the child (the provider's Anthropic-compatible base). |
| `api_key` | string | Sets `ANTHROPIC_API_KEY` in the child. |
| `config_dir` | string | Sets `CLAUDE_CONFIG_DIR` in the child (isolates it from your real `~/.claude`). |
| `env` | object | Arbitrary extra environment variables for the child. |
| `extra_args` | array | Flags appended verbatim to the `claude` CLI invocation. |

### Isolation defaults

- If `base_url` is set but `config_dir` is **not**, `config_dir` defaults to
  `~/.spindle/profiles/<name>/claude-config` and is created if missing. This
  keeps an alt-endpoint agent from inheriting your real `~/.claude` MCP servers
  and `CLAUDE.md`.
- For a same-endpoint profile (no `base_url`, just e.g. `extra_args`), no
  `config_dir` is forced.
- If `base_url` is set with a base harness other than `claude-code`, spin
  returns a clear error — only the Claude path supports endpoint injection.

## Value resolution

Every string value (`base_url`, `api_key`, `config_dir`, each `env` value, each
`extra_args` element) is resolved at **spawn time**, fresh on every spin and
respin (so rotated secrets take effect):

1. `${ENV_VAR}` is expanded from the environment. An unset variable is left as
   the literal `${VAR}` and a warning is logged — nothing crashes.
2. If the resolved value contains `op://`, it is passed through
   `strongbox inject` (if the `strongbox` binary is on PATH), else `op inject`
   (if `op` is on PATH). If neither exists, the value is left literal and a
   warning is logged. This keeps strongbox/1Password an **optional** local
   convenience — the baseline `${ENV}` path needs no external tool.

This example uses `${EXAMPLE_API_KEY}` so it works with nothing but an env var.

## How to activate

Profiles are loaded from two locations (later overrides earlier):

1. **Canonical:** `~/.spindle/profiles/<name>/profile.json` — where real,
   private profiles live, physically outside any repo.
2. **Dev convenience:** `./profiles/<name>/profile.json` relative to the current
   working directory (gitignored).

To try this example, copy the folder into the canonical location and set the
env var it references:

```bash
mkdir -p ~/.spindle/profiles
cp -r examples/profiles/anthropic-compatible ~/.spindle/profiles/
export EXAMPLE_API_KEY="sk-..."        # your provider key
```

Then spin with the profile name as the harness:

```python
spool_id = spin("Summarize this module", harness="anthropic-compatible",
                 working_dir="/path/to/project")
spool_id = spin("Quick pass", harness="anthropic-compatible", model="fast")  # uses model_aliases
```

`unspool(spool_id)` and `respin(...)` work unchanged — the profile only shapes
how the child process is launched.
