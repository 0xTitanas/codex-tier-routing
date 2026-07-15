#!/usr/bin/env python3
# Project: codex-tier-routing
# Original project by: 0xTitanas
# Canonical source: https://github.com/0xTitanas/codex-tier-routing
# Copyright: 2026 0xTitanas
# SPDX-License-Identifier: MIT

from __future__ import annotations

import ast
import codecs
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REQUIRED_SKILL_MARKERS = (
    "Check capability first",
    "Local dispatcher",
    "codex-tier-routing/local-dispatch-receipt/v2",
    "codex-tier-routing/local-dispatch-receipt/v3",
    "dispatcher_configured_and_host_observed",
    "Cap concurrent dispatches at four",
    "Do not automatically retry",
    "policy-check",
    "receipt-check",
    "policy.toml",
    "surgeon",
)

REQUIRED_LOCAL_GUIDE_MARKERS = (
    "dispatcher_configured_and_host_observed",
    "host-written rollout",
    "four active",
    "no automatic retry",
    "provider-side",
    "policy-check",
    "receipt-check",
    "policy.toml",
    "codex-tier-routing/local-dispatch-receipt/v3",
)

REQUIRED_PUBLIC_FILES = (
    ".github/workflows/ci.yml",
    "README.md",
    "LICENSE",
    "NOTICE.md",
    "CITATION.cff",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "VERSION",
    "docs/compatibility.md",
    "docs/local-dispatcher.md",
    "docs/releasing.md",
    "docs/threat-model.md",
    "policy.sample.toml",
    "install.ps1",
    "bin/codex-tier.ps1",
    "schemas/local-dispatch-receipt.schema.json",
    "schemas/local-dispatch-envelope.schema.json",
    "schemas/local-dispatch-receipt-v3.schema.json",
    "schemas/local-dispatch-envelope-v3.schema.json",
)

LOCAL_SCHEMA_SHA256 = "e30a229e33ec6a4f2658eec8d595a73dcc302cb6f48d25b5ca322f239b10bd42"
LOCAL_ENVELOPE_SCHEMA_SHA256 = (
    "bb80ad56a1eec1a2139a9c6124f73582e54862e768960220265f1d73e53b6010"
)
LOCAL_V3_SCHEMA_SHA256 = (
    "f2ec6dd7fd614867d7bf45127285c0a2d42029db1209a0b368fefb944878fc41"
)
LOCAL_V3_ENVELOPE_SCHEMA_SHA256 = (
    "84d67c92eac18fb075fb40705c4241754c7151aa33c2712e5850de698048f368"
)

ALLOWED_PUBLIC_EMAIL_DOMAINS = frozenset(
    {"example.com", "example.net", "example.org", "users.noreply.github.com"}
)
PUBLIC_EMAIL_PATTERN = re.compile(
    r"(?i)(?<![A-Z0-9._%+-])([A-Z0-9._%+-]+)@"
    r"([A-Z0-9.-]+\.[A-Z]{2,})(?![A-Z0-9._%+-])"
)
PUBLIC_POSIX_HOME_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])/(?:Users|home)/[A-Za-z0-9._-]+(?:/|\b)"
)
PUBLIC_WINDOWS_HOME_PATTERN = re.compile(
    r"(?i)(?:[A-Z]:[\\/])Users[\\/][A-Za-z0-9._-]+"
)
PUBLIC_IPV4_PATTERN = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
PUBLIC_LAB_HOST_PATTERN = re.compile(r"\b[a-z][a-z0-9-]*-lab\b", re.IGNORECASE)

CI_WORKFLOW_SHA256 = "75547bd7d24c8a3dd1905916b16f904bb79654ddce66fc11b2b4e5e41b1b54b0"


def check_markers(path: Path, markers: tuple[str, ...], root: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        print(f"[FAIL] {path.relative_to(root)}: unreadable ({err})")
        return 1

    failures = 0
    for marker in markers:
        if marker not in text:
            failures += 1
            print(f"[FAIL] {path.relative_to(root)}: missing marker {marker!r}")
        else:
            print(f"[PASS] {path.relative_to(root)}: marker {marker!r}")
    return failures


def check_readme_links(root: Path) -> int:
    readme = (root / "README.md").read_text(encoding="utf-8")
    failures = 0
    links = re.findall(r"(?<!!)\[[^]]+\]\(([^)]+)\)", readme)
    for link in links:
        if "://" in link or link.startswith(("#", "mailto:")):
            continue
        target = root / link.split("#", 1)[0]
        if not target.exists():
            failures += 1
            print(f"[FAIL] README.md: missing local link target {link!r}")
    if failures == 0:
        print(f"[PASS] README.md: {len(links)} link(s) checked")
    return failures


def public_privacy_categories(text: str) -> tuple[str, ...]:
    categories: set[str] = set()
    for match in PUBLIC_EMAIL_PATTERN.finditer(text):
        if match.group(2).casefold() not in ALLOWED_PUBLIC_EMAIL_DOMAINS:
            categories.add("unapproved email address")
    if PUBLIC_POSIX_HOME_PATTERN.search(text) or PUBLIC_WINDOWS_HOME_PATTERN.search(
        text
    ):
        categories.add("personal home path")
    if PUBLIC_LAB_HOST_PATTERN.search(text):
        categories.add("local lab hostname")
    for match in PUBLIC_IPV4_PATTERN.finditer(text):
        octets = tuple(int(value) for value in match.group().split("."))
        if any(value > 255 for value in octets):
            continue
        if (
            octets[0] == 10
            or (octets[0] == 172 and 16 <= octets[1] <= 31)
            or octets[:2] in ((192, 168), (169, 254))
        ):
            categories.add("private network address")
    return tuple(sorted(categories))


def decode_public_text(data: bytes) -> tuple[str, ...]:
    encodings = (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
        (codecs.BOM_UTF8, "utf-8-sig"),
    )
    for bom, encoding in encodings:
        if data.startswith(bom):
            return (data.decode(encoding),)

    decoded = [data.decode("latin-1")]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        if text not in decoded:
            decoded.append(text)
    if b"\0" in data:
        for encoding, unit in (
            ("utf-32-le", 4),
            ("utf-32-be", 4),
            ("utf-16-le", 2),
            ("utf-16-be", 2),
        ):
            if len(data) % unit:
                continue
            try:
                text = data.decode(encoding)
            except UnicodeDecodeError:
                continue
            if text not in decoded:
                decoded.append(text)
    return tuple(decoded)


def check_public_privacy(root: Path) -> int:
    failures = 0
    excluded_parts = {".git", ".ruff_cache", "__pycache__"}
    candidates = sorted(
        path
        for path in root.rglob("*")
        if (path.is_file() or path.is_symlink())
        and not any(part in excluded_parts for part in path.relative_to(root).parts)
    )
    scanned = 0
    for path in candidates:
        if path.is_symlink():
            failures += 1
            print(f"[FAIL] {path.relative_to(root)}: refusing symlink during text scan")
            continue
        try:
            data = path.read_bytes()
        except OSError as err:
            failures += 1
            print(f"[FAIL] {path.relative_to(root)}: text scan failed ({err})")
            continue
        try:
            texts = decode_public_text(data)
        except UnicodeDecodeError:
            failures += 1
            print(f"[FAIL] {path.relative_to(root)}: malformed Unicode text")
            continue
        if not texts:
            continue
        scanned += 1
        categories = {
            category for text in texts for category in public_privacy_categories(text)
        }
        for category in sorted(categories):
            failures += 1
            print(f"[FAIL] {path.relative_to(root)}: contains {category}")
    if failures == 0:
        print(f"[PASS] public privacy scan: {scanned} file(s) checked")
    return failures


def check_ci_contract(root: Path) -> int:
    path = root / ".github" / "workflows" / "ci.yml"
    try:
        digest = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    except OSError as err:
        print(f"[FAIL] .github/workflows/ci.yml: unreadable ({err})")
        return 1
    if digest != CI_WORKFLOW_SHA256:
        print("[FAIL] .github/workflows/ci.yml: reviewed CI contract changed")
        return 1
    print("[PASS] CI matches the reviewed platform and Python matrix")
    return 0


def check_default_policy_alignment(root: Path) -> int:
    """Keep the sample, packaged profiles, and documented route boundary aligned."""
    sample_path = root / "policy.sample.toml"
    try:
        sample = tomllib.loads(sample_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as err:
        print(f"[FAIL] policy.sample.toml: invalid ({err})")
        return 1
    expected_routes = {
        "scout": "read-only",
        "grunt": "workspace-write",
        "builder": "workspace-write",
        "reviewer": "read-only",
        "architect": "read-only",
    }
    failures = 0
    if set(sample) != {"schema", "routes"}:
        failures += 1
        print("[FAIL] policy.sample.toml: unexpected top-level fields")
    if sample.get("schema") != "codex-tier-routing/policy/v1":
        failures += 1
        print("[FAIL] policy.sample.toml: unexpected schema")
    routes = sample.get("routes")
    if not isinstance(routes, dict) or set(routes) != set(expected_routes):
        failures += 1
        print("[FAIL] policy.sample.toml: route set does not match packaged roles")
        return failures
    for name, sandbox in expected_routes.items():
        selection = routes[name]
        if not isinstance(selection, dict) or set(selection) != {"model", "effort"}:
            failures += 1
            print(f"[FAIL] policy.sample.toml: invalid {name} fields")
            continue
        try:
            profile = tomllib.loads(
                (root / "agents" / f"{name}.toml").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as err:
            failures += 1
            print(f"[FAIL] agents/{name}.toml: invalid ({err})")
            continue
        if (
            profile.get("name") != name
            or profile.get("model") != selection.get("model")
            or profile.get("model_reasoning_effort") != selection.get("effort")
            or profile.get("sandbox_mode") != sandbox
        ):
            failures += 1
            print(f"[FAIL] agents/{name}.toml: default policy mismatch")
    try:
        surgeon = tomllib.loads(
            (root / "agents" / "surgeon.toml").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as err:
        failures += 1
        print(f"[FAIL] agents/surgeon.toml: invalid ({err})")
    else:
        if (
            surgeon.get("name") != "surgeon"
            or surgeon.get("model") != "gpt-5.6-sol"
            or surgeon.get("model_reasoning_effort") != "high"
            or surgeon.get("sandbox_mode") != "workspace-write"
        ):
            failures += 1
            print("[FAIL] agents/surgeon.toml: fixed experimental route mismatch")
    if failures == 0:
        print("[PASS] sample policy and fixed surgeon match packaged profiles")
    return failures


def check_release_metadata(root: Path) -> int:
    """Require every public version surface to agree with VERSION."""
    failures = 0
    try:
        raw_version = (root / "VERSION").read_text(encoding="utf-8")
    except OSError as err:
        print(f"[FAIL] VERSION: unreadable ({err})")
        return 1
    version = raw_version.strip()
    if (
        raw_version != f"{version}\n"
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version) is None
    ):
        print("[FAIL] VERSION: expected one canonical semantic version line")
        return 1

    try:
        runtime = (root / "bin" / "codex-tier.py").read_text(encoding="utf-8")
        readme = (root / "README.md").read_text(encoding="utf-8")
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        citation = (root / "CITATION.cff").read_text(encoding="utf-8")
        security = (root / "SECURITY.md").read_text(encoding="utf-8")
        releasing = (root / "docs" / "releasing.md").read_text(encoding="utf-8")
    except OSError as err:
        print(f"[FAIL] release metadata: unreadable ({err})")
        return 1

    changelog_match = re.search(
        rf"^## \[{re.escape(version)}\] - ([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})$",
        changelog,
        re.MULTILINE,
    )
    release_date = changelog_match.group(1) if changelog_match else None
    if release_date is None:
        failures += 1
        print("[FAIL] CHANGELOG.md: missing dated VERSION release heading")

    try:
        runtime_tree = ast.parse(runtime)
    except SyntaxError:
        runtime_versions = []
    else:
        runtime_versions = [
            node.value.value
            for node in runtime_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "PROJECT_VERSION"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ]
    if runtime_versions != [version]:
        failures += 1
        print("[FAIL] dispatcher runtime: PROJECT_VERSION assignment mismatch")

    exact_lines = (
        (
            readme,
            f"The current release candidate is `v{version}`. It remains a candidate until",
            "README release",
        ),
        (citation, f"version: {version}", "citation version"),
        (citation, f"date-released: {release_date}", "citation release date"),
        (
            security,
            f"line is `v{version}`. Compatibility is limited to the versions and platforms",
            "security support line",
        ),
        (
            releasing,
            f"This procedure publishes `v{version}` from a reviewed `main` commit. It keeps",
            "release procedure",
        ),
    )
    for text, expected, label in exact_lines:
        if text.splitlines().count(expected) != 1:
            failures += 1
            print(f"[FAIL] {label}: authoritative field mismatch")
    if failures == 0:
        print(f"[PASS] release metadata agrees on v{version} ({release_date})")
    return failures


def check_command(root: Path, command: list[str], label: str) -> int:
    result = subprocess.run(
        command,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        print(f"[FAIL] {label}: {detail}")
        return 1
    print(f"[PASS] {label}")
    return 0


def check_powershell_syntax(root: Path, path: Path, label: str) -> int:
    script = (
        "& { param($Path) $tokens=$null; $errors=$null; "
        "[Management.Automation.Language.Parser]::ParseFile($Path, "
        "[ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 } }"
    )
    return check_command(
        root,
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            str(path),
        ],
        label,
    )


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    failures = 0

    for relative in REQUIRED_PUBLIC_FILES:
        path = root / relative
        if path.is_file():
            print(f"[PASS] required file: {relative}")
        else:
            failures += 1
            print(f"[FAIL] required file missing: {relative}")

    failures += check_markers(
        root / "skills" / "model-routing" / "SKILL.md",
        REQUIRED_SKILL_MARKERS,
        root,
    )
    failures += check_markers(
        root / "docs" / "local-dispatcher.md",
        REQUIRED_LOCAL_GUIDE_MARKERS,
        root,
    )

    for relative, expected_hash, label in (
        (
            "schemas/local-dispatch-receipt.schema.json",
            LOCAL_SCHEMA_SHA256,
            "local receipt schema",
        ),
        (
            "schemas/local-dispatch-envelope.schema.json",
            LOCAL_ENVELOPE_SCHEMA_SHA256,
            "local envelope schema",
        ),
        (
            "schemas/local-dispatch-receipt-v3.schema.json",
            LOCAL_V3_SCHEMA_SHA256,
            "local v3 receipt schema",
        ),
        (
            "schemas/local-dispatch-envelope-v3.schema.json",
            LOCAL_V3_ENVELOPE_SCHEMA_SHA256,
            "local v3 envelope schema",
        ),
    ):
        schema_path = root / relative
        try:
            raw = schema_path.read_bytes().replace(b"\r\n", b"\n")
            json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as err:
            failures += 1
            print(f"[FAIL] {schema_path.relative_to(root)}: unreadable ({err})")
        else:
            observed = hashlib.sha256(raw).hexdigest()
            if observed != expected_hash:
                failures += 1
                print(f"[FAIL] {label} drifted from the reviewed contract")
            else:
                print(f"[PASS] {label} matches the reviewed contract")

    launcher = root / "bin" / "codex-tier"
    powershell_launcher = root / "bin" / "codex-tier.ps1"
    dispatcher = root / "bin" / "codex-tier.py"
    if not launcher.is_file() or (os.name != "nt" and not os.access(launcher, os.X_OK)):
        failures += 1
        print("[FAIL] bin/codex-tier: launcher missing or not executable")
    elif os.name != "nt":
        failures += check_command(
            root, ["/bin/sh", "-n", str(launcher)], "bin/codex-tier shell syntax"
        )
    if os.name == "nt":
        if not powershell_launcher.is_file():
            failures += 1
            print("[FAIL] bin/codex-tier.ps1: launcher missing")
        else:
            failures += check_powershell_syntax(
                root,
                powershell_launcher,
                "bin/codex-tier.ps1 PowerShell syntax",
            )
    if not dispatcher.is_file():
        failures += 1
        print("[FAIL] bin/codex-tier.py: runtime missing")
    else:
        with tempfile.TemporaryDirectory(
            prefix="codex-tier-validate-pycache-"
        ) as pycache:
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", str(dispatcher)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "PYTHONPYCACHEPREFIX": pycache,
                },
            )
        if result.returncode:
            failures += 1
            print(f"[FAIL] bin/codex-tier.py: compile failed ({result.stderr.strip()})")
        else:
            print("[PASS] bin/codex-tier.py: runtime compiles")

    if os.name == "nt":
        failures += check_powershell_syntax(
            root, root / "install.ps1", "install.ps1 PowerShell syntax"
        )
    else:
        failures += check_command(
            root, ["/bin/sh", "-n", "install.sh"], "install.sh shell syntax"
        )

    required_agent_fields = (
        "name",
        "description",
        "model",
        "model_reasoning_effort",
        "sandbox_mode",
        "developer_instructions",
    )
    names: dict[str, Path] = {}
    toml_files = sorted(root.glob("agents/*.toml")) + [root / "config.sample.toml"]
    for path in toml_files:
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as err:
            failures += 1
            print(f"[FAIL] {path.relative_to(root)}: invalid TOML ({err})")
            continue

        if path.parent.name == "agents":
            for field in required_agent_fields:
                value = data.get(field)
                if not isinstance(value, str) or not value.strip():
                    failures += 1
                    print(f"[FAIL] {path.relative_to(root)}: missing field {field!r}")
            name = data.get("name")
            if isinstance(name, str):
                if name in names:
                    failures += 1
                    print(
                        f"[FAIL] duplicate agent name {name!r}: "
                        f"{names[name].relative_to(root)} and {path.relative_to(root)}"
                    )
                else:
                    names[name] = path
        print(f"[PASS] parsed TOML: {path.relative_to(root)}")

    expected_names = {"scout", "grunt", "builder", "reviewer", "architect", "surgeon"}
    if set(names) != expected_names:
        failures += 1
        print(
            f"[FAIL] agent names: expected {sorted(expected_names)}, got {sorted(names)}"
        )
    else:
        print("[PASS] agent names match the stable and experimental route tables")

    failures += check_readme_links(root)
    failures += check_ci_contract(root)
    failures += check_default_policy_alignment(root)
    failures += check_release_metadata(root)
    failures += check_public_privacy(root)

    if failures:
        print(f"[FAIL] validation completed with {failures} issue(s)")
        return 1
    print("[PASS] repository validation complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
