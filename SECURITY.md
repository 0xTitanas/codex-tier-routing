# Security policy

## Supported versions

Security fixes are made on the current `main` branch. This project is
experimental and does not promise compatibility with unlisted Codex versions or
platforms.

## Reporting a vulnerability

Use GitHub private vulnerability reporting for security issues. Do not include
credentials, personal data, private paths, or working exploit details in a
public issue.

If private vulnerability reporting is unavailable, open a minimal public issue
asking the maintainer to establish a private channel. Do not include the
vulnerability details in that issue.

A useful report includes:

- affected commit or file;
- expected impact;
- reproduction conditions;
- whether the issue requires a write-capable route;
- any suggested mitigation.

Reports about Codex or provider behavior should go to the appropriate upstream
project unless this repository's files are the cause.

## Security boundary

The project installs local configuration and starts the installed Codex CLI. It
does not provide credential isolation. The child inherits the invoking process
environment, and normal Codex session data may contain task text or output.

The installer resets `PATH` to the platform's system utility path before running
filesystem tools. Before strict installation, it validates the lexical and
resolved ancestry of every runtime-path directory and the selected Python
3.11-or-newer interpreter. Strict enable, rollback, and the generated uninstall
command invoke Python in isolated mode (`-I`) with a minimal environment
containing `HOME`, `PATH`, `CODEX_HOME`, and an explicit
`CODEX_TIER_CODEX_BIN` when supplied. The
dispatcher then requires the configured or discovered
Codex executable to use an absolute path whose lexical and resolved components
are root- or current-owner and not unsafely writable.

Installer fault and command seams are disabled by a literal in the shipped
script. Tests activate them only after rewriting a temporary installer copy;
environment variables alone cannot enable them in the published artifact.

The installed dispatcher ships as a `/bin/sh` launcher that clears
dynamic-loader and Python startup variables inherited from the caller, then
resolves a system Python 3.11-or-newer interpreter through the platform's system
utility path and execs it in isolated mode. Codex subprocesses launched by the
dispatcher receive a minimal explicit environment rather than a copy of the
invoking environment, preventing propagation of loader or startup injection.

The installer and dispatcher validate ownership, symlinks, write permissions,
and macOS ACLs on protected paths. Local state is kept under `CODEX_HOME` in an
owner-only directory. The receipt ledger and strict-mode records are owner-only
files. Codex rollout files remain managed by Codex; the dispatcher validates
their complete path before use but does not change their permissions.

Workspace-write routes can modify the selected workspace before a later receipt
failure is detected. Use a clean worktree, inspect effects after ambiguous
outcomes, and do not retry until state has been reconciled.

The installer provides rollback for handled errors and signals. It does not
claim crash or power-loss atomicity and does not defend against another
malicious process running as the same operating-system user.
