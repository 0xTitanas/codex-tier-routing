# Compatibility

This is the compatibility target for `v1.1.0-rc.1`. It becomes the tested
boundary only after the release gates below pass.

## Current tested boundary

| Component | Status |
|---|---|
| macOS on Apple silicon | Local installer, dispatcher, and live read-only route checks |
| Ubuntu Linux on x86-64 | 22.04 and 24.04 LTS on Python 3.11-3.14; 26.04 LTS on its system Python 3.14 |
| Other Linux distributions or architectures | Outside the release-gated compatibility boundary |
| Windows 11 | Native CI and fresh install plus manual dispatcher, negative-canary, and all-six-route live checks in an interactive console/RDP session; OpenSSH/Session 0 is excluded for Codex CLI 0.144.4 as described below |
| Python | 3.11-3.14 in CI; runtime requires 3.11 or newer |
| Codex CLI | 0.144.4 used for bounded POSIX checks and as a native `codex.exe` for Windows checks |

The prior recorded Codex checks covered a narrow local environment and small
tasks. Before the v1.1.0-rc.1 tag, the remapped Terra/low `scout`, remapped
Sol/high `reviewer`, and experimental Sol/high/workspace-write `surgeon` each
require a bounded live canary. Deterministic tests must also cover all six
routes and the per-workdir writer exclusion. These checks are compatibility
evidence, not a benchmark, broad fleet certification, or provider-side model
attestation.
The experimental `surgeon` preflight accepts SHA-1 and SHA-256 Git object IDs
but deliberately rejects submodules and any Git `filter` attributes.
For workspace-write sessions, 0.144.4 serializes an empty extra-writable-root
list by omitting `writable_roots` and serializes a nonempty list explicitly.
The dispatcher verifies that raw encoding and writes an explicit empty list to
its normalized receipt. An encoding change in a later CLI version HALTs until
the adapter is reviewed.

## What to recheck

Run `probe` and a small read-only canary after changing:

- Codex CLI version;
- account or model access;
- `CODEX_HOME`;
- operating system;
- named profile or project configuration;
- route model or effort policy.

A successful `policy-check` confirms policy syntax, exact fields, fixed
authority boundaries, and the effort values supported by this release. A
successful probe confirms local prerequisites and strict-mode state. Neither
enumerates account entitlements or guarantees that a later model call will
succeed; use one bounded canary per changed route.

## Platform notes

Windows uses native equivalents: SID-based owner/DACL validation, reparse-point
and junction rejection, `LockFileEx` writer locks, suspended-process Job Object
containment, and `MoveFileEx` replacement. Install with `install.ps1`; it performs
transactional rollback, timestamped collision backups, and a user `PATH` update.
Run `bin\\codex-tier.ps1` from a fresh interactive console or RDP session.
Write-capable routes preserve an explicit `[windows] sandbox = "elevated"`
setting and otherwise select Codex's documented `unelevated` fallback.
The tested Windows entrypoint is a native `codex.exe`. Windows `.cmd` and
`.bat` wrappers are outside this trust boundary and are rejected before
dispatch.

In a manual check on the tested Codex CLI 0.144.4 host, authenticated dispatch
and receipt verification succeeded for all six routes from an interactive
Windows session. The `surgeon` canary ran in a clean linked Git worktree under
the elevated sandbox; its starting HEAD and exact one-line diff were
independently verified. Dirty-worktree and active writer-lock checks halted
before unsafe execution.

Codex CLI 0.144.4 still cannot initialize sandboxed tool processes when launched
from native Windows OpenSSH/Session 0. In `unelevated` mode, model-invoked
helpers exit with Windows status `0xc0000142`. Elevated sandbox setup succeeds,
but its signed command runner exits with the same status before opening its
named pipes, so the parent times out. Direct `codex exec` and `codex sandbox`
reproductions outside this dispatcher establish the Codex CLI process-start
boundary.

This Windows SSH/Remote-SSH failure is tracked in upstream
[issue #17847](https://github.com/openai/codex/issues/17847) and
[issue #20346](https://github.com/openai/codex/issues/20346). Disabling the
sandbox or broadening window-station ACLs is not an acceptable workaround. Use
an interactive console or RDP session for supported Windows dispatches;
OpenSSH/Session 0 remains outside the supported boundary until the CLI is fixed
and the native canaries are rerun. See Codex's
[Windows sandbox documentation](https://developers.openai.com/codex/windows).

The Ubuntu 22.04 and 24.04 jobs cover every supported Python minor. The Ubuntu
26.04 job uses and verifies the image's system Python 3.14 while that hosted
runner is in public preview. A release requires all of these jobs to pass;
passing on another Linux distribution or architecture is useful evidence but
does not expand this compatibility boundary.

On macOS, protected paths must not contain ACL `allow` grants. Deny-only ACLs
are accepted. Root-owned and current-user-owned runtime chains are accepted;
world write is rejected. Group write is accepted only for a component owned by
the current user to support owner-managed package-manager layouts. Other members
who can write such a component are inside the documented trust boundary.

Strict mode applies to the documented native `multi_agent` feature in new
sessions using the selected Codex home. A separate collaboration tool injected
by a host application is not intercepted and cannot support a tier claim without
equivalent host-provided execution evidence.
