# Security policy

## Supported versions

Security fixes are made on the current `main` branch. The supported prerelease
line is `v1.0.0-rc.1`. Compatibility is limited to the versions and platforms
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

The installed dispatcher ships as a `/bin/sh` launcher plus a non-executable
Python source file. The launcher clears
dynamic-loader and Python startup variables inherited from the caller, then
resolves and validates Python 3.11 or newer before executing it in isolated
mode. The Python runtime independently validates the complete launcher,
dispatcher-source, interpreter, Codex-entrypoint, and Codex-shebang-runtime
chains. Script entrypoints are invoked through the validated runtime instead of
letting the kernel resolve the shebang again. Passing receipts bind all five
paths and hashes, and re-hash them after the child returns.

Codex subprocesses receive an explicit environment containing the validated
`HOME` and `CODEX_HOME` plus `PATH`, `TERM`, and locale values when present.
Arbitrary caller variables, dynamic-loader variables, Python startup hooks, and
environment-only API credentials are not forwarded. Codex can still use its
normal saved login state.

The installer and dispatcher validate ownership, symlinks, write permissions,
and macOS ACLs on protected paths. Local state is kept under `CODEX_HOME` in an
owner-only directory. The receipt ledger and strict-mode records are owner-only
files. Codex rollout files remain managed by Codex; the dispatcher validates
their complete path before use but does not change their permissions.

An optional custom policy is accepted only at the fixed owner-only
`tier-routing/policy.toml` path. It may configure model and effort for exactly
five routes; it cannot configure sandbox, network, approval, tools, commands,
environment, or concurrency. The parser bounds file size and identifier syntax,
rejects unknown fields and unsafe paths, and rechecks the source after the child
returns. Receipts embed the effective policy and bind its canonical and input
hashes. For a custom policy, the input hash covers the exact TOML bytes; for the
builtin policy, it covers the canonical default selection. Policy validation does not enumerate or attest account model
access; operators must use a bounded canary.

Workspace-write routes can modify the selected workspace before a later receipt
failure is detected. Use a clean worktree, inspect effects after ambiguous
outcomes, and do not retry until state has been reconciled.

The dispatcher forces workspace network access off and additional writable
roots empty through command-line configuration, then verifies that the
host-written sandbox policy reflects those constraints. This local shell
boundary does not constrain external MCP, plugin, connector, or host tools.

The installer provides rollback for handled errors and signals. It does not
claim crash or power-loss atomicity and does not defend against another
malicious process running as the same operating-system user.

`read-only` and `workspace-write` are local shell/filesystem sandbox claims.
They do not prove that configured MCP servers, plugins, connectors, web search,
or other external tools are side-effect-free. A host-provided collaboration tool
outside the selected Codex home's native feature configuration is also outside
the dispatcher's enforcement boundary.

The local hash-chained ledger provides self-consistency checks, not durable
tamper-proofing. A writer who can replace the ledger can truncate, delete, or
re-chain it. Use an independently protected checkpoint when that threat matters.

The complete assumptions and exclusions are documented in
[docs/threat-model.md](docs/threat-model.md).
