# Example service files

Reference copies of what `spindle install-service` generates. Prefer the
command — it fills in the real executable path, port, spool store, and `PATH`
for your machine, and it will not overwrite a service file spindle did not
write:

```bash
spindle install-service                                  # name spindle, port 8002
spindle install-service --name spindle-b --port 8042     # a second install, alongside the first
spindle doctor                                           # confirm the service is this install
```

Install one of these by hand only if you want to hand-edit the result.

## Linux (systemd user unit)

```bash
cp examples/spindle.service ~/.config/systemd/user/spindle.service
# Edit ExecStart to the output of `which spindle`, and edit PATH (see below)
systemctl --user daemon-reload
systemctl --user enable spindle
systemctl --user start spindle
spindle doctor
```

## macOS (launchd agent)

```bash
cp examples/com.spindle.server.plist ~/Library/LaunchAgents/
# Replace YOURUSERNAME, set the spindle path to `which spindle`, edit PATH
plutil -lint ~/Library/LaunchAgents/com.spindle.server.plist
launchctl load ~/Library/LaunchAgents/com.spindle.server.plist
spindle doctor
```

## Two things to get right

**Keep the `managed-by: spindle install-service` marker.** It is how
`install-service` recognizes a file as its own. Strip it and the command will
refuse to replace the file later, on the assumption that someone else wrote it.

**`PATH` is the one that bites.** A systemd user unit starts with a minimal
`PATH`, and a launchd agent does not inherit your shell's at all — so neither
can find `claude`/`codex`/`gemini` unless you list the directories they live
in. When it is wrong, nothing fails at startup; it fails much later as a spool
that dies at spawn. `spindle doctor` compares the service's `PATH` against the
harnesses it can see and names any the service cannot reach. Re-check after
anything relocates a CLI — a node upgrade moving `codex` is the usual culprit.

## Profiles

`profiles/anthropic-compatible/` is a worked example of a lodged profile (an
alternate model endpoint driven through the Claude Code harness), with the full
schema reference. See the Profiles section of the main README.
