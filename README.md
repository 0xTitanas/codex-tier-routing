<div align="center">

# codex-tier-routing

**Fixed local routes for Codex tasks, with host-observed receipts and fail-closed checks.**

[![License: MIT](https://img.shields.io/badge/license-MIT-2ea44f.svg)](LICENSE)
[![Status: Experimental](https://img.shields.io/badge/status-experimental-f0ad4e.svg)](CHANGELOG.md)
[![Platforms: macOS + Linux](https://img.shields.io/badge/platforms-macOS%20%2B%20Linux-4c78ff.svg)](docs/compatibility.md)

</div>

![Codex Tier Routing: route by task, verify before trust](docs/assets/codex-tier-routing-header.png)

<p align="center">
Original project by <a href="https://github.com/0xTitanas">0xTitanas</a> ·
<a href="https://github.com/0xTitanas/codex-tier-routing">Canonical repository</a>
</p>

`codex-tier-routing` installs five Codex agent profiles and a local dispatcher.
The profiles make task intent explicit. The dispatcher adds the part a profile
cannot provide on its own: a host-observed receipt that records the model,
effort, sandbox, approval policy, working directory, and child session seen in
the Codex rollout.

The project is experimental. It does not prove provider-side model identity,
measure cost or quality improvements, or make delegated output trustworthy by
itself. The parent still has to inspect the result.

## Routes

| Route | Requested model | Effort | Sandbox | Typical use |
|---|---|---|---|---|
| `scout` | `gpt-5.6-luna` | low | read-only | Search, dependency surveys, documentation lookup |
| `grunt` | `gpt-5.6-luna` | low | workspace-write | Formatting, boilerplate, mechanical edits |
| `builder` | `gpt-5.6-terra` | medium | workspace-write | Scoped features, fixes, tests, and refactors |
| `reviewer` | `gpt-5.6-terra` | high | read-only | Correctness, security, and coverage review |
| `architect` | `gpt-5.6-sol` | xhigh | read-only | Difficult design decisions |

The local dispatcher uses this fixed table. It does not accept arbitrary model,
effort, sandbox, or approval overrides.

## How it works

```text
Task
  -> choose one fixed route
  -> run codex exec with fixed policy
  -> read the host-written rollout
  -> compare requested and observed fields
  -> append a private local receipt
  -> return output and receipt to the parent
```

The dispatcher refuses to run while native generic subagent spawning is enabled.
Strict mode disables that path in the selected Codex home, then verifies the
effective feature state before a routed child starts.

## Requirements

- Python 3.11 or newer
- An installed Codex CLI with `codex exec`
- Access to the configured model names
- macOS or Linux

See [compatibility](docs/compatibility.md) for the tested boundary.

## Check out the project safely

These commands inspect the repository and preview installation without changing
your Codex home:

```sh
python3 scripts/validate.py
python3 -m unittest discover -s tests -v
./install.sh --strict --dry-run
```

## Install

Review the installer and preview its changes first. Then install into your Codex
home:

```sh
./install.sh --strict
```

The installer copies:

- `agents/*.toml` to `$CODEX_HOME/agents/`
- `skills/model-routing/` to `$CODEX_HOME/skills/model-routing/`
- `bin/codex-tier` to `$CODEX_HOME/bin/codex-tier`

If `CODEX_HOME` is unset, the installer uses `$HOME/.codex`. Existing files are
moved into an owner-only timestamped backup tree before replacement. The
installer prints an exact uninstall command for the files and strict
configuration it manages.

To exercise the complete install path without touching your normal Codex home:

```sh
export CODEX_HOME="$(python3 -c 'import os,tempfile; print(os.path.realpath(tempfile.mkdtemp()))')"
./install.sh --strict
"$CODEX_HOME/bin/codex-tier" probe
```

The temporary directory remains available for inspection after the commands
finish.

## Dispatch a canary

Run a small read-only canary after installation and after material Codex CLI,
account, or model-access changes:

```text
printf '%s\n' 'Reply exactly ROUTE_CANARY_OK. Do not inspect files.' | \
  "$CODEX_HOME/bin/codex-tier" dispatch \
  --route scout --workdir "$PWD"
```

A passing receipt shows what the local Codex host recorded for that child. It
does not prove that the provider served a particular model or that the returned
answer is correct.

## Commands

```text
codex-tier probe
codex-tier strict-enable
codex-tier strict-disable
codex-tier dispatch --route ROUTE --workdir /absolute/project/path
```

`dispatch` reads the task from standard input. `--prompt-file` is also available
for an owner-controlled regular file. The command performs no automatic retry.

## Security boundary

The installer and dispatcher:

- reject symlinked, cross-owner, and unsafe writable Codex-home paths;
- resolve the Codex executable from an absolute, owner-checked path;
- run installer filesystem utilities through the platform's system utility path;
- validate lexical and resolved Python interpreter paths before strict installation;
- start strict-mode Python processes with a minimal environment;
- launch the installed dispatcher through a hardened `/bin/sh` wrapper that
  clears loader and Python startup variables before resolving the interpreter;
- check the complete path below the Codex home before reading rollout files;
- require owner-private state and ledger files;
- require `network_access: false` in host-observed workspace-write policy;
- launch each child with approval policy `never` and verify the observed
  value afterward;
- kill the child process group on timeout or stream failure;
- record a halt instead of accepting missing or mismatched observations.

It still runs the installed Codex CLI with the invoking environment. It is not a
credential-isolation boundary. A write-capable child may change its workspace
before a later receipt mismatch is detected. Use a clean worktree and inspect
state after every ambiguous outcome.

Read [SECURITY.md](SECURITY.md) and the
[local dispatcher guide](docs/local-dispatcher.md) before relying on strict mode.

## Limits

- Windows is not supported.
- Compatibility is bounded to the versions and platforms listed in
  [docs/compatibility.md](docs/compatibility.md).
- The ledger detects mutation and interior deletion, but it cannot prove that a
  valid suffix was not deleted without an external checkpoint.
- Strict mode governs sessions that use the same Codex home and configuration.
  Administrator-managed requirements are needed to prevent user or project
  overrides.
- No benchmark, cost reduction, reliability improvement, or provider-side
  attestation claim is made.

## Development

```sh
DEV_VENV="$(python3 -c 'import os,tempfile; print(os.path.realpath(tempfile.mkdtemp()))')/venv"
python3 -m venv "$DEV_VENV"
. "$DEV_VENV/bin/activate"
python -m pip install --upgrade pip
python -m pip install --disable-pip-version-check -r requirements-dev.txt
python scripts/validate.py
python -m unittest discover -s tests -v
sh -n install.sh
PYTHONPYCACHEPREFIX=/tmp/codex-tier-pycache python -m py_compile \
  bin/codex-tier scripts/validate.py tests/test_codex_tier.py tests/test_installer.py
ruff check .
ruff format --check scripts tests bin/codex-tier
```

## Repository map

- [`agents/`](agents/): five Codex agent profiles
- [`bin/codex-tier`](bin/codex-tier): local dispatcher
- [`skills/model-routing/SKILL.md`](skills/model-routing/SKILL.md): routing policy
- [`schemas/local-dispatch-receipt.schema.json`](schemas/local-dispatch-receipt.schema.json): receipt contract
- [`docs/local-dispatcher.md`](docs/local-dispatcher.md): detailed behavior and limits
- [`docs/compatibility.md`](docs/compatibility.md): tested compatibility boundary
- [`install.sh`](install.sh): transactional installer
- [`scripts/validate.py`](scripts/validate.py): repository validator
- [`tests/`](tests/): dispatcher and installer regression suite

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues belong in GitHub private
vulnerability reporting, not a public issue.

## Provenance

The original creator is [0xTitanas](https://github.com/0xTitanas). The canonical
source is
[`0xTitanas/codex-tier-routing`](https://github.com/0xTitanas/codex-tier-routing).
Reuse does not imply endorsement.

- License: [MIT](LICENSE)
- Notice: [NOTICE.md](NOTICE.md)
- Citation metadata: [CITATION.cff](CITATION.cff)
