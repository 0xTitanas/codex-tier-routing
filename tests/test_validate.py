from __future__ import annotations

# Project: codex-tier-routing
# Original project by: 0xTitanas
# Canonical source: https://github.com/0xTitanas/codex-tier-routing
# Copyright: 2026 0xTitanas
# SPDX-License-Identifier: MIT

import contextlib
import io
import runpy
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATE = runpy.run_path(ROOT / "scripts" / "validate.py")
RELEASE_FILES = (
    "VERSION",
    "bin/codex-tier.py",
    "README.md",
    "CHANGELOG.md",
    "CITATION.cff",
    "SECURITY.md",
    "docs/releasing.md",
)


def copy_release_files(root: Path) -> None:
    for relative in RELEASE_FILES:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / relative).read_bytes())


class ValidateTests(unittest.TestCase):
    def test_public_privacy_categories(self) -> None:
        classify = VALIDATE["public_privacy_categories"]
        unsafe_email = "person" + chr(64) + "mail" + chr(46) + "invalid"
        posix_home = chr(47).join(("", "Users", "sample", "project"))
        windows_home = chr(92).join(("C:", "Users", "sample", "project"))
        private_ip = ".".join(("192", "168", "1", "25"))
        lab_host = "linux" + "-lab"

        self.assertEqual(classify(unsafe_email), ("unapproved email address",))
        self.assertEqual(classify(posix_home), ("personal home path",))
        self.assertEqual(classify(windows_home), ("personal home path",))
        self.assertEqual(classify(private_ip), ("private network address",))
        self.assertEqual(classify(lab_host), ("local lab hostname",))

    def test_public_privacy_allows_documentation_values(self) -> None:
        classify = VALIDATE["public_privacy_categories"]
        safe = "contact 123+sample@users.noreply.github.com or user@example.com"
        invalid_ip = ".".join(("999", "168", "1", "25"))

        self.assertEqual(classify(safe), ())
        self.assertEqual(classify(invalid_ip), ())

    def test_public_privacy_scans_all_utf8_files_without_echoing_content(self) -> None:
        check = VALIDATE["check_public_privacy"]
        unsafe_email = "person" + chr(64) + "mail" + chr(46) + "invalid"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "payload.custom").write_text(unsafe_email, encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(check(root), 1)
        self.assertNotIn(unsafe_email, output.getvalue())

    def test_public_privacy_scans_bom_marked_utf16_without_echoing_content(
        self,
    ) -> None:
        check = VALIDATE["check_public_privacy"]
        unsafe_email = "person" + chr(64) + "mail" + chr(46) + "invalid"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "payload.custom").write_bytes(unsafe_email.encode("utf-16"))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(check(root), 1)
        self.assertNotIn(unsafe_email, output.getvalue())

    def test_public_privacy_scans_unmarked_utf16_and_utf32(self) -> None:
        check = VALIDATE["check_public_privacy"]
        unsafe_email = "person" + chr(64) + "mail" + chr(46) + "invalid"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "utf16.custom").write_bytes(unsafe_email.encode("utf-16-le"))
            (root / "utf32.custom").write_bytes(unsafe_email.encode("utf-32-be"))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(check(root), 2)
        self.assertNotIn(unsafe_email, output.getvalue())

    def test_public_privacy_fails_closed_on_malformed_bom_text(self) -> None:
        check = VALIDATE["check_public_privacy"]
        malformed = "sample".encode("utf-16")[:-1]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "payload.custom").write_bytes(malformed)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check(root), 1)

    def test_public_privacy_scans_lossless_single_byte_fallback(self) -> None:
        check = VALIDATE["check_public_privacy"]
        unsafe_email = "person" + chr(64) + "mail" + chr(46) + "invalid"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "payload.custom").write_bytes(unsafe_email.encode() + b"\xff")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(check(root), 1)
        self.assertNotIn(unsafe_email, output.getvalue())

    def test_public_privacy_rejects_live_and_dangling_symlinks(self) -> None:
        check = VALIDATE["check_public_privacy"]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "target").write_text("safe", encoding="utf-8")
            (root / "live-link").symlink_to("target")
            (root / "dangling-link").symlink_to("missing")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check(root), 2)

    def test_ci_contract_rejects_reduced_and_inert_workflows(self) -> None:
        check = VALIDATE["check_ci_contract"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check(ROOT), 0)
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                path = root / ".github" / "workflows" / "ci.yml"
                path.parent.mkdir(parents=True)
                path.write_text("name: CI\non: workflow_dispatch\n", encoding="utf-8")
                self.assertEqual(check(root), 1)
                path.write_text(
                    """name: inert
on: workflow_dispatch
notes: |
  push:
    tags:
      - "v*"
        if: startsWith(github.ref, 'refs/tags/v')
        run: test "$GITHUB_REF_NAME" = "v$(cat VERSION)"
        os: [ubuntu-latest, macos-latest]
        python: ["3.11", "3.13", "3.14"]
""",
                    encoding="utf-8",
                )
                self.assertEqual(check(root), 1)
                workflow = (
                    (ROOT / ".github" / "workflows" / "ci.yml")
                    .read_bytes()
                    .replace(b"\r\n", b"\n")
                )
                path.write_bytes(workflow.replace(b"\n", b"\r\n"))
                self.assertEqual(check(root), 0)

    def test_release_metadata_rejects_commented_runtime_assignment(self) -> None:
        check = VALIDATE["check_release_metadata"]
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        assignment = f'PROJECT_VERSION = "{version}"'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check(ROOT), 0)
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                copy_release_files(root)
                runtime_path = root / "bin" / "codex-tier.py"
                runtime = runtime_path.read_text(encoding="utf-8")
                runtime_path.write_text(
                    runtime.replace(assignment, f"# {assignment}", 1),
                    encoding="utf-8",
                )
                self.assertEqual(check(root), 1)

    def test_release_metadata_rejects_commented_citation_field(self) -> None:
        check = VALIDATE["check_release_metadata"]
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        field = f"version: {version}"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_release_files(root)
            citation_path = root / "CITATION.cff"
            citation = citation_path.read_text(encoding="utf-8")
            citation_path.write_text(
                citation.replace(field, f"# {field}\nnote: {field}", 1),
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check(root), 1)


if __name__ == "__main__":
    unittest.main()
