# Contributing

Contributions are welcome when they keep the project small, testable, and
fail-closed.

## Before opening a pull request

1. Open an issue for behavior changes that affect route policy, receipt fields,
   installation boundaries, or supported platforms.
2. Keep each pull request focused on one problem.
3. Add regression coverage for behavior changes.
4. Run the full local checks:

```sh
DEV_VENV="$(python3 -c 'import os,tempfile; print(os.path.realpath(tempfile.mkdtemp()))')/venv"
python3 -m venv "$DEV_VENV"
. "$DEV_VENV/bin/activate"
python -m pip install --upgrade pip
python -m pip install --disable-pip-version-check -r requirements-dev.txt
python scripts/validate.py
python -m unittest discover -s tests -v
sh -n install.sh
sh -n bin/codex-tier
PYTHONPYCACHEPREFIX=/tmp/codex-tier-pycache python -m py_compile \
  bin/codex-tier.py scripts/validate.py tests/test_codex_tier.py \
  tests/test_installer.py tests/test_validate.py tests/test_windows.py \
  tests/test_windows_installer.py
ruff check .
ruff format --check scripts tests bin/codex-tier.py
```

Windows contributors should additionally run:

```powershell
python scripts\validate.py
python -m unittest discover -s tests -v
powershell -NoProfile -File .\install.ps1 -Strict -DryRun
```

Then exercise `codex-tier probe` and a bounded workspace-write canary from a
fresh interactive PowerShell console or RDP session, and independently inspect
the resulting diff. Do not use OpenSSH/Session 0 for that live canary with Codex
CLI 0.144.4; see `docs/compatibility.md` for the current CLI limitation.

## Pull request expectations

Describe:

- the problem;
- the smallest behavior change that solves it;
- tests added or changed;
- security or compatibility consequences;
- anything intentionally left out of scope.

Do not include credentials, private paths, session data, generated local state,
or unrelated formatting changes.

Maintainer release steps, including clean GitHub CI and tag verification, are
documented in [docs/releasing.md](docs/releasing.md).

## Design rules

- Stable route roles and authority boundaries stay explicit and reviewable;
  custom policies may select only model and effort.
- Unknown observations fail closed.
- Requested configuration is not execution proof.
- Generic or host-injected spawning is not a tier-routing receipt.
- Write-capable failures are treated as potentially effectful.
- User configuration changes must be reversible.
- New dependencies need a clear reason and a maintenance plan.
- Security claims must remain within [the threat model](docs/threat-model.md).

## Security reports

Do not report vulnerabilities in a public pull request or issue. Follow
[SECURITY.md](SECURITY.md).
