# Local dispatcher

The bundled `codex-tier` command is the portable execution path for Codex
surfaces that can run terminal commands but cannot select a registered custom
agent through their delegation tool.

It does not choose tasks or verify their results. The parent still
classifies the task, chooses one stable route, verifies the result, and owns the
final answer.

## Route table

| Route | Requested model | Effort | Sandbox |
|---|---|---|---|
| `scout` | `gpt-5.6-luna` | low | read-only |
| `grunt` | `gpt-5.6-luna` | low | workspace-write |
| `builder` | `gpt-5.6-terra` | medium | workspace-write |
| `reviewer` | `gpt-5.6-terra` | high | read-only |
| `architect` | `gpt-5.6-sol` | xhigh | read-only |

Arbitrary model, effort, sandbox, and approval overrides are intentionally not
accepted. Change the reviewed route table in source and retest instead of
smuggling a different execution policy through command arguments.

## Commands

Probe the local installation without a model call:

```sh
"${CODEX_HOME:-$HOME/.codex}/bin/codex-tier" probe
```

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
  `network_access: false`, both temporary-directory exclusion booleans, and no
  unexpected writable-root expansion (a missing network field is not accepted);
- `never` approval policy;
- resolved working directory.

The dispatcher requests `never` through both the Codex approval flag and an
explicit config override, then trusts neither request: the host-written rollout
must still report `never` or the receipt HALTs. This defends against profile
precedence differences across Codex installations.

The receipt also records the resolved Codex executable and its SHA-256, hashes
the worker output and rollout, and is appended to an owner-private,
hash-chained ledger at
`${CODEX_HOME:-$HOME/.codex}/tier-routing/ledger.jsonl`.
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

The dispatcher halts on a missing executable, invalid task input, four active
dispatches, nonzero child exit, malformed or incomplete JSONL, missing or stale
rollout, route mismatch, unsafe local ledger path, or timeout. A halt receipt is
recorded when execution reached the dispatch boundary and the local ledger is
safe. Preflight refusals occur before a model call.

Run one realistic, read-only canary after installation and after material
Codex-version, surface, account, or model-access changes. Do not fan out until
the canary receipt passes and the parent independently verifies its exact
result.

Strict mode covers the native spawn channel in new sessions that load the same
Codex home and do not override the setting in a project or one-off invocation.
Named profile files that explicitly re-enable the feature make `strict-enable`
HALT. A separate raw `codex exec` launched through the terminal with
contradictory configuration has no local dispatcher receipt and cannot support
a routing claim. Administrator-managed requirements are needed to pin the
feature beyond the current user's control.

The ledger hash chain detects modification and reordering of retained entries.
Without an independently anchored checkpoint, it cannot prove that an attacker
did not delete a suffix of the ledger.

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
  and the dispatcher as `0700`, regardless of the caller's umask; and
- rejects skill symlinks and special nodes, removes inherited macOS ACLs from the
  staged private copy, and checks the installed tree again after copying it.

The installed dispatcher repeats the complete lexical home-chain check before
probing, changing strict state, accessing the ledger, or launching a child.

Strict installation records cleanup intent before invoking `strict-enable`.
If the subprocess succeeds but the installer is interrupted before it can
classify the response, rollback still runs `strict-disable` and restores the
preinstall bundle/configuration rather than leaving an untracked strict change.

The local receipt schema publishes a calendar-aware, pattern-only
RFC3339-derived profile for `started_at` and `finished_at`, rather than relying
on optional Draft 2020-12 `format` assertion. It accepts years `0000`–`9999`,
lowercase `t`/`z`, fractional seconds, and numeric offsets. It deliberately
rejects every `:60` leap second: this deterministic profile is narrower than
full RFC3339 because a static schema cannot safely encode future IERS leap
second announcements. Canonical lowercase RFC4122 UUID constraints are
unchanged.

The installer uses the platform's system utility path for its own filesystem
commands, rather than trusting the caller's `PATH`. Before strict installation,
it validates the lexical and resolved ancestry of each runtime-path directory
and the selected Python 3.11-or-newer interpreter. Strict enable, rollback, and
the printed strict uninstall command run that interpreter in isolated mode
(`-I`) with `HOME`, `PATH`, `CODEX_HOME`, and an explicit
`CODEX_TIER_CODEX_BIN` when supplied. The
dispatcher accepts an explicit
`CODEX_TIER_CODEX_BIN` or discovers `codex` from `PATH`, then requires an
absolute path and checks every lexical and resolved component for ownership and
unsafe write access before execution. Test-only installer seams are disabled in
the shipped script and are activated only in temporary copies made by the test
suite.

The dispatcher does not intentionally read, copy, log, or persist provider
credentials. It nevertheless inherits the invoking process environment and
launches the installed Codex CLI, so it is not a credential-isolation boundary.

The dispatcher is currently supported on macOS and Linux. It uses Unix process
groups, advisory file locks, ownership checks, and no-follow file opens; Windows
is not supported.
