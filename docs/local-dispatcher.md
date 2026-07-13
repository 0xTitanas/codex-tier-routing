# Local dispatcher

The bundled `codex-tier` command is the portable execution path for Codex
surfaces that can run terminal commands but cannot select a registered custom
agent through their delegation tool.

It does not choose tasks or verify their results. The parent still
classifies the task, chooses one stable route, verifies the result, and owns the
final answer.

## Default route table

| Route | Requested model | Effort | Sandbox |
|---|---|---|---|
| `scout` | `gpt-5.6-luna` | low | read-only |
| `grunt` | `gpt-5.6-luna` | low | workspace-write |
| `builder` | `gpt-5.6-terra` | medium | workspace-write |
| `reviewer` | `gpt-5.6-terra` | high | read-only |
| `architect` | `gpt-5.6-sol` | xhigh | read-only |

The dispatcher accepts no per-dispatch model, effort, sandbox, or approval
override. An optional owner-only policy file may replace only the model and
effort values. Sandbox, network, approval, role, and concurrency boundaries are
not configurable.

## Commands

Print the installed dispatcher version without resolving Codex or making a
model call:

```sh
"${CODEX_HOME:-$HOME/.codex}/bin/codex-tier" --version
```

Probe the local installation without a model call:

```sh
"${CODEX_HOME:-$HOME/.codex}/bin/codex-tier" probe
```

Validate the active policy without invoking Codex:

```sh
"${CODEX_HOME:-$HOME/.codex}/bin/codex-tier" policy-check
```

Check the critical semantics of one retained JSON envelope without a model
call:

```sh
"${CODEX_HOME:-$HOME/.codex}/bin/codex-tier" receipt-check \
  --receipt-file /absolute/owner-only-envelope.json
```

`receipt-check` verifies the policy/document/request/observation relationship,
status/result invariants, critical host fields, sandbox policy, and output hash.
It intentionally reports its limits: consumers must also validate the published
receipt and envelope JSON Schemas, and the command does not prove ledger
membership or provider-side identity. The file form rejects symlinks and
group/other permissions; stdin is supported for pipelines.

### Optional single-file policy

The active custom policy has one fixed path:

```text
${CODEX_HOME:-$HOME/.codex}/tier-routing/policy.toml
```

Copy `policy.sample.toml` there as mode `0600` inside a mode-`0700`
`tier-routing/` directory. The file schema is
`codex-tier-routing/policy/v1`. Its top-level keys must be exactly `schema` and
`routes`; all five routes are required; and each route accepts
exactly `model` and `effort`. Unknown fields halt before a model call.

Model IDs are bounded ASCII identifiers. Accepted efforts are `none`, `low`,
`medium`, `high`, `xhigh`, and `max`, matching the published default GPT-5.6
family; `minimal` and `ultra` are outside this release's bounded policy.
`policy-check` reports syntax and the resolved mapping, not account entitlement
or compatibility for a different model. The tested Codex CLI
does not provide a trusted non-interactive model-entitlement enumeration. Run
one bounded canary per changed route to test actual access. No unavailable-model
fallback or automatic retry occurs. The dispatcher does not detect subscription
plans or infer model price, quality, or relative strength.

The policy file is read before dispatch and again after the child returns. An
input-byte or effective-policy change during that interval halts the receipt.
The custom policy controls the local dispatcher only. Packaged agent profiles
remain default role descriptions and are not regenerated from this file.

Before the first dispatch, enable strict mode and restart Codex:

```sh
"${CODEX_HOME:-$HOME/.codex}/bin/codex-tier" strict-enable
```

Strict mode disables the native generic subagent channel in the user's Codex
configuration. The operation preserves the original bytes in an owner-only
backup and records before/after hashes. `strict-disable` restores those bytes
only if the managed config has not drifted. Both operations are zero-model-call
configuration actions.

The strict-state manifest and backup are retained at
`${CODEX_HOME:-$HOME/.codex}/tier-routing/strict-config.json` and
`${CODEX_HOME:-$HOME/.codex}/tier-routing/strict-config.backup`, respectively.
The dispatcher makes `tier-routing/` mode `0700` and these records mode `0600`.
A successful `strict-disable` deletes the two strict-state records; it does not
delete the directory or the receipt ledger.

Run one route. The task is read from stdin until EOF:

```sh
"${CODEX_HOME:-$HOME/.codex}/bin/codex-tier" dispatch \
  --route scout --workdir /absolute/path/to/project
```

Use the invoking environment's stdin support when possible. A shell pipe or
owner-only regular file is supported, but the task text and worker output may
still be recorded in the normal Codex session rollout. Do not pass secrets.
`--prompt-file` rejects symlinks and non-regular files.

The command performs no automatic retry. A timeout is reported as ambiguous
because a write-capable child may have completed an effect before the parent
lost its response.

The dispatcher refuses to run while `codex features list` reports native
`multi_agent` enabled. It also passes `features.multi_agent=false` to every
child and separately asks the host feature inspector to resolve that exact
override before dispatch. These are configuration and host-probe observations,
not a feature-state field attested by the child rollout. Restart is required
after changing strict mode because already-running sessions retain their
previous tool surface, which the dispatch-time probe cannot inspect.

`grunt` and `builder` operate directly in the selected workspace. Their edits
may exist even when post-execution receipt verification halts. Use a clean
worktree or disposable branch, inspect repository state after every ambiguous
outcome, and do not retry until effects are reconciled.

## What the receipt proves

`codex-tier` parses the child command's JSONL stream, obtains the unique child
session identifier, and reads the matching host-written rollout under the
current `CODEX_HOME`. It requires the rollout to be current-owner, regular,
non-symlinked, and created after the dispatch began. Every path component below
the validated home must also be current-owner, non-symlinked, free of
group/other write, and (on macOS) free of ACL `allow` grants.

The receipt passes only when the rollout matches all of these values:

- child session identifier;
- `exec` source and `codex_exec` originator;
- OpenAI model-provider label recorded by the Codex host;
- requested model and reasoning effort;
- requested sandbox;
- the full recorded sandbox policy; write-capable routes must explicitly record
  `network_access: false`, both temporary-directory exclusion booleans, and
  Codex 0.144.1's canonical empty-root encoding (a missing network field or any
  raw `writable_roots` field is not accepted);
- `never` approval policy;
- resolved working directory.

For every child, the invocation pins
`sandbox_workspace_write.network_access=false` and
`sandbox_workspace_write.writable_roots=[]` at command-line configuration
precedence and enables `--strict-config` so an unrecognized config key fails
before execution. Codex 0.144.1 omits `writable_roots` from its raw rollout when
the list is empty and emits the field when roots are present. The verifier
rejects every raw field, then normalizes the verified empty state to the
explicit `writable_roots: []` stored in the receipt. This prevents a user or
project config from granting the access first and merely causing a
post-execution halt. A future Codex encoding change fails closed until this
compatibility adapter and its live tests are updated.

The dispatcher requests `never` through both the Codex approval flag and an
explicit config override, then trusts neither request: the host-written rollout
must still report `never` or the receipt HALTs. This defends against profile
precedence differences across Codex installations.

The receipt also records the resolved Codex executable and its SHA-256, hashes
the worker output and rollout, and is appended to an owner-private,
hash-chained ledger at
`${CODEX_HOME:-$HOME/.codex}/tier-routing/ledger.jsonl`.
Receipt v2 also binds the dispatcher launcher, dispatcher Python source,
selected Python interpreter, Codex entrypoint, and actual native or shebang
runtime by path and SHA-256. Each is re-hashed after the child returns. This
detects an observed file change during the bounded dispatch; it is not a
signature or a defense against a trusted writer that can replace files and
recompute hashes.

Each receipt also embeds the complete effective policy document, its canonical
SHA-256, the policy-input SHA-256, its source kind (`builtin` or `custom`), and
its code-assigned policy ID. For a custom policy, the input hash covers the
exact TOML bytes; for the builtin policy, it covers the canonical default
selection. The dispatcher semantically checks the selected
policy route against requested and observed model, effort, sandbox, and approval
fields before returning `pass`. The complete document makes the binding
reconstructible after the active file changes; the policy ID is non-authority
metadata and must never be trusted without the hashes and document.
The ledger is mode `0600` inside the mode-`0700` `tier-routing/` directory and
is persistent: neither `strict-disable` nor the installer's printed uninstall
command removes it. Inspect it before manually deleting it once retention is no
longer needed. Existing private state directories and ledger files are checked
for macOS ACL `allow` grants before use. Codex writes rollout files below
`${CODEX_HOME:-$HOME/.codex}/sessions/`; the dispatcher accepts only files whose
complete provenance chain satisfies the ownership, symlink, write-mode, and
macOS ACL checks above. It does not manage their modes.

The returned envelope places `receipt_ref` beside the receipt, not inside it.
This avoids a self-referential hash: the reference is the SHA-256 of the ledger
entry containing the exact emitted receipt. A verifier can remove only the
entry's `entry_hash`, canonicalize the remaining entry, and recompute the
reference without transforming the receipt.

This evidence has claim class
`dispatcher_configured_and_host_observed`. It proves the local Codex host wrote
the observed execution configuration. It does not prove provider-side model
identity, lineage, output quality, or correctness. Worker output remains
untrusted until the parent checks it against the task and repository state.

## Failure behavior

The dispatcher halts on a missing executable, invalid or unsafe policy, invalid
task input, four active dispatches, nonzero child exit, malformed or incomplete
JSONL, missing or stale rollout, route or policy mismatch, unsafe local ledger
path, or timeout. A halt receipt is recorded when execution reached the
dispatch boundary and the local ledger is safe. Preflight refusals occur before
a model call.

Run one realistic, read-only canary after installation and after material
Codex-version, surface, account, or model-access changes. Do not fan out until
the canary receipt passes and the parent independently verifies its exact
result.

Strict mode covers the native spawn channel in new sessions that load the same
Codex home and do not override the setting in a project or one-off invocation.
Named profile files that explicitly re-enable the feature make `strict-enable`
HALT. A separate raw `codex exec` launched through the terminal with
contradictory configuration has no local dispatcher receipt and cannot support
a routing claim. A collaboration tool injected independently by an Ultra or
other host surface is outside this configuration boundary and likewise cannot
support a tier claim without equivalent host evidence. Administrator-managed
requirements are needed to pin the feature beyond the current user's control.

The ledger hash chain provides local self-consistency for retained entries. An
actor who can rewrite the file can modify entries and recompute the chain,
delete a prefix or suffix, replace the file, or remove it entirely. Stronger
retention evidence requires an independently protected checkpoint.

The installer uses `$HOME/.codex` only when `CODEX_HOME` and `--codex-home` are
absent; present empty or non-absolute spellings HALT. It moves collision targets
as a single tree under `snapshot/` in atomically reserved timestamped
`${CODEX_HOME:-$HOME/.codex}/.codex-tier-routing-backups/` directories. Those
backups preserve moved objects and are left behind by uninstall; restore or
delete them manually after inspection. An existing backup parent must be owned
by the effective UID and not group- or world-writable; a new one is mode `0700`.
That is the promotion boundary that prevents other users or groups from swapping
the reserved leaf. The effective UID is trusted because it already controls
`CODEX_HOME`; this does not defend against a malicious same-UID process, nor
does it claim crash or power-loss atomicity.

Before installation changes anything, it:

- rejects every lexical `.` or `..` component, including terminal and
  repeated-slash-adjacent spellings;
- checks each existing component of the absolute `CODEX_HOME` path and rejects
  live or dangling symlinks, non-root/non-current owners, unsafe writable
  ancestors, and macOS ACL `allow` entries;
- requires an existing home to be a current-owner, non-symlink directory without
  group or other write access;
- creates a missing home as mode `0700` only after checking its ancestors, and
  repeats the complete check after an `EEXIST` race;
- requires existing `agents/`, `skills/`, and `bin/` boundaries and replacement
  objects to be current-owner and mode/ACL-safe;
- creates new internal directories as `0700`, agent and skill files as `0600`,
  the shell launcher as `0700`, and the dispatcher Python source as `0600`,
  regardless of the caller's umask; and
- rejects skill symlinks and special nodes, removes inherited macOS ACLs from the
  staged private copy, and checks the installed tree again after copying it.

The installed dispatcher repeats the complete lexical home-chain check before
probing, changing strict state, accessing the ledger, or launching a child.

Strict installation records cleanup intent before invoking `strict-enable`.
If the subprocess succeeds but the installer is interrupted before it can
classify the response, rollback still runs `strict-disable` and restores the
preinstall bundle/configuration rather than leaving an untracked strict change.

The versioned receipt schema and emitted-envelope schema are published under
`schemas/`. The receipt schema publishes a calendar-aware, pattern-only
RFC3339-derived profile for `started_at` and `finished_at`, rather than relying
on optional Draft 2020-12 `format` assertion. It accepts years `0000`–`9999`,
lowercase `t`/`z`, fractional seconds, and numeric offsets. It deliberately
rejects every `:60` leap second: this deterministic profile is narrower than
full RFC3339 because a static schema cannot safely encode future IERS leap
second announcements. Canonical lowercase RFC4122 UUID constraints are
unchanged.

JSON Schema cannot express every equality between an arbitrary custom policy,
the selected route, and requested/observed values. The dispatcher enforces
those equalities before returning `pass`; `receipt-check` publishes the same
critical semantic verification path for retained envelopes. Structural schema
validation and semantic checking are both required for external verification.

The installer uses the platform's system utility path for its own filesystem
commands, rather than trusting the caller's `PATH`. Before strict installation,
it validates the lexical and resolved ancestry of each runtime-path directory
and the selected Python 3.11-or-newer interpreter. Strict enable, rollback, and
the printed strict uninstall command run that interpreter in isolated mode
(`-I`) with `HOME`, `PATH`, `CODEX_HOME`, and an explicit
`CODEX_TIER_CODEX_BIN` when supplied. The dispatcher accepts an explicit
`CODEX_TIER_CODEX_BIN` or discovers `codex` from `PATH`, then requires an
absolute path and checks every lexical and resolved component for ownership and
unsafe write access before execution. A script entrypoint may use either one
absolute shebang runtime or `/usr/bin/env NAME` without arguments. The
dispatcher resolves and validates that runtime, then invokes it explicitly.
Argument-bearing and otherwise ambiguous shebang forms HALT. Test-only
installer seams are disabled in the shipped script and are activated only in
temporary copies made by the test suite.

The dispatcher does not intentionally read, copy, log, or persist provider
credentials. It gives Codex a narrow explicit environment and does not forward
environment-only API credentials, but Codex can use its normal saved login
state. This is not a credential-isolation boundary.

The dispatcher bounds task input, control-command output, dispatch stdout and
stderr, rollout size, and individual ledger entries. It kills and reaps the
entire child process group on timeout, stream overflow, malformed UTF-8, and
other bounded-run failures. A timeout or transport failure remains ambiguous
for workspace effects and is never retried automatically.

Local sandbox observations do not attest the behavior of configured MCP
servers, plugins, connectors, cached or live web search, or another tool surface
injected by the host. Inspect and constrain those systems separately when an
external side effect matters. See the [threat model](threat-model.md).

The dispatcher is currently supported on macOS and Linux. It uses Unix process
groups, advisory file locks, ownership checks, and no-follow file opens; Windows
is not supported.
