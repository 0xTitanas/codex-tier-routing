---
name: model-routing
description: Fail-closed local routing guidance for Codex agent profiles and the bundled codex-tier dispatcher.
---

<!--
Project: codex-tier-routing
Original project by: 0xTitanas
Canonical source: https://github.com/0xTitanas/codex-tier-routing
Copyright: 2026 0xTitanas
SPDX-License-Identifier: MIT
-->

# Model routing

Agent profiles describe requested behavior. They do not prove which model,
effort level, sandbox, or working directory a child process actually used.
Treat route selection as verified only when the active Codex host supplies the
observation or the bundled dispatcher returns a passing local receipt.

## Check capability first

Use one of these paths:

1. A Codex surface that can select a registered profile from `agents/` and
   exposes host-controlled execution details.
2. The installed local dispatcher at
   `${CODEX_HOME:-$HOME/.codex}/bin/codex-tier`.

If neither path can select and verify the requested route, keep the work in the
parent session. A role name in a prompt or response is not route evidence.

## Local dispatcher

Run the zero-model-call probe before dispatching work:

```sh
"${CODEX_HOME:-$HOME/.codex}/bin/codex-tier" probe
```

The probe must report `status: ready` and
`native_multi_agent_enabled: false`. If it halts, enable strict mode and restart
all Codex sessions that use the same home:

```sh
"${CODEX_HOME:-$HOME/.codex}/bin/codex-tier" strict-enable
```

Run the probe from the target project. Project settings, named profiles, and
one-off command-line configuration can override user configuration.

Pass tasks over standard input so the task text is not placed in the child
process arguments:

```sh
printf '%s\n' 'Inspect the current diff and report concrete correctness risks.' | \
  "${CODEX_HOME:-$HOME/.codex}/bin/codex-tier" dispatch \
  --route reviewer --workdir "$PWD"
```

The dispatcher returns one JSON envelope. The `result` remains untrusted until
the parent checks it against files, tests, or another appropriate oracle. Accept
a route claim only when the receipt has `status: pass` and every requested and
observed field matches.

Do not automatically retry a timeout or a failed write-capable route. Inspect
workspace state first because the child may have completed an effect before the
response was lost.

## Route table

The local dispatcher accepts these fixed routes:

| Work | Route | Model | Effort | Sandbox |
|---|---|---|---|---|
| Search and dependency inventory | `scout` | `gpt-5.6-luna` | low | read-only |
| Formatting and mechanical edits | `grunt` | `gpt-5.6-luna` | low | workspace-write |
| Scoped implementation and tests | `builder` | `gpt-5.6-terra` | medium | workspace-write |
| Correctness and coverage review | `reviewer` | `gpt-5.6-terra` | high | read-only |
| Difficult design decisions | `architect` | `gpt-5.6-sol` | xhigh | read-only |

These values are requests until the host-written rollout confirms them. The
command does not accept model, effort, sandbox, or approval overrides.

## Task classification

Classify non-trivial work before selecting a route:

- **Do-shaped:** bounded work with clear acceptance criteria.
- **Decide-shaped:** architecture, security, competing designs, or an expensive
  decision without a stable reproduction.
- **Mixed:** decide first, then send only the bounded implementation step.
- **Project-scale:** multiple dependent deliverables; plan the dependency order
  before dispatching any slice.

Keep decomposition, security decisions, final verification, and synthesis in
the parent session.

## Verification rules

- Treat child output as a claim, not a result.
- Verify changed files and tests directly before accepting write-capable work.
- Use a clean worktree or disposable branch for write-capable routes.
- Reconcile effects before retrying any ambiguous failure.
- Keep dependent writes sequential.
- Cap concurrent dispatches at four.
- Run one bounded read-only canary before a larger batch.
- Never describe local host observation as provider-side model attestation.

## Retry policy

For read-only work, or writes protected by a stable idempotency key and
independent effect checks:

1. Retry once with a narrower task.
2. If the second attempt fails, choose one stronger fixed route when available.
3. Stop after the third failure and return the evidence to the operator.

Credential changes, destructive actions, public writes, and uncertain effects
stop without an automatic retry.

## Receipt boundary

A valid `codex-tier-routing/local-dispatch-receipt/v1` receipt binds the stable
route to the observed model, effort, sandbox policy, approval policy, working
directory, child session, executable hash, output hash, and rollout hash. Its
claim class is `dispatcher_configured_and_host_observed`.

That proves what the local Codex host recorded. It does not prove provider-side
model identity, lineage, output quality, or correctness.
