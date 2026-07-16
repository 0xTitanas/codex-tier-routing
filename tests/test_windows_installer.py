from __future__ import annotations

# Project: codex-tier-routing
# Original project by: 0xTitanas
# Canonical source: https://github.com/0xTitanas/codex-tier-routing
# Copyright: 2026 0xTitanas
# SPDX-License-Identifier: MIT

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

if os.name == "nt":
    import winreg

ROOT = Path(__file__).resolve().parents[1]
AGENTS = {
    "architect.toml",
    "builder.toml",
    "grunt.toml",
    "reviewer.toml",
    "scout.toml",
    "surgeon.toml",
}


@unittest.skipUnless(os.name == "nt", "native Windows installer regression")
class WindowsInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.base = Path(self.temp.name)
        self.home = self.base / "codex-home"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            try:
                self.user_path, self.user_path_type = winreg.QueryValueEx(key, "Path")
                self.user_path_existed = True
            except FileNotFoundError:
                self.user_path = ""
                self.user_path_type = winreg.REG_EXPAND_SZ
                self.user_path_existed = False

    def tearDown(self) -> None:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if self.user_path_existed:
                winreg.SetValueEx(key, "Path", 0, self.user_path_type, self.user_path)
            else:
                try:
                    winreg.DeleteValue(key, "Path")
                except FileNotFoundError:
                    pass
        self.temp.cleanup()

    def install(
        self,
        *arguments: str,
        root: Path = ROOT,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(root / "install.ps1"),
                "-CodexHome",
                str(self.home),
                *arguments,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy() if env is None else env,
            check=False,
        )

    def set_user_path(self, value: str, kind: int) -> None:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "Path", 0, kind, value)

    def user_path_value(self) -> tuple[str, int]:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return winreg.QueryValueEx(key, "Path")

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.home)
        env["CODEX_TIER_PYTHON_BIN"] = sys.executable
        env["PATH"] = str(self.home / "bin") + os.pathsep + env["PATH"]
        return subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "codex-tier",
                *arguments,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )

    def fault_build(self) -> Path:
        copied = self.base / "fault-build"
        shutil.copytree(ROOT / "agents", copied / "agents")
        shutil.copytree(ROOT / "skills", copied / "skills")
        shutil.copytree(ROOT / "bin", copied / "bin")
        source = (ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("$INSTALL_TEST_BUILD = $false", source)
        (copied / "install.ps1").write_text(
            source.replace(
                "$INSTALL_TEST_BUILD = $false",
                "$INSTALL_TEST_BUILD = $true",
                1,
            ),
            encoding="utf-8",
        )
        return copied

    def installed_bytes(self) -> dict[str, bytes]:
        paths = [self.home / "agents" / name for name in sorted(AGENTS)]
        paths += [
            self.home / "bin" / "codex-tier.ps1",
            self.home / "bin" / "codex-tier.py",
        ]
        return {str(path.relative_to(self.home)): path.read_bytes() for path in paths}

    def seed_legacy_launchers(self) -> dict[str, bytes]:
        legacy = {
            "codex-tier.cmd": b"@echo legacy command launcher\r\n",
            "codex-tier-launcher.ps1": b"Write-Output 'legacy PowerShell launcher'\r\n",
        }
        for name, content in legacy.items():
            (self.home / "bin" / name).write_bytes(content)
        return legacy

    def fake_codex(self) -> Path:
        from pip._vendor.distlib.scripts import ScriptMaker

        maker = ScriptMaker(None, str(self.base))
        maker.executable = sys.executable
        maker.script_template = 'print("multi_agent stable false")\n'
        maker.variants = {""}
        maker.clobber = True
        return Path(maker.make("fake-codex = fixture:main")[0])

    def test_fresh_install_command_routes_upgrade_backup_and_custom_profile(
        self,
    ) -> None:
        dry = self.install("-DryRun")
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertFalse(self.home.exists())

        first = self.install()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(
            {path.name for path in (self.home / "agents").glob("*.toml")},
            AGENTS,
        )
        self.assertTrue((self.home / "skills" / "model-routing" / "SKILL.md").is_file())
        version = self.command("--version")
        self.assertEqual(version.returncode, 0, version.stdout + version.stderr)
        self.assertEqual(version.stdout.strip(), "codex-tier 1.1.0-rc.1")
        policy = self.command("policy-check")
        self.assertEqual(policy.returncode, 0, policy.stdout + policy.stderr)
        payload = json.loads(policy.stdout)
        self.assertEqual(payload["status"], "pass")
        self.assertEqual(
            set(payload["routes"]),
            {name.removesuffix(".toml") for name in AGENTS - {"surgeon.toml"}},
        )
        self.assertEqual(
            payload["experimental_routes"]["surgeon"]["model"], "gpt-5.6-sol"
        )

        custom = self.home / "agents" / "custom.toml"
        custom.write_text("name = 'custom'\n", encoding="utf-8")
        runtime = self.home / "bin" / "codex-tier.py"
        runtime.write_text("old runtime\n", encoding="utf-8")
        upgrade = self.install()
        self.assertEqual(upgrade.returncode, 0, upgrade.stdout + upgrade.stderr)
        self.assertTrue(custom.is_file())
        backups = list(
            (self.home / ".codex-tier-routing-backups").glob(
                "*/snapshot/bin/codex-tier.py"
            )
        )
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "old runtime\n")

    def test_faults_restore_every_original_including_move_gap(self) -> None:
        first = self.install()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        custom = self.home / "agents" / "custom.toml"
        custom.write_text("custom\n", encoding="utf-8")
        expected = self.installed_bytes()
        fault_root = self.fault_build()

        for point in ("after-original-1", "after-replacement-5", "backup-promotion"):
            with self.subTest(point=point):
                env = os.environ.copy()
                env["CODEX_TIER_INSTALL_TEST_FAIL_POINT"] = point
                result = self.install(root=fault_root, env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.installed_bytes(), expected)
                self.assertEqual(custom.read_text(encoding="utf-8"), "custom\n")
                self.assertFalse(
                    list(self.home.glob(".codex-tier-routing-transaction.*"))
                )

    def test_upgrade_removes_and_backs_up_legacy_launchers(self) -> None:
        first = self.install()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        legacy = self.seed_legacy_launchers()

        dry = self.install("-DryRun")
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        for name in legacy:
            self.assertIn(
                f"Would remove legacy launcher: {self.home / 'bin' / name}",
                dry.stdout,
            )

        upgrade = self.install()
        self.assertEqual(upgrade.returncode, 0, upgrade.stdout + upgrade.stderr)
        for name, content in legacy.items():
            self.assertFalse((self.home / "bin" / name).exists())
            backups = list(
                (self.home / ".codex-tier-routing-backups").glob(
                    f"*/snapshot/bin/{name}"
                )
            )
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), content)

    def test_rollback_restores_legacy_launchers(self) -> None:
        first = self.install()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        legacy = self.seed_legacy_launchers()
        fault_root = self.fault_build()
        for point in ("after-legacy-1", "backup-promotion"):
            with self.subTest(point=point):
                env = os.environ.copy()
                env["CODEX_TIER_INSTALL_TEST_FAIL_POINT"] = point
                failed = self.install(root=fault_root, env=env)
                self.assertNotEqual(failed.returncode, 0)
                for name, content in legacy.items():
                    self.assertEqual((self.home / "bin" / name).read_bytes(), content)
                self.assertFalse(
                    list(self.home.glob(".codex-tier-routing-transaction.*"))
                )

    def test_expandable_user_path_kind_survives_install_and_rollback(self) -> None:
        original = r"%USERPROFILE%\codex-tier-expandable-fixture"
        self.set_user_path(original, winreg.REG_EXPAND_SZ)

        first = self.install()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        installed_path, installed_kind = self.user_path_value()
        self.assertEqual(installed_kind, winreg.REG_EXPAND_SZ)
        self.assertEqual(installed_path.split(";")[0], original)
        self.assertIn(str(self.home / "bin"), installed_path.split(";"))

        self.set_user_path(original, winreg.REG_EXPAND_SZ)
        fault_root = self.fault_build()
        env = os.environ.copy()
        env["CODEX_TIER_INSTALL_TEST_FAIL_POINT"] = "backup-promotion"
        failed = self.install(root=fault_root, env=env)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(
            self.user_path_value(),
            (original, winreg.REG_EXPAND_SZ),
        )

    def test_strict_failure_restores_config_and_bundle(self) -> None:
        first = self.install()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        expected = self.installed_bytes()
        fault_root = self.fault_build()
        env = os.environ.copy()
        env["CODEX_TIER_INSTALL_TEST_FAIL_POINT"] = "strict-enable-succeeded"
        env["CODEX_TIER_PYTHON_BIN"] = sys.executable
        fake_codex = self.fake_codex()
        env["CODEX_TIER_CODEX_BIN"] = str(fake_codex)
        result = self.install("-Strict", root=fault_root, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "injected installer failure at strict-enable-succeeded",
            result.stdout + result.stderr,
        )
        self.assertEqual(self.installed_bytes(), expected)
        self.assertFalse((self.home / "tier-routing" / "strict-config.json").exists())
        self.assertFalse((self.home / "tier-routing" / "strict-config.backup").exists())

    def test_strict_install_succeeds_with_fake_codex(self) -> None:
        fake_codex = self.fake_codex()
        env = os.environ.copy()
        env["CODEX_TIER_PYTHON_BIN"] = sys.executable
        env["CODEX_TIER_CODEX_BIN"] = str(fake_codex)
        result = self.install("-Strict", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.home / "tier-routing" / "strict-config.json").is_file())

    def test_arbitrary_untrusted_acl_and_reparse_points_are_rejected(self) -> None:
        self.home.mkdir()
        unsafe = self.home / "bin"
        unsafe.mkdir()
        acl = subprocess.run(
            ["icacls.exe", str(unsafe), "/grant", "*S-1-5-4:(OI)(CI)(M)"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(acl.returncode, 0, acl.stdout + acl.stderr)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stdout + result.stderr, "owner|write|ACL")

        shutil.rmtree(self.home)
        target = self.base / "target"
        target.mkdir()
        junction = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(self.home), str(target)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(junction.returncode, 0, junction.stdout + junction.stderr)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reparse", (result.stdout + result.stderr).lower())


if __name__ == "__main__":
    unittest.main()
