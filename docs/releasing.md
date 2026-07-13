# Releasing

This procedure publishes `v1.0.0-rc.1` from a reviewed `main` commit. It keeps
the release tag bound to the exact commit tested on fresh GitHub-hosted runners.
The workflow does not use Codex credentials or make model calls.

## What clean CI means

A clean CI result has all of these properties:

- every intended source change is committed and pushed;
- GitHub checks out that commit into a fresh hosted runner;
- lint and all six operating-system/Python test-matrix jobs pass;
- repository validation, schema locks, shell syntax, compilation, installer
  dry-run, and checkout-cleanliness checks pass; and
- the release tag names the same commit and passes its own tag-triggered run.

Local tests are necessary preflight evidence, but they are not a substitute for
the GitHub-hosted run because local uncommitted files and machine state are not
part of the pushed commit.

## 1. Prepare the release branch

Run the documented development checks from the repository root. Then confirm
that `VERSION`, the CLI, citation metadata, and changelog agree:

```sh
python3 scripts/validate.py
./bin/codex-tier --version
test "$(cat VERSION)" = "1.0.0-rc.1"
git diff --check
git status --short
```

Review every listed change. Commit the complete candidate on a non-`main`
branch using the repository's normal signing policy. Do not include local
receipts, Codex sessions, credentials, caches, or test artifacts.

If the tag will be published on a date other than `2026-07-13`, update the
dated heading in `CHANGELOG.md` and `date-released` in `CITATION.cff` before
committing. `scripts/validate.py` requires those values to agree.

## 2. Run GitHub CI through a pull request

Push the branch and open a pull request. The GitHub web interface can perform
the same actions; these commands use the optional GitHub CLI:

```sh
BRANCH="$(git branch --show-current)"
test -n "$BRANCH"
test "$BRANCH" != "main"
git push --set-upstream origin "$BRANCH"
gh pr create --fill --base main --head "$BRANCH"
gh pr checks --watch
```

Require the `Lint and format` job and all six macOS/Ubuntu Python matrix jobs.
If a job fails, fix it in a new commit and let GitHub rerun the complete
workflow. Do not merge using a local-only pass.

For the canonical repository, configure a `main` ruleset that requires pull
requests, requires these CI checks, requires the branch to be current before
merge, and blocks force-pushes and branch deletion. Select the exact check names
shown by the first pull request rather than guessing them in advance.

## 3. Merge and verify the tested commit

Merge through GitHub after review and all required checks pass. Refresh the
local `main` branch without creating a merge commit:

```sh
git switch main
git pull --ff-only origin main
test -z "$(git status --porcelain=v1 --untracked-files=all)"
python3 scripts/validate.py
test "$(./bin/codex-tier --version)" = "codex-tier 1.0.0-rc.1"
```

Record the resulting commit ID in the release notes.

## 4. Tag, verify tag CI, and publish the prerelease

Create an annotated tag on the verified `main` commit. Use a signed tag when a
maintainer signing key is already configured; otherwise use the annotated form
shown here and do not imply that it is signed:

```sh
git tag -a v1.0.0-rc.1 -m "codex-tier-routing v1.0.0-rc.1"
git push origin v1.0.0-rc.1
```

The CI workflow checks that a `v*` tag exactly matches `VERSION`. Wait for the
tag's `CI` workflow to pass in GitHub Actions. Then create a GitHub prerelease:

```sh
gh release create v1.0.0-rc.1 \
  --prerelease \
  --verify-tag \
  --generate-notes \
  --title "codex-tier-routing v1.0.0-rc.1"
```

Inspect the rendered release notes, source archives, README links, and install
instructions from the public release page.

## 5. After publication

Exercise one fresh macOS installation and one fresh Linux installation from the
tagged source archive. Record the operating system, Python version, Codex CLI
version, and whether the check was deterministic-only or included an authorized
bounded live canary.

Do not move or reuse a published tag. If a release-blocking defect appears,
document it, fix it on a new branch, and publish `v1.0.0-rc.2` after the same
gates. Promote to stable `v1.0.0` only when the release-candidate evidence meets
the compatibility and support commitment intended for the stable line.
