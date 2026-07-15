from __future__ import annotations

# Project: codex-tier-routing
# Original project by: 0xTitanas
# Canonical source: https://github.com/0xTitanas/codex-tier-routing
# Copyright: 2026 0xTitanas
# SPDX-License-Identifier: MIT

import ctypes
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = ROOT / "bin" / "codex-tier.py"
DISPATCHER_SPEC = importlib.util.spec_from_loader(
    "codex_tier_windows",
    importlib.machinery.SourceFileLoader("codex_tier_windows", str(DISPATCHER)),
)
assert DISPATCHER_SPEC is not None and DISPATCHER_SPEC.loader is not None
CODEX_TIER = importlib.util.module_from_spec(DISPATCHER_SPEC)
sys.modules[DISPATCHER_SPEC.name] = CODEX_TIER
DISPATCHER_SPEC.loader.exec_module(CODEX_TIER)

FAKE_CODEX = r"""
import json
import os
import sys
import tomllib
from pathlib import Path

home = Path(os.environ["CODEX_HOME"])
mode = json.loads((home / "tier-routing" / "fake-codex-mode.json").read_text())
if sys.argv[-2:] == ["features", "list"]:
    enabled = True
    config = home / "config.toml"
    if config.exists():
        enabled = tomllib.loads(config.read_text()).get("features", {}).get("multi_agent", True)
    if "features.multi_agent=false" in sys.argv:
        enabled = False
    print(f"multi_agent stable {'true' if enabled else 'false'}")
    raise SystemExit(0)
if "--version" in sys.argv:
    print("codex-cli 0.test")
    raise SystemExit(0)

args = sys.argv[1:]
def after(flag):
    return args[args.index(flag) + 1]

model = after("-m")
effort = next(value.split('"')[1] for index, value in enumerate(args) if index and args[index - 1] == "-c" and value.startswith("model_reasoning_effort="))
sandbox = after("-s")
cwd = str(Path(after("-C")).resolve())
thread = mode["thread_id"]
session_dir = home / "sessions" / "2026" / "07" / "14"
session_dir.mkdir(parents=True, exist_ok=True)
windows_sandbox = next(
    (
        value.split('"')[1]
        for index, value in enumerate(args)
        if index
        and args[index - 1] == "-c"
        and value.startswith("windows.sandbox=")
    ),
    None,
)
sandbox_policy = {
    "type": (
        "read-only"
        if sandbox == "workspace-write"
        and windows_sandbox not in {"elevated", "unelevated"}
        else sandbox
    )
}
if sandbox_policy["type"] == "workspace-write":
    sandbox_policy.update({
        "network_access": False,
        "exclude_slash_tmp": False,
        "exclude_tmpdir_env_var": False,
    })
events = [
    {
        "type": "session_meta",
        "payload": {
            "session_id": thread,
            "id": thread,
            "cwd": cwd,
            "originator": "codex_exec",
            "cli_version": "0.test",
            "source": "exec",
            "model_provider": "openai",
        },
    },
    {
        "type": "turn_context",
        "payload": {
            "cwd": cwd,
            "approval_policy": "never",
            "sandbox_policy": sandbox_policy,
            "model": model,
            "effort": effort,
        },
    },
]
rollout = session_dir / f"rollout-test-{thread}.jsonl"
rollout.write_text("".join(json.dumps(event) + "\n" for event in events))
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": thread}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "FAKE_OK"}}))
print(json.dumps({"type": "turn.completed"}))
"""


@unittest.skipUnless(os.name == "nt", "native Windows regression")
class WindowsRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.base = Path(self.temp.name).resolve()
        self.local_app_data = self.base / "local-app-data"
        self.local_app_data.mkdir()
        CODEX_TIER.ensure_private_dir(self.local_app_data)
        environment = mock.patch.dict(
            os.environ, {"LOCALAPPDATA": str(self.local_app_data)}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.home = self.base / "codex-home"
        self.home.mkdir()
        CODEX_TIER.ensure_private_dir(self.home)
        self.workdir = self.base / "work"
        self.workdir.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fake_codex(self, directory: Path) -> Path:
        from pip._vendor.distlib.scripts import ScriptMaker

        maker = ScriptMaker(None, str(directory))
        maker.executable = sys.executable
        maker.script_template = FAKE_CODEX
        maker.variants = {""}
        maker.clobber = True
        executable = Path(maker.make("codex = fixture:main")[0])
        CODEX_TIER._windows_secure_private(executable)
        return executable

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def linked_worktree(self, object_format: str = "sha1") -> tuple[Path, Path]:
        suffix = uuid.uuid4().hex
        primary = self.base / f"primary-{suffix}"
        linked = self.base / f"linked-{suffix}"
        self.git("init", "-q", f"--object-format={object_format}", str(primary))
        (primary / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self.git("-C", str(primary), "add", "tracked.txt")
        self.git(
            "-C",
            str(primary),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=fixture",
            "-c",
            "core.hooksPath=NUL",
            "commit",
            "-qm",
            "initial",
        )
        self.git(
            "-C",
            str(primary),
            "worktree",
            "add",
            "-q",
            "-b",
            f"surgeon-{suffix}",
            str(linked),
        )
        return primary, linked

    def run_launcher(
        self, launcher: Path, *arguments: str, **kwargs: object
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
                str(launcher),
                *arguments,
            ],
            **kwargs,
        )

    def test_private_acl_rejects_inherited_write_and_junctions(self) -> None:
        unsafe = self.base / "unsafe"
        unsafe.mkdir()
        result = subprocess.run(
            ["icacls.exe", str(unsafe), "/grant", "*S-1-1-0:(OI)(CI)(M)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        child = unsafe / "child"
        child.mkdir()
        with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "ACL|write"):
            CODEX_TIER.validate_executable_chain(
                child, label="unsafe inherited path", require_directory=True
            )
        unsafe_junction = self.base / "unsafe-junction"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(unsafe_junction), str(unsafe)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "ACL|write"):
            CODEX_TIER.validate_executable_chain(
                unsafe_junction,
                label="unsafe junction path",
                require_directory=True,
            )

        target = self.base / "junction-target"
        target.mkdir()
        junction = self.base / "junction"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(
            CODEX_TIER.paths_same(
                CODEX_TIER.validate_executable_chain(
                    junction, label="junction path", require_directory=True
                ),
                target,
            )
        )
        with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "reparse"):
            CODEX_TIER.resolve_workdir(str(junction))

    def test_dangerous_unsupported_ace_types_fail_closed(self) -> None:
        for ace_type in (4, 0x7F):
            with self.subTest(ace_type=ace_type):
                raw = ctypes.create_string_buffer(12)
                header = CODEX_TIER._AceHeader.from_buffer(raw)
                header.type = ace_type
                header.size = len(raw)
                ctypes.c_uint32.from_buffer(
                    raw, 4
                ).value = CODEX_TIER._WINDOWS_WRITE_RIGHTS
                with self.assertRaisesRegex(
                    CODEX_TIER.DispatchHalt, "unverifiable Windows ACE"
                ):
                    CODEX_TIER._windows_validate_acl_ace(
                        ctypes.addressof(raw),
                        label="unsupported ACE fixture",
                        dangerous=CODEX_TIER._WINDOWS_WRITE_RIGHTS,
                        trusted=set(),
                    )

    def test_atomic_write_and_case_insensitive_path_overlap(self) -> None:
        state = self.home / "tier-routing"
        CODEX_TIER.ensure_private_dir(state)
        target = state / "state.json"
        CODEX_TIER.atomic_write(target, b'{"ok":true}\n', 0o600)
        self.assertEqual(target.read_bytes(), b'{"ok":true}\n')
        CODEX_TIER.current_owner_regular(target, label="atomic state")
        self.assertTrue(
            CODEX_TIER.paths_overlap(
                Path(str(self.workdir).swapcase()), self.workdir / "nested"
            )
        )
        self.assertFalse(CODEX_TIER.paths_overlap(self.home, self.workdir))

    def test_windows_sandbox_mode_defaults_safely_and_preserves_elevated(self) -> None:
        self.assertEqual(CODEX_TIER.windows_sandbox_mode(self.home), "unelevated")

        config = self.home / "config.toml"
        config.write_text('[windows]\nsandbox = "elevated"\n', encoding="utf-8")
        CODEX_TIER._windows_secure_private(config)
        self.assertEqual(CODEX_TIER.windows_sandbox_mode(self.home), "elevated")

        config.write_text('[windows]\nsandbox = "invalid"\n', encoding="utf-8")
        CODEX_TIER._windows_secure_private(config)
        with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "Windows sandbox"):
            CODEX_TIER.windows_sandbox_mode(self.home)

    def test_two_process_cross_home_writer_lock_collision_and_cleanup(self) -> None:
        other_home = self.base / "other-codex-home"
        other_home.mkdir()
        CODEX_TIER.ensure_private_dir(other_home)
        self.assertEqual(
            CODEX_TIER.WorkspaceWriterLock(other_home, self.workdir)._path,
            CODEX_TIER.WorkspaceWriterLock(self.home, self.workdir)._path,
        )
        child_code = textwrap.dedent(
            f"""
            import importlib.machinery, importlib.util, pathlib, sys, time
            path = pathlib.Path({str(DISPATCHER)!r})
            spec = importlib.util.spec_from_loader('lock_child', importlib.machinery.SourceFileLoader('lock_child', str(path)))
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            with module.WorkspaceWriterLock(pathlib.Path({str(other_home)!r}), pathlib.Path({str(self.workdir)!r})):
                print('LOCKED', flush=True)
                time.sleep(30)
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", child_code],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), "LOCKED")
            with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "already held"):
                with CODEX_TIER.WorkspaceWriterLock(self.home, self.workdir):
                    pass
        finally:
            process.kill()
            process.communicate(timeout=10)
        with CODEX_TIER.WorkspaceWriterLock(self.home, self.workdir):
            pass

    def test_strict_disable_preserves_host_added_configuration(self) -> None:
        config = self.home / "config.toml"
        with (
            mock.patch.object(CODEX_TIER, "resolve_codex", return_value=Path("codex")),
            mock.patch.object(
                CODEX_TIER, "native_multi_agent_enabled", return_value=False
            ),
        ):
            self.assertEqual(CODEX_TIER.strict_enable(mock.Mock(), self.home), 0)
        addition = b'\n[projects."C:\\\\synthetic"]\ntrust_level = "trusted"\n'
        config.write_bytes(config.read_bytes() + addition)
        self.assertEqual(CODEX_TIER.strict_disable(mock.Mock(), self.home), 0)
        self.assertEqual(config.read_bytes(), addition)
        self.assertFalse((self.home / "tier-routing" / "strict-config.json").exists())
        self.assertFalse((self.home / "tier-routing" / "strict-config.backup").exists())

    def test_concurrent_ledger_writers_preserve_chain(self) -> None:
        child_code = textwrap.dedent(
            f"""
            import importlib.machinery, importlib.util, pathlib, sys
            path = pathlib.Path({str(DISPATCHER)!r})
            spec = importlib.util.spec_from_loader('ledger_child', importlib.machinery.SourceFileLoader('ledger_child', str(path)))
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            home = pathlib.Path({str(self.home)!r})
            for index in range(10):
                module.ledger_append(home, {{'writer': sys.argv[1], 'index': index}})
            """
        )
        processes = [
            subprocess.Popen([sys.executable, "-I", "-c", child_code, str(index)])
            for index in range(3)
        ]
        returncodes = [process.wait(timeout=30) for process in processes]
        self.assertEqual(returncodes, [0, 0, 0])
        ledger = self.home / "tier-routing" / "ledger.jsonl"
        with ledger.open("r", encoding="utf-8") as handle:
            self.assertIsNotNone(CODEX_TIER.validate_ledger_lines(handle))
        self.assertEqual(len(ledger.read_text(encoding="utf-8").splitlines()), 30)

    def test_timeout_terminates_windows_process_tree(self) -> None:
        marker = self.base / "grandchild-survived"
        child_code = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c',"
            + repr(
                "import pathlib,time; time.sleep(2); "
                f"pathlib.Path({str(marker)!r}).write_text('survived')"
            )
            + "]); time.sleep(30)"
        )
        with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "timed out"):
            CODEX_TIER.run_process_bounded(
                [sys.executable, "-I", "-c", child_code],
                input_bytes=b"",
                cwd=self.workdir,
                env=os.environ.copy(),
                timeout=1,
                stdout_limit=1024,
                stderr_limit=1024,
            )
        time.sleep(2.5)
        self.assertFalse(marker.exists())

    def test_executable_discovery_rejects_untrusted_acl_and_owner(self) -> None:
        executable = self.base / "fake-codex.exe"
        shutil.copy2(sys.executable, executable)
        CODEX_TIER._windows_secure_private(executable)
        with mock.patch.dict(os.environ, {"CODEX_TIER_CODEX_BIN": str(executable)}):
            self.assertTrue(
                CODEX_TIER.paths_same(CODEX_TIER.resolve_codex().entrypoint, executable)
            )

        for name, content, message in (
            ("fake-codex.cmd", b"@echo off\r\n", "native .exe"),
            ("fake-codex", b"#!/usr/bin/env python\n", "native .exe"),
            ("fake-codex.ps1", b"Write-Output fake\r\n", "native .exe"),
            ("renamed-shebang.exe", b"#!/usr/bin/env python\n", "native PE"),
            ("not-pe.exe", b"not a native executable\n", "native PE"),
            (
                "mz-not-pe.exe",
                b"MZ" + b"\0" * 58 + (64).to_bytes(4, "little") + b"NOPE",
                "native PE",
            ),
        ):
            with self.subTest(name=name):
                candidate = self.base / name
                candidate.write_bytes(content)
                CODEX_TIER._windows_secure_private(candidate)
                with (
                    mock.patch.dict(
                        os.environ, {"CODEX_TIER_CODEX_BIN": str(candidate)}
                    ),
                    self.assertRaisesRegex(CODEX_TIER.DispatchHalt, message),
                ):
                    CODEX_TIER.resolve_codex()

        result = subprocess.run(
            ["icacls.exe", str(executable), "/grant", "*S-1-5-4:(M)"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with (
            mock.patch.dict(os.environ, {"CODEX_TIER_CODEX_BIN": str(executable)}),
            self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "write access"),
        ):
            CODEX_TIER.resolve_codex()

        foreign_owner = self.base / "foreign-owner.exe"
        shutil.copy2(sys.executable, foreign_owner)
        CODEX_TIER._windows_secure_private(foreign_owner)
        result = subprocess.run(
            ["icacls.exe", str(foreign_owner), "/setowner", "*S-1-5-19"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with (
            mock.patch.dict(os.environ, {"CODEX_TIER_CODEX_BIN": str(foreign_owner)}),
            self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "untrusted Windows owner"),
        ):
            CODEX_TIER.resolve_codex()

    def test_native_launcher_treats_metacharacters_as_arguments(self) -> None:
        installed = self.base / "bootstrap-bin"
        installed.mkdir()
        CODEX_TIER.ensure_private_dir(installed)
        for name in ("codex-tier.ps1", "codex-tier.py"):
            target = installed / name
            shutil.copy2(ROOT / "bin" / name, target)
            CODEX_TIER._windows_secure_private(target)
        marker = self.base / "must-not-exist"
        env = os.environ.copy()
        env.update(
            {
                "CODEX_TIER_PYTHON_BIN": sys.executable,
            }
        )
        result = self.run_launcher(
            installed / "codex-tier.ps1",
            "policy-check",
            f"; New-Item -ItemType File -Path '{marker}'",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(marker.exists(), result.stdout + result.stderr)

    def test_all_routes_dispatch_and_receipt_check_through_native_launcher(
        self,
    ) -> None:
        installed = self.base / "installed-bin"
        installed.mkdir()
        CODEX_TIER.ensure_private_dir(installed)
        for name in (
            "codex-tier.ps1",
            "codex-tier.py",
        ):
            target = installed / name
            shutil.copy2(ROOT / "bin" / name, target)
            CODEX_TIER._windows_secure_private(target)

        config = self.home / "config.toml"
        config.write_text("[features]\nmulti_agent = false\n", encoding="utf-8")
        CODEX_TIER._windows_secure_private(config)
        mode_dir = self.home / "tier-routing"
        CODEX_TIER.ensure_private_dir(mode_dir)

        fake_dir = self.base / "fake-bin"
        fake_dir.mkdir()
        CODEX_TIER.ensure_private_dir(fake_dir)
        fake_command = self.fake_codex(fake_dir)

        env = os.environ.copy()
        env.update(
            {
                "CODEX_HOME": str(self.home),
                "CODEX_TIER_CODEX_BIN": str(fake_command),
                "CODEX_TIER_PYTHON_BIN": sys.executable,
            }
        )
        launcher = installed / "codex-tier.ps1"
        _, surgeon_workdir = self.linked_worktree()
        expected = {**CODEX_TIER.ROUTES, "surgeon": CODEX_TIER.SURGEON_ROUTE}
        envelopes = {}
        for route, selection in expected.items():
            with self.subTest(route=route):
                mode = mode_dir / "fake-codex-mode.json"
                mode.write_text(
                    json.dumps({"thread_id": str(uuid.uuid4())}), encoding="utf-8"
                )
                CODEX_TIER._windows_secure_private(mode)
                result = self.run_launcher(
                    launcher,
                    "dispatch",
                    "--route",
                    route,
                    "--workdir",
                    str(surgeon_workdir if route == "surgeon" else self.workdir),
                    "--timeout",
                    "20",
                    input="bounded native Windows fixture",
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                envelope = json.loads(result.stdout)
                envelopes[route] = envelope
                receipt = envelope["receipt"]
                self.assertEqual(receipt["status"], "pass")
                self.assertEqual(
                    receipt["claim_class"], "dispatcher_configured_and_host_observed"
                )
                self.assertTrue(envelope["receipt_ref"].startswith("local-ledger://"))
                for key, value in (
                    ("model", selection.model),
                    ("effort", selection.effort),
                    ("sandbox", selection.sandbox),
                    ("approval_policy", "never"),
                ):
                    self.assertEqual(receipt["requested"][key], value)
                    self.assertEqual(receipt["observed"][key], value)
                checked = self.run_launcher(
                    launcher,
                    "receipt-check",
                    input=json.dumps(envelope),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

        if "scout" not in envelopes:
            return
        tampered = json.loads(json.dumps(envelopes["scout"]))
        tampered["receipt"]["observed"]["model"] = "gpt-5.6-luna"
        checked = self.run_launcher(
            launcher,
            "receipt-check",
            input=json.dumps(tampered),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=20,
            check=False,
        )
        self.assertEqual(checked.returncode, 2)
        self.assertEqual(json.loads(checked.stdout)["status"], "halt")

    def test_surgeon_git_sha1_sha256_and_fail_closed_boundaries(self) -> None:
        for object_format, length in (("sha1", 40), ("sha256", 64)):
            with self.subTest(object_format=object_format):
                _, linked = self.linked_worktree(object_format)
                observed = CODEX_TIER.surgeon_workspace_precondition(linked, self.home)
                self.assertEqual(len(observed["starting_head"]), length)

        _, dirty = self.linked_worktree()
        (dirty / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "must be clean"):
            CODEX_TIER.surgeon_workspace_precondition(dirty, self.home)

        _, filtered = self.linked_worktree()
        (filtered / ".gitattributes").write_text(
            "tracked.txt filter=unsafe\n", encoding="utf-8"
        )
        self.git("-C", str(filtered), "add", ".gitattributes")
        self.git(
            "-C",
            str(filtered),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=fixture",
            "-c",
            "core.hooksPath=NUL",
            "commit",
            "-qm",
            "attributes",
        )
        with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "Git filter"):
            CODEX_TIER.surgeon_workspace_precondition(filtered, self.home)

        _, submodule = self.linked_worktree()
        head = self.git("-C", str(submodule), "rev-parse", "HEAD")
        self.git(
            "-C",
            str(submodule),
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{head},nested-repository",
        )
        self.git(
            "-C",
            str(submodule),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=fixture",
            "-c",
            "core.hooksPath=NUL",
            "commit",
            "-qm",
            "gitlink",
        )
        with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "submodules"):
            CODEX_TIER.surgeon_workspace_precondition(submodule, self.home)


if __name__ == "__main__":
    unittest.main()
