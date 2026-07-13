# Threat model

This document defines what `codex-tier-routing` is designed to protect, what it
trusts, and what remains outside its security boundary. It is a local execution
policy and evidence mechanism, not a remote attestation service or a general
credential sandbox.

## Security goals

For a dispatch that returns `status: pass`, the project aims to provide local
evidence that:

- one stable route and validated policy selected the requested model, reasoning effort, local
  filesystem sandbox, approval policy, and working directory;
- the complete effective policy, canonical policy hash, and policy-input hash
  were bound into the receipt without making model-entitlement claims;
- the Codex host wrote matching session and turn observations;
- the dispatcher used the recorded launcher, dispatcher source, Python
  interpreter, Codex entrypoint, and script runtime without an observed hash
  change during the dispatch;
- captured command streams, task input, rollout input, and ledger entries stayed
  within explicit memory or file-size limits;
- a timeout, malformed stream, incomplete rollout, route mismatch, unsafe path,
  or ambiguous process outcome failed closed; and
- installation either completed as one reviewed bundle or restored the prior
  bundle after a handled failure or signal.

The dispatcher also prevents a routed child from using Codex's native generic
multi-agent feature when the active CLI honors the documented feature override.

## Trust anchors

The implementation trusts:

- the operating-system kernel and filesystem metadata;
- root;
- the effective operating-system user running the installer and dispatcher;
- any identity that can write a runtime-path component which the effective user
  owns and deliberately leaves group-writable;
- the installed Python standard library;
- the installed Codex CLI to write truthful local rollout fields; and
- the operator to verify worker output and workspace effects independently.

Root-owned and effective-user-owned runtime paths are accepted. World-writable
runtime components are rejected. A group-writable component is accepted only
when the effective user owns it, which supports common owner-managed package
manager layouts. Other members with write access to that directory are inside
the trust boundary; installations that require protection from those members
must remove group write before use.

## Defended failure classes

- accidental or malicious route drift visible in the host rollout;
- user or project configuration attempting to enable workspace network access
  or additional writable roots, enforced through command-line overrides,
  strict config parsing, and the tested host rollout encoding;
- malformed, over-permissive, oversized, symlinked, or concurrently changed
  model/effort policy input;
- caller-controlled loader and Python startup variables reaching the dispatcher;
- an untrusted `PATH` entry supplying the dispatcher interpreter or a script
  entrypoint runtime before that path is checked;
- unsafe symlinks, foreign ownership, world-writable runtime ancestors, unsafe
  Codex-home paths, and macOS ACL `allow` grants on protected state;
- missing, duplicate, malformed, stale, oversized, or contradictory execution
  evidence;
- unbounded stdout, stderr, prompt, rollout, or ledger-entry growth;
- blind retries after an ambiguous write-capable outcome; and
- handled installer failures that would otherwise leave a partial bundle.

## Explicit exclusions

The project does not defend against:

- root, the current operating-system identity, or another trusted writer
  replacing files and recomputing hashes;
- a same-identity race between path validation and process execution;
- a compromised kernel, Python runtime, Codex binary, Codex host, or provider;
- provider-side model substitution or cryptographic model attestation;
- correctness, safety, or trustworthiness of model output;
- account entitlement or selected-model compatibility inferred from policy
  syntax alone;
- crash or power-loss atomicity during installation;
- arbitrary raw `codex exec` commands launched outside the dispatcher;
- a host-injected delegation tool that ignores the selected Codex home's strict
  feature configuration;
- local shell sandbox labels proving that MCP servers, plugins, connectors,
  web-search tools, or other external integrations are side-effect-free;
- credential isolation from Codex's normal saved authentication state; or
- deletion, truncation, replacement, or complete re-chaining of the local ledger
  by an actor with write access.

The receipt ledger is therefore a local self-consistency record. Stronger
tamper-evidence requires periodic receipt or chain-head checkpoints stored in an
independently protected system.

## Operational requirements

Operators should:

1. Review the installer and run strict dry-run validation before installation.
2. Restart every Codex session that uses the modified home.
3. Run `policy-check`, then `probe`, then one bounded read-only canary for each
   changed route.
4. Inspect configured MCP, plugin, connector, and web-search capabilities
   separately when external effects matter.
5. Use a clean worktree or disposable branch for write-capable routes.
6. Treat every child result as untrusted and independently verify files, tests,
   and claimed facts.
7. Reconcile workspace state before retrying a timeout or any ambiguous failure.
8. Retain or externally checkpoint receipts according to the required audit
   strength.
