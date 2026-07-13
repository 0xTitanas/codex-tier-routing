# Compatibility

## Current tested boundary

| Component | Status |
|---|---|
| macOS on Apple silicon | Local installer and dispatcher regression suite passed |
| Linux | Deterministic installer and dispatcher coverage; CI runs on Ubuntu |
| Windows | Not supported |
| Python | 3.11 or newer |
| Codex CLI | 0.144.1 used for bounded local canaries |

The recorded Codex checks covered one account, one machine, one read-only route,
one workspace-write route, and small tasks. They are compatibility evidence, not
a benchmark or provider-side model attestation.

## What to recheck

Run `probe` and a small read-only canary after changing:

- Codex CLI version;
- account or model access;
- `CODEX_HOME`;
- operating system;
- named profile or project configuration;
- route model, effort, sandbox, or approval policy.

A successful probe confirms local prerequisites and strict-mode state. It does
not guarantee that a later model call will succeed.

## Platform notes

The dispatcher depends on Unix process groups, advisory file locks, ownership
checks, and no-follow file opens. These are why Windows is not currently
supported.

On macOS, protected paths must not contain ACL `allow` grants. Deny-only ACLs
are accepted. On Linux, group or world write access is rejected on protected
paths even when a user-private group is in use.
