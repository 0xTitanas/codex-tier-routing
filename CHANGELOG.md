# Changelog

## Unreleased

- Added five fixed Codex task routes with explicit model, effort, and sandbox
  policy.
- Added a bounded local `codex exec` dispatcher for Codex surfaces that cannot
  select a registered profile directly.
- Added host-written rollout checks for model, effort, sandbox, approval policy,
  working directory, and child session identity.
- Added strict-mode configuration with reversible owner-only backups.
- Added a private hash-chained local receipt ledger.
- Added fail-closed checks for unsafe paths, ambiguous process outcomes, receipt
  mismatches, and unexpected workspace policy.
- Added trusted installer utility resolution, complete Python and Codex
  executable-chain validation, sanitized strict-mode invocation, receipt
  field type checks, a hardened dispatcher launcher, and a minimal child
  process environment.
- Added transactional installation, exact uninstall output, and deterministic
  macOS/Linux regression coverage.
- Added public documentation, contribution guidance, security reporting, and
  cross-platform continuous integration.

This project remains experimental. No cost, speed, reliability, quality, or
provider-side model-attestation claim is made.
