# Security policy

## Supported versions

Security fixes are made on the current `main` branch. The supported prerelease
line is `v1.1.0-rc.1`. Compatibility is limited to the versions and platforms
listed in [docs/compatibility.md](docs/compatibility.md).

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
does not provide credential isolation, and normal Codex session data may contain
task text or output.

The POSIX installer resets `PATH` to the platform's system utility path before
running filesystem tools. Before strict installation, it validates the lexical
and resolved ancestry of every runtime-path directory and the selected Python
3.11-or-newer interpreter. Strict enable, rollback, and the generated POSIX
uninstall command invoke Python in isolated mode (`-I`) with a minimal
environment containing `HOME`, `PATH`, `CODEX_HOME`, and an explicit
`CODEX_TIER_CODEX_BIN` when supplied. The dispatcher then requires the
configured or discovered Codex executable to use an absolute path whose
lexical and resolved components are trusted for the current platform and not
unsafely writable.

Installer fault and command seams are disabled by a literal in the shipped
script. Tests activate them only after rewriting a temporary installer copy;
environment variables alone cannot enable them in the published artifact.

On POSIX, the installed dispatcher ships as a `/bin/sh` launcher plus a
non-executable Python source file. The launcher clears dynamic-loader and Python
startup variables inherited from the caller, then resolves and validates Python
3.11 or newer before executing it in isolated mode. On Windows, the `.cmd`
entrypoint invokes the fixed system Windows PowerShell with profiles and
interactive input disabled; the PowerShell launcher clears the same startup
variables, validates itself and Python, and invokes the Python source with
`-I`. The Python runtime independently validates the complete launcher,
dispatcher-source, interpreter, Codex-entrypoint, and Codex-runtime chains.
Script entrypoints are invoked through the validated runtime instead of letting
the operating system resolve them again. Passing receipts bind those paths and
hashes, and re-hash them after the child returns.

Codex subprocesses receive an explicit environment containing the validated
user home and `CODEX_HOME` plus a narrow platform allowlist that includes
`PATH`. Arbitrary caller variables, dynamic-loader variables, Python startup
hooks, and environment-only API credentials are not forwarded. Codex can still
use its normal saved login state.

The installer and dispatcher validate ownership, links, write permissions, and
platform access controls on protected paths. POSIX checks include symlinks,
mode bits, and macOS ACLs. Windows checks use SID ownership and DACL inspection
and reject symlinks, junctions, and other reparse points. Local state is kept
under `CODEX_HOME` in an owner-only directory. The receipt ledger and
strict-mode records are owner-only files. Codex rollout files remain managed by
Codex; the dispatcher validates their complete path before use but does not
change their permissions.

An optional custom policy is accepted only at the fixed owner-only
`tier-routing/policy.toml` path. It may configure model and effort for exactly
five stable routes; the experimental `surgeon` tuple is fixed. It cannot
configure sandbox, network, approval, tools, commands,
environment, or concurrency. The parser bounds file size and identifier syntax,
rejects unknown fields and unsafe paths, and rechecks the source after the child
returns. Receipts embed the effective policy and bind its canonical and input
hashes. For a custom policy, the input hash covers the exact TOML bytes; for the
builtin policy, it covers the canonical default selection. Policy validation does not enumerate or attest account model
access; operators must use a bounded canary.

Workspace-write routes can modify the selected workspace before a later receipt
failure is detected. Write routes are mutually exclusive per resolved workdir;
concurrent writes to the same workdir are rejected even across different Codex
homes used by the same operating-system identity. POSIX uses `flock`; Windows
uses `LockFileEx` in a private per-SID namespace. The experimental `surgeon`
route additionally enforces a clean linked Git worktree root and valid starting
HEAD. Its preflight rejects submodules and any Git `filter` attributes rather
than risk invoking repository-configured helper processes outside the Codex
sandbox. Operators must provide explicit completion criteria and parent review
before integration. Inspect effects after ambiguous outcomes, and do not retry
until state has been reconciled.

The dispatcher forces workspace network access off and additional writable
roots empty through command-line configuration, then verifies that the
host-written sandbox policy reflects those constraints. This local shell
boundary does not constrain external MCP, plugin, connector, or host tools.

Both installers provide transactional rollback for handled failures. The
Windows installer also restores the prior user `PATH` when its transaction
fails; successful Windows installation does not generate an uninstall command.
Neither installer claims crash or power-loss atomicity or defends against
another malicious process running as the same operating-system user.

`read-only` and `workspace-write` are local shell/filesystem sandbox claims.
They do not prove that configured MCP servers, plugins, connectors, web search,
or other external tools are side-effect-free. A host-provided collaboration tool
outside the selected Codex home's native feature configuration is also outside
the dispatcher's enforcement boundary.

The local hash-chained ledger provides self-consistency checks, not durable
tamper-proofing. A writer who can replace the ledger can truncate, delete, or
re-chain it. Use an independently protected checkpoint when that threat matters.

The dispatcher kills and reaps the entire POSIX child process group or
terminates the Windows Job Object on bounded-run failures. Native Windows
support is limited to interactive console and RDP sessions. Codex CLI 0.144.4
cannot start sandboxed tool processes under OpenSSH/Session 0; that surface is
outside this release's supported boundary.

The complete assumptions and exclusions are documented in
[docs/threat-model.md](docs/threat-model.md).
