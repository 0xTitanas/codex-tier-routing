#!/usr/bin/env python3
# Project: codex-tier-routing
# Original project by: 0xTitanas
# Canonical source: https://github.com/0xTitanas/codex-tier-routing
# Copyright: 2026 0xTitanas
# SPDX-License-Identifier: MIT

from __future__ import annotations

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
    "codex-tier-routing/local-dispatch-receipt/v1",
    "dispatcher_configured_and_host_observed",
    "Cap concurrent dispatches at four",
    "Do not automatically retry",
)

REQUIRED_LOCAL_GUIDE_MARKERS = (
    "dispatcher_configured_and_host_observed",
    "host-written rollout",
    "four active",
    "no automatic retry",
    "provider-side",
)

REQUIRED_PUBLIC_FILES = (
    "README.md",
    "LICENSE",
    "NOTICE.md",
    "CITATION.cff",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "docs/compatibility.md",
    "docs/local-dispatcher.md",
    "schemas/local-dispatch-receipt.schema.json",
)

LOCAL_SCHEMA_SHA256 = "982c1e96a7f53cf4b9c157960f6af509206e52bbcfdb2b4ecf7ce560147776fb"


def canonical_json_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


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

    schema_path = root / "schemas" / "local-dispatch-receipt.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        failures += 1
        print(f"[FAIL] {schema_path.relative_to(root)}: unreadable ({err})")
    else:
        observed = canonical_json_sha256(schema)
        if observed != LOCAL_SCHEMA_SHA256:
            failures += 1
            print("[FAIL] local receipt schema drifted from the reviewed contract")
        else:
            print("[PASS] local receipt schema matches the reviewed contract")

    dispatcher = root / "bin" / "codex-tier"
    if not dispatcher.is_file() or not os.access(dispatcher, os.X_OK):
        failures += 1
        print("[FAIL] bin/codex-tier: missing or not executable")
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
            print(f"[FAIL] bin/codex-tier: compile failed ({result.stderr.strip()})")
        else:
            print("[PASS] bin/codex-tier: executable and compiles")

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

    expected_names = {"scout", "grunt", "builder", "reviewer", "architect"}
    if set(names) != expected_names:
        failures += 1
        print(
            f"[FAIL] agent names: expected {sorted(expected_names)}, got {sorted(names)}"
        )
    else:
        print("[PASS] agent names match the fixed route table")

    failures += check_readme_links(root)

    if failures:
        print(f"[FAIL] validation completed with {failures} issue(s)")
        return 1
    print("[PASS] repository validation complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
