# Compatibility

This is the tested boundary for `v1.0.0-rc.1`.

## Current tested boundary

| Component | Status |
|---|---|
| macOS on Apple silicon | Local installer, dispatcher, and live read-only route checks |
| Linux | Deterministic regression coverage; CI runs on Ubuntu |
| Windows | Not supported |
| Python | 3.11, 3.13, and 3.14 in CI; runtime requires 3.11 or newer |
| Codex CLI | 0.144.1 used for bounded local canaries |

The recorded Codex checks covered a narrow local environment, read-only and
workspace-write routes, and small tasks. They are compatibility evidence, not a
benchmark, a broad fleet certification, or provider-side model attestation.
For workspace-write sessions, 0.144.1 serializes an empty extra-writable-root
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

The dispatcher depends on Unix process groups, advisory file locks, ownership
checks, and no-follow file opens. These are why Windows is not currently
supported.

On macOS, protected paths must not contain ACL `allow` grants. Deny-only ACLs
are accepted. Root-owned and current-user-owned runtime chains are accepted;
world write is rejected. Group write is accepted only for a component owned by
the current user to support owner-managed package-manager layouts. Other members
who can write such a component are inside the documented trust boundary.

Strict mode applies to the documented native `multi_agent` feature in new
sessions using the selected Codex home. A separate collaboration tool injected
by a host application is not intercepted and cannot support a tier claim without
equivalent host-provided execution evidence.
