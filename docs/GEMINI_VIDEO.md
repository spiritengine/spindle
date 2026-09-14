# Gemini video in Spindle

Research and implementation plan, 2026-09-13.

Astra was already present in commit 039c8eb: `harness="codex", model="astra"`
selects `gpt-6-astra`. Its discovery and command-construction tests pass.

## Findings

The installed Gemini CLI is 0.39.1. Its file reader recognizes video MIME types
and sends the file as an `inlineData` part. This works through Spindle's existing
headless command. A synthetic six-second MP4 containing purple, yellow, then
blue was correctly described in that order with timestamps by 3.8 Flash through
Spindle. The prompt contained only the attachment and a question, not the answers.
The updated `video` alias repeated that result using Gemini's saved credential;
the new default Pro model also returned the requested smoke-test marker.

Google lists 3.8 Flash as stable with video, audio, image, text, and PDF input.
3.1 Pro remains a preview. The former `gemini-3-pro-preview` target is shut down.
An API catalog entry does not guarantee availability under Google-account CLI
authentication; the live video probe used API-key authentication.

Sources:

- [Current model catalog](https://ai.google.dev/gemini-api/docs/models)
- [3.8 Flash capabilities](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)
- [CLI file reader source](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/utils/fileUtils.ts)
- [API video input](https://ai.google.dev/gemini-api/docs/video-understanding)
- [CLI trusted folders](https://geminicli.com/docs/cli/trusted-folders/)

## Plan and ownership

Keep the existing CLI transport and process owner. Refresh aliases and use one
constant for the advertised and actual Pro default. Add `video` as an alias for
3.8 Flash. Preserve explicit 2.5 choices and redirect `3-pro` to its successor.
Gemini CLI owns reading attachments, authenticating, and enforcing its file and
trust checks; Spindle owns selecting the model and collecting the result.

Verify model selection and attachment-prompt preservation in tests, then run a
live video call and a Pro call. Obtain one independent review of the complete
diff, limited to concrete introduced defects. This change adds no API client,
upload service, automatic frame extraction, or permission bypass. Spindle's
lodged profiles remain Claude-only.

## Use

```bash
spindle spin --harness gemini --model video \
  --working-dir /path/to/clips \
  'Watch @clip.mp4. Describe what changes, with timestamps.'
```

```python
spin(
    prompt="Watch @clip.mp4. Describe what changes, with timestamps.",
    harness="gemini",
    model="video",
    working_dir="/path/to/clips",
)
```

The `@` matters: it asks the CLI to attach the file before inference. Use a
simple filename within the working directory. The video alias is a model
shortcut, not a different permission profile or a separate harness.

Authenticate Gemini first. For API-key auth, Gemini can load `GEMINI_API_KEY`
from `~/.gemini/.env`; protect that file with mode 0600. This also works when
Spindle's service lacks the variable in its own environment. Never put a key in
the prompt. Google-account auth is also supported by the CLI, subject to model
access. Launch `gemini` interactively in the intended directory to establish
workspace trust before using Spindle headlessly.

## Limits

- The 0.39.1 CLI file reader rejects files larger than 20 MiB. The API's upload
  limits are different and do not enlarge this CLI limit. Trim or compress a
  clip before attaching it when necessary.
- A YouTube URL written in a prompt is not the same as an attached video. The
  API documents native YouTube inputs; this Spindle path has only been verified
  with local MP4 input. Do not claim a URL was watched from a textual answer alone.
- Native video input still has model-side sampling and resolution limits. It is
  not proof of frame-perfect inspection or of understanding every audio event.
- Spindle launches Gemini with its sandbox enabled. For an explicitly trusted
  temporary test directory, the 0.39.1 sandbox needs the trust setting both in
  `GEMINI_CLI_TRUST_WORKSPACE=true` and in
  `SANDBOX_ENV=GEMINI_CLI_TRUST_WORKSPACE=true`; the outer variable alone is lost
  at sandbox startup. Do not set those globally to bypass workspace trust.
