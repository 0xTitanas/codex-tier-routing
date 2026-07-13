from __future__ import annotations

# Project: codex-tier-routing
# Original project by: 0xTitanas
# Canonical source: https://github.com/0xTitanas/codex-tier-routing
# Copyright: 2026 0xTitanas
# SPDX-License-Identifier: MIT

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        # macOS commonly presents /var as a symlink to /private/var.  The
        # installer deliberately rejects lexical symlink components, so tests
        # use the physical spelling for their controlled home boundary.
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "codex-home"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def install(
        self,
        root: Path = ROOT,
        *extra: str,
        env: dict[str, str] | None = None,
        test_build: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        run_env = os.environ.copy() if env is None else env.copy()
        installer = root / "install.sh"
        if test_build:
            run_env.setdefault("CODEX_TIER_INSTALL_TEST_SEAM", "1")
            run_env.setdefault("CODEX_TIER_INSTALL_TEST_PYTHON_BIN", sys.executable)
            source = installer.read_text(encoding="utf-8")
            self.assertEqual(source.count("INSTALL_TEST_BUILD=0"), 1)
            self.assertEqual(source.count("REPO_ROOT=$SCRIPT_DIR"), 1)
            source = source.replace("INSTALL_TEST_BUILD=0", "INSTALL_TEST_BUILD=1", 1)
            source = source.replace(
                "REPO_ROOT=$SCRIPT_DIR",
                f"REPO_ROOT={shlex.quote(str(root))}",
                1,
            )
            installer = self.base / "install-test.sh"
            installer.write_text(source, encoding="utf-8")
            installer.chmod(0o700)
        return subprocess.run(
            [
                "/bin/sh",
                str(installer),
                "--codex-home",
                str(self.home),
                *extra,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=run_env,
            check=False,
        )

    def test_dry_run_changes_nothing(self) -> None:
        result = self.install(ROOT, "--strict", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Would install", result.stdout)
        self.assertIn("Would enable strict routing", result.stdout)
        self.assertFalse(self.home.exists())

    def test_installer_ignores_untrusted_caller_path(self) -> None:
        fake_bin = self.base / "untrusted-bin"
        fake_bin.mkdir()
        marker = self.base / "path-takeover"
        fake_id = fake_bin / "id"
        fake_id.write_text(
            f"#!/bin/sh\nprintf compromised > {marker}\n",
            encoding="utf-8",
        )
        fake_id.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        result = self.install(ROOT, "--dry-run", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())

    def test_strict_install_rejects_unsafe_runtime_path_before_python(self) -> None:
        fake_bin = self.base / "unsafe-runtime-bin"
        fake_bin.mkdir(mode=0o700)
        marker = self.base / "python-takeover"
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            f"#!/bin/sh\nprintf compromised > {marker}\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        fake_bin.chmod(0o777)
        env = os.environ.copy()
        env["CODEX_TIER_PYTHON_BIN"] = str(fake_python)
        result = self.install(ROOT, "--strict", "--dry-run", env=env, test_build=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3 executable path", result.stderr)
        self.assertFalse(marker.exists())

    def test_shipped_installer_cannot_activate_test_utility_seam(self) -> None:
        fake_bin = self.base / "disabled-test-bin"
        fake_bin.mkdir()
        marker = self.base / "disabled-test-seam"
        fake_id = fake_bin / "id"
        fake_id.write_text(
            f"#!/bin/sh\nprintf compromised > {marker}\n",
            encoding="utf-8",
        )
        fake_id.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
            }
        )
        result = self.install(ROOT, "--dry-run", env=env, test_build=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())

    def test_strict_python_startup_environment_is_sanitized(self) -> None:
        startup = self.base / "startup"
        startup.mkdir()
        marker = self.base / "sitecustomize-ran"
        (startup / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(startup)
        env["PYTHONHOME"] = str(self.base / "missing-python-home")
        env["LD_PRELOAD"] = str(self.base / "missing-library.so")
        env["DYLD_INSERT_LIBRARIES"] = str(self.base / "missing-library.dylib")
        result = self.install(ROOT, "--strict", "--dry-run", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())

    def test_python_symlink_target_requires_a_safe_resolved_chain(self) -> None:
        marker = self.base / "unsafe-python-ran"
        unsafe_parent = self.base / "unsafe-python-parent"
        unsafe_parent.mkdir(mode=0o700)
        unsafe_python = unsafe_parent / "python3"
        unsafe_python.write_text(
            f"#!/bin/sh\nprintf compromised > {marker}\n",
            encoding="utf-8",
        )
        unsafe_python.chmod(0o755)
        unsafe_parent.chmod(0o777)
        safe_parent = Path.home() / ".cache" / "codex-tier-routing-tests"
        safe_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        safe_parent.chmod(0o700)
        with tempfile.TemporaryDirectory(dir=safe_parent) as safe_temp:
            safe_bin = Path(safe_temp) / "bin"
            safe_bin.mkdir(mode=0o700)
            (safe_bin / "python3").symlink_to(unsafe_python)
            env = os.environ.copy()
            env["PATH"] = f"{safe_bin}{os.pathsep}/usr/bin{os.pathsep}/bin"
            result = self.install(
                ROOT, "--strict", "--dry-run", env=env, test_build=False
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("resolved Python 3 executable path", result.stderr)
        self.assertFalse(marker.exists())

    def test_group_or_world_writable_home_halts_before_mutation(self) -> None:
        for mode in (0o770, 0o777):
            with self.subTest(mode=oct(mode)):
                shutil.rmtree(self.home, ignore_errors=True)
                self.home.mkdir(mode=0o700)
                self.home.chmod(mode)
                result = self.install()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("group/world-writable Codex home", result.stderr)
                self.assertFalse(list(self.home.glob(".codex-tier-routing-stage.*")))
                self.assertFalse(
                    list(self.home.glob(".codex-tier-routing-transaction.*"))
                )
                self.assertFalse((self.home / ".codex-tier-routing-backups").exists())
                self.assertFalse((self.home / "config.toml").exists())

    def test_dot_components_are_rejected_lexically_before_mutation(self) -> None:
        for suffix in (
            ".",
            "..",
            "./home",
            "../home",
            "home/.",
            "home/..",
            "home//.",
            "//home//..",
        ):
            with self.subTest(suffix=suffix):
                candidate = f"{self.base}/{suffix}"
                result = subprocess.run(
                    ["/bin/sh", str(ROOT / "install.sh"), "--codex-home", candidate],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must not contain . or .. components", result.stderr)
        ordinary = self.base / "a.b" / "codex.home"
        result = subprocess.run(
            [
                "sh",
                str(ROOT / "install.sh"),
                "--codex-home",
                str(ordinary),
                "--dry-run",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_absent_home_race_is_revalidated_without_residue(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                "CODEX_TIER_INSTALL_TEST_FAIL_AFTER": "home-create-race",
            }
        )
        result = self.install(env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("group/world-writable Codex home", result.stderr)
        self.assertTrue(self.home.is_dir())
        self.assertFalse(list(self.home.glob(".codex-tier-routing-*")))

    def test_live_and_dangling_symlinked_home_chain_halt(self) -> None:
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                link = self.base / ("dangling-link" if dangling else "live-link")
                if dangling:
                    link.symlink_to(self.base / "missing-target")
                else:
                    target = self.base / "real-target"
                    target.mkdir()
                    link.symlink_to(target, target_is_directory=True)
                candidate = link / "codex-home"
                result = subprocess.run(
                    [
                        "/bin/sh",
                        str(ROOT / "install.sh"),
                        "--codex-home",
                        str(candidate),
                    ],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Refusing symlinked Codex home component", result.stderr)
                self.assertFalse((candidate / ".codex-tier-routing-backups").exists())

    def test_strict_install_disables_native_spawn_and_binds_uninstall_home(
        self,
    ) -> None:
        fake_codex = self.base / "fake-codex"
        fake_codex.write_text(
            "#!/usr/bin/env python3\n"
            "import os, tomllib\n"
            "from pathlib import Path\n"
            "config = Path(os.environ['CODEX_HOME']) / 'config.toml'\n"
            "enabled = True if not config.exists() else "
            "tomllib.loads(config.read_text()).get('features', {}).get('multi_agent', True)\n"
            "print(f\"multi_agent stable {'true' if enabled else 'false'}\")\n"
        )
        fake_codex.chmod(0o755)
        env = os.environ.copy()
        env["CODEX_TIER_CODEX_BIN"] = str(fake_codex)
        result = self.install(ROOT, "--strict", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = self.home / "config.toml"
        self.assertFalse(tomllib.loads(config.read_text())["features"]["multi_agent"])
        self.assertTrue((self.home / "tier-routing" / "strict-config.json").is_file())
        self.assertIn(f"'CODEX_HOME={self.home}'", result.stdout)
        uninstall = result.stdout.strip().splitlines()[-1]
        self.assertIn(" -i ", uninstall)
        startup = self.base / "uninstall-startup"
        startup.mkdir()
        marker = self.base / "uninstall-sitecustomize-ran"
        (startup / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        uninstall_env = env.copy()
        uninstall_env["PYTHONPATH"] = str(startup)
        uninstall_env["PYTHONHOME"] = str(self.base / "missing-uninstall-home")
        removed = subprocess.run(
            ["/bin/sh", "-c", uninstall],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=uninstall_env,
            check=False,
        )
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse(config.exists())
        self.assertFalse((self.home / "bin" / "codex-tier").exists())
        self.assertFalse((self.home / "bin" / "codex-tier.py").exists())

    def test_installs_complete_bundle_and_probe_runs(self) -> None:
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / "skills" / "model-routing" / "SKILL.md").is_file())
        self.assertEqual(len(list((self.home / "agents").glob("*.toml"))), 5)
        dispatcher = self.home / "bin" / "codex-tier"
        runtime = self.home / "bin" / "codex-tier.py"
        self.assertTrue(dispatcher.is_file())
        self.assertTrue(os.access(dispatcher, os.X_OK))
        self.assertTrue(runtime.is_file())
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o600)
        self.assertIn(str(dispatcher), result.stdout)
        self.assertIn(str(runtime), result.stdout)
        fake_codex = self.base / "fake codex"
        fake_codex.write_text(
            "#!/bin/sh\n"
            'if [ "$1 $2" = "features list" ]; then\n'
            "  printf 'multi_agent stable false\\n'\n"
            "else\n"
            "  printf 'codex-cli test\\n'\n"
            "fi\n"
        )
        fake_codex.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {"CODEX_HOME": str(self.home), "CODEX_TIER_CODEX_BIN": str(fake_codex)}
        )
        probe = subprocess.run(
            [str(dispatcher), "probe"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn('"status": "ready"', probe.stdout)

    def test_install_upgrade_and_uninstall_preserve_custom_policy(self) -> None:
        state = self.home / "tier-routing"
        state.mkdir(parents=True, mode=0o700)
        state.chmod(0o700)
        policy = state / "policy.toml"
        original = (ROOT / "policy.sample.toml").read_bytes()
        policy.write_bytes(original)
        policy.chmod(0o600)

        first = self.install()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(policy.read_bytes(), original)
        second = self.install()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(policy.read_bytes(), original)

        uninstall = second.stdout.strip().splitlines()[-1]
        removed = subprocess.run(
            ["/bin/sh", "-c", uninstall],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertEqual(policy.read_bytes(), original)

    def test_new_internal_directories_are_private_under_permissive_umask(self) -> None:
        env = os.environ.copy()
        result = subprocess.run(
            [
                "sh",
                "-c",
                'umask 000; exec sh "$1" --codex-home "$2"',
                "sh",
                str(ROOT / "install.sh"),
                str(self.home),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for directory in (
            self.home / "agents",
            self.home / "skills",
            self.home / "bin",
        ):
            with self.subTest(directory=directory.name):
                self.assertEqual(directory.stat().st_mode & 0o077, 0)
        for directory in (self.home / "skills" / "model-routing",):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for profile in (self.home / "agents").glob("*.toml"):
            self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
        for skill_file in (self.home / "skills" / "model-routing").rglob("*"):
            expected = 0o700 if skill_file.is_dir() else 0o600
            self.assertEqual(skill_file.stat().st_mode & 0o777, expected)
        self.assertEqual(
            (self.home / "bin" / "codex-tier").stat().st_mode & 0o777, 0o700
        )
        self.assertEqual(
            (self.home / "bin" / "codex-tier.py").stat().st_mode & 0o777,
            0o600,
        )

    def test_skill_source_symlink_or_special_node_halts_before_staging(self) -> None:
        for label in ("symlink", "fifo"):
            with self.subTest(label=label):
                source = self.base / f"source-{label}"
                shutil.copytree(ROOT, source)
                node = source / "skills" / "model-routing" / f"unsafe-{label}"
                if label == "symlink":
                    node.symlink_to(source / "README.md")
                else:
                    os.mkfifo(node)
                result = self.install(source)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("symlink or special node", result.stderr)
                self.assertFalse(list(self.home.glob(".codex-tier-routing-stage.*")))

    def test_unsafe_existing_internal_boundary_or_leaf_halts_before_staging(
        self,
    ) -> None:
        for relative, is_directory in (
            (Path("agents"), True),
            (Path("skills"), True),
            (Path("bin"), True),
            (Path("agents/scout.toml"), False),
        ):
            with self.subTest(relative=relative):
                shutil.rmtree(self.home, ignore_errors=True)
                self.home.mkdir(mode=0o700)
                target = self.home / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if is_directory:
                    target.mkdir(exist_ok=True)
                else:
                    target.write_text("preserve-me")
                target.chmod(0o777 if is_directory else 0o666)
                result = self.install()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("group/world-writable", result.stderr)
                self.assertFalse(list(self.home.glob(".codex-tier-routing-stage.*")))
                self.assertFalse(
                    list(self.home.glob(".codex-tier-routing-transaction.*"))
                )
                self.assertFalse((self.home / ".codex-tier-routing-backups").exists())

    def test_codex_home_with_spaces_installs_and_quotes_uninstall(self) -> None:
        self.home = self.base / "Codex Home With Spaces"
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        dispatcher = self.home / "bin" / "codex-tier"
        self.assertTrue(dispatcher.is_file())
        self.assertIn(f"'{dispatcher}'", result.stdout)
        uninstall = result.stdout.strip().splitlines()[-1]
        removed = subprocess.run(
            ["sh", "-c", uninstall],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertFalse(dispatcher.exists())

    def test_collision_is_backed_up_before_replacement(self) -> None:
        dispatcher = self.home / "bin" / "codex-tier"
        dispatcher.parent.mkdir(parents=True)
        dispatcher.write_text("old-dispatcher")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = list(
            (self.home / ".codex-tier-routing-backups").glob(
                "*/snapshot/bin/codex-tier"
            )
        )
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "old-dispatcher")
        self.assertTrue(dispatcher.read_text().startswith("#!/bin/sh"))
        self.assertEqual(
            (self.home / ".codex-tier-routing-backups").stat().st_mode & 0o077,
            0,
        )

    def test_leaf_replacement_objects_halt_before_any_replacement(self) -> None:
        targets = {
            "agent": self.home / "agents" / "scout.toml",
            "skill": self.home / "skills" / "model-routing",
            "dispatcher": self.home / "bin" / "codex-tier",
        }
        for label, target in targets.items():
            with self.subTest(label=label):
                shutil.rmtree(self.home, ignore_errors=True)
                target.parent.mkdir(parents=True)
                if label == "skill":
                    target.mkdir()
                    (target / "old").write_text("preserve-me")
                else:
                    target.write_text("preserve-me")
                fake_bin = self.base / f"fake-{label}"
                fake_bin.mkdir()
                (fake_bin / "uname").write_text("#!/bin/sh\nprintf 'Linux\\n'\n")
                (fake_bin / "id").write_text("#!/bin/sh\nprintf '4242\\n'\n")
                (fake_bin / "stat").write_text(
                    "#!/bin/sh\n"
                    "case \"$2\" in %a) printf '700\\n' ;; %u) "
                    f"case \"$3\" in {target!s}) printf '9999\\n' ;; *) printf '4242\\n' ;; esac ;; esac\n"
                )
                for tool in fake_bin.iterdir():
                    tool.chmod(0o755)
                env = os.environ.copy()
                env.update(
                    {
                        "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                        "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
                    }
                )
                result = self.install(env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "Refusing cross-owner replacement destination", result.stderr
                )
                self.assertFalse((self.home / ".codex-tier-routing-backups").exists())
                self.assertFalse(
                    (self.home / ".codex-tier-routing-transaction").exists()
                )
                if label == "skill":
                    self.assertEqual((target / "old").read_text(), "preserve-me")
                else:
                    self.assertEqual(target.read_text(), "preserve-me")

    def test_unknown_owner_and_leaf_symlinks_halt_without_replacement(self) -> None:
        target = self.home / "bin" / "codex-tier"
        for label in ("unknown-owner", "live-symlink", "dangling-symlink"):
            with self.subTest(label=label):
                shutil.rmtree(self.home, ignore_errors=True)
                target.parent.mkdir(parents=True)
                if label == "unknown-owner":
                    target.write_text("preserve-me")
                    fake_bin = self.base / f"unknown-{label}"
                    fake_bin.mkdir()
                    (fake_bin / "uname").write_text("#!/bin/sh\nprintf 'Linux\\n'\n")
                    (fake_bin / "id").write_text("#!/bin/sh\nprintf '4242\\n'\n")
                    (fake_bin / "stat").write_text(
                        "#!/bin/sh\ncase \"$2\" in %a) printf '700\\n' ;; %u) "
                        '[ "$3" = \''
                        + str(target)
                        + "' ] && exit 0; printf '4242\\n' ;; esac\n"
                    )
                    for tool in fake_bin.iterdir():
                        tool.chmod(0o755)
                    env = os.environ.copy()
                    env.update(
                        {
                            "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                            "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
                        }
                    )
                else:
                    outside = self.base / f"outside-{label}"
                    if label == "live-symlink":
                        outside.write_text("outside-bytes")
                    target.symlink_to(outside)
                    env = None
                result = self.install(env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.home / ".codex-tier-routing-backups").exists())
                if label == "unknown-owner":
                    self.assertIn(
                        "Unable to determine numeric ownership", result.stderr
                    )
                    self.assertEqual(target.read_text(), "preserve-me")
                else:
                    self.assertIn(
                        "Refusing symlinked replacement destination", result.stderr
                    )
                    self.assertTrue(target.is_symlink())

    def test_fault_injection_restores_bundle_after_each_replacement_and_strict_failure(
        self,
    ) -> None:
        def populate_old_bundle() -> dict[Path, bytes]:
            old: dict[Path, bytes] = {}
            for name in (
                "scout.toml",
                "grunt.toml",
                "builder.toml",
                "reviewer.toml",
                "architect.toml",
            ):
                path = self.home / "agents" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                value = f"old-{name}".encode()
                path.write_bytes(value)
                old[path] = value
            skill_file = self.home / "skills" / "model-routing" / "old.txt"
            skill_file.parent.mkdir(parents=True, exist_ok=True)
            skill_file.write_bytes(b"old-skill")
            old[skill_file] = b"old-skill"
            dispatcher = self.home / "bin" / "codex-tier"
            dispatcher.parent.mkdir(parents=True, exist_ok=True)
            dispatcher.write_bytes(b"old-dispatcher")
            old[dispatcher] = b"old-dispatcher"
            return old

        for point in [
            *[str(number) for number in range(1, 8)],
            "signal-1",
            "pre-move-1",
            "move-failure-1",
            "signal-pre-move-1",
            "post-move-1",
            "signal-post-move-1",
            "pre-move-6",
            "signal-post-move-6",
            "prepromotion-race",
            "backup-promotion",
        ]:
            with self.subTest(point=point):
                shutil.rmtree(self.home, ignore_errors=True)
                old = populate_old_bundle()
                env = os.environ.copy()
                env.update(
                    {
                        "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                        "CODEX_TIER_INSTALL_TEST_FAIL_AFTER": point,
                    }
                )
                result = self.install(ROOT, env=env)
                self.assertNotEqual(result.returncode, 0)
                for path, expected in old.items():
                    self.assertEqual(path.read_bytes(), expected)
                self.assertFalse(list(self.home.glob(".codex-tier-routing-stage.*")))
                self.assertFalse(
                    list(self.home.glob(".codex-tier-routing-transaction.*"))
                )
                self.assertFalse((self.home / ".codex-tier-routing-backups").exists())

        shutil.rmtree(self.home, ignore_errors=True)
        old = populate_old_bundle()
        failing_codex = self.base / "strict-enable-fails"
        failing_codex.write_text("#!/bin/sh\nprintf 'multi_agent stable true\\n'\n")
        failing_codex.chmod(0o755)
        env = os.environ.copy()
        env["CODEX_TIER_CODEX_BIN"] = str(failing_codex)
        result = self.install(ROOT, "--strict", env=env)
        self.assertNotEqual(result.returncode, 0)
        for path, expected in old.items():
            self.assertEqual(path.read_bytes(), expected)
        self.assertFalse(list(self.home.glob(".codex-tier-routing-stage.*")))
        self.assertFalse(list(self.home.glob(".codex-tier-routing-transaction.*")))
        self.assertFalse((self.home / ".codex-tier-routing-backups").exists())
        self.assertFalse((self.home / "tier-routing").exists())

        shutil.rmtree(self.home, ignore_errors=True)
        old = populate_old_bundle()
        config = self.home / "config.toml"
        original_config = b"[features]\nmulti_agent = true\n"
        config.write_bytes(original_config)
        working_codex = self.base / "strict-enable-succeeds"
        working_codex.write_text(
            "#!/usr/bin/env python3\nimport os, tomllib\nfrom pathlib import Path\nconfig = Path(os.environ['CODEX_HOME']) / 'config.toml'\nenabled = True if not config.exists() else tomllib.loads(config.read_text()).get('features', {}).get('multi_agent', True)\nprint(f\"multi_agent stable {'true' if enabled else 'false'}\")\n"
        )
        working_codex.chmod(0o755)
        for point in ("backup-promotion", "strict-enable-succeeded"):
            with self.subTest(strict_success_failure=point):
                shutil.rmtree(self.home, ignore_errors=True)
                old = populate_old_bundle()
                config = self.home / "config.toml"
                config.write_bytes(original_config)
                env = os.environ.copy()
                env.update(
                    {
                        "CODEX_TIER_CODEX_BIN": str(working_codex),
                        "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                        "CODEX_TIER_INSTALL_TEST_FAIL_AFTER": point,
                    }
                )
                result = self.install(ROOT, "--strict", env=env)
                self.assertNotEqual(result.returncode, 0)
                for path, expected in old.items():
                    self.assertEqual(path.read_bytes(), expected)
                self.assertEqual(config.read_bytes(), original_config)
                self.assertFalse(list(self.home.glob(".codex-tier-routing-stage.*")))
                self.assertFalse(
                    list(self.home.glob(".codex-tier-routing-transaction.*"))
                )
                self.assertFalse((self.home / ".codex-tier-routing-backups").exists())
                self.assertFalse((self.home / "tier-routing").exists())

    def test_codex_home_defaults_only_when_absent(self) -> None:
        default = self.base / "default-home" / ".codex"
        env = os.environ.copy()
        env.pop("CODEX_HOME", None)
        env["HOME"] = str(self.base / "default-home")
        result = subprocess.run(
            ["/bin/sh", str(ROOT / "install.sh"), "--dry-run"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Codex home: {default}", result.stdout)
        for supplied, environment in (
            (("--codex-home", ""), None),
            ((), {"CODEX_HOME": ""}),
            (("--codex-home", "~/.codex"), None),
            ((), {"CODEX_HOME": "."}),
            (("--codex-home", "relative"), None),
        ):
            with self.subTest(supplied=supplied, environment=environment):
                candidate_env = os.environ.copy()
                candidate_env["HOME"] = str(self.base / "must-not-exist")
                if environment:
                    candidate_env.update(environment)
                else:
                    candidate_env.pop("CODEX_HOME", None)
                result = subprocess.run(
                    ["/bin/sh", str(ROOT / "install.sh"), *supplied],
                    cwd=self.base,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=candidate_env,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Codex home must be an absolute path", result.stderr)
                self.assertFalse((self.base / "must-not-exist").exists())

    def test_backup_leaf_symlinks_and_reservation_race_do_not_clobber(self) -> None:
        fake_bin = self.base / "date-bin"
        fake_bin.mkdir()
        (fake_bin / "date").write_text("#!/bin/sh\nprintf '20990101T000000'\n")
        (fake_bin / "date").chmod(0o755)
        for kind in ("live", "dangling", "race"):
            with self.subTest(kind=kind):
                shutil.rmtree(self.home, ignore_errors=True)
                dispatcher = self.home / "bin" / "codex-tier"
                dispatcher.parent.mkdir(parents=True)
                dispatcher.write_bytes(b"original")
                leaf = self.home / ".codex-tier-routing-backups" / "20990101T000000"
                env = os.environ.copy()
                env.update(
                    {
                        "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                        "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
                    }
                )
                if kind == "live":
                    leaf.mkdir(parents=True)
                    (leaf / "unrelated").write_bytes(b"keep")
                elif kind == "dangling":
                    leaf.parent.mkdir(parents=True)
                    leaf.symlink_to(self.base / "missing")
                else:
                    env.update(
                        {
                            "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                            "CODEX_TIER_INSTALL_TEST_FAIL_AFTER": "reservation-race",
                        }
                    )
                result = self.install(env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                if kind == "live":
                    self.assertEqual((leaf / "unrelated").read_bytes(), b"keep")
                if kind == "dangling":
                    self.assertTrue(leaf.is_symlink())
                self.assertEqual(dispatcher.read_bytes().splitlines()[0], b"#!/bin/sh")
                backups = list(
                    (self.home / ".codex-tier-routing-backups").glob(
                        "*/snapshot/bin/codex-tier"
                    )
                )
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), b"original")

    def test_bounded_backup_reservation_exhaustion_restores_original(self) -> None:
        fake_bin = self.base / "bounded-date-bin"
        fake_bin.mkdir()
        (fake_bin / "date").write_text("#!/bin/sh\nprintf '20990101T000000'\n")
        (fake_bin / "date").chmod(0o755)
        dispatcher = self.home / "bin" / "codex-tier"
        dispatcher.parent.mkdir(parents=True)
        dispatcher.write_bytes(b"original")
        backup_parent = self.home / ".codex-tier-routing-backups"
        backup_parent.mkdir()
        leaves = [backup_parent / "20990101T000000"] + [
            backup_parent / f"20990101T000000_{index}" for index in range(1, 100)
        ]
        for leaf in leaves:
            leaf.mkdir()
            (leaf / "sentinel").write_bytes(b"unrelated")
        env = os.environ.copy()
        env.update(
            {
                "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
            }
        )
        result = self.install(env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bounded backup directory", result.stderr)
        self.assertEqual(dispatcher.read_bytes(), b"original")
        for leaf in leaves:
            self.assertEqual((leaf / "sentinel").read_bytes(), b"unrelated")
        self.assertFalse(list(self.home.glob(".codex-tier-routing-stage.*")))
        self.assertFalse(list(self.home.glob(".codex-tier-routing-transaction.*")))

    def test_device_mismatch_halts_and_restores_original(self) -> None:
        dispatcher = self.home / "bin" / "codex-tier"
        dispatcher.parent.mkdir(parents=True)
        dispatcher.write_bytes(b"original")
        fake_bin = self.base / "device-bin"
        fake_bin.mkdir()
        (fake_bin / "uname").write_text("#!/bin/sh\nprintf 'Linux\\n'\n")
        (fake_bin / "id").write_text("#!/bin/sh\nprintf '4242\\n'\n")
        (fake_bin / "stat").write_text(
            "#!/bin/sh\ncase \"$2\" in %u) printf '4242\\n' ;; "
            "%a) case \"$3\" in */agents/*.toml|*/bin/codex-tier.py) printf '600\\n' ;; *) printf '700\\n' ;; esac ;; "
            "%d) case \"$3\" in *backups) printf '2\\n' ;; *) printf '1\\n' ;; esac ;; esac\n"
        )
        for tool in fake_bin.iterdir():
            tool.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
            }
        )
        result = self.install(env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different devices", result.stderr)
        self.assertEqual(dispatcher.read_bytes(), b"original")

    def test_darwin_and_linux_device_and_permission_probes_are_numeric(self) -> None:
        for platform, flag, permission_flag in (
            ("Darwin", "-f", "%Mp%Lp"),
            ("Linux", "-c", "%a"),
        ):
            with self.subTest(platform=platform):
                shutil.rmtree(self.home, ignore_errors=True)
                dispatcher = self.home / "bin" / "codex-tier"
                dispatcher.parent.mkdir(parents=True)
                dispatcher.write_bytes(b"original")
                fake_bin = self.base / f"device-{platform}"
                fake_bin.mkdir()
                log = self.base / f"device-{platform}.log"
                (fake_bin / "uname").write_text(f"#!/bin/sh\nprintf '{platform}\\n'\n")
                (fake_bin / "id").write_text("#!/bin/sh\nprintf '4242\\n'\n")
                (fake_bin / "stat").write_text(
                    f'#!/bin/sh\nprintf \'%s %s\\n\' "$1" "$2" >> {log!s}\n'
                    "case \"$2\" in %a|%Mp%Lp) case \"$3\" in */agents/*.toml|*/bin/codex-tier.py) printf '600\\n' ;; *) printf '700\\n' ;; esac ;; "
                    "*) printf '4242\\n' ;; esac\n"
                )
                if platform == "Darwin":
                    (fake_bin / "ls").write_text("#!/bin/sh\nexit 0\n")
                    (fake_bin / "chmod").write_text(
                        '#!/bin/sh\n[ "$1" = "-RN" ] && exit 0\nexec /bin/chmod "$@"\n'
                    )
                for tool in fake_bin.iterdir():
                    tool.chmod(0o755)
                env = os.environ.copy()
                env.update(
                    {
                        "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                        "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
                    }
                )
                result = self.install(env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"{flag} %u", log.read_text())
                self.assertIn(f"{flag} %d", log.read_text())
                self.assertIn(f"{flag} {permission_flag}", log.read_text())

    def test_platform_stat_numeric_failures_halt(self) -> None:
        for platform, flag, mode_flag in (
            ("Darwin", "-f", "%Mp%Lp"),
            ("Linux", "-c", "%a"),
        ):
            for bad_field, expected in (
                ("%u", "numeric ownership"),
                (mode_flag, "numeric permissions"),
            ):
                with self.subTest(platform=platform, bad_field=bad_field):
                    shutil.rmtree(self.home, ignore_errors=True)
                    fake_bin = (
                        self.base / f"numeric-{platform}-{bad_field.replace('%', '')}"
                    )
                    fake_bin.mkdir()
                    (fake_bin / "uname").write_text(
                        f"#!/bin/sh\nprintf '{platform}\\n'\n"
                    )
                    (fake_bin / "id").write_text("#!/bin/sh\nprintf '4242\\n'\n")
                    (fake_bin / "stat").write_text(
                        "#!/bin/sh\n"
                        f"case \"$1:$2\" in {flag}:{bad_field}) printf 'not-numeric\\n' ;; "
                        f"{flag}:%u) printf '4242\\n' ;; {flag}:{mode_flag}) printf '700\\n' ;; {flag}:%d) printf '1\\n' ;; *) exit 99 ;; esac\n"
                    )
                    if platform == "Darwin":
                        (fake_bin / "ls").write_text("#!/bin/sh\nexit 0\n")
                    for tool in fake_bin.iterdir():
                        tool.chmod(0o755)
                    env = os.environ.copy()
                    env.update(
                        {
                            "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                            "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
                        }
                    )
                    result = self.install(ROOT, "--dry-run", env=env)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected, result.stderr)

    def test_unsafe_existing_backup_parent_halts_and_restores_every_original(
        self,
    ) -> None:
        for platform, permission_flag in (("Darwin", "%Mp%Lp"), ("Linux", "%a")):
            with self.subTest(platform=platform):
                shutil.rmtree(self.home, ignore_errors=True)
                originals: dict[Path, bytes] = {}
                for name in (
                    "scout.toml",
                    "grunt.toml",
                    "builder.toml",
                    "reviewer.toml",
                    "architect.toml",
                ):
                    path = self.home / "agents" / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    originals[path] = f"old-{name}".encode()
                    path.write_bytes(originals[path])
                skill = self.home / "skills" / "model-routing" / "old"
                skill.parent.mkdir(parents=True)
                originals[skill] = b"old-skill"
                skill.write_bytes(originals[skill])
                dispatcher = self.home / "bin" / "codex-tier"
                dispatcher.parent.mkdir(parents=True)
                originals[dispatcher] = b"old-dispatcher"
                dispatcher.write_bytes(originals[dispatcher])
                backup_parent = self.home / ".codex-tier-routing-backups"
                backup_parent.mkdir()
                unrelated = backup_parent / "unrelated"
                unrelated.write_bytes(b"keep")
                fake_bin = self.base / f"unsafe-backup-{platform}"
                fake_bin.mkdir()
                (fake_bin / "uname").write_text(f"#!/bin/sh\nprintf '{platform}\\n'\n")
                (fake_bin / "id").write_text("#!/bin/sh\nprintf '4242\\n'\n")
                (fake_bin / "stat").write_text(
                    "#!/bin/sh\n"
                    'case "$2" in\n'
                    "%u) printf '4242\\n' ;;\n"
                    f"{permission_flag}) case \"$3\" in *codex-tier-routing-backups) printf '777\\n' ;; *) printf '700\\n' ;; esac ;;\n"
                    "%d) printf '1\\n' ;;\n"
                    "*) exit 99 ;;\n"
                    "esac\n"
                )
                if platform == "Darwin":
                    (fake_bin / "ls").write_text("#!/bin/sh\nexit 0\n")
                for tool in fake_bin.iterdir():
                    tool.chmod(0o755)
                env = os.environ.copy()
                env.update(
                    {
                        "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                        "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
                    }
                )
                result = self.install(env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("group/world-writable", result.stderr)
                self.assertIn(str(backup_parent), result.stderr)
                for path, expected in originals.items():
                    self.assertEqual(path.read_bytes(), expected)
                self.assertEqual(unrelated.read_bytes(), b"keep")
                self.assertFalse(list(self.home.glob(".codex-tier-routing-stage.*")))
                self.assertFalse(
                    list(self.home.glob(".codex-tier-routing-transaction.*"))
                )

    def test_cross_owner_nested_destination_halts_before_replacement(self) -> None:
        self.home.mkdir()
        nested = self.home / "agents"
        nested.mkdir()
        installed = nested / "scout.toml"
        installed.write_text("preserve-me")
        fake_bin = self.base / "fake-bin"
        fake_bin.mkdir()
        (fake_bin / "uname").write_text("#!/bin/sh\nprintf 'Linux\\n'\n")
        (fake_bin / "id").write_text("#!/bin/sh\nprintf '4242\\n'\n")
        (fake_bin / "stat").write_text(
            "#!/bin/sh\n"
            "case \"$2\" in %a) printf '700\\n' ;; %u) "
            f"case \"$3\" in {nested!s}) printf '9999\\n' ;; *) printf '4242\\n' ;; esac ;; esac\n"
        )
        for tool in fake_bin.iterdir():
            tool.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
            }
        )
        result = self.install(env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing cross-owner install boundary", result.stderr)
        self.assertEqual(installed.read_text(), "preserve-me")
        self.assertFalse((self.home / ".codex-tier-routing-backups").exists())

    def test_unreadable_dispatcher_source_fails_before_replacement(self) -> None:
        checkout = self.base / "checkout"
        for relative in ("agents", "skills", "bin"):
            shutil.copytree(ROOT / relative, checkout / relative)
        shutil.copy2(ROOT / "install.sh", checkout / "install.sh")
        installed = self.home / "agents" / "scout.toml"
        installed.parent.mkdir(parents=True)
        installed.write_text("preserve-me")
        source = checkout / "bin" / "codex-tier"
        source.chmod(0)
        try:
            result = self.install(checkout)
        finally:
            source.chmod(0o755)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(installed.read_text(), "preserve-me")
        self.assertFalse((self.home / ".codex-tier-routing-backups").exists())

    def test_symlinked_destination_boundary_fails_before_replacement(self) -> None:
        self.home.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        (self.home / "agents").symlink_to(outside, target_is_directory=True)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinked install boundary", result.stderr)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.home / ".codex-tier-routing-backups").exists())

    def test_unknown_install_boundary_owner_halts(self) -> None:
        fake_bin = self.base / "fake-bin"
        fake_bin.mkdir()
        stat_log = self.base / "stat-log"
        fake_uname = fake_bin / "uname"
        fake_uname.write_text("#!/bin/sh\nprintf 'Linux\\n'\n")
        fake_uname.chmod(0o755)
        fake_stat = fake_bin / "stat"
        fake_stat.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$1\" > {stat_log!s}\n"
            '[ "$1" = "-c" ] || exit 99\n'
            "exit 0\n"
        )
        fake_stat.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
            }
        )
        result = self.install(env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Unable to determine numeric ownership for Codex home component",
            result.stderr,
        )
        self.assertEqual(stat_log.read_text(), "-c\n")
        self.assertFalse(self.home.exists())

    def test_safe_sticky_root_parent_passes_on_both_platform_branches(self) -> None:
        for platform, flag, mode_flag in (
            ("Darwin", "-f", "%Mp%Lp"),
            ("Linux", "-c", "%a"),
        ):
            with self.subTest(platform=platform):
                parent = self.base / f"sticky-{platform}"
                parent.mkdir(mode=0o700)
                parent.chmod(0o1777)
                self.home = parent / "codex-home"
                self.home.mkdir(mode=0o700)
                fake_bin = self.base / f"sticky-bin-{platform}"
                fake_bin.mkdir()
                (fake_bin / "uname").write_text(f"#!/bin/sh\nprintf '{platform}\\n'\n")
                (fake_bin / "id").write_text("#!/bin/sh\nprintf '4242\\n'\n")
                (fake_bin / "stat").write_text(
                    '#!/bin/sh\ncase "$1:$2" in\n'
                    f"{flag}:%u) case \"$3\" in {parent!s}) printf '0\\n' ;; *) printf '4242\\n' ;; esac ;;\n"
                    f"{flag}:{mode_flag}) case \"$3\" in {parent!s}) printf '1777\\n' ;; *) printf '700\\n' ;; esac ;;\n"
                    f"{flag}:%d) printf '1\\n' ;; *) exit 99 ;; esac\n"
                )
                if platform == "Darwin":
                    (fake_bin / "ls").write_text("#!/bin/sh\nexit 0\n")
                for tool in fake_bin.iterdir():
                    tool.chmod(0o755)
                env = os.environ.copy()
                env.update(
                    {
                        "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                        "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
                    }
                )
                result = self.install(ROOT, "--dry-run", env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unsafe_or_foreign_ancestor_halts_before_staging(self) -> None:
        for label, uid, mode, expected in (
            ("non-sticky", "4242", "0777", "non-sticky group/world-writable"),
            ("foreign", "9999", "0700", "foreign-owned"),
        ):
            with self.subTest(label=label):
                parent = self.base / f"ancestor-{label}"
                parent.mkdir()
                self.home = parent / "codex-home"
                fake_bin = self.base / f"ancestor-bin-{label}"
                fake_bin.mkdir()
                (fake_bin / "uname").write_text("#!/bin/sh\nprintf 'Linux\\n'\n")
                (fake_bin / "id").write_text("#!/bin/sh\nprintf '4242\\n'\n")
                (fake_bin / "stat").write_text(
                    '#!/bin/sh\ncase "$2" in %u) case "$3" in '
                    f"{parent!s}) printf '{uid}\\n' ;; *) printf '4242\\n' ;; esac ;; "
                    f"%a) case \"$3\" in {parent!s}) printf '{mode}\\n' ;; *) printf '700\\n' ;; esac ;; %d) printf '1\\n' ;; *) exit 99 ;; esac\n"
                )
                for tool in fake_bin.iterdir():
                    tool.chmod(0o755)
                env = os.environ.copy()
                env.update(
                    {
                        "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                        "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
                    }
                )
                result = self.install(env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
                self.assertFalse(list(parent.glob("codex-home/.codex-tier-routing-*")))

    def test_darwin_acl_allow_denial_and_probe_failure_are_fail_closed(self) -> None:
        for index, (acl, expected) in enumerate(
            (
                ("0: everyone deny delete", None),
                ("0: everyone allow read", "ACL allow"),
                (None, "Unable to inspect ACLs"),
            )
        ):
            with self.subTest(acl=acl):
                fake_bin = self.base / f"acl-{index}"
                fake_bin.mkdir()
                (fake_bin / "uname").write_text("#!/bin/sh\nprintf 'Darwin\\n'\n")
                (fake_bin / "id").write_text("#!/bin/sh\nprintf '4242\\n'\n")
                (fake_bin / "stat").write_text(
                    "#!/bin/sh\ncase \"$2\" in %u) printf '4242\\n' ;; %Mp%Lp) printf '700\\n' ;; %d) printf '1\\n' ;; *) exit 99 ;; esac\n"
                )
                ls_body = "exit 1" if acl is None else f"printf '%s\\n' '{acl}'"
                (fake_bin / "ls").write_text(f"#!/bin/sh\n{ls_body}\n")
                for tool in fake_bin.iterdir():
                    tool.chmod(0o755)
                env = os.environ.copy()
                env.update(
                    {
                        "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                        "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
                    }
                )
                result = self.install(ROOT, "--dry-run", env=env)
                if expected is None:
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                else:
                    self.assertNotEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertIn(expected, result.stderr)

    def test_linux_owner_probe_uses_gnu_stat_syntax(self) -> None:
        fake_bin = self.base / "fake-bin"
        fake_bin.mkdir()
        stat_log = self.base / "stat-log"
        fake_id = fake_bin / "id"
        fake_id.write_text("#!/bin/sh\nprintf '4242\\n'\n")
        fake_id.chmod(0o755)
        fake_uname = fake_bin / "uname"
        fake_uname.write_text("#!/bin/sh\nprintf 'Linux\\n'\n")
        fake_uname.chmod(0o755)
        fake_stat = fake_bin / "stat"
        fake_stat.write_text(
            "#!/bin/sh\n"
            f'printf \'%s %s\\n\' "$1" "$2" >> {stat_log!s}\n'
            '[ "$1" = "-c" ] || exit 99\n'
            "case \"$2\" in %a) printf '700\\n' ;; *) printf '4242\\n' ;; esac\n"
        )
        fake_stat.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
            }
        )
        result = self.install(ROOT, "--dry-run", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-c %u\n", stat_log.read_text())

    def test_darwin_owner_probe_uses_bsd_stat_syntax(self) -> None:
        fake_bin = self.base / "fake-bin"
        fake_bin.mkdir()
        stat_log = self.base / "stat-log"
        fake_id = fake_bin / "id"
        fake_id.write_text("#!/bin/sh\nprintf '4242\\n'\n")
        fake_id.chmod(0o755)
        fake_uname = fake_bin / "uname"
        fake_uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n")
        fake_uname.chmod(0o755)
        fake_stat = fake_bin / "stat"
        fake_stat.write_text(
            "#!/bin/sh\n"
            f'printf \'%s %s\\n\' "$1" "$2" >> {stat_log!s}\n'
            '[ "$1" = "-f" ] || exit 99\n'
            "case \"$2\" in %Mp%Lp) printf '700\\n' ;; *) printf '4242\\n' ;; esac\n"
        )
        fake_stat.chmod(0o755)
        fake_ls = fake_bin / "ls"
        fake_ls.write_text("#!/bin/sh\nexit 0\n")
        fake_ls.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
            }
        )
        result = self.install(ROOT, "--dry-run", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-f %u\n", stat_log.read_text())

    @unittest.skipUnless(sys.platform == "darwin", "live Darwin stat boundary")
    def test_live_darwin_sticky_tmp_boundary_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            candidate = Path(temporary) / "codex home"
            result = subprocess.run(
                [
                    "sh",
                    str(ROOT / "install.sh"),
                    "--codex-home",
                    str(candidate),
                    "--dry-run",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Dry-run complete", result.stdout)

    def test_unsupported_platform_halts_before_install(self) -> None:
        fake_bin = self.base / "fake-bin"
        fake_bin.mkdir()
        fake_uname = fake_bin / "uname"
        fake_uname.write_text("#!/bin/sh\nprintf 'UnsupportedOS\\n'\n")
        fake_uname.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "CODEX_TIER_INSTALL_TEST_SEAM": "1",
                "CODEX_TIER_INSTALL_TEST_PATH": str(fake_bin),
            }
        )
        result = self.install(env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unable to determine supported platform", result.stderr)
        self.assertFalse(self.home.exists())

    def test_relative_codex_home_halts_without_looping(self) -> None:
        result = subprocess.run(
            ["/bin/sh", str(ROOT / "install.sh"), "--codex-home", "relative-home"],
            cwd=self.base,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=3,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Codex home must be an absolute path", result.stderr)
        self.assertFalse((self.base / "relative-home").exists())


if __name__ == "__main__":
    unittest.main()
