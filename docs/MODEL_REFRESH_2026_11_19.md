# Model refresh, 2026-11-19

Implemented from the research handoff brief-20261119-2pl6 and findings
finding-20261119-sj73 (Claude/Codex) and finding-20261119-bf1n
(Gemini/Kimi/profiles). Sources below were checked by that research session;
implementation did not repeat its authenticated catalog queries.

## Evidence and defaults

Claude: [model overview](https://platform.claude.com/docs/en/models/overview),
[deprecations](https://platform.claude.com/docs/en/about-claude/model-deprecations),
and [CLI model configuration](https://code.claude.com/docs/en/model-config).
New version pins are documented. All existing pins remain because none was
confirmed retired. Rolling aliases remain delegated to the CLI, with `sonnet`
as the default. Direct Anthropic defaults differ from some other provider routes.

Codex: [model docs](https://developers.openai.com/api/docs/models),
[6.2 Astra](https://developers.openai.com/api/docs/models/gpt-6.2-astra),
[6.2 Sol](https://developers.openai.com/api/docs/models/gpt-6.2-sol),
[6.1 Terra](https://developers.openai.com/api/docs/models/gpt-6.1-terra),
[6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol),
[6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna), and
[deprecations](https://developers.openai.com/api/docs/deprecations).
The authenticated account catalog at
`https://chatgpt.com/backend-api/codex/models?client_version=0.170.0`
contains every new target and the retained 6 and 5.6 tiers. Provider guidance
supports current Sol for everyday coding. One `CODEX_DEFAULT_MODEL` now supplies
both discovery and launch: `gpt-6.2-sol` replaces `gpt-5.6-sol`.
The explicit `gpt-5.6` ChatGPT compatibility mapping still selects 5.6 Sol;
API-key/unknown auth still passes that explicit model through unchanged.

Codex removals have confirmed shutdown evidence: 5.1 Codex Max/Mini on July 23,
2026, and 4.1 Nano on October 23. Deprecated 5.3 Codex, 5.4 Nano and 5.1 remain
until April 1, 2027. The previous 5.5/5.5 Pro October retirement claim could not
be reconfirmed; those and all other unconfirmed-retirement pins remain.
A fresh cache for client 0.154.0 returns an older catalog than client 0.170.0;
absence from that older list is not retirement evidence.

Gemini: [models](https://aistudio.google.com/docs/models),
[latest model](https://aistudio.google.com/docs/latest-model),
[Hydrogen](https://aistudio.google.com/docs/models/gemini-4-hydrogen),
[deprecations](https://aistudio.google.com/docs/deprecations), and authenticated
`GET https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000`.
All four element models advertise generateContent. The default stays
`gemini-3.1-pro-preview`, as do `pro`, `3-pro`, and `3.1-pro`; their values are
explicit so a future default change cannot silently move version pins.
Flash/video remain on 3.8 and Flash-Lite on 3.5. No existing target was confirmed
shut down in this sweep. Redirecting 3.5/3.7 Flash and still-available 2.5 pins
remain; 3.1 Customtools and Gemma pins remain in the catalog.

Kimi: [models](https://platform.kimi.ai/docs/models),
[K3.1 Pro guide](https://platform.kimi.ai/docs/guide/kimi-k3-1-pro-quickstart),
and authenticated `GET https://api.moonshot.ai/v1/models`.
Both new targets and all retained targets are in the catalog. Default moves
from `moonshot-ai/kimi-k3` to `moonshot-ai/kimi-k3.1-pro`. Both K3.1 targets
force `--thinking`, including alias, full managed ID, and default selections;
K3/K2.7 requirements remain. Local registration is still validated before spawn.
K2.6/K2.7 are restricted to accounts registered before November 1, not retired.
No global Kimi configuration changed.

## Complete alias audit

Each entry below gives the exact action and mapping. The evidence and retention
rules in the corresponding provider paragraph above apply to every entry.
No unlisted alias was changed. Unknown strings continue to pass through.

### Claude

Retained: haiku → haiku

Retained: sonnet → sonnet

Retained: opus → opus

Retained: fable → fable

Added: fable-5.2 → claude-fable-5-2

Retained: opus-5.5 → claude-opus-5-5

Added: sonnet-5.5 → claude-sonnet-5-5

Added: haiku-5.5 → claude-haiku-5-5

Retained: fable-5.1 → claude-fable-5-1

Retained: sonnet-5 → claude-sonnet-5

Retained: haiku-4.5 → claude-haiku-4-5

Retained: fable-5 → claude-fable-5

Retained: opus-5 → claude-opus-5

Retained: opus-4.8 → claude-opus-4-8

Retained: opus-4.7 → claude-opus-4-7

Retained: opus-4.6 → claude-opus-4-6

Retained: sonnet-4.6 → claude-sonnet-4-6

Retained: opus-4.7-1m → claude-opus-4-7[1m]

Retained: opus-1m → claude-opus-4-7[1m]


### Codex

Moved from gpt-6-astra: astra → gpt-6.2-astra

Moved from gpt-5.6-sol: sol → gpt-6.2-sol

Moved from gpt-5.6-sol: codex → gpt-6.2-sol

Moved from gpt-5.6-terra: terra → gpt-6.1-terra

Moved from gpt-5.6-luna: luna → gpt-6-luna

Added: 6.2 → gpt-6.2-astra

Added: 6.2-astra → gpt-6.2-astra

Added: 6.2-sol → gpt-6.2-sol

Added: 6.1-sol → gpt-6.1-sol

Added: 6.1-terra → gpt-6.1-terra

Retained: 6 → gpt-6-astra

Retained: 6-astra → gpt-6-astra

Retained: 6-sol → gpt-6-sol

Retained: 6-luna → gpt-6-luna

Retained: reserve → gpt-reserve

Retained: 5.6 → gpt-5.6-sol

Retained: 5.6-sol → gpt-5.6-sol

Retained: 5.6-terra → gpt-5.6-terra

Retained: 5.6-luna → gpt-5.6-luna

Retained: 5.5 → gpt-5.5

Retained: 5.5-pro → gpt-5.5-pro

Retained: 5.3-codex → gpt-5.3-codex

Retained: 5.4 → gpt-5.4

Retained: 5.4-mini → gpt-5.4-mini

Retained: 5.4-nano → gpt-5.4-nano

Retained: 5 → gpt-5

Retained: 5-mini → gpt-5-mini

Retained: 5.1 → gpt-5.1

Retained: 4.1 → gpt-4.1

Retained: 4.1-mini → gpt-4.1-mini

Removed: 5.1-codex-max → gpt-5.1-codex-max

Removed: 5.1-codex-mini → gpt-5.1-codex-mini

Removed: codex-mini → gpt-5.1-codex-mini

Removed: 4.1-nano → gpt-4.1-nano


### Gemini

Added: carbon → gemini-4-carbon

Added: 4-carbon → gemini-4-carbon

Added: neon → gemini-4-neon

Added: 4-neon → gemini-4-neon

Added: hydrogen → gemini-4-hydrogen

Added: 4-hydrogen → gemini-4-hydrogen

Added: argon → gemini-4-argon

Added: 4-argon → gemini-4-argon

Retained: flash → gemini-3.8-flash

Retained: pro → gemini-3.1-pro-preview

Retained: video → gemini-3.8-flash

Retained: flash-lite → gemini-3.5-flash-lite

Retained: 2.5-flash → gemini-2.5-flash

Retained: 2.5-pro → gemini-2.5-pro

Retained: 2.5-flash-lite → gemini-2.5-flash-lite

Retained: 3-pro → gemini-3.1-pro-preview

Retained: 3-flash → gemini-3-flash-preview

Retained: 3.1-pro → gemini-3.1-pro-preview

Retained: 3.1-flash-lite → gemini-3.1-flash-lite

Retained: 3.1-customtools → gemini-3.1-pro-preview-customtools

Retained: 3.5-flash → gemini-3.5-flash

Retained: 3.5-flash-lite → gemini-3.5-flash-lite

Retained: 3.6-flash → gemini-3.6-flash

Retained: 3.7-flash → gemini-3.7-flash

Retained: 3.8-flash → gemini-3.8-flash

Retained: gemma-4 → gemma-4-31b-it

Retained: gemma-4-mini → gemma-4-26b-a4b-it


### Kimi

Moved from moonshot-ai/kimi-k3: thinking → moonshot-ai/kimi-k3.1-pro

Moved from moonshot-ai/kimi-k3: latest → moonshot-ai/kimi-k3.1-pro

Added: k3.1 → moonshot-ai/kimi-k3.1-pro

Added: k3.1-pro → moonshot-ai/kimi-k3.1-pro

Added: k3.1-pro-highspeed → moonshot-ai/kimi-k3.1-pro-highspeed

Retained: k3 → moonshot-ai/kimi-k3

Retained: k2.6 → moonshot-ai/kimi-k2.6

Retained: k2.7-code → moonshot-ai/kimi-k2.7-code

Retained: k2.7 → moonshot-ai/kimi-k2.7-code

Retained: code → moonshot-ai/kimi-k2.7-code

Retained: k2.7-code-highspeed → moonshot-ai/kimi-k2.7-code-highspeed

Moved from moonshot-ai/kimi-k2.7-code-highspeed: highspeed → moonshot-ai/kimi-k3.1-pro-highspeed

## Live probes and limitations

Installed CLI versions checked: Claude 2.1.328; Codex 0.170.0; Gemini 0.39.1;
kimi-cli 1.49.0. Each attempt ran outside user projects, under `/tmp`, with a
50-second outer timeout. No files under `~/.spindle` were edited.

Claude probe command (substitute each full ID for MODEL; Haiku budget was 0.10,
Sonnet and Fable 0.25):

```bash
timeout 50 claude -p 'Reply exactly MODEL_OK. Do not use tools.' \
  --model MODEL --tools '' --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --no-session-persistence --output-format json --max-budget-usd 0.25
```

`claude-haiku-5-5`: exit 0, success, MODEL_OK; canonical model in result metadata
matched; reported cost $0.00313219. A SessionEnd hook was cancelled after the
successful result.

`claude-sonnet-5-5`: exit 0, success, MODEL_OK; modelUsage matched the requested
ID; reported cost $0.0593269.

`claude-fable-5-2`: exit 1, error_max_budget_usd, no final answer; modelUsage
named the requested ID, reported cost $0.2600578. This is not a successful smoke
validation. The alias remains supported by documentation evidence.

```bash
timeout 50 codex exec --skip-git-repo-check --ephemeral --sandbox read-only \
  --model gpt-6.2-sol --json 'Reply exactly MODEL_OK. Do not use tools.'
```

Exit 1 before inference: app-server client initialization failed with read-only
filesystem. Other new Codex names were not probed after this shared failure.
All new Codex targets rely on supplied documentation/account catalog evidence;
the research finding also records an earlier successful 6.2 Astra spool.
Both reviewers verified that the service resolves and launches Codex 0.170.0,
which meets Terra 6.1's minimum of 0.159.0. Spools that recorded 0.154.0 after
the in-place upgrade inherited a stale version lookup. Commit `b5e02ab` fixes
the cache that produced those records.

```bash
timeout 50 gemini -p 'Reply exactly MODEL_OK. Do not use tools.' \
  -m gemini-4-hydrogen -o json
```

Exit 124 after 50 seconds with empty stdout/stderr. No inference success and no
specific cause established. Carbon, Neon and Argon were not probed. All four
aliases rely on supplied documentation/live catalog evidence; default unchanged.

```bash
timeout 50 kimi-cli --print --thinking -m moonshot-ai/kimi-k3.1-pro \
  --max-steps-per-turn 1 --max-retries-per-step 1 \
  -p 'Reply exactly MODEL_OK. Do not use tools.'
```

Exit 1 before inference: read-only `~/.kimi/logs/kimi.log`. Highspeed was not
probed after this shared failure. Both new aliases rely on supplied
documentation/live catalog evidence; command tests verify required thinking.

## Open decisions and external profiles

Gemini: whether Hydrogen should become the default after CLI smoke validation,
and whether `pro` should ever track an element model. Neither moved.

Codex: confirm entitlement for the new default. Confirm product/account-specific 5.5/5.5 Pro retirement
before removing either pin. No ambiguous `6.1` or undocumented tier IDs added.

Profile discovery now advertises existing `model_aliases` as `models` (empty
object when absent). Resolution and explicit-alias precedence are unchanged.
No lodged profile JSON was edited. Recommendations from finding-20261119-bf1n:

GLM: retain 5.3 defaults/current aliases; consider explicit `5.3-flashx-legal`
and `flashx-legal` → `glm-5.3-flashx-legal`. Authenticated Z.ai catalog confirms
existence; Anthropic-route tool use remains untested. Do not move `flashx`.

MiMo: retain V2.6 defaults/aliases. Remove external `2.5` and `2.5-pro` only in
separately authorized profile upkeep: indexed provider announcement says retired
October 21, corroborated by catalog absence, but the announcement body was
unreadable and no retirement inference probe ran. Refresh checked-date prose.

Grok: retain 4.7 default/latest and all existing targets; refresh the checked
date and avoid reconfirming the unsupported `grok-4-latest` → 4.3 assertion.

DeepSeek: retain `deepseek-v4-pro` and `deepseek-flash` request IDs; update backend
version descriptions to V4-Pro-0813 and V4.1-Flash-1017 only. No API key was
available in research; no authenticated probe. Do not invent backend-version IDs.

## Files changed

spindle/__init__.py — aliases/defaults, required thinking, profile discovery,
spin docstring, argparse help and dated evidence comments.

tests/test_spindle.py — command/spool/discovery assertions, Fable family,
retired-alias exclusions, retained pins, Kimi local-registration checks.

README.md, docs/MULTI_HARNESS_GUIDE.md, docs/CODEX_SETUP.md, CODEX_HARNESS.md —
current model choices, defaults, availability and version caveats.

docs/GEMINI_VIDEO.md — preserved historical proof, added dated current-use note.

CHANGELOG.md — dated refresh, preserving the September sweep as historical.

docs/MODEL_REFRESH_2026_11_19.md — this evidence, alias, probe and validation report.

## Validation

Environment: Linux CPython 3.12.0, `os.pidfd_open` present, bubblewrap 0.4.0;
`timeout 10 bwrap --ro-bind / / --unshare-pid --proc /proc --dev /dev -- true`
returned 0. No namespace tests were deselected.

The system Ruff was 0.15.0. Installed pinned Ruff 0.16.3 into a temporary venv
using `UV_CACHE_DIR=/tmp/spindle-refresh-uv-cache uv venv /tmp/spindle-refresh-tools`
and `UV_CACHE_DIR=/tmp/spindle-refresh-uv-cache uv pip install --python
/tmp/spindle-refresh-tools/bin/python ruff==0.16.3`. The initial install attempt
without the temporary cache override failed on the read-only default uv cache.

Initial focused test command (after `mkdir -p /tmp/spindle-empty-spools`):

```bash
timeout 240 bwrap --ro-bind / / --bind /tmp /tmp --bind "$PWD" "$PWD" \
  --ro-bind /tmp/spindle-empty-spools "$HOME/.spindle/spools" \
  --dev /dev --proc /proc --chdir "$PWD" -- \
  env PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest tests/test_spindle.py -p no:cacheprovider -q --tb=short -ra \
  -k 'alias or TestSpinHarnesses or TestGeminiHarness or TestKimiHarness or explicit_gpt_56 or profile_appears_in_harnesses'
```

Result: 27 failed, 155 passed, 912 deselected in 211.47 seconds. All failures
were in newly added assertions: six assumed Claude spools store canonical IDs
(they preserve the requested alias for respin), and 21 used an unqualified
Codex map name. Corrected both; the latter also produced the initial Ruff F821.
Claude tests now verify that the recorded alias resolves through discovery to
the exact command model. No production behavior was changed to satisfy these tests.

Final lint commands, using Ruff 0.16.3:

```bash
/tmp/spindle-refresh-tools/bin/ruff format --check .
/tmp/spindle-refresh-tools/bin/ruff check .
```

Results: 69 files already formatted; all checks passed.

Full-suite command:

```bash
mkdir -p /tmp/spindle-empty-spools
timeout 1800 bwrap --ro-bind / / --bind /tmp /tmp --bind "$PWD" "$PWD" \
  --ro-bind /tmp/spindle-empty-spools "$HOME/.spindle/spools" \
  --dev /dev --proc /proc --chdir "$PWD" -- \
  env PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest tests/ -p no:cacheprovider -q --tb=short -ra \
  > /tmp/spindle-refresh-full-tests.log 2>&1
```

Full-suite result: **5 failed, 3066 passed in 813.56 seconds (13m33s)**,
exit 1. **Zero deselected, zero skipped**, including namespace tests. All
model-refresh behavior tests passed. This is not a clean full-suite pass.

The four real-Codex failures came from running the suite inside a Codex sandbox.
The outer sandbox mounts `/tmp/codex-daemon-1000` read-only with mode `000`,
so the nested Codex 0.170.0 rejects its socket directory. In independent review,
all four tests passed on both `e23ccc7` and `b5e02ab` with the CONTRIBUTING.md
bwrap command run outside a Codex session. A second reviewer exercised live
read-only and research spools through the service and verified that both
enforced their configured write boundaries. SKEIN records the evidence in
`finding-20261119-5hyz`.

The fifth failure was
`test_stateless_cancel_disconnect_releases_waiter[yield-True]`: MCP's
`message_router` logged `anyio.ClosedResourceError` while receiving from its
closed memory stream, violating the test's no-error-log assertion. This test
passed alone on both base and changed worktree. No cancellation, stream, or
process-containment code was changed here. The full-run failure remains recorded;
it is not erased by the successful reruns.

Baseline diagnosis used an untouched archive of
`e23ccc7e85c7da149056f1a7829cefbfe725d5f7`:

```bash
mkdir -p /tmp/spindle-refresh-base
git archive HEAD | tar -x -C /tmp/spindle-refresh-base
timeout 240 bwrap --ro-bind / / --bind /tmp /tmp \
  --ro-bind /tmp/spindle-empty-spools "$HOME/.spindle/spools" \
  --dev /dev --proc /proc --chdir /tmp/spindle-refresh-base -- \
  env PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest tests/test_codex_containment.py \
  -p no:cacheprovider -q --tb=short -ra \
  -k 'test_real_codex_research_profile_reaches_loopback_and_writes_only_its_target or test_real_codex_probe_refuses_a_widened_research_write_set or test_real_codex_sandbox_starts_inside_shard_wrapper' \
  > /tmp/spindle-refresh-base-codex.log 2>&1
```

Inside the outer Codex sandbox, 3 failed and 68 were deselected in 26.34
seconds on the base. The nested CLI reported “app-server socket directory must
be a user-owned directory with mode 0700.”

```bash
timeout 90 bwrap --ro-bind / / --bind /tmp /tmp \
  --ro-bind /tmp/spindle-empty-spools "$HOME/.spindle/spools" \
  --dev /dev --proc /proc --chdir /tmp/spindle-refresh-base -- \
  env PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest 'tests/test_blocking_spin.py::test_stateless_cancel_disconnect_releases_waiter[yield-True]' \
  -p no:cacheprovider -q --tb=short -ra \
  > /tmp/spindle-refresh-base-cancel.log 2>&1
```

Result: 1 passed in 10.48 seconds on the base, no deselections.

The same cancellation test was rerun on the changed worktree:

```bash
timeout 90 bwrap --ro-bind / / --bind /tmp /tmp --bind "$PWD" "$PWD" \
  --ro-bind /tmp/spindle-empty-spools "$HOME/.spindle/spools" \
  --dev /dev --proc /proc --chdir "$PWD" -- \
  env PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest 'tests/test_blocking_spin.py::test_stateless_cancel_disconnect_releases_waiter[yield-True]' \
  -p no:cacheprovider -q --tb=short -ra \
  > /tmp/spindle-refresh-current-cancel.log 2>&1
```

Result: 1 passed in 4.91 seconds, no deselections.


Fourth real-Codex failure, baseline command:

```bash
timeout 120 bwrap --ro-bind / / --bind /tmp /tmp \
  --ro-bind /tmp/spindle-empty-spools "$HOME/.spindle/spools" \
  --dev /dev --proc /proc --chdir /tmp/spindle-refresh-base -- \
  env PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest tests/test_spindle.py::TestCodexSandboxEnforcesProbe::test_real_codex_binary_enforces \
  -p no:cacheprovider -q --tb=short -ra \
  > /tmp/spindle-refresh-base-codex-enforces.log 2>&1
```

Inside the same outer Codex sandbox, the enforcement probe failed on the base
in 14.48 seconds, with zero deselections.

Collection-only diagnostic used the same full-suite bwrap invocation with
`timeout 120` and pytest arguments
`tests/ --collect-only -p no:cacheprovider -q`, redirected to
`/tmp/spindle-refresh-collected.log`: 3071 tests collected in 34.92 seconds.

`git diff --check` passed during the implementation run.
Worktree: `/home/patrick/projects/spindle/worktrees/codex-0464ae91-20261119-001`.
