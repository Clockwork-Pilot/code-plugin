# claude-plugin

A Claude Code plugin, written in Python, that wires every hook event (SessionStart,
PreToolUse/PostToolUse for Bash/Edit/Write/Agent, Stop, SubagentStart/
Stop, Notification, PreCompact, SessionEnd, TeammateIdle) to handler scripts under
`hooks/`. The handlers themselves are generic — they read/write a shared ledger
(`src/ledger/`), track file changes, gate sub-agent spawning, etc. — and reach any
project-specific logic only by shelling out to commands a *consuming project* configures
in `cw-plugin-cli-hooks.json`. Nothing in this repo imports a consuming project's code
directly; `cli_stubs/*` are runnable reference implementations of each event's contract,
to copy from when you wire in your own.

## Quick start
``` bash
claude --dangerously-skip-permissions --plugin-dir=<path>/plugin
```

cwpilot itself is installed separately with `install.sh` (see
[Resolving binaries](#resolving-binaries)); `CWPILOT_BIN` is not consulted.

## Python fallback

When no binary available it uses python fallback for hooks, it may require setting env:
`CWPILOT_PYTHON=<path/to/python>`


## How it's invoked

`hooks/hooks.json` is the plugin's hook manifest. Every entry calls the one entrypoint:

```
${CLAUDE_PLUGIN_ROOT}/dist/run-hook.sh <handler_name>
```

`dist/run-hook.sh` is a plain shell shim — the only shim in the plugin. It runs the
hookrunner binary `install.sh` placed for this plugin's version, and otherwise falls back
to running the same handlers from source. It never downloads anything (see
[Resolving binaries](#resolving-binaries)).

1. `<versions dir>/hookrunner-v<plugin version>/hookrunner` — a precompiled `hookrunner`
   built by the separate Nuitka flow (see
   [Building the distributable binary](#building-the-distributable-binary)).
2. Otherwise `python3 -m hooks <handler_name>` from the plugin root, which routes
   into `hooks/<handler_name>.py` via `hooks/__main__.py`.

This means a plain dev checkout (no binary built) needs nothing but
`python3` and `requirements.txt` installed; an end-user install can ship a compiled
binary so it needs no system Python at all. Currently supported target platforms:
**Linux (x86_64)** and **macOS** (once you build a binary there — see below). Windows is
not supported yet.

## Configuring

### Project-specific behavior: `cw-plugin-cli-hooks.json`

The one file a consuming project actually edits. It maps each hook event name to the
command this plugin shells out to when that event fires:

```json
{
  "hook_cli_calls": {
    "call_on_stop_hook": {
      "timeout": 120,
      "cmd": ["/opt/mytool/bin/mytool", "hooks", "stop", "--json"]
    }
  }
}
```

- Format is **JSON only** (no YAML — kept dependency-free so the compiled binary needs
  no third-party parser).
- Each event maps to an object with `cmd` (the argv list) and an optional `timeout` in
  seconds. Omit `timeout` and the call uses `CLI_CALL_TIMEOUT_SECONDS`, which is sized
  for a heartbeat stamp: abandoning one of those is right, because a late stamp is
  worthless. A call that is a **decision** rather than a stamp should say so — abandoning
  `call_on_stop_hook` does not skip a stale write, it allows the stop it was meant to
  gate.
- A bare argv list (the pre-release shape) is **not** accepted: a file still written that
  way leaves every event unwired, rather than half-working with its timeouts ignored.
- An event omitted from the file is left unwired — the file is the whole
  configuration, not an overlay.
- A sibling top-level **`env`** block adds variables to the environment of every
  configured call. It is a *merge* over the hook process's own environment, so `PATH`,
  `HOME` and the harness's `CLAUDE_*` variables still reach the CLI:

  ```json
  {
    "env": {
      "CWPILOT_PROJECT_ROOT": "{project_root}",
    },
    "hook_cli_calls": { }
  }
  ```

  **Ship this block.** Passing the roots explicitly is what keeps a CLI off the
  fallback path described in [Root resolution](#root-resolution-and-why-this-plugin-stores-nothing):
  a CLI given no root may use a project-defined fallback with no provenance. A root read
  from such a source points at the graph document, and the graph document supplies text
  injected into the agent's context — so an unset variable is not just a missing
  convenience, it hands the answer to an untrusted source.
- Only **load-time** placeholders belong in `env` — `{plugin_root}` / `{project_root}`,
  whose values always exist. `{run_id}` / `{agent_id}` must stay in `cmd`: they can be
  absent, and `_fill` answers an absent one by abandoning the whole call rather than
  recording a fact against a run literally named `{run_id}`. That check reads `cmd`
  only, so a call-time placeholder hidden in `env` would slip past it.
- `{plugin_root}` / `{project_root}` are substituted at *load* time so a shipped file
  stays relocatable across machines; `{run_id}` / `{agent_id}` are substituted at *call*
  time by `src/hook_cli_calls.py`.
- The location is fixed at `cw-plugin-cli-hooks.json` in the plugin checkout
  (`HOOK_CLI_CALLS_FILE` in `src/config.py`) — there is no environment override; edit
  that file to point the events at your project's CLI. The shipped file wires every
  event to `{project_root}/bin/*`, i.e. the consuming project's own binaries.
- Full event list, stdout/exit-code conventions per event, and per-event notes are
  documented in the comment block above `HOOK_CLI_EVENTS` in `src/config.py`.

See [`cli_stubs/README.md`](cli_stubs/README.md): each script there is a runnable no-op
that documents one event's argv/stdout contract in its header, and
`cli_stubs/hook_cli_calls.json` is an example wiring that points every event at those
stubs — copy it over `cw-plugin-cli-hooks.json` for a checkout that runs end to end
before any real CLI exists.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_PLUGIN_ROOT` | none — required | Root of this plugin checkout, set by Claude Code in every hook subprocess. Read directly by `dist/run-hook.sh`, `scripts/*-binary.sh`, and `src/config.py`. There is no `__file__` fallback (it resolves incorrectly inside a frozen binary and for a relocated install), so an unset value is an error, not a guess. |
| `CLAUDE_PLUGIN_DATA` / `PLUGIN_DATA` | none — required | Directory for plugin-owned data. `CLAUDE_PLUGIN_DATA` takes precedence. If neither variable is set, configuration fails rather than silently placing data under the project or plugin root. |
| `CWPILOT_PLUGIN_LOG_FILE` | `~/.claude/cache/cwpilot-plugin-hooks.log` | Where hook handlers log. |
| `CHANGES_TRACKER_DIR` | `<project root>/.changes-tracker` | Where the file-change tracker persists state. `<project root>` is `config.PROJECT_ROOT`, resolved as below — not an environment variable of that name. |
| `CLAUDE_PROJECT_DIR` | falls back to the plugin checkout | Project root, set by Claude Code in every hook subprocess and read by `src/config.py`. |
| `CWPILOT_PROJECT_ROOT` | set by the `env` block of `cw-plugin-cli-hooks.json` | The project root handed to configured CLI calls. |

### Root resolution, and why this plugin stores nothing

Every Python module in this repo runs inside a **hook subprocess**, and Claude Code sets
`CLAUDE_PROJECT_DIR` / `CLAUDE_PLUGIN_ROOT` in each one. So `src/config.py` reads both
straight from the environment and consults nothing else. The plugin persists no session
state of its own.

The plugin hands both roots to `call_on_session_start` as arguments instead:

```json
"cmd": ["{plugin_root}/bin/cwpilot", "hook", "session-start",
        "--doc", "{project_root}/executable_graph.k.json",
        "--project-root", "{project_root}", "--plugin-root", "{plugin_root}"]
```

Two writers of one file disagree sooner or later, and the layout belongs to whoever
reads it.

#### Notes for a consuming project

The arguments — not the inherited environment — are what mark a call as
**hook-originated**. Both are true of that invocation and of no other: it was started by
a hook, and its roots came from the harness. A CLI run the user or the model starts
directly inherits neither. So:

- **Prefer better-attested sources when reading.** An explicit `--doc`, then
  `CWPILOT_PROJECT_ROOT` declared in the project's own `.claude/settings.json` `env`
  block, then an upward search from `cwd` for the document. The name is namespaced on
  purpose: a bare `PROJECT_ROOT` is common in Makefiles, `direnv`, and CI images, and a
  stray one would outrank everything below it by accident.
- **Validate whichever source wins**: the resolved root must actually contain the
  document. That turns a wrong answer into a visible error instead of a silent redirect.

## Building the distributable binary

Only needed to ship a plugin install that doesn't require a system `python3`. A dev
checkout works without ever doing this (see [How it's invoked](#how-its-invoked)).

### Prerequisites

- Docker
- `requirements.txt` (runtime deps) and
  `requirements-build.txt` (adds `nuitka`) are already checked in — nothing to
  install locally, the build happens inside the container.

### Linux (x86_64)

```sh
./scripts/build-linux.sh
```

This builds `docker/Dockerfile.build-linux` (manylinux_2_28 base — deliberately an old glibc,
since glibc is forward-compatible but not backward-compatible: building on a
bleeding-edge base would produce a binary that refuses to run on an older distro), runs
Nuitka inside the container against `hooks/__main__.py` (the single dispatch
entrypoint that `runpy`-invokes any `hooks/handler_*.py` by name), and copies the result to
`bin/linux-x86_64/hookrunner` — the one cache location the resolver looks in.

Docker itself is platform-agnostic — what varies is the *architecture* of the machine
running the Docker daemon (an x86_64 host builds an x86_64 binary; you'd need an arm64
host, or emulation, for arm64). It does **not** help for macOS: there's no macOS Docker
image, and Nuitka does not cross-compile, so a macOS binary can only be built by
running the equivalent Nuitka command on an actual Mac.

### macOS

`scripts/build-darwin.sh` builds natively (no Docker — Nuitka can't cross-compile, so
this can only run on an actual Mac) and copies the result to `bin/darwin-<arch>/hookrunner`
(`arm64` or `x86_64`, matching `uname -m` — note `aarch64` is normalised to `arm64`).
`.github/workflows/build-hookrunner.yml` runs it in CI on a `macos-14` runner, alongside
the Linux leg, and publishes both under one GitHub release.

### Verifying a build

```sh
echo '{}' | CLAUDE_PLUGIN_ROOT="$PWD" \
  ./dist/run-hook.sh handler_stop
```

Should exit 0. It prints the `call_on_stop_hook` command's JSON response if that event
is wired to something this machine has; with the shipped wiring (the consuming project's
`bin/`, absent here) it prints nothing and still exits 0 — every hook fails soft.

Compiled binaries under `bin/*/` are gitignored (`dist/run-hook.sh` itself is tracked)
— build them as a release step, not something you commit to source control.

## Resolving binaries

The plugin never downloads or installs a binary. **`install.sh`** (served at
`install.clockwork-pilot.com`, source in the cwpilot-release repo) is the only thing that
puts one on disk; the plugin only looks. Both binaries live in one shared directory,
`$CWPILOT_VERSIONS_DIR` (default `~/.local/share/cwpilot-versions`):

```
v<X.Y.Z>/cwpilot                    + cwpilot.sig        cwpilot, from cwpilot-release
hookrunner-v<X.Y.Z>/hookrunner      + hookrunner.sig     the plugin's hookrunner, from code-plugin
signing.pub                                              the key both signatures verify with
```

`install.sh` verifies each download's detached RSA-SHA256 signature (`openssl`) against the
public key embedded in the script **before** anything lands in that directory, then stores
the `.sig` and the key beside the binary.

| Binary | Install | Where the plugin looks |
|---|---|---|
| both (what SessionStart recommends) | `curl -fsSL install.clockwork-pilot.com \| sh -s -- <plugin version>` | cwpilot: newest `v<major>.<minor>.*` of **this plugin's own** major.minor; hookrunner: `hookrunner-v<plugin version>`, exactly |
| cwpilot only | `curl -fsSL install.clockwork-pilot.com \| sh -s -- cwpilot [<major.minor>]` | newest patch of that family (an exact patch is rejected) |
| hookrunner only | `curl -fsSL install.clockwork-pilot.com \| sh -s -- hookrunner <plugin version>` | as above |

hookrunner is optional: it is released under the plugin's own version, and without it the
hooks run from Python source (see above). SessionStart tells the user the one install
command; running a remote script is theirs to approve, never done silently from a hook.
cwpilot is a prerequisite and has no source fallback.

`src/binary_resolver.py` does the lookup (`find_cwpilot()`, `find_hookrunner()`) and
`dist/run-hook.sh` repeats the hookrunner half in shell, because it runs before any
Python is known to exist. **`$PATH` and `$CWPILOT_BIN` / `$HOOKRUNNER_BIN` are never
consulted**: hookrunner runs on every hook with nobody typing a command, and cwpilot is
what the PreToolUse guard (`src/cwpilot_guard.py`) lets the agent run, so neither may be
nameable from the environment.

### Integrity

`binary_resolver.verify_signature(path)` re-checks an installed binary against the `.sig`
and `signing.pub` stored beside it. SessionStart does this for cwpilot and the PreToolUse
guard re-checks it (once per file change). A mismatch **or a missing `.sig`/key** counts as
a failure — an install with no signature is unverified, so deleting the file is not a way
to switch the check off. Only a check that cannot run at all (no `openssl`) passes
unverified. A cwpilot installed before signatures were kept is repaired by re-running its
install command: `install.sh` fetches the release's `.sig`, verifies the cached binary
against it and stores it, without re-downloading the binary. The key lives next to the
binary, so this catches a corrupt or replaced file, not an attacker able to rewrite that
whole directory — signing protects the release and install path, not the local disk.

### Releasing hookrunner

A release is two manual workflow runs, in order, with the same version:

```
gh workflow run publish-public.yml   -f version=1.0.0   # tag + source snapshot
gh workflow run build-hookrunner.yml -f version=1.0.0   # signed binaries
```

1. **`publish-public.yml`** (dispatch it from `main`) tags this repo `v<version>` at that
   commit, then adds **one commit per version** to the public `code-plugin` repo's `main`,
   carrying the same tag. `code-plugin` keeps no development history, only the released
   versions; nothing there is rebased or force-pushed. `git archive` builds the snapshot, so
   `.gitattributes` `export-ignore` decides what stays private. A re-run for a version that
   is already published does nothing.
2. **`build-hookrunner.yml`** builds and signs each platform, then uploads the binaries to
   the release on that public tag. It refuses to release without the tag from step 1.

The version must equal both plugin manifests' version (`plugin/.claude-plugin/plugin.json`
and `plugin/.codex-plugin/plugin.json`), so the plugin can find `hookrunner-v<its
version>`; both workflows check this before doing anything.
