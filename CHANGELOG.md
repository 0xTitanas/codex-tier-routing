# Changelog

## [Unreleased]

- Made per-workdir writer exclusion effective across different Codex homes for
  the same operating-system identity.
- Preserved unrelated Codex configuration additions during strict disable and
  uninstall while retaining fail-closed managed-setting conflict handling.
- Restored repository privacy validation and made test fixtures independent of
  the caller's umask.
- Expanded release gates to Ubuntu 22.04, 24.04, and 26.04 LTS and every
  supported Python minor from 3.11 through 3.14 where hosted runtimes exist.
- Added native Windows installer and launcher support, including SID/DACL and
  reparse-point checks, `LockFileEx` writer exclusion, Job Object containment,
  atomic replacement, rollback, collision backups, and user `PATH` setup.
  Native deterministic and installation checks plus manual negative and
  all-six-route live checks pass in an interactive Windows 11 session,
  including an independently verified `surgeon` diff. Codex CLI 0.144.4
  sandboxed tool processes still exit before command execution with status
  `0xc0000142` under OpenSSH/Session 0, which is outside the supported Windows
  boundary.

## [1.1.0-rc.1] - 2026-07-14

- Added the six-route table, including Terra/low `scout`, Sol/high `reviewer`, and
  experimental Sol/high/workspace-write `surgeon` routing.
- Added per-workdir writer exclusion so write-capable routes cannot run concurrently
  in the same resolved workdir.
- Clarified routing-policy language and repaired CI trigger and matrix coverage.

This is a release candidate. No cost, speed, reliability, quality, OpenAI
endorsement, or provider-side model-attestation claim is made.

## [1.0.0-rc.1] - 2026-07-13

- Added five stable Codex task routes with explicit default model, effort, and
  fixed sandbox policy.
- Added an optional owner-only single-file model/effort policy, offline
  validation with actionable effort reporting, policy drift detection, and
  self-contained receipt bindings. Authority boundaries remain non-configurable.
- Added an offline critical-semantic receipt checker and launch-time overrides
  that pin workspace network off and additional writable roots empty before a
  child executes, use strict Codex config parsing, and normalize the verified
  host empty-root encoding into each workspace receipt.
- Added a bounded local `codex exec` dispatcher for Codex surfaces that cannot
  select a registered profile directly.
- Added host-written rollout checks for model, effort, sandbox, approval policy,
  working directory, and child session identity.
- Added strict-mode configuration with reversible owner-only backups.
- Added a private hash-chained local receipt ledger.
- Added fail-closed checks for unsafe paths, ambiguous process outcomes, receipt
  mismatches, and unexpected workspace policy.
- Added trusted installer utility resolution, complete Python and Codex
  executable-chain validation, sanitized strict-mode invocation, receipt field
  type checks, a hardened POSIX launcher, a non-executable dispatcher runtime,
  explicit shebang-runtime invocation, and a minimal child process environment.
- Versioned the receipt contract at v2, added an emitted-envelope schema, and
  bound the complete launcher/runtime chain into dispatch evidence.
- Added aggregate process-stream, rollout-scan, rollout-file, ledger-entry, and
  ledger-file resource bounds with process-group teardown on failures.
- Prevented strict-enable rollback from overwriting a concurrent configuration
  change; recovery records are preserved when automatic rollback is unsafe.
- Added transactional installation, exact uninstall output, and deterministic
  macOS/Linux regression coverage.
- Added a public threat model, privacy validation, contribution guidance,
  security reporting, and cross-platform continuous integration.

This is a release candidate. No cost, speed, reliability, quality, OpenAI
endorsement, or provider-side model-attestation claim is made.

[Unreleased]: https://github.com/0xTitanas/codex-tier-routing/compare/v1.1.0-rc.1...HEAD
[1.1.0-rc.1]: https://github.com/0xTitanas/codex-tier-routing/releases/tag/v1.1.0-rc.1
[1.0.0-rc.1]: https://github.com/0xTitanas/codex-tier-routing/releases/tag/v1.0.0-rc.1
