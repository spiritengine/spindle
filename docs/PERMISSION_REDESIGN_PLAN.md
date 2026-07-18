> # ⛔ DEPRECATED — DO NOT BUILD FROM THIS DOCUMENT
> ---
> This plan was **superseded**, and its central codex-side premise was **REFUTED by review**.
> Kept only as a record of the investigation (and of how the fell/DCA caught the errors).
>
> **What was wrong:** this plan claimed codex "has no working sandbox lever on this box"
> and proposed disabling codex's own sandbox to re-wrap it in spindle's older bwrap. That was
> tested against the **wrong codex binary** (a login-shell 0.144.x, not the 0.125.0 the service
> actually ran). Codex ships its own vendored bubblewrap+seccomp sandbox that **works without
> landlock**; the real fix was to stop discarding it. The `bwrap sealed/shard/open` write-set model
> here was **not built**.
>
> **What actually shipped** (master `1bf11e5`, 2026-07-18): Claude `careful`/default →
> `--permission-mode auto` (classifier-vetted, allowlist retired); codex passes `--sandbox <mode>`
> (config-proofed with `-c sandbox_mode`) with a fail-loud behavioral probe; `readonly`/`manual`
> tight tier; respin carries the tier. Trust the **code + SKEIN findings** (`finding-20260716-9xpk`,
> `-ud07`, `-20260718-34ry`), not this document.
> ---

# Spindle permission redesign — plan (DEPRECATED / SUPERSEDED — see banner above)

Status: ❌ DEPRECATED. Was: PROPOSED, awaiting fell. Branch `permission-redesign`, base `b4dfde0`.
Author: glitch-0716, 2026-07-16. Paired with Patrick. Superseded 2026-07-18.

## Prime directive

**Spindle is a functional trash fire and the "functional" half is load-bearing.** It is a
workhorse that is, in practice, mostly fine. This redesign is not licensed to make it
prettier at the cost of making it flakier. Every change below must leave the common path
(spin a reviewer, spin an implementer, get a result) working exactly as well or better.
If a step cannot preserve that, the step is wrong, not the workhorse.

Rollback is `git checkout master`. Base commit is `b4dfde0`.

---

## 1. What is actually broken (verified, not inherited)

Every claim in this section was mechanically re-verified for this plan. Claims that came
from prose summaries and could NOT be verified are marked as such and are not built on.

### 1.1 Codex has no working permission lever on this box — at all

- Kernel is `5.4.0-89-generic`. Landlock needs 5.13+. `/sys/kernel/security/landlock` does
  not exist. `_has_landlock_support()` returns **False**. (Verified by execution.)
- `_codex_sandbox_for_permission()` (line 346-363) has no `careful` branch; it falls
  through to `workspace-write`.
- Line 5077, `if sandbox and has_landlock:` — **`--sandbox` is never passed on this box.**
  The computed value is discarded.
- Line 5072 launches codex with `--dangerously-bypass-approvals-and-sandbox`, documented by
  codex-cli as *"EXTREMELY DANGEROUS. Intended solely for running in environments that are
  externally sandboxed."* Spindle provides no external sandbox for non-shard spools.
- 1218 occurrences of "lacks Landlock support" in `~/.spindle/spindle-stdout.log`.

**And fixing the flag would not help.** Tested directly against codex 0.144.4 on this box:

    codex exec --sandbox read-only "write hello to /home/patrick/probe.txt"
    -> exit 0. File written. No warning. No error.

Codex's `--sandbox` is **inert and silently fails open** without landlock. Therefore
finding-20260716-9xpk's fix options 3 and 4 ("pass `--sandbox` correctly", "add a `careful`
branch") are dead: they would pass a correct flag, record a truthful-looking value, and
enforce nothing. That is the current lie with better paperwork.

Note the installed codex is **0.144.4**, not the 0.125.0 cited in the findings and in two
source comments (line 353, line 5072 vicinity). Those comments are stale.

### 1.2 The record lies about what happened

- Line 5126 stores `"sandbox": sandbox or "workspace-write"` — a value that was never
  applied to anything.
- **Codex spool records contain no `permission` field at all: 0 of 67.** So the record
  neither states what was requested nor what was enforced.
- Live artifact: spool `adedeadd` (claude, `readonly`) and `codex-9d9dab67` (codex,
  `readonly`) were spun 13 seconds apart, same round, same prompt — a read-only plan fell,
  two genotypes. Claude got a real boundary (Read/Grep/cat/git, no python). Codex got
  unrestricted shell. **Codex's record says `sandbox: read-only`.**

### 1.3 The Claude allowlist is a syntax filter, not a boundary

- The matcher lives in Claude Code, not spindle. Spindle only emits `--allowedTools`
  (line 2317) and `--permission-mode acceptEdits` (line 2310). Spindle cannot strip,
  rewrite, or normalize a command before matching. Any fix of the form "normalize the
  command first" has nowhere to live.
- `careful` permits at least eight independent arbitrary-code paths: `python`, `python3`,
  `pip`, `uv`, `npm`, `npx`, `node`, `make`, plus `git` hooks/pager. **It contains nothing.**
- The source says so itself, twice, as justification for appending rules:
  *"Not a security widening: `careful` already allows arbitrary Python via Bash(python:*)"*
  (line 143-144, and again at line 162-165). The codebase has twice written down the reason
  the allowlist is theatre and twice used it as a license to append one more rule.
- All seven `careful` claude spools in the hoard M1 fell carry a **byte-identical** 41-rule
  allowlist, md5 `90ae289a`. (Verified.) **The profile did not vary. The phrasing did.**

### 1.4 It fails open, silently — and this is the third instance of the class

- No denial telemetry exists anywhere in spindle. Denials live only in the CC transcript.
- Prior scar tissue, each a patch that appended more rules: `PINNED_INTERPRETERS`
  (line 145-149, after absolute-path interpreters broke), `VENV_TOOLS` (line 166-179, after
  friction-20260709-vfx2 — *"a Claude-genotype reviewer that couldn't run the suite missed
  bugs the executing genotype caught"*).
- Appending `Bash(env:*)` would be patch three and would not prevent a fourth.

### 1.5 Respin drops the profile on both harnesses (defect 4 — not previously recorded)

- `_codex_respin_sync(session_id, prompt)` takes no permission argument and never calls
  `_codex_sandbox_for_permission`. It passes `--full-auto` or `--dangerously-bypass`
  regardless of the original tier. This is **kernel-independent** — a landlock upgrade
  would not fix it.
- The claude respin path builds `["claude","-p",prompt,"--resume",session_id,
  "--output-format","json"]` with **no `--permission-mode` and no `--allowedTools`**.
  Tested against the live CLI: that exact shape **denies** `python3 -c 'print(42)'`, which
  `careful` permits.
- So codex respin silently escalates and claude respin silently restricts.
- Blast radius today is small and stated honestly: **1 of 120 spools is respin-shaped.**
  This is latent, not burning. It is in scope because it is the same class and the fix is
  free under the new design.

### 1.6 A claim we could NOT verify, and are not building on

finding-20260716-9xpk states that b626611d (opus) and codex-92e27a84 ran the "same nominal
profile, same round." **This is not verifiable from the record**, because codex spools store
no `permission` field. It may be true from the caller's memory. The `adedeadd`/`9d9dab67`
pair above demonstrates the same point and *is* verifiable, so this plan cites that instead.
finding-20260716-9xpk should be corrected separately.

### 1.7 Two real bugs in `_has_landlock_support()`, found during this design

Both surfaced by the sealed-tier experiments in section 3, independently, by two genotypes:

- It returns True on kernel >= 5.13 **without checking `platform.system() == "Linux"`** and
  without probing actual landlock availability. A kernel with landlock compiled out, or not
  enabled in the boot LSM list, returns True.
- The securityfs check is **unreachable**: the version guess returns first, so
  `os.path.exists("/sys/kernel/security/landlock")` is dead code on every kernel >= 5.13 —
  the only kernels where it matters. `docs/MULTI_HARNESS_GUIDE.md:402` and
  `docs/CODEX_SETUP.md:388` document the *intended* algorithm (version **and** securityfs),
  which the code does not implement.
- 8 test references to this function all mock it. Nothing exercises the real one.

This matters for the plan: it means the "upgrade to a 5.15 HWE kernel and let codex sandbox
itself" path is a trap. `_has_landlock_support()` would return True on the version check
alone, spindle would pass `--sandbox`, record it as applied, and codex would silently ignore
it if landlock is not enabled in the boot LSM list. **That is strictly worse than today**,
where the bypass flag is at least honest. The function is deleted from the decision path
rather than fixed.

---

## 2. Why the levels are wrong, not just the enforcement

The current ladder conflates two independent axes — what an agent may **execute** and what
it may **write** — and restricts exec (by command syntax) as a proxy for restricting write.
It fails at both ends: `python` defeats the write containment, `PYTHONPATH=` defeats the
exec ergonomics.

Under the operative threat model — **an agent doing something destructive by accident, not
an adversary escaping** — write containment is the whole job, and exec restriction buys
nothing that write containment does not already buy. It only costs reviewer capability.

### The false congruency

The design names a tier and assumes it means one thing on every harness. It cannot: on
claude the tier compiles to a syntax filter; on codex it compiles to a no-op. Worse, the
direction is **inverted** — `readonly`, the strictest-sounding name, yields the narrowest
capability on claude and the widest possible on codex (`adedeadd` vs `9d9dab67`, 13 seconds
apart).

---

## 3. Evidence that the proposed design works

Both experiments run on this box, against the real CLIs, before writing this plan.

### 3.1 Codex under sealed bwrap, NOT told about the boundary (the worst case)

    17 commands, all exit 0
    pytest: 351 passed in 22.27s
    read-only encounters: 1 (.pytest_cache)
    sandbox complaints / flailing / hallucinated workarounds: 0

Its own words: *"There was one warning: pytest could not write .pytest_cache because that
path is read-only."* It reported the boundary accurately, unprompted, and delivered a real
finding (section 1.7).

### 3.2 Claude under sealed bwrap + `bypassPermissions` + preamble

    10 commands
    harness denials: 0
    pytest: 351 passed
    EROFS encounters: 1

Commands it ran freely that are **denied or coin-flips under `careful`**:

    SPINDLE_HOME=$(mktemp -d /tmp/spindle-probe-XXXX) python3 probe.py   (env-var prefix)
    cd /tmp && cat > probe.py <<'EOF' ...                                (cd-compound + heredoc)
    .venv/bin/python --version                                            (venv path)

It also self-applied the preamble's guidance, running pytest with `-p no:cacheprovider` —
so **telling the agent solves the cache papercut for free**, with no tmpfs bind required.

### 3.3 The mechanism this reveals

`EROFS: read-only file system` is a **self-describing** error: the agent knows exactly what
happened and adapts honestly. `"This Bash command requires approval"` is not: it does not
tell the agent what *would* be permitted, so the agent guesses — and guessing is the origin
of both phrasing-roulette and silent degradation.

**An OS boundary produces honest, capable agents. A syntax filter produces hobbled, quiet
ones.** That is the whole argument, and it is now empirical rather than theoretical.

### 3.4 bwrap enforcement, verified

    bwrap 0.4.0, /usr/bin/bwrap, not setuid, rides unprivileged_userns_clone=1
    write to $HOME under --ro-bind /     -> BLOCKED ("Read-only file system")
    write to bound worktree             -> OK
    write to /etc under --ro-bind /     -> BLOCKED
    --unshare-net                       -> network isolated

bwrap is **already used by spindle for shards on all four harnesses** and both codex shard
spools ever run completed clean with zero read-only complaints (n=2 — weak evidence, stated
as weak).

---

## 4. The design

### 4.1 The primitive is the write set. Levels are presets over it.

`readonly`, `careful`, `shard`, `research` were modeled as unrelated tiers with unrelated
enforcement. They are one mechanism with a different bind list.

**`sealed`** — writable: `/tmp` + harness state. Everything else read-only.
Exec unrestricted; nothing approval-gated. Read anything, run anything, mutate nothing.
Fits ~90% of codex spools and ~85% of today's `careful` claude spools.

**`shard`** — sealed + worktree + git refs (worktree gitdir, objects, refs/heads,
logs/refs/heads). Exec unrestricted. Exists today, works, semantics unchanged.

**`open`** — no bwrap. Honest about being unrestricted. Setup, provisioning, live-tree work.

**Parameter, not a level: extra writable paths.** `research` collapses to
`sealed --write <target>`. That is all `research_target` ever was.

**Not an axis: network.** The agent needs the model API. `--unshare-net` is unusable for
agent spools. Verified; door closed.

Harness state written into every write set (from the existing wrap, which already learned
this): `~/.claude`, `~/.claude.json`, `~/.anthropic`, `~/.codex`, `~/.gemini`, `~/.spindle`,
`~/.config`, `~/.cache`.

### 4.2 Per-harness compilation

**Claude family** — claude, kimi, mimo, grok, glm, deepseek. All four lodged profiles hit
`/anthropic` base_urls, i.e. **they are Claude Code with a different endpoint**, so one
implementation covers all six:

    bwrap(write_set) + claude --permission-mode bypassPermissions
    NO --allowedTools

**Codex:**

    bwrap(write_set) + codex exec --dangerously-bypass-approvals-and-sandbox
    NO --sandbox, NO landlock branch

This uses that flag **as documented for the first time**: *"intended solely for running in
environments that are externally sandboxed."* bwrap is that external sandbox. Today spindle
passes it with no external sandbox, which is precisely the misuse the doc warns against.

**Gemini:** same shape; already routes through the shared wrap. **Untested by me — must be
verified before trusting, not assumed.**

Same pattern both sides: strip the harness's fake boundary, wrap it in a real one. The
congruency becomes *true* because one mechanism enforces it, instead of two unrelated ones
sharing a name.

### 4.3 Tell the agent its write set

A preamble stating the writable paths, that everything else fails with EROFS, that this is
expected, that tool-cache warnings are harmless, and that nothing is approval-gated.
Verified in 3.2 to work and to pay for itself.

### 4.4 Two rules that must ship with it, or we rebuild the same bug

1. **bwrap missing = refuse to launch** for `sealed`/`shard`. Today `_codex_bwrap_wrap`
   prints a warning and returns the command **unwrapped** — the boundary silently
   evaporates. That is the same fail-open in a new costume. `open` still runs without bwrap.
2. **The record stores the truth**: `tier`, resolved `write_set`, `boundary` ("bwrap" or
   "none"), and the actual argv. The `sandbox` field is **deleted**, not corrected.

---

## 5. What this dissolves rather than patches

Leaving the decision path entirely: `--allowedTools` as security, `PERMISSION_PROFILES`,
`PINNED_INTERPRETERS`, `VENV_TOOLS`, `_has_landlock_support()`,
`_codex_sandbox_for_permission()`. That is the whole three-round scar-tissue lineage,
deleted rather than extended to a fourth round.

Against the original defect list:

- Defect 1 (codex bypass): fixed properly — bwrap, the only verifiable boundary.
- Defect 2 (phrasing roulette): **dissolved.** No syntax filter remains to lose against.
- Defect 3 (silent denial telemetry): **dissolved.** Under `bypassPermissions` there are no
  denials to report. The failure mode becomes EROFS, which is in-band and which both
  genotypes demonstrably self-report. The investigator's top recommendation was to build
  denial telemetry; this design makes it unnecessary instead.
- Defect 4 (respin): trivial — respin re-derives the wrap from the recorded tier.

---

## 6. Implementation steps

Ordered. Each step keeps the suite green (351 tests today).

1. `_bwrap_wrap(cmd, write_set, cwd, *, required)` — ONE implementation. Replaces
   `_codex_bwrap_wrap` (line 4879) **and** the duplicate inline copy in the claude path
   (line 2325). Base: `--ro-bind / /`, `--bind /tmp /tmp`, `--dev /dev`, `--proc /proc`,
   `--chdir cwd`, `--die-with-parent`, plus harness-state binds, plus `write_set`.
   If bwrap absent and `required` -> raise (fail loud).
2. `_write_set_for_tier(tier, shard_info, extra_writes)` -> list of writable paths.
3. `_preamble_for_write_set(write_set)` -> the prose from 4.3.
4. Rewire claude spin: `bwrap + --permission-mode bypassPermissions`; drop `--allowedTools`.
5. Rewire codex spin: `bwrap + --dangerously-bypass`; delete the landlock branch and
   `--sandbox` entirely.
6. Rewire gemini + kimi to the shared wrap. **Verify gemini empirically.**
7. Rewire both respin paths: read `tier` + `write_set` from the original spool, re-derive.
8. Record: add `tier`, `write_set`, `boundary`, `argv`. Delete `sandbox`.
9. Name mapping (back-compat): `readonly`->`sealed`, `careful`->`sealed`,
   `research`->`sealed`+target, `shard`/`careful+shard`->`shard`, `full`->`open`.
   Old names accepted and mapped, with a deprecation note in the record.
10. Delete the dead machinery from section 5.
11. Tests (section 7).

## 7. Test plan

- bwrap enforcement: a write outside the write set raises EROFS (real bwrap, not mocked).
- bwrap absent + `sealed`/`shard` -> refuses to launch. `open` -> runs.
- `_write_set_for_tier` mapping for every tier, incl. `research_target`.
- respin carries the tier: a `sealed` spool respun stays `sealed`.
- record has `tier`/`write_set`/`boundary`, and **no** `sandbox` field.
- Old names map correctly.
- Live smoke, both harnesses, sealed: spool runs the full suite and returns a real review.
  (Both already demonstrated by hand in section 3; this makes them regression tests.)
- **The existing 351 tests stay green.** Note they currently mock `_has_landlock_support()`
  in 8 places; those mocks come out with the function.

## 8. Risks, honestly

1. **`careful` callers that write outside a worktree now get EROFS.** This is the real
   migration risk. It fails loudly rather than silently, and CLAUDE.md already says "prefer
   shards for code changes" — but it is a behavior change and **the population has not been
   measured.** Count it before flipping, not after. If it is non-trivial, map `careful` to
   `shard` rather than `sealed` for callers that write.
2. **Harness state is writable** (`~/.claude`, `~/.config`, `~/.cache`). A determined agent
   can walk out through its own settings or a git hook. Accepted under the accident threat
   model; **`sealed` is not an adversary boundary and must not be described as one.**
3. **bwrap absent on another box** -> sealed/shard refuse to launch. Intentional, but it
   converts a silent degradation into a hard failure. That is the point, and it will be felt.
4. **Gemini is unverified.** Do not ship it on inference.
5. **Callers outside spindle** (mill chains, horizon, shuttle, CLAUDE.md docs) pass
   `permission=` strings. The name mapping covers them, but the docs need updating and
   CLAUDE.md's permission-profile guidance becomes wrong the moment this lands.
6. n=2 on codex shard spools; n=1 on each sealed experiment. The experiments are strong
   signal, not proof.

## 9. Explicitly out of scope

- The fell doctrine ("both lineages agree") — deferred by Patrick, tracked separately.
- Correcting finding-20260716-9xpk's unverifiable "same nominal profile" claim.
- hoard. The M1 fell is finished and merged; it is evidence here, not a target.
- Kernel upgrade / landlock (section 1.7 explains why it is a trap, not a fix).
