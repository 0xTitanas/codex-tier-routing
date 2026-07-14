# Changelog

## [Unreleased]

No changes yet.

## [1.0.0-rc.2] - 2026-07-14

- Replaced repository-specific privacy literals with generic checks for
  unapproved email addresses, personal home paths, and private network
  addresses.
- Added focused regression coverage for the public privacy checks.
- Added tag-triggered CI and restored the documented macOS/Ubuntu matrix for
  Python 3.11, 3.13, and 3.14.
- Added repository validation for the release trigger and CI matrix contract.

## 1.0.0-rc.1 (withdrawn) - 2026-07-13

Withdrawn on 2026-07-14 during public-history cleanup.

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

[Unreleased]: https://github.com/0xTitanas/codex-tier-routing/compare/v1.0.0-rc.2...HEAD
[1.0.0-rc.2]: https://github.com/0xTitanas/codex-tier-routing/releases/tag/v1.0.0-rc.2
