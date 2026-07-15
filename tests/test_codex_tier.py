from __future__ import annotations

# Project: codex-tier-routing
# Original project by: 0xTitanas
# Canonical source: https://github.com/0xTitanas/codex-tier-routing
# Copyright: 2026 0xTitanas
# SPDX-License-Identifier: MIT

import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import tomllib
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin" / "codex-tier"
DISPATCHER = ROOT / "bin" / "codex-tier.py"
DISPATCHER_SPEC = importlib.util.spec_from_loader(
    "codex_tier", importlib.machinery.SourceFileLoader("codex_tier", str(DISPATCHER))
)
assert DISPATCHER_SPEC is not None and DISPATCHER_SPEC.loader is not None
CODEX_TIER = importlib.util.module_from_spec(DISPATCHER_SPEC)
sys.modules[DISPATCHER_SPEC.name] = CODEX_TIER
DISPATCHER_SPEC.loader.exec_module(CODEX_TIER)
VALIDATOR = ROOT / "scripts" / "validate.py"
VALIDATOR_SPEC = importlib.util.spec_from_loader(
    "codex_tier_validate",
    importlib.machinery.SourceFileLoader("codex_tier_validate", str(VALIDATOR)),
)
assert VALIDATOR_SPEC is not None and VALIDATOR_SPEC.loader is not None
CODEX_TIER_VALIDATE = importlib.util.module_from_spec(VALIDATOR_SPEC)
sys.modules[VALIDATOR_SPEC.name] = CODEX_TIER_VALIDATE
VALIDATOR_SPEC.loader.exec_module(CODEX_TIER_VALIDATE)

RFC3339_DATETIME = re.compile(
    r"^(?P<year>[0-9]{4})-(?P<month>0[1-9]|1[0-2])-(?P<day>0[1-9]|[12][0-9]|3[01])"
    r"[Tt](?P<hour>[01][0-9]|2[0-3]):(?P<minute>[0-5][0-9]):(?P<second>[0-5][0-9])"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)
RFC4122_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def is_rfc3339_datetime(value: str) -> bool:
    match = RFC3339_DATETIME.fullmatch(value)
    if match is None:
        return False
    year, month, day = (int(match[name]) for name in ("year", "month", "day"))
    month_days = (
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    )
    return day <= month_days[month - 1]


def is_canonical_rfc4122_uuid(value: str) -> bool:
    return RFC4122_UUID.fullmatch(value) is not None and str(uuid.UUID(value)) == value


def validate_local_schema(
    instance: object, schema: dict, root: dict | None = None
) -> list[str]:
    """Small, dependency-free validator for every keyword in this local schema."""
    root = schema if root is None else root
    if "$ref" in schema:
        reference = schema["$ref"]
        assert reference.startswith("#/$defs/")
        return validate_local_schema(
            instance, root["$defs"][reference.rsplit("/", 1)[1]], root
        )
    errors: list[str] = []
    expected_type = schema.get("type")
    types = expected_type if isinstance(expected_type, list) else [expected_type]
    if expected_type is not None:
        valid_types = {
            "object": lambda value: isinstance(value, dict),
            "array": lambda value: isinstance(value, list),
            "string": lambda value: isinstance(value, str),
            "boolean": lambda value: isinstance(value, bool),
            "null": lambda value: value is None,
        }
        if not any(valid_types[name](instance) for name in types):
            return ["type"]
    if "const" in schema and instance != schema["const"]:
        errors.append("const")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append("enum")
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            errors.append("minLength")
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            errors.append("pattern")
        if schema.get("format") == "uuid":
            if not is_canonical_rfc4122_uuid(instance):
                errors.append("format uuid")
        if schema.get("format") == "date-time":
            if not is_rfc3339_datetime(instance):
                errors.append("format date-time")
    if isinstance(instance, list) and len(instance) > schema.get(
        "maxItems", sys.maxsize
    ):
        errors.append("maxItems")
    if isinstance(instance, dict):
        required = schema.get("required", [])
        errors.extend(f"required {key}" for key in required if key not in instance)
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            errors.extend(
                f"additional {key}" for key in instance if key not in properties
            )
        for key, value in instance.items():
            if key in properties:
                errors.extend(validate_local_schema(value, properties[key], root))
    if (
        "oneOf" in schema
        and sum(
            not validate_local_schema(instance, option, root)
            for option in schema["oneOf"]
        )
        != 1
    ):
        errors.append("oneOf")
    for condition in schema.get("allOf", []):
        if "if" in condition:
            if not validate_local_schema(instance, condition["if"], root):
                errors.extend(validate_local_schema(instance, condition["then"], root))
        else:
            errors.extend(validate_local_schema(instance, condition, root))
    return errors


def ordinary_draft202012_valid(
    instance: object, schema: dict, root: dict | None = None
) -> bool:
    """Independent, format-disabled Draft 2020-12 subset for this contract."""
    root = schema if root is None else root
    if "$ref" in schema:
        return ordinary_draft202012_valid(
            instance, root["$defs"][schema["$ref"].rsplit("/", 1)[1]], root
        )
    expected_type = schema.get("type")
    if expected_type is not None:
        types = expected_type if isinstance(expected_type, list) else [expected_type]
        type_checks = {
            "object": lambda value: isinstance(value, dict),
            "array": lambda value: isinstance(value, list),
            "string": lambda value: isinstance(value, str),
            "boolean": lambda value: isinstance(value, bool),
            "null": lambda value: value is None,
        }
        if not any(type_checks[name](instance) for name in types):
            return False
    if "const" in schema and instance != schema["const"]:
        return False
    if "enum" in schema and instance not in schema["enum"]:
        return False
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            return False
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            return False
    if isinstance(instance, list) and len(instance) > schema.get(
        "maxItems", sys.maxsize
    ):
        return False
    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        if any(key not in instance for key in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in instance
        ):
            return False
        if any(
            not ordinary_draft202012_valid(value, properties[key], root)
            for key, value in instance.items()
            if key in properties
        ):
            return False
    if (
        "oneOf" in schema
        and sum(
            ordinary_draft202012_valid(instance, option, root)
            for option in schema["oneOf"]
        )
        != 1
    ):
        return False
    for condition in schema.get("allOf", []):
        if "if" in condition:
            if ordinary_draft202012_valid(
                instance, condition["if"], root
            ) and not ordinary_draft202012_valid(instance, condition["then"], root):
                return False
        elif not ordinary_draft202012_valid(instance, condition, root):
            return False
    return True


def schema_keywords(schema: dict) -> tuple[set[str], set[str]]:
    supported = {
        "$ref",
        "type",
        "additionalProperties",
        "required",
        "properties",
        "const",
        "format",
        "enum",
        "oneOf",
        "allOf",
        "if",
        "then",
        "pattern",
        "minLength",
        "maxItems",
    }
    used: set[str] = set()

    def visit(node: dict) -> None:
        keys = set(node)
        assert keys <= supported, f"validator does not cover: {keys - supported}"
        used.update(keys)
        for child in node.get("properties", {}).values():
            visit(child)
        for child in node.get("oneOf", []):
            visit(child)
        for child in node.get("allOf", []):
            used.update({"if", "then"})
            visit(child["if"])
            visit(child["then"])

    metadata = {"$schema", "$id", "title", "$defs"}
    assert metadata <= set(schema), "published schema metadata is incomplete"
    visit({key: value for key, value in schema.items() if key not in metadata})
    for definition in schema["$defs"].values():
        visit(definition)
    return used, metadata


def expand_envelope_schema(envelope: dict, receipt: dict) -> dict:
    """Inline the published local receipt reference for dependency-free tests."""

    def expand(node: object, root: dict) -> object:
        if isinstance(node, list):
            return [expand(value, root) for value in node]
        if not isinstance(node, dict):
            return node
        reference = node.get("$ref")
        if reference in {
            "local-dispatch-receipt.schema.json",
            "local-dispatch-receipt-v3.schema.json",
        }:
            return expand(
                {
                    key: value
                    for key, value in receipt.items()
                    if key not in {"$schema", "$id", "title", "$defs"}
                },
                receipt,
            )
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            return expand(root["$defs"][reference.rsplit("/", 1)[1]], root)
        return {key: expand(value, root) for key, value in node.items()}

    return expand(envelope, envelope)  # type: ignore[return-value]


FAKE_CODEX = r"""#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

home = Path(os.environ["CODEX_HOME"])
mode_file = home / "tier-routing" / "fake-codex-mode.json"
mode_data = {}
if mode_file.exists():
    mode_data = json.loads(mode_file.read_text())
mode = mode_data.get("mode", "success")
fake_thread_id = mode_data.get("thread_id", "00000000-0000-4000-8000-000000000001")
fake_grandchild_marker = mode_data.get("grandchild_marker", "")
fake_native_multi_agent = mode_data.get("native_multi_agent", False)
fake_child_override_ignored = mode_data.get("child_override_ignored", False)

if sys.argv[-2:] == ["features", "list"]:
    config = home / "config.toml"
    enabled = True
    if config.exists():
        enabled = tomllib.loads(config.read_text()).get("features", {}).get("multi_agent", True)
    if fake_native_multi_agent:
        enabled = True
    if "features.multi_agent=false" in sys.argv:
        enabled = fake_child_override_ignored
    print(f"multi_agent stable {'true' if enabled else 'false'}")
    raise SystemExit(0)

if "--version" in sys.argv:
    print("codex-cli 0.test")
    raise SystemExit(0)

if mode == "timeout":
    time.sleep(3)
if mode == "timeout_grandchild":
    subprocess.Popen([
        sys.executable,
        "-c",
        "import pathlib,time; time.sleep(2); pathlib.Path(r'" + fake_grandchild_marker + "').write_text('survived')"
    ])
    time.sleep(3)

args = sys.argv[1:]
def after(flag):
    return args[args.index(flag) + 1]

model = after("-m")
effort = after("-c").split('"')[1]
sandbox = after("-s")
cwd = str(Path(after("-C")).resolve())
prompt = sys.stdin.read()
home = Path(os.environ["CODEX_HOME"])
(home / "argv.json").write_text(json.dumps({"argv": args, "prompt": prompt, "process_cwd": os.getcwd()}))
if mode == "mutate_policy":
    policy = home / "tier-routing" / "policy.toml"
    policy.write_text(policy.read_text() + "\n# changed during dispatch\n")

if mode == "nonzero":
    print("synthetic error", file=sys.stderr)
    raise SystemExit(9)
if mode == "malformed_stdout":
    print("not-json")
    raise SystemExit(0)

thread = fake_thread_id
if mode != "no_rollout":
    session_dir = home / "sessions" / "2026" / "07" / "12"
    session_dir.mkdir(parents=True, exist_ok=True)
    observed_model = "wrong-model" if mode == "wrong_model" else model
    observed_effort = "wrong-effort" if mode == "wrong_effort" else effort
    observed_sandbox = "danger-full-access" if mode == "wrong_sandbox" else sandbox
    meta = {
        "type": "session_meta",
        "payload": {
            "session_id": thread,
            "id": thread,
            "cwd": "/wrong/session/cwd" if mode == "wrong_session_cwd" else cwd,
            "originator": "wrong-originator" if mode == "wrong_originator" else "codex_exec",
            "cli_version": (
                {"unexpected": True}
                if mode == "invalid_cli_version"
                else "0.test"
            ),
            "source": "wrong-source" if mode == "wrong_source" else "exec",
            "model_provider": "wrong-provider" if mode == "wrong_provider" else "openai"
        }
    }
    sandbox_policy = {"type": observed_sandbox}
    if sandbox == "workspace-write" and mode != "wrong_sandbox":
        sandbox_policy.update({
            "exclude_slash_tmp": False,
            "exclude_tmpdir_env_var": False,
        })
        if mode != "missing_network":
            sandbox_policy["network_access"] = (
                mode == "network_enabled"
                or (
                    mode == "configured_authority_expansion"
                    and "sandbox_workspace_write.network_access=false" not in args
                )
            )
        if (
            mode == "configured_authority_expansion"
            and "sandbox_workspace_write.writable_roots=[]" not in args
        ):
            sandbox_policy["writable_roots"] = ["/configured-extra-root"]
        if mode == "missing_exclude_slash_tmp":
            sandbox_policy.pop("exclude_slash_tmp")
        if mode == "nonboolean_exclude_tmpdir":
            sandbox_policy["exclude_tmpdir_env_var"] = "false"
    if mode == "extra_writable_root":
        sandbox_policy["writable_roots"] = ["/unexpected"]
    if mode == "explicit_empty_writable_roots":
        sandbox_policy["writable_roots"] = []
    turn = {
        "type": "turn_context",
        "payload": {
            "cwd": "/wrong/cwd" if mode == "wrong_cwd" else cwd,
            "approval_policy": "on-request" if mode == "wrong_approval" else "never",
            "sandbox_policy": sandbox_policy,
            "model": observed_model,
            "effort": observed_effort
        }
    }
    path = session_dir / f"rollout-test-{thread}.jsonl"
    lines = [json.dumps(meta), json.dumps(turn)]
    if mode == "second_meta":
        lines.append(json.dumps(meta))
    if mode == "second_turn":
        second = json.loads(json.dumps(turn))
        second["payload"]["model"] = "wrong-model"
        lines.append(json.dumps(second))
    if mode == "delayed_turn":
        path.write_text(json.dumps(meta) + "\n")
        delayed_code = (
            "import pathlib,time; time.sleep(.2); pathlib.Path(" + repr(str(path))
            + ").open('a').write(" + repr(json.dumps(turn) + "\n") + ")"
        )
        subprocess.Popen(
            [sys.executable, "-c", delayed_code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        path.write_text("\n".join(lines) + "\n")
    if mode == "writable_rollout":
        path.chmod(0o666)
    if mode == "writable_rollout_parent":
        path.parent.chmod(0o777)
    if mode == "stale_rollout":
        os.utime(path, (1, 1))
    if mode == "multiple_rollout":
        duplicate = session_dir / f"rollout-duplicate-{thread}.jsonl"
        duplicate.write_text(path.read_text())

print(json.dumps({"type": "thread.started", "thread_id": thread}))
if mode == "duplicate_thread":
    print(json.dumps({"type": "thread.started", "thread_id": thread}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "FAKE_OK"}}))
if mode != "no_terminal":
    print(json.dumps({"type": "turn.completed"}))
"""


class RepositoryValidatorTests(unittest.TestCase):
    def test_public_privacy_scan_rejects_absolute_user_home_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("$HOME/project", encoding="utf-8")
            self.assertEqual(CODEX_TIER_VALIDATE.check_public_privacy(root), 0)
            private_path = (
                "/" + bytes((85, 115, 101, 114, 115)).decode() + "/example/project"
            )
            bare_homes = (
                private_path.rsplit("/", 1)[0],
                "/" + bytes((104, 111, 109, 101)).decode() + "/example",
            )
            for private_home in (*bare_homes, private_path):
                (root / "Makefile").write_text(private_home, encoding="utf-8")
                self.assertEqual(CODEX_TIER_VALIDATE.check_public_privacy(root), 1)
            (root / "Makefile").write_text("clean", encoding="utf-8")
            (root / "Dockerfile").write_text("synthetic-" + "lab", encoding="utf-8")
            self.assertEqual(CODEX_TIER_VALIDATE.check_public_privacy(root), 1)


class DispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(os.umask, os.umask(0o077))
        self.temp = tempfile.TemporaryDirectory()
        # macOS exposes /var as a symlink to /private/var.  The dispatcher
        # validates the caller's complete lexical CODEX_HOME chain, so normal
        # fixtures use the physical spelling and dedicated tests introduce
        # lexical symlinks explicitly.
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "codex-home"
        self.home.mkdir(mode=0o700)
        (self.home / "config.toml").write_text("[features]\nmulti_agent = false\n")
        self.workdir = self.base / "work"
        self.workdir.mkdir()
        installed_bin = self.base / "installed-bin"
        installed_bin.mkdir(mode=0o700)
        self.launcher = installed_bin / "codex-tier"
        self.runtime = installed_bin / "codex-tier.py"
        shutil.copyfile(LAUNCHER, self.launcher)
        shutil.copyfile(DISPATCHER, self.runtime)
        self.launcher.chmod(0o700)
        self.runtime.chmod(0o600)
        self.fake = self.base / "codex"
        self.fake.write_text(textwrap.dedent(FAKE_CODEX), encoding="utf-8")
        self.fake.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_fake_mode(
        self,
        *,
        mode: str = "success",
        thread_id: str | None = None,
        native_multi_agent: bool = False,
        child_override_ignored: bool = False,
    ) -> None:
        mode_dir = self.home / "tier-routing"
        mode_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        mode_dir.chmod(0o700)
        (mode_dir / "fake-codex-mode.json").write_text(
            json.dumps(
                {
                    "mode": mode,
                    "thread_id": thread_id or str(uuid.uuid4()),
                    "grandchild_marker": str(self.base / "grandchild-marker"),
                    "native_multi_agent": native_multi_agent,
                    "child_override_ignored": child_override_ignored,
                }
            )
        )

    def run_dispatch(
        self,
        route: str = "scout",
        *,
        prompt: str = "bounded synthetic task",
        mode: str = "success",
        timeout: int = 10,
        harness_timeout: float = 15,
        workdir: Path | None = None,
        prompt_file: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "CODEX_HOME": str(self.home),
                "CODEX_TIER_CODEX_BIN": str(self.fake),
            }
        )
        self._write_fake_mode(mode=mode)
        command = [
            str(self.launcher),
            "dispatch",
            "--route",
            route,
            "--workdir",
            str(workdir or self.workdir),
            "--timeout",
            str(timeout),
        ]
        if prompt_file is not None:
            command.extend(["--prompt-file", str(prompt_file)])
        return subprocess.run(
            command,
            input=None if prompt_file is not None else prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
            timeout=harness_timeout,
        )

    def envelope(self, result: subprocess.CompletedProcess[str]) -> dict:
        return json.loads(result.stdout)

    def writer_lock_path(self, workdir: Path) -> Path:
        details = workdir.resolve().stat()
        return (
            CODEX_TIER.workspace_writer_lock_dir()
            / f"workspace-write-{details.st_dev}-{details.st_ino}.lock"
        )

    def git(self, *args: str, cwd: Path | None = None) -> str:
        executable = shutil.which("git")
        self.assertIsNotNone(executable)
        result = subprocess.run(
            [executable, *args],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def linked_worktree(self, *, object_format: str | None = None) -> tuple[Path, Path]:
        suffix = uuid.uuid4().hex
        primary = self.base / f"primary-{suffix}"
        linked = self.base / f"linked-{suffix}"
        init_args = ["init", "-q"]
        if object_format is not None:
            init_args.append(f"--object-format={object_format}")
        self.git(*init_args, str(primary))
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
            "core.hooksPath=/dev/null",
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

    def write_policy(
        self,
        *,
        overrides: dict[str, tuple[str, str]] | None = None,
    ) -> Path:
        selections = {
            name: (route.model, route.effort)
            for name, route in CODEX_TIER.ROUTES.items()
        }
        selections.update(overrides or {})
        lines = [f'schema = "{CODEX_TIER.POLICY_SCHEMA}"']
        for name in CODEX_TIER.ROUTES:
            model, effort = selections[name]
            lines.extend(
                [
                    "",
                    f"[routes.{name}]",
                    f'model = "{model}"',
                    f'effort = "{effort}"',
                ]
            )
        state = self.home / "tier-routing"
        state.mkdir(mode=0o700, exist_ok=True)
        state.chmod(0o700)
        policy = state / "policy.toml"
        policy.write_text("\n".join(lines) + "\n", encoding="utf-8")
        policy.chmod(0o600)
        return policy

    def test_probe_has_no_model_call(self) -> None:
        env = os.environ.copy()
        env.update(
            {"CODEX_HOME": str(self.home), "CODEX_TIER_CODEX_BIN": str(self.fake)}
        )
        result = subprocess.run(
            [str(self.launcher), "probe"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["dispatcher_version"], CODEX_TIER.PROJECT_VERSION)
        self.assertEqual(
            set(payload["routes"]),
            {"scout", "grunt", "builder", "reviewer", "architect"},
        )
        self.assertEqual(payload["policy"]["kind"], "builtin")
        self.assertEqual(payload["policy"]["model_access"], "not_checked")
        self.assertEqual(
            payload["experimental_routes"],
            {
                "surgeon": {
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                    "sandbox": "workspace-write",
                    "configurable": False,
                }
            },
        )
        self.assertFalse((self.home / "argv.json").exists())

    def test_version_is_machine_readable_and_offline(self) -> None:
        env = os.environ.copy()
        env.update(
            {"CODEX_HOME": str(self.home), "CODEX_TIER_CODEX_BIN": str(self.fake)}
        )
        result = subprocess.run(
            [str(self.launcher), "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, f"codex-tier {CODEX_TIER.PROJECT_VERSION}\n")
        self.assertFalse((self.home / "argv.json").exists())

    def test_policy_check_is_offline_and_reports_supported_parameters(self) -> None:
        env = {
            **os.environ,
            "CODEX_HOME": str(self.home),
            # A policy check must not need or execute this path.
            "CODEX_TIER_CODEX_BIN": str(self.base / "missing-codex"),
        }
        result = subprocess.run(
            [str(self.launcher), "policy-check"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(payload["status"], "pass")
        self.assertEqual(payload["dispatcher_version"], CODEX_TIER.PROJECT_VERSION)
        self.assertEqual(payload["policy_kind"], "builtin")
        self.assertEqual(payload["model_access"], "not_checked")
        self.assertEqual(payload["model_catalog"], CODEX_TIER.MODEL_CATALOG_URL)
        self.assertEqual(payload["accepted_efforts"], list(CODEX_TIER.ACCEPTED_EFFORTS))
        self.assertIn("canary", payload["model_access_note"])
        self.assertEqual(set(payload["routes"]), set(CODEX_TIER.ROUTES))
        self.assertFalse(payload["experimental_routes"]["surgeon"]["configurable"])
        self.assertFalse((self.home / "argv.json").exists())

    def test_v1_policy_stays_five_routes_and_cannot_configure_surgeon(self) -> None:
        stable = {"scout", "grunt", "builder", "reviewer", "architect"}
        self.assertEqual(set(CODEX_TIER.ROUTES), stable)
        sample = tomllib.loads((ROOT / "policy.sample.toml").read_text())
        self.assertEqual(sample["schema"], CODEX_TIER.POLICY_SCHEMA)
        self.assertEqual(set(sample["routes"]), stable)
        self.assertNotIn("surgeon", sample["routes"])

        policy = self.write_policy()
        policy.write_text(
            policy.read_text()
            + '\n[routes.surgeon]\nmodel = "owner-choice"\neffort = "none"\n',
            encoding="utf-8",
        )
        policy.chmod(0o600)
        env = {**os.environ, "CODEX_HOME": str(self.home)}
        result = subprocess.run(
            [str(self.launcher), "policy-check"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("routes must be exactly", result.stdout)

    def test_custom_policy_changes_only_model_and_effort(self) -> None:
        policy_path = self.write_policy(overrides={"builder": ("gpt-5.6", "max")})
        expected_hash = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        result = self.run_dispatch(route="builder")
        envelope = self.envelope(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = envelope["receipt"]
        self.assertEqual(receipt["policy_kind"], "custom")
        self.assertEqual(receipt["policy_id"], CODEX_TIER.CUSTOM_POLICY_ID)
        self.assertEqual(receipt["policy_input_sha256"], expected_hash)
        self.assertEqual(
            receipt["policy_sha256"],
            hashlib.sha256(
                json.dumps(
                    receipt["policy_document"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        )
        self.assertEqual(receipt["observed"]["model"], "gpt-5.6")
        self.assertEqual(receipt["observed"]["effort"], "max")
        self.assertEqual(receipt["observed"]["sandbox"], "workspace-write")
        recorded = json.loads((self.home / "argv.json").read_text())
        self.assertIn("features.multi_agent=false", recorded["argv"])
        self.assertIn('approval_policy="never"', recorded["argv"])
        self.assertIn("sandbox_workspace_write.network_access=false", recorded["argv"])
        self.assertIn("sandbox_workspace_write.writable_roots=[]", recorded["argv"])
        self.assertIn("--strict-config", recorded["argv"])

    def test_custom_policy_validation_is_exact_and_actionable(self) -> None:
        policy = self.write_policy()
        env = {**os.environ, "CODEX_HOME": str(self.home)}
        invalid_cases = (
            (
                "unknown effort",
                policy.read_text().replace('effort = "low"', 'effort = "turbo"', 1),
                "effort must be one of",
            ),
            (
                "unsafe model",
                policy.read_text().replace(
                    'model = "gpt-5.6-luna"', 'model = "provider/model"', 1
                ),
                "model must match",
            ),
            (
                "mutable boundary",
                policy.read_text().replace(
                    'effort = "low"',
                    'effort = "low"\nsandbox = "danger-full-access"',
                    1,
                ),
                "exactly model and effort",
            ),
            (
                "missing route",
                policy.read_text().split("\n[routes.architect]", 1)[0] + "\n",
                "routes must be exactly",
            ),
            (
                "policy id is not owner-configurable",
                policy.read_text().replace(
                    "\n[routes.scout]",
                    '\npolicy_id = "owner-choice"\n\n[routes.scout]',
                    1,
                ),
                "top-level keys must be exactly schema and routes",
            ),
            (
                "extra top-level key",
                policy.read_text() + "unexpected = true\n",
                "must contain exactly model and effort",
            ),
        )
        for label, contents, marker in invalid_cases:
            with self.subTest(case=label):
                policy.write_text(contents, encoding="utf-8")
                policy.chmod(0o600)
                result = subprocess.run(
                    [str(self.launcher), "policy-check"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(marker, json.loads(result.stdout)["error"])
                policy = self.write_policy()

    def test_policy_file_security_and_bounds_fail_closed(self) -> None:
        policy = self.write_policy()
        env = {**os.environ, "CODEX_HOME": str(self.home)}

        policy.chmod(0o644)
        result = subprocess.run(
            [str(self.launcher), "policy-check"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("mode 0600", result.stdout)

        policy.unlink()
        target = self.base / "external-policy.toml"
        target.write_text((ROOT / "policy.sample.toml").read_text())
        policy.symlink_to(target)
        result = subprocess.run(
            [str(self.launcher), "policy-check"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlinked routing policy", result.stdout)

        policy.unlink()
        policy.write_bytes(b"x" * (CODEX_TIER.MAX_POLICY_BYTES + 1))
        policy.chmod(0o600)
        result = subprocess.run(
            [str(self.launcher), "policy-check"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("byte limit", result.stdout)

        policy.write_bytes(b"\xff")
        policy.chmod(0o600)
        result = subprocess.run(
            [str(self.launcher), "policy-check"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("UTF-8 TOML", result.stdout)

        policy.unlink()
        (self.home / "tier-routing").chmod(0o755)
        result = subprocess.run(
            [str(self.launcher), "policy-check"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("mode 0700", result.stdout)

    def test_every_supported_effort_parses_and_unknown_model_never_falls_back(
        self,
    ) -> None:
        for effort in CODEX_TIER.ACCEPTED_EFFORTS:
            with self.subTest(effort=effort):
                policy = self.write_policy(overrides={"scout": ("gpt-5.6", effort)})
                parsed = CODEX_TIER.load_policy(self.home)
                self.assertEqual(parsed.routes["scout"].effort, effort)
                policy.unlink()

        self.write_policy(overrides={"scout": ("gpt-synthetic-unavailable", "low")})
        result = self.run_dispatch(mode="nonzero")
        receipt = self.envelope(result)["receipt"]
        self.assertEqual(result.returncode, 2)
        self.assertEqual(receipt["requested"]["model"], "gpt-synthetic-unavailable")
        self.assertIn("exited nonzero", receipt["error"])
        ledger = self.home / "tier-routing" / "ledger.jsonl"
        self.assertEqual(len(ledger.read_text().splitlines()), 1)
        argv = json.loads((self.home / "argv.json").read_text())["argv"]
        self.assertEqual(argv.count("gpt-synthetic-unavailable"), 1)

    def test_policy_rejects_foreign_owner_and_macos_allow_acl(self) -> None:
        policy = self.write_policy()
        foreign = mock.Mock(
            st_mode=stat.S_IFREG | 0o600,
            st_uid=os.geteuid() + 1,
            st_size=policy.stat().st_size,
        )
        with mock.patch.object(CODEX_TIER.os, "fstat", return_value=foreign):
            with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "current-owner"):
                CODEX_TIER.load_policy(self.home)

        allow = subprocess.CompletedProcess(
            args=["/bin/ls"],
            returncode=0,
            stdout="drwx------@ 1 user staff 32 Jul 12 00:00 path\n"
            " 0: user:other allow read\n",
            stderr="",
        )
        with (
            mock.patch.object(CODEX_TIER.sys, "platform", "darwin"),
            mock.patch.object(CODEX_TIER.subprocess, "run", return_value=allow),
        ):
            with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "ACL allow"):
                CODEX_TIER.load_policy(self.home)

    def test_policy_change_during_dispatch_halts_with_initial_receipt(self) -> None:
        policy = self.write_policy()
        initial_hash = hashlib.sha256(policy.read_bytes()).hexdigest()
        result = self.run_dispatch(mode="mutate_policy")
        receipt = self.envelope(result)["receipt"]
        self.assertEqual(result.returncode, 2)
        self.assertEqual(receipt["status"], "halt")
        self.assertEqual(receipt["policy_input_sha256"], initial_hash)
        self.assertIn("policy changed during dispatch", receipt["error"])
        self.assertIsNone(self.envelope(result)["result"])

    def test_policy_binding_semantics_reject_tampering(self) -> None:
        receipt = self.envelope(self.run_dispatch())["receipt"]
        CODEX_TIER.verify_policy_binding(receipt)
        mutations = (
            (
                "document hash",
                lambda value: value["policy_document"]["routes"]["scout"].update(
                    model="gpt-5.6"
                ),
            ),
            (
                "identifier",
                lambda value: value.update(policy_id="other-policy"),
            ),
            (
                "route resolution",
                lambda value: value.update(route_resolved="reviewer"),
            ),
            (
                "request",
                lambda value: value["requested"].update(effort="max"),
            ),
            (
                "observation",
                lambda value: value["observed"].update(effort="max"),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(tamper=label):
                value = json.loads(json.dumps(receipt))
                mutate(value)
                with self.assertRaises(CODEX_TIER.DispatchHalt):
                    CODEX_TIER.verify_policy_binding(value)

    def test_codex_executable_requires_a_safe_absolute_path(self) -> None:
        unsafe_dir = self.base / "unsafe-bin"
        unsafe_dir.mkdir(mode=0o700)
        unsafe = unsafe_dir / "codex"
        unsafe.write_text(textwrap.dedent(FAKE_CODEX), encoding="utf-8")
        unsafe.chmod(0o755)
        unsafe_dir.chmod(0o777)

        env = os.environ.copy()
        env.update({"CODEX_HOME": str(self.home), "CODEX_TIER_CODEX_BIN": str(unsafe)})
        result = subprocess.run(
            [str(self.launcher), "probe"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("executable path", json.loads(result.stdout)["error"])

        env["CODEX_TIER_CODEX_BIN"] = "relative/codex"
        relative = subprocess.run(
            [str(self.launcher), "probe"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(relative.returncode, 2)
        self.assertIn("absolute", json.loads(relative.stdout)["error"])

    def test_child_environment_excludes_loader_and_python_injection(self) -> None:
        fake_codex = self.base / "env-dump-codex"
        env_dump = self.base / "child-env.json"
        fake_codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os\n"
            f"from pathlib import Path\n"
            f"Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)))\n"
            "print('multi_agent stable false')\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "CODEX_HOME": str(self.home),
                "CODEX_TIER_CODEX_BIN": str(fake_codex),
                "LD_PRELOAD": str(self.base / "evil.so"),
                "DYLD_INSERT_LIBRARIES": str(self.base / "evil.dylib"),
                "PYTHONPATH": str(self.base / "evil"),
                "PYTHONSTARTUP": str(self.base / "evil.py"),
                "PYTHONHOME": str(self.base / "evil-home"),
                "EVIL_EXTRA": "should_not_propagate",
            }
        )
        result = subprocess.run(
            [str(self.launcher), "probe"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        child_env = json.loads(env_dump.read_text(encoding="utf-8"))
        self.assertEqual(child_env["CODEX_HOME"], str(self.home))
        for name in (
            "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES",
            "PYTHONPATH",
            "PYTHONSTARTUP",
            "PYTHONHOME",
            "EVIL_EXTRA",
        ):
            self.assertNotIn(name, child_env)

    def test_dispatcher_launcher_clears_loader_variables_before_python(self) -> None:
        marker = self.base / "sitecustomize-ran"
        startup = self.base / "launcher-startup"
        startup.mkdir()
        (startup / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        fake_codex = self.base / "safe-codex"
        fake_codex.write_text(
            "#!/bin/sh\nprintf 'multi_agent stable false\\n'\n", encoding="utf-8"
        )
        fake_codex.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "CODEX_HOME": str(self.home),
                "CODEX_TIER_CODEX_BIN": str(fake_codex),
                "PYTHONPATH": str(startup),
                "PYTHONHOME": str(self.base / "evil-home"),
                "LD_PRELOAD": str(self.base / "evil.so"),
                "DYLD_INSERT_LIBRARIES": str(self.base / "evil.dylib"),
            }
        )
        result = subprocess.run(
            [str(self.launcher), "probe"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())

    def test_launcher_rejects_untrusted_python_before_execution(self) -> None:
        unsafe_dir = self.base / "unsafe-python-bin"
        unsafe_dir.mkdir(mode=0o700)
        marker = self.base / "unsafe-python-ran"
        fake_python = unsafe_dir / "python3"
        fake_python.write_text(
            f"#!/bin/sh\nprintf compromised > {marker}\n", encoding="utf-8"
        )
        fake_python.chmod(0o755)
        unsafe_dir.chmod(0o777)
        env = os.environ.copy()
        env.update(
            {
                "CODEX_HOME": str(self.home),
                "CODEX_TIER_CODEX_BIN": str(self.fake),
                "CODEX_TIER_PYTHON_BIN": str(fake_python),
            }
        )
        result = subprocess.run(
            [str(self.launcher), "probe"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("world-writable Python interpreter path", result.stderr)
        self.assertFalse(marker.exists())

    def test_launcher_accepts_current_owner_group_writable_python_chain(self) -> None:
        compatible_dir = self.base / "owner-managed-python-bin"
        compatible_dir.mkdir(mode=0o770)
        compatible_dir.chmod(0o770)
        python_link = compatible_dir / "python3"
        python_link.symlink_to(Path(sys.executable).resolve())
        env = os.environ.copy()
        env.update(
            {
                "CODEX_HOME": str(self.home),
                "CODEX_TIER_CODEX_BIN": str(self.fake),
                "CODEX_TIER_PYTHON_BIN": str(python_link),
            }
        )
        result = subprocess.run(
            [str(self.launcher), "probe"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_launcher_accepts_normal_dot_relative_invocation(self) -> None:
        env = os.environ.copy()
        env.update(
            {"CODEX_HOME": str(self.home), "CODEX_TIER_CODEX_BIN": str(self.fake)}
        )
        result = subprocess.run(
            ["./codex-tier", "probe"],
            cwd=self.launcher.parent,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_codex_shebang_runtime_is_validated_before_execution(self) -> None:
        unsafe_dir = self.base / "unsafe-codex-runtime-bin"
        unsafe_dir.mkdir(mode=0o700)
        marker = self.base / "unsafe-codex-runtime-ran"
        runtime_name = "codex-test-runtime"
        unsafe_runtime = unsafe_dir / runtime_name
        unsafe_runtime.write_text(
            f"#!/bin/sh\nprintf compromised > {marker}\n", encoding="utf-8"
        )
        unsafe_runtime.chmod(0o755)
        unsafe_dir.chmod(0o777)
        entrypoint = self.base / "codex-env-runtime"
        entrypoint.write_text(f"#!/usr/bin/env {runtime_name}\n", encoding="utf-8")
        entrypoint.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{unsafe_dir}{os.pathsep}{env.get('PATH', '')}",
                "CODEX_HOME": str(self.home),
                "CODEX_TIER_CODEX_BIN": str(entrypoint),
                "CODEX_TIER_PYTHON_BIN": str(Path(sys.executable).resolve()),
            }
        )
        result = subprocess.run(
            [str(self.launcher), "probe"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("shebang runtime path", json.loads(result.stdout)["error"])
        self.assertFalse(marker.exists())

    def test_codex_shebang_rejects_argument_bearing_env_form(self) -> None:
        entrypoint = self.base / "codex-unsupported-shebang"
        entrypoint.write_text(
            "#!/usr/bin/env -S node --no-warnings\n", encoding="utf-8"
        )
        entrypoint.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "CODEX_HOME": str(self.home),
                "CODEX_TIER_CODEX_BIN": str(entrypoint),
            }
        )
        result = subprocess.run(
            [str(self.launcher), "probe"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "must name exactly one runtime", json.loads(result.stdout)["error"]
        )

    def test_relative_codex_home_halts_before_subprocess_or_mutation(self) -> None:
        commands = (
            ["probe"],
            ["strict-enable"],
            ["strict-disable"],
            ["dispatch", "--route", "scout", "--workdir", str(self.workdir)],
        )
        for configured in ("", "~/.codex", ".", "relative-codex-home"):
            env = os.environ.copy()
            env.update(
                {"CODEX_HOME": configured, "CODEX_TIER_CODEX_BIN": str(self.fake)}
            )
            for command in commands:
                with self.subTest(configured=configured, command=command[0]):
                    result = subprocess.run(
                        [str(self.launcher), *command],
                        input="task",
                        cwd=self.base,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("CODEX_HOME must be an absolute path", result.stdout)
                    self.assertFalse((self.base / "relative-codex-home").exists())
                    self.assertFalse((self.home / "argv.json").exists())

    def test_codex_home_lexical_dot_components_halt_before_subprocess(self) -> None:
        for configured in (
            f"{self.base}/./codex-home",
            f"{self.base}/nested/../codex-home",
        ):
            with self.subTest(configured=configured):
                env = os.environ.copy()
                env.update(
                    {"CODEX_HOME": configured, "CODEX_TIER_CODEX_BIN": str(self.fake)}
                )
                result = subprocess.run(
                    [str(self.launcher), "probe"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("must not contain . or .. components", result.stdout)
                self.assertFalse((self.home / "argv.json").exists())

    def test_codex_home_chain_rejects_unsafe_or_symlinked_components(self) -> None:
        unsafe_parent = self.base / "unsafe-parent"
        unsafe_home = unsafe_parent / "codex-home"
        unsafe_home.mkdir(parents=True, mode=0o700)
        unsafe_parent.chmod(0o777)

        real_parent = self.base / "real-parent"
        linked_home = real_parent / "codex-home"
        linked_home.mkdir(parents=True, mode=0o700)
        link = self.base / "linked-parent"
        link.symlink_to(real_parent, target_is_directory=True)

        for configured, marker in (
            (unsafe_home, "non-sticky group/world-writable"),
            (link / "codex-home", "symlinked Codex home component"),
        ):
            with self.subTest(configured=configured):
                env = os.environ.copy()
                env.update(
                    {
                        "CODEX_HOME": str(configured),
                        "CODEX_TIER_CODEX_BIN": str(self.fake),
                    }
                )
                result = subprocess.run(
                    [str(self.launcher), "probe"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(marker, result.stdout)
                self.assertFalse((self.home / "argv.json").exists())

    def test_codex_home_rejects_group_or_world_writable_final_directory(self) -> None:
        for mode in (0o770, 0o777):
            with self.subTest(mode=oct(mode)):
                self.home.chmod(mode)
                env = os.environ.copy()
                env.update(
                    {
                        "CODEX_HOME": str(self.home),
                        "CODEX_TIER_CODEX_BIN": str(self.fake),
                    }
                )
                result = subprocess.run(
                    [str(self.launcher), "probe"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("group/world-writable", result.stdout)
                self.home.chmod(0o700)

    def test_darwin_acl_probe_allows_deny_only_and_rejects_allow_or_probe_failure(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess(
            args=["/bin/ls"],
            returncode=0,
            stdout="drwx------@ 1 user staff 32 Jul 12 00:00 path\n 0: group:everyone deny delete\n",
            stderr="",
        )
        with (
            mock.patch.object(CODEX_TIER.sys, "platform", "darwin"),
            mock.patch.object(CODEX_TIER.subprocess, "run", return_value=completed),
        ):
            CODEX_TIER.reject_darwin_allow_acl(self.home, label="test path")

        for result in (
            subprocess.CompletedProcess(
                args=["/bin/ls"],
                returncode=0,
                stdout="drwx------+ 1 user staff 32 Jul 12 00:00 path\n 0: user:other allow read\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["/bin/ls"], returncode=1, stdout="", stderr="failed"
            ),
        ):
            with self.subTest(returncode=result.returncode, stdout=result.stdout):
                with (
                    mock.patch.object(CODEX_TIER.sys, "platform", "darwin"),
                    mock.patch.object(
                        CODEX_TIER.subprocess, "run", return_value=result
                    ),
                ):
                    with self.assertRaises(CODEX_TIER.DispatchHalt):
                        CODEX_TIER.reject_darwin_allow_acl(self.home, label="test path")

    def test_absent_codex_home_uses_default_home(self) -> None:
        default_home = self.base / "default-home" / ".codex"
        default_home.mkdir(parents=True)
        (default_home / "config.toml").write_text("[features]\nmulti_agent = false\n")
        env = os.environ.copy()
        env.pop("CODEX_HOME", None)
        env.update(
            {"HOME": str(default_home.parent), "CODEX_TIER_CODEX_BIN": str(self.fake)}
        )
        result = subprocess.run(
            [str(self.launcher), "probe"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["codex_home"], str(default_home.resolve())
        )

    def test_all_routes_are_host_observed(self) -> None:
        expected = {
            "scout": ("gpt-5.6-terra", "low", "read-only"),
            "grunt": ("gpt-5.6-luna", "low", "workspace-write"),
            "builder": ("gpt-5.6-terra", "medium", "workspace-write"),
            "reviewer": ("gpt-5.6-sol", "high", "read-only"),
            "architect": ("gpt-5.6-sol", "xhigh", "read-only"),
        }
        for route, values in expected.items():
            with self.subTest(route=route):
                result = self.run_dispatch(route)
                envelope = self.envelope(result)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(envelope["result"], "FAKE_OK")
                receipt = envelope["receipt"]
                self.assertEqual(receipt["status"], "pass")
                self.assertEqual(receipt["schema"], CODEX_TIER.SCHEMA)
                self.assertEqual(set(receipt), CODEX_TIER.RECEIPT_FIELDS)
                self.assertEqual(
                    (
                        receipt["observed"]["model"],
                        receipt["observed"]["effort"],
                        receipt["observed"]["sandbox"],
                    ),
                    values,
                )
                self.assertEqual(
                    receipt["claim_class"], "dispatcher_configured_and_host_observed"
                )
                for path_field, hash_field in (
                    ("dispatcher_launcher", "dispatcher_launcher_sha256"),
                    ("dispatcher_runtime", "dispatcher_runtime_sha256"),
                    ("dispatcher_python", "dispatcher_python_sha256"),
                    ("codex_executable", "codex_executable_sha256"),
                    ("codex_runtime", "codex_runtime_sha256"),
                ):
                    self.assertEqual(
                        hashlib.sha256(
                            Path(receipt[path_field]).read_bytes()
                        ).hexdigest(),
                        receipt[hash_field],
                    )
                self.assertIn(
                    receipt["codex_launch_method"],
                    {"native", "explicit-shebang-runtime"},
                )
                if values[2] == "workspace-write":
                    self.assertEqual(
                        receipt["observed"]["sandbox_policy"]["writable_roots"],
                        [],
                    )

    def test_prompt_is_stdin_not_child_argv(self) -> None:
        secret_shape = "PROMPT_ONLY_SENTINEL"
        result = self.run_dispatch(prompt=secret_shape)
        self.assertEqual(result.returncode, 0)
        recorded = json.loads((self.home / "argv.json").read_text())
        self.assertNotIn(secret_shape, recorded["argv"])
        self.assertEqual(recorded["prompt"], secret_shape)

    def test_child_process_cwd_is_resolved_workdir(self) -> None:
        result = self.run_dispatch()
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = json.loads((self.home / "argv.json").read_text())
        self.assertEqual(recorded["process_cwd"], str(self.workdir.resolve()))

    def test_authority_boundaries_are_forced_in_child_argv(self) -> None:
        result = self.run_dispatch(
            route="builder", mode="configured_authority_expansion"
        )
        self.assertEqual(result.returncode, 0)
        recorded = json.loads((self.home / "argv.json").read_text())
        approval_flag = recorded["argv"].index("-a")
        self.assertEqual(recorded["argv"][approval_flag + 1], "never")
        self.assertIn('approval_policy="never"', recorded["argv"])
        self.assertIn("features.multi_agent=false", recorded["argv"])
        self.assertEqual(
            recorded["argv"].count("sandbox_workspace_write.network_access=false"),
            1,
        )
        self.assertEqual(
            recorded["argv"].count("sandbox_workspace_write.writable_roots=[]"), 1
        )

    def test_native_multi_agent_enabled_halts_before_dispatch(self) -> None:
        self._write_fake_mode(native_multi_agent=True)
        env = os.environ.copy()
        env.update(
            {
                "CODEX_HOME": str(self.home),
                "CODEX_TIER_CODEX_BIN": str(self.fake),
            }
        )
        result = subprocess.run(
            [
                str(self.launcher),
                "dispatch",
                "--route",
                "scout",
                "--workdir",
                str(self.workdir),
            ],
            input="bounded synthetic task",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("native multi-agent spawning is enabled", result.stdout)
        self.assertFalse((self.home / "argv.json").exists())

    def test_child_multi_agent_override_must_be_honored(self) -> None:
        self._write_fake_mode(child_override_ignored=True)
        env = os.environ.copy()
        env.update(
            {
                "CODEX_HOME": str(self.home),
                "CODEX_TIER_CODEX_BIN": str(self.fake),
            }
        )
        result = subprocess.run(
            [
                str(self.launcher),
                "dispatch",
                "--route",
                "scout",
                "--workdir",
                str(self.workdir),
            ],
            input="bounded synthetic task",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("did not honor", result.stdout)
        self.assertFalse((self.home / "argv.json").exists())

    def test_strict_enable_disable_preserves_existing_config(self) -> None:
        config = self.home / "config.toml"
        original = b"# keep\r\n[features]\r\nhooks = true\r\nmulti_agent = true # strict target\r\n"
        config.write_bytes(original)
        env = os.environ.copy()
        env.update(
            {"CODEX_HOME": str(self.home), "CODEX_TIER_CODEX_BIN": str(self.fake)}
        )
        enable = subprocess.run(
            [str(self.launcher), "strict-enable"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(enable.returncode, 0, enable.stdout + enable.stderr)
        updated = config.read_bytes()
        self.assertIn(b"hooks = true\r\n", updated)
        self.assertIn(b"multi_agent = false # strict target\r\n", updated)
        disable = subprocess.run(
            [str(self.launcher), "strict-disable"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(disable.returncode, 0, disable.stdout + disable.stderr)
        self.assertEqual(config.read_bytes(), original)

    def test_strict_enable_does_not_overwrite_concurrent_config_change(self) -> None:
        config = self.home / "config.toml"
        config.write_text("[features]\nmulti_agent = true\n", encoding="utf-8")
        concurrent = b"# concurrent owner update\n[features]\nmulti_agent = true\n"

        def mutate_then_fail(*_: object, **__: object) -> bool:
            config.write_bytes(concurrent)
            raise CODEX_TIER.DispatchHalt("synthetic feature probe failure")

        env = {
            **os.environ,
            "CODEX_HOME": str(self.home),
            "CODEX_TIER_CODEX_BIN": str(self.fake),
        }
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(
                CODEX_TIER,
                "native_multi_agent_enabled",
                side_effect=mutate_then_fail,
            ),
        ):
            with self.assertRaisesRegex(
                CODEX_TIER.DispatchHalt, "changed concurrently"
            ):
                CODEX_TIER.strict_enable(mock.Mock(), self.home)
        self.assertEqual(config.read_bytes(), concurrent)
        state_dir = self.home / "tier-routing"
        self.assertTrue((state_dir / "strict-config.json").is_file())
        self.assertTrue((state_dir / "strict-config.backup").is_file())

    def test_strict_disable_preserves_host_added_project_metadata(self) -> None:
        config = self.home / "config.toml"
        config.unlink()
        with (
            mock.patch.object(CODEX_TIER, "resolve_codex", return_value=self.fake),
            mock.patch.object(
                CODEX_TIER, "native_multi_agent_enabled", return_value=False
            ),
        ):
            self.assertEqual(CODEX_TIER.strict_enable(mock.Mock(), self.home), 0)
        addition = b'\n[projects."/synthetic/project"]\ntrust_level = "trusted"\n'
        config.write_bytes(config.read_bytes() + addition)
        self.assertEqual(CODEX_TIER.strict_disable(mock.Mock(), self.home), 0)
        self.assertEqual(config.read_bytes(), addition)
        self.assertFalse((self.home / "tier-routing" / "strict-config.json").exists())
        self.assertFalse((self.home / "tier-routing" / "strict-config.backup").exists())

    def test_strict_disable_preserves_additions_for_each_managed_edit(self) -> None:
        addition = b'\n[projects."/synthetic/project"]\ntrust_level = "trusted"\n'
        for label, before_exists, original in (
            ("replace", True, b"[features]\nmulti_agent = true # keep\n"),
            ("replace without newline", True, b"[features]\nmulti_agent = true"),
            ("insert", True, b"[features]\nhooks = true\n"),
            ("new table", False, b""),
        ):
            with self.subTest(case=label):
                config = self.home / "config.toml"
                config.unlink(missing_ok=True)
                if before_exists:
                    config.write_bytes(original)
                with (
                    mock.patch.object(
                        CODEX_TIER, "resolve_codex", return_value=self.fake
                    ),
                    mock.patch.object(
                        CODEX_TIER,
                        "native_multi_agent_enabled",
                        return_value=False,
                    ),
                ):
                    self.assertEqual(
                        CODEX_TIER.strict_enable(mock.Mock(), self.home), 0
                    )
                    config.write_bytes(config.read_bytes() + addition)
                    self.assertEqual(
                        CODEX_TIER.strict_enable(mock.Mock(), self.home), 0
                    )
                self.assertEqual(CODEX_TIER.strict_disable(mock.Mock(), self.home), 0)
                self.assertEqual(config.read_bytes(), original + addition)

    def test_strict_disable_recovers_after_merged_config_write(self) -> None:
        config = self.home / "config.toml"
        config.unlink()
        with (
            mock.patch.object(CODEX_TIER, "resolve_codex", return_value=self.fake),
            mock.patch.object(
                CODEX_TIER, "native_multi_agent_enabled", return_value=False
            ),
        ):
            self.assertEqual(CODEX_TIER.strict_enable(mock.Mock(), self.home), 0)
        addition = b'\n[projects."/synthetic/project"]\ntrust_level = "trusted"\n'
        state = self.home / "tier-routing"
        manifest_path = state / "strict-config.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["state"] = "disabling"
        manifest_path.write_text(json.dumps(manifest))
        config.write_bytes(addition)
        self.assertEqual(CODEX_TIER.strict_disable(mock.Mock(), self.home), 0)
        self.assertEqual(config.read_bytes(), addition)
        self.assertFalse(manifest_path.exists())
        self.assertFalse((state / "strict-config.backup").exists())

    def test_strict_disable_refuses_managed_setting_drift(self) -> None:
        config = self.home / "config.toml"
        config.write_text("[features]\nmulti_agent = true\n")
        with (
            mock.patch.object(CODEX_TIER, "resolve_codex", return_value=self.fake),
            mock.patch.object(
                CODEX_TIER, "native_multi_agent_enabled", return_value=False
            ),
        ):
            self.assertEqual(CODEX_TIER.strict_enable(mock.Mock(), self.home), 0)
        config.write_text(config.read_text().replace("false", "true"))
        with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "drifted"):
            CODEX_TIER.strict_disable(mock.Mock(), self.home)
        self.assertIn("multi_agent = true", config.read_text())
        self.assertTrue((self.home / "tier-routing" / "strict-config.json").exists())
        self.assertTrue((self.home / "tier-routing" / "strict-config.backup").exists())

    def test_strict_disable_rejects_symlinked_config(self) -> None:
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                home = self.base / f"symlink-home-{dangling}"
                home.mkdir(mode=0o700)
                config = home / "config.toml"
                config.write_text("[features]\nmulti_agent = true\n")
                env = os.environ.copy()
                env.update(
                    {"CODEX_HOME": str(home), "CODEX_TIER_CODEX_BIN": str(self.fake)}
                )
                enable = subprocess.run(
                    [str(self.launcher), "strict-enable"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    check=False,
                )
                self.assertEqual(enable.returncode, 0, enable.stdout + enable.stderr)
                managed = config.read_bytes()
                state = home / "tier-routing"
                before_state = {
                    path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                    for path in state.iterdir()
                }
                before_home_mode = stat.S_IMODE(home.stat().st_mode)
                target = self.base / f"untrusted-config-{dangling}"
                if not dangling:
                    target.write_bytes(managed)
                config.unlink()
                config.symlink_to(target)
                disable = subprocess.run(
                    [str(self.launcher), "strict-disable"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    check=False,
                )
                self.assertEqual(disable.returncode, 2)
                self.assertIn("symlinked config.toml", disable.stdout)
                self.assertEqual(stat.S_IMODE(home.stat().st_mode), before_home_mode)
                self.assertEqual(
                    {
                        path.name: (
                            path.read_bytes(),
                            stat.S_IMODE(path.stat().st_mode),
                        )
                        for path in state.iterdir()
                    },
                    before_state,
                )
                if not dangling:
                    self.assertEqual(target.read_bytes(), managed)

    def test_strict_disable_rejects_symlink_before_unmanaged_state_creation(
        self,
    ) -> None:
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                home = self.base / f"disable-unmanaged-{dangling}"
                home.mkdir(mode=0o700)
                target = self.base / f"disable-unmanaged-target-{dangling}"
                original = b"[features]\nmulti_agent = true\n"
                if not dangling:
                    target.write_bytes(original)
                config = home / "config.toml"
                config.symlink_to(target)
                before_mode = stat.S_IMODE(home.stat().st_mode)
                env = os.environ.copy()
                env.update(
                    {"CODEX_HOME": str(home), "CODEX_TIER_CODEX_BIN": str(self.fake)}
                )
                result = subprocess.run(
                    [str(self.launcher), "strict-disable"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("symlinked config.toml", result.stdout)
                self.assertTrue(config.is_symlink())
                self.assertFalse((home / "tier-routing").exists())
                self.assertEqual(stat.S_IMODE(home.stat().st_mode), before_mode)
                if not dangling:
                    self.assertEqual(target.read_bytes(), original)

    def test_strict_enable_rejects_live_and_dangling_config_symlinks_before_state_changes(
        self,
    ) -> None:
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                home = self.base / f"enable-symlink-home-{dangling}"
                home.mkdir(mode=0o700)
                target = self.base / f"enable-symlink-target-{dangling}"
                original = b"[features]\nmulti_agent = true\n"
                if not dangling:
                    target.write_bytes(original)
                (home / "config.toml").symlink_to(target)
                env = os.environ.copy()
                env.update(
                    {"CODEX_HOME": str(home), "CODEX_TIER_CODEX_BIN": str(self.fake)}
                )
                result = subprocess.run(
                    [str(self.launcher), "strict-enable"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("symlinked config.toml", result.stdout)
                self.assertFalse((home / "tier-routing").exists())
                if not dangling:
                    self.assertEqual(target.read_bytes(), original)

    def test_strict_enable_rejects_symlinked_config_during_managed_recovery(
        self,
    ) -> None:
        home = self.base / "recovery-symlink-home"
        home.mkdir(mode=0o700)
        target = self.base / "recovery-symlink-target"
        target.write_bytes(b"[features]\nmulti_agent = false\n")
        (home / "config.toml").symlink_to(target)
        state = home / "tier-routing"
        state.mkdir(mode=0o700)
        manifest = state / "strict-config.json"
        backup = state / "strict-config.backup"
        manifest.write_text(
            json.dumps(
                {
                    "schema": CODEX_TIER.STRICT_MANIFEST_SCHEMA,
                    "before_exists": True,
                    "before_mode": 0o600,
                    "before_sha256": "0" * 64,
                    "after_sha256": "1" * 64,
                    "state": "pending",
                }
            )
        )
        backup.write_bytes(b"original")
        before_manifest, before_backup = manifest.read_bytes(), backup.read_bytes()
        env = os.environ.copy()
        env.update({"CODEX_HOME": str(home), "CODEX_TIER_CODEX_BIN": str(self.fake)})
        result = subprocess.run(
            [str(self.launcher), "strict-enable"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlinked config.toml", result.stdout)
        self.assertEqual(target.read_bytes(), b"[features]\nmulti_agent = false\n")
        self.assertEqual(manifest.read_bytes(), before_manifest)
        self.assertEqual(backup.read_bytes(), before_backup)

    def test_strict_disable_applies_owner_regular_guard_to_config(self) -> None:
        env = os.environ.copy()
        env.update(
            {"CODEX_HOME": str(self.home), "CODEX_TIER_CODEX_BIN": str(self.fake)}
        )
        config = self.home / "config.toml"
        config.write_text("[features]\nmulti_agent = true\n")
        enable = subprocess.run(
            [str(self.launcher), "strict-enable"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(enable.returncode, 0, enable.stdout + enable.stderr)
        real_guard = CODEX_TIER.current_owner_regular

        def guarded(path: Path, *, label: str) -> os.stat_result:
            if label == "config.toml":
                raise CODEX_TIER.DispatchHalt(
                    "config.toml is not a current-owner regular file"
                )
            return real_guard(path, label=label)

        with mock.patch.object(
            CODEX_TIER, "current_owner_regular", side_effect=guarded
        ):
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(
                    CODEX_TIER.DispatchHalt, "current-owner regular file"
                ):
                    CODEX_TIER.strict_disable(mock.Mock())

    def test_strict_enable_rejects_profile_override(self) -> None:
        (self.home / "config.toml").write_text("[features]\nmulti_agent = true\n")
        (self.home / "review.config.toml").write_text(
            "[features]\nmulti_agent = true\n"
        )
        env = os.environ.copy()
        env.update(
            {"CODEX_HOME": str(self.home), "CODEX_TIER_CODEX_BIN": str(self.fake)}
        )
        result = subprocess.run(
            [str(self.launcher), "strict-enable"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("profile review.config.toml overrides", result.stdout)
        self.assertEqual(
            (self.home / "config.toml").read_text(), "[features]\nmulti_agent = true\n"
        )

    def test_pending_strict_enable_is_reconciled_after_config_write(self) -> None:
        original = b"[features]\nmulti_agent = true\n"
        candidate = b"[features]\nmulti_agent = false\n"
        config = self.home / "config.toml"
        config.write_bytes(candidate)
        state = self.home / "tier-routing"
        state.mkdir(mode=0o700)
        (state / "strict-config.backup").write_bytes(original)
        manifest = {
            "schema": "codex-tier-routing/strict-config/v1",
            "before_exists": True,
            "before_mode": 0o644,
            "before_sha256": hashlib.sha256(original).hexdigest(),
            "after_sha256": hashlib.sha256(candidate).hexdigest(),
            "state": "pending",
        }
        (state / "strict-config.json").write_text(json.dumps(manifest))
        env = os.environ.copy()
        env.update(
            {"CODEX_HOME": str(self.home), "CODEX_TIER_CODEX_BIN": str(self.fake)}
        )
        enable = subprocess.run(
            [str(self.launcher), "strict-enable"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(enable.returncode, 0, enable.stdout + enable.stderr)
        active = json.loads((state / "strict-config.json").read_text())
        self.assertEqual(active["state"], "active")
        disable = subprocess.run(
            [str(self.launcher), "strict-disable"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(disable.returncode, 0, disable.stdout + disable.stderr)
        self.assertEqual(config.read_bytes(), original)

    def test_observation_mismatches_halt(self) -> None:
        for mode, marker in (
            ("wrong_model", "model"),
            ("wrong_effort", "effort"),
            ("wrong_sandbox", "sandbox"),
            ("wrong_cwd", "cwd"),
            ("wrong_session_cwd", "session_cwd"),
            ("wrong_approval", "approval_policy"),
            ("wrong_source", "source"),
            ("wrong_originator", "originator"),
            ("wrong_provider", "model_provider"),
            ("invalid_cli_version", "cli_version"),
        ):
            with self.subTest(mode=mode):
                result = self.run_dispatch(mode=mode)
                receipt = self.envelope(result)["receipt"]
                self.assertEqual(result.returncode, 2)
                self.assertEqual(receipt["status"], "halt")
                self.assertIn(marker, receipt["error"])
                self.assertIsNone(self.envelope(result)["result"])

    def test_stream_and_transport_failures_halt(self) -> None:
        for mode in (
            "nonzero",
            "malformed_stdout",
            "no_terminal",
            "duplicate_thread",
            "no_rollout",
            "stale_rollout",
            "multiple_rollout",
            "second_turn",
            "second_meta",
        ):
            with self.subTest(mode=mode):
                result = self.run_dispatch(mode=mode)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.envelope(result)["receipt"]["status"], "halt")

    def test_delayed_rollout_flush_is_retried(self) -> None:
        result = self.run_dispatch(mode="delayed_turn")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_expanded_workspace_sandbox_halts(self) -> None:
        for mode in (
            "network_enabled",
            "missing_network",
            "missing_exclude_slash_tmp",
            "nonboolean_exclude_tmpdir",
            "extra_writable_root",
            "explicit_empty_writable_roots",
        ):
            with self.subTest(mode=mode):
                result = self.run_dispatch(route="builder", mode=mode)
                receipt = self.envelope(result)["receipt"]
                self.assertEqual(result.returncode, 2)
                self.assertIn("sandbox_policy", receipt["error"])

    def test_writable_rollout_file_or_parent_halts(self) -> None:
        for mode in ("writable_rollout", "writable_rollout_parent"):
            with self.subTest(mode=mode):
                result = self.run_dispatch(mode=mode)
                self.assertEqual(result.returncode, 2)
                receipt = self.envelope(result)["receipt"]
                self.assertIn("rollout provenance path", receipt["error"])

    def test_private_directory_and_rollout_chain_reject_darwin_allow_acl(self) -> None:
        allow = subprocess.CompletedProcess(
            args=["/bin/ls"],
            returncode=0,
            stdout="drwx------@ 1 user staff 32 Jul 12 00:00 path\n 0: user:other allow read\n",
            stderr="",
        )
        private = self.home / "private-state"
        with (
            mock.patch.object(CODEX_TIER.sys, "platform", "darwin"),
            mock.patch.object(CODEX_TIER.subprocess, "run", return_value=allow),
        ):
            with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "ACL allow"):
                CODEX_TIER.ensure_private_dir(private)

        rollout = self.home / "sessions" / "2026" / "07" / "12" / "rollout-test.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("{}\n")
        with (
            mock.patch.object(CODEX_TIER.sys, "platform", "darwin"),
            mock.patch.object(CODEX_TIER.subprocess, "run", return_value=allow),
        ):
            with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "ACL allow"):
                CODEX_TIER.validate_rollout_path(self.home, rollout)

        deny = subprocess.CompletedProcess(
            args=["/bin/ls"],
            returncode=0,
            stdout="drwx------@ 1 user staff 32 Jul 12 00:00 path\n 0: group:everyone deny delete\n",
            stderr="",
        )
        ledger_dir = self.home / "tier-routing"
        ledger_dir.mkdir(mode=0o700)
        ledger = ledger_dir / "ledger.jsonl"
        ledger.write_text("")
        ledger.chmod(0o600)

        def acl_by_path(
            args: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            return allow if str(args[-1]).endswith("ledger.jsonl") else deny

        with (
            mock.patch.object(CODEX_TIER.sys, "platform", "darwin"),
            mock.patch.object(CODEX_TIER.subprocess, "run", side_effect=acl_by_path),
        ):
            with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "ACL allow"):
                CODEX_TIER.preflight_ledger(self.home)

    def test_timeout_is_ambiguous_and_not_retried(self) -> None:
        result = self.run_dispatch(mode="timeout", timeout=1)
        receipt = self.envelope(result)["receipt"]
        self.assertEqual(result.returncode, 2)
        self.assertIn("ambiguous", receipt["error"])

    def test_timeout_kills_child_process_group(self) -> None:
        result = self.run_dispatch(mode="timeout_grandchild", timeout=1)
        self.assertEqual(result.returncode, 2)
        time.sleep(2.5)
        self.assertFalse((self.base / "grandchild-marker").exists())

    def test_stream_failure_kills_child_process_group(self) -> None:
        marker = self.base / "grandchild-marker"
        ready = self.base / "child-ready"
        child_code = (
            "import pathlib,subprocess,sys,time; sys.stdin.read(); "
            + f"pathlib.Path({str(ready)!r}).write_text('ready'); "
            + "subprocess.Popen([sys.executable, '-c', "
            + repr(
                f"import pathlib,time; time.sleep(1); pathlib.Path({str(marker)!r}).write_text('survived')"
            )
            + "], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
            + "sys.stdout.buffer.write(b'\\xff'); sys.stdout.buffer.flush()"
        )
        with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "non-UTF-8"):
            CODEX_TIER.run_codex(
                [sys.executable, "-c", child_code], "task", 10, self.workdir, self.home
            )
        self.assertEqual(ready.read_text(), "ready")
        time.sleep(1.5)
        self.assertFalse(marker.exists())

    def test_bounded_process_output_limits_kill_child_process_group(self) -> None:
        for stream_name in ("stdout", "stderr"):
            with self.subTest(stream=stream_name):
                marker = self.base / f"{stream_name}-limit-grandchild"
                stream = (
                    "sys.stdout.buffer"
                    if stream_name == "stdout"
                    else "sys.stderr.buffer"
                )
                child_code = (
                    "import pathlib,subprocess,sys,time; "
                    + "subprocess.Popen([sys.executable, '-c', "
                    + repr(
                        f"import pathlib,time; time.sleep(1); pathlib.Path({str(marker)!r}).write_text('survived')"
                    )
                    + "], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                    + f"{stream}.write(b'x' * 2048); {stream}.flush(); time.sleep(5)"
                )
                with self.assertRaisesRegex(
                    CODEX_TIER.DispatchHalt, "exceeded 1024 bytes"
                ):
                    CODEX_TIER.run_process_bounded(
                        [sys.executable, "-c", child_code],
                        input_bytes=b"",
                        cwd=self.workdir,
                        env=os.environ.copy(),
                        timeout=10,
                        stdout_limit=1024,
                        stderr_limit=1024,
                    )
                time.sleep(1.25)
                self.assertFalse(marker.exists())

    def test_rollout_and_ledger_entry_size_limits_halt(self) -> None:
        rollout = self.home / "oversized-rollout.jsonl"
        rollout.write_bytes(b"x" * 65)
        rollout.chmod(0o600)
        with mock.patch.object(CODEX_TIER, "MAX_ROLLOUT_BYTES", 64):
            with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "exceeds 64 bytes"):
                CODEX_TIER.read_rollout(rollout)

        ledger_dir = self.home / "tier-routing"
        ledger_dir.mkdir(mode=0o700, exist_ok=True)
        ledger = ledger_dir / "ledger.jsonl"
        ledger.write_text("x" * 65 + "\n", encoding="utf-8")
        ledger.chmod(0o600)
        with mock.patch.object(CODEX_TIER, "MAX_LEDGER_ENTRY_BYTES", 64):
            with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "size limit"):
                CODEX_TIER.preflight_ledger(self.home)

        ledger.write_text("x" * 65, encoding="utf-8")
        with (
            mock.patch.object(CODEX_TIER, "MAX_LEDGER_ENTRY_BYTES", 1024),
            mock.patch.object(CODEX_TIER, "MAX_LEDGER_BYTES", 64),
        ):
            with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "total size limit"):
                CODEX_TIER.preflight_ledger(self.home)

    def test_rollout_candidate_scan_has_aggregate_entry_limit(self) -> None:
        session_dir = self.home / "sessions" / "2026" / "07" / "13"
        session_dir.mkdir(parents=True)
        for index in range(3):
            (session_dir / f"unrelated-{index}.jsonl").write_text("{}\n")
        with mock.patch.object(CODEX_TIER, "MAX_ROLLOUT_SCAN_ENTRIES", 5):
            with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "entry limit"):
                CODEX_TIER.find_rollout(
                    self.home,
                    "00000000-0000-4000-8000-000000000001",
                    0,
                )

    def test_bounded_reader_control_flow_propagates_after_cleanup(self) -> None:
        for interruption in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(interruption=type(interruption).__name__):
                with (
                    mock.patch.object(
                        CODEX_TIER.selectors.DefaultSelector,
                        "select",
                        side_effect=interruption,
                    ),
                    mock.patch.object(
                        CODEX_TIER,
                        "reap_process_group",
                        wraps=CODEX_TIER.reap_process_group,
                    ) as reaper,
                ):
                    with self.assertRaises(type(interruption)):
                        CODEX_TIER.run_process_bounded(
                            [sys.executable, "-c", "import time; time.sleep(30)"],
                            input_bytes=b"",
                            cwd=self.workdir,
                            env=os.environ.copy(),
                            timeout=10,
                            stdout_limit=1024,
                            stderr_limit=1024,
                        )
                reaper.assert_called_once()

    def test_empty_and_oversized_prompts_stop_before_model_call(self) -> None:
        for prompt in ("", "x" * (256 * 1024 + 1)):
            with self.subTest(size=len(prompt)):
                result = self.run_dispatch(prompt=prompt)
                self.assertEqual(result.returncode, 2)
        self.assertFalse((self.home / "argv.json").exists())

    def test_symlinked_ledger_stops_before_model_call(self) -> None:
        ledger_dir = self.home / "tier-routing"
        ledger_dir.mkdir(mode=0o700)
        target = self.base / "target"
        target.write_text("untouched")
        (ledger_dir / "ledger.jsonl").symlink_to(target)
        result = self.run_dispatch()
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlinked", json.loads(result.stdout)["error"])
        self.assertEqual(target.read_text(), "untouched")
        self.assertFalse((self.home / "argv.json").exists())

    def test_ledger_is_private_and_hash_chained(self) -> None:
        first = self.run_dispatch("scout")
        second = self.run_dispatch("reviewer")
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        ledger = self.home / "tier-routing" / "ledger.jsonl"
        entries = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertEqual(len(entries), 2)
        self.assertIsNone(entries[0]["previous_hash"])
        self.assertEqual(entries[1]["previous_hash"], entries[0]["entry_hash"])
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
        self.assertEqual(ledger.parent.stat().st_mode & 0o777, 0o700)

        emitted = self.envelope(second)
        retained = entries[1]
        self.assertEqual(retained["receipt"], emitted["receipt"])
        without_hash = dict(retained)
        entry_hash = without_hash.pop("entry_hash")
        recomputed = hashlib.sha256(
            json.dumps(without_hash, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(recomputed, entry_hash)
        self.assertEqual(emitted["receipt_ref"], f"local-ledger://{entry_hash}")

    def test_concurrent_dispatches_preserve_ledger_chain(self) -> None:
        processes = []
        self._write_fake_mode(mode="success")
        for _ in range(4):
            env = os.environ.copy()
            env.update(
                {
                    "CODEX_HOME": str(self.home),
                    "CODEX_TIER_CODEX_BIN": str(self.fake),
                }
            )
            processes.append(
                subprocess.Popen(
                    [
                        str(self.launcher),
                        "dispatch",
                        "--route",
                        "scout",
                        "--workdir",
                        str(self.workdir),
                        "--timeout",
                        "10",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
            )

        envelopes = []
        for process in processes:
            stdout, stderr = process.communicate(input="concurrent task", timeout=15)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            envelopes.append(json.loads(stdout))

        ledger = self.home / "tier-routing" / "ledger.jsonl"
        entries = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertEqual(len(entries), 4)
        previous_hash = None
        for entry in entries:
            self.assertEqual(entry["previous_hash"], previous_hash)
            without_hash = dict(entry)
            entry_hash = without_hash.pop("entry_hash")
            self.assertEqual(
                hashlib.sha256(
                    json.dumps(
                        without_hash, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest(),
                entry_hash,
            )
            previous_hash = entry_hash
        self.assertEqual(
            {envelope["receipt_ref"] for envelope in envelopes},
            {f"local-ledger://{entry['entry_hash']}" for entry in entries},
        )

    def test_pass_and_halt_receipts_match_public_contract(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "local-dispatch-receipt.schema.json").read_text()
        )
        self.assertEqual(
            schema_keywords(schema)[0],
            {
                "$ref",
                "type",
                "additionalProperties",
                "required",
                "properties",
                "const",
                "format",
                "enum",
                "oneOf",
                "allOf",
                "if",
                "then",
                "pattern",
                "minLength",
                "maxItems",
            },
        )
        self.assertEqual(
            schema_keywords(schema)[1], {"$schema", "$id", "title", "$defs"}
        )
        for result in (
            self.run_dispatch(),
            self.run_dispatch(route="builder"),
            self.run_dispatch(mode="wrong_model"),
        ):
            envelope = self.envelope(result)
            receipt = envelope["receipt"]
            self.assertEqual(validate_local_schema(receipt, schema), [])
            self.assertRegex(envelope["receipt_ref"], r"^local-ledger://[0-9a-f]{64}$")
        passed = self.envelope(self.run_dispatch())
        self.assertEqual(
            passed["receipt"]["output_sha256"],
            hashlib.sha256(passed["result"].encode()).hexdigest(),
        )

    def test_pass_and_halt_envelopes_match_public_contract(self) -> None:
        receipt_schema = json.loads(
            (ROOT / "schemas" / "local-dispatch-receipt.schema.json").read_text()
        )
        envelope_schema = json.loads(
            (ROOT / "schemas" / "local-dispatch-envelope.schema.json").read_text()
        )
        expanded = expand_envelope_schema(envelope_schema, receipt_schema)
        passed = self.envelope(self.run_dispatch())
        halted = self.envelope(self.run_dispatch(mode="wrong_model"))
        self.assertEqual(validate_local_schema(passed, expanded), [])
        self.assertEqual(validate_local_schema(halted, expanded), [])

        invalid = json.loads(json.dumps(passed))
        invalid["result"] = None
        self.assertTrue(validate_local_schema(invalid, expanded))
        invalid = json.loads(json.dumps(halted))
        invalid["result"] = "must be null"
        self.assertTrue(validate_local_schema(invalid, expanded))
        invalid = json.loads(json.dumps(passed))
        invalid["receipt_ref"] = "local-ledger://not-a-hash"
        self.assertTrue(validate_local_schema(invalid, expanded))
        invalid = json.loads(json.dumps(passed))
        invalid["unexpected"] = True
        self.assertTrue(validate_local_schema(invalid, expanded))

    def test_surgeon_pass_and_halt_match_v3_contract_and_receipt_check(self) -> None:
        primary, linked = self.linked_worktree()
        starting_head = self.git("-C", str(linked), "rev-parse", "HEAD")
        passed = self.envelope(self.run_dispatch(route="surgeon", workdir=linked))
        halted = self.envelope(self.run_dispatch(route="surgeon", workdir=primary))
        late_halt = self.envelope(
            self.run_dispatch(route="surgeon", workdir=linked, mode="wrong_model")
        )

        receipt = passed["receipt"]
        self.assertEqual(receipt["schema"], CODEX_TIER.SURGEON_SCHEMA)
        self.assertEqual(receipt["status"], "pass")
        self.assertEqual(receipt["policy_id"], CODEX_TIER.DEFAULT_POLICY_V2_ID)
        self.assertEqual(
            receipt["workspace_precondition"],
            {
                "kind": "clean-linked-git-worktree",
                "starting_head": starting_head,
            },
        )
        self.assertEqual(
            receipt["policy_document"]["schema"], "codex-tier-routing/policy/v2"
        )
        self.assertEqual(
            set(receipt["policy_document"]["routes"]),
            {*CODEX_TIER.ROUTES, "surgeon"},
        )
        self.assertEqual(
            receipt["policy_document"]["routes"]["surgeon"],
            {
                "model": "gpt-5.6-sol",
                "effort": "high",
                "sandbox": "workspace-write",
                "approval_policy": "never",
            },
        )
        self.assertEqual(halted["receipt"]["schema"], CODEX_TIER.SURGEON_SCHEMA)
        self.assertIsNone(halted["receipt"]["workspace_precondition"])
        self.assertEqual(
            late_halt["receipt"]["workspace_precondition"],
            receipt["workspace_precondition"],
        )

        receipt_schema = json.loads(
            (ROOT / "schemas" / "local-dispatch-receipt-v3.schema.json").read_text()
        )
        envelope_schema = json.loads(
            (ROOT / "schemas" / "local-dispatch-envelope-v3.schema.json").read_text()
        )
        expanded = expand_envelope_schema(envelope_schema, receipt_schema)
        self.assertEqual(validate_local_schema(passed, expanded), [])
        self.assertEqual(validate_local_schema(halted, expanded), [])
        self.assertEqual(validate_local_schema(late_halt, expanded), [])

        env = {**os.environ, "CODEX_HOME": str(self.home)}
        for envelope in (passed, halted, late_halt):
            checked = subprocess.run(
                [str(self.launcher), "receipt-check"],
                input=json.dumps(envelope),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

        invalid = json.loads(json.dumps(passed))
        invalid["receipt"]["workspace_precondition"] = None
        self.assertTrue(validate_local_schema(invalid, expanded))
        checked = subprocess.run(
            [str(self.launcher), "receipt-check"],
            input=json.dumps(invalid),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(checked.returncode, 2)

        invalid = json.loads(json.dumps(passed))
        surgeon = invalid["receipt"]["policy_document"]["routes"]["surgeon"]
        surgeon["model"] = "gpt-owner-choice"
        invalid["receipt"]["requested"]["model"] = "gpt-owner-choice"
        invalid["receipt"]["observed"]["model"] = "gpt-owner-choice"
        invalid["receipt"]["policy_sha256"] = hashlib.sha256(
            json.dumps(
                invalid["receipt"]["policy_document"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        checked = subprocess.run(
            [str(self.launcher), "receipt-check"],
            input=json.dumps(invalid),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(checked.returncode, 2)

    def test_surgeon_custom_v2_policy_preserves_v1_input_hash(self) -> None:
        policy = self.write_policy(overrides={"scout": ("gpt-owner-choice", "max")})
        expected_input_hash = hashlib.sha256(policy.read_bytes()).hexdigest()
        _, linked = self.linked_worktree()
        result = self.run_dispatch(route="surgeon", workdir=linked)
        receipt = self.envelope(result)["receipt"]
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(receipt["policy_kind"], "custom")
        self.assertEqual(receipt["policy_id"], CODEX_TIER.CUSTOM_POLICY_V2_ID)
        self.assertEqual(receipt["policy_input_sha256"], expected_input_hash)
        self.assertEqual(
            receipt["policy_document"]["routes"]["scout"]["model"],
            "gpt-owner-choice",
        )
        self.assertEqual(
            receipt["policy_document"]["routes"]["surgeon"]["model"],
            "gpt-5.6-sol",
        )

    def test_surgeon_accepts_sha256_linked_worktree_head(self) -> None:
        _, linked = self.linked_worktree(object_format="sha256")
        result = self.run_dispatch(route="surgeon", workdir=linked)
        receipt = self.envelope(result)["receipt"]

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(
            receipt["workspace_precondition"]["starting_head"], r"^[0-9a-f]{64}$"
        )

    def test_surgeon_rejects_active_clean_filter_without_executing_it(self) -> None:
        for driver in ("tripwire", "unspecified", "unset"):
            with self.subTest(driver=driver):
                _, linked = self.linked_worktree()
                attributes = linked / ".gitattributes"
                attributes.write_text(
                    f"tracked.txt filter={driver}\n", encoding="utf-8"
                )
                self.git("-C", str(linked), "add", ".gitattributes")
                self.git(
                    "-C",
                    str(linked),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=fixture",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "-qm",
                    "attributes",
                )

                marker = self.base / f"clean-filter-{driver}-ran"
                filter_script = self.base / f"clean-filter-{driver}.sh"
                filter_script.write_text(
                    f"#!/bin/sh\ncat\nprintf ran > {shlex.quote(str(marker))}\n",
                    encoding="utf-8",
                )
                filter_script.chmod(0o700)
                self.git(
                    "-C",
                    str(linked),
                    "config",
                    f"filter.{driver}.clean",
                    shlex.quote(str(filter_script)),
                )

                result = self.run_dispatch(route="surgeon", workdir=linked)
                receipt = self.envelope(result)["receipt"]
                self.assertEqual(result.returncode, 2)
                self.assertIn("Git filter attributes", receipt["error"])
                self.assertFalse(marker.exists())

    def test_receipt_check_verifies_critical_semantics_offline(self) -> None:
        passed = self.envelope(self.run_dispatch())
        halted = self.envelope(self.run_dispatch(mode="wrong_model"))
        env = {**os.environ, "CODEX_HOME": str(self.home)}
        (self.home / "argv.json").unlink(missing_ok=True)
        for label, envelope in (("pass", passed), ("halt", halted)):
            with self.subTest(status=label):
                result = subprocess.run(
                    [str(self.launcher), "receipt-check"],
                    input=json.dumps(envelope),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    check=False,
                )
                payload = json.loads(result.stdout)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(payload["status"], "pass")
                self.assertEqual(payload["check"], "critical_semantics")

        retained = json.loads(json.dumps(passed))
        retained["receipt"]["policy_document"]["routes"]["scout"]["model"] = (
            "gpt-5.6-luna"
        )
        retained["receipt"]["requested"]["model"] = "gpt-5.6-luna"
        retained["receipt"]["observed"]["model"] = "gpt-5.6-luna"
        retained["receipt"]["policy_sha256"] = hashlib.sha256(
            json.dumps(
                retained["receipt"]["policy_document"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        result = subprocess.run(
            [str(self.launcher), "receipt-check"],
            input=json.dumps(retained),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.home / "argv.json").exists())

        mutations = (
            (
                "policy document",
                lambda value: value["receipt"]["policy_document"]["routes"][
                    "scout"
                ].update(model="gpt-5.6"),
            ),
            (
                "requested route",
                lambda value: value["receipt"]["requested"].update(effort="max"),
            ),
            ("result hash", lambda value: value.update(result="tampered")),
            (
                "receipt reference",
                lambda value: value.update(receipt_ref="local-ledger://bad"),
            ),
            ("extra field", lambda value: value.update(unexpected=True)),
        )
        for label, mutate in mutations:
            with self.subTest(tamper=label):
                invalid = json.loads(json.dumps(passed))
                mutate(invalid)
                result = subprocess.run(
                    [str(self.launcher), "receipt-check"],
                    input=json.dumps(invalid),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stdout)["status"], "halt")

    def test_receipt_check_file_contract_and_input_bound(self) -> None:
        envelope = self.envelope(self.run_dispatch())
        env = {**os.environ, "CODEX_HOME": str(self.home)}
        receipt_file = self.base / "receipt.json"
        receipt_file.write_text(json.dumps(envelope), encoding="utf-8")
        receipt_file.chmod(0o600)
        result = subprocess.run(
            [
                str(self.launcher),
                "receipt-check",
                "--receipt-file",
                str(receipt_file),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        receipt_file.chmod(0o644)
        result = subprocess.run(
            [
                str(self.launcher),
                "receipt-check",
                "--receipt-file",
                str(receipt_file),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("mode 0600", result.stdout)

        result = subprocess.run(
            [str(self.launcher), "receipt-check"],
            input="x" * (CODEX_TIER.MAX_RECEIPT_CHECK_BYTES + 1),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("size limit", result.stdout)

    def test_schema_validator_rejects_every_published_constraint_family(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "local-dispatch-receipt.schema.json").read_text()
        )
        valid = self.envelope(self.run_dispatch())["receipt"]

        def mutated(change: callable) -> list[str]:
            value = json.loads(json.dumps(valid))
            change(value)
            return validate_local_schema(value, schema)

        cases = {
            "required": lambda value: value.pop("schema"),
            "additionalProperties": lambda value: value.update(unexpected=True),
            "const": lambda value: value.update(mechanism="other"),
            "enum": lambda value: value.update(status="unknown"),
            "pattern": lambda value: value.update(codex_executable_sha256="g" * 64),
            "minLength": lambda value: value.update(codex_executable=""),
            "type": lambda value: value.update(error=7),
            "oneOf": lambda value: value.update(output_sha256=7),
            "uuid format": lambda value: value.update(
                receipt_id="00000000-0000-0000-0000-000000000000"
            ),
            "uuid lowercase pattern": lambda value: value.update(
                receipt_id="123E4567-E89B-42D3-A456-426614174000"
            ),
            "date-time calendar pattern": lambda value: value.update(
                started_at="2026-02-30T12:00:00Z"
            ),
            "pass conditional": lambda value: value.update(
                error="must be null for pass"
            ),
            "halt conditional": lambda value: value.update(
                status="halt",
                observed=None,
                output_sha256=None,
                rollout_sha256=None,
                error="",
            ),
            "route resolution mismatch": lambda value: value.update(
                route_resolved="architect"
            ),
            "requested model syntax": lambda value: value["requested"].update(
                model="provider/model"
            ),
            "observed effort enum": lambda value: value["observed"].update(
                effort="turbo"
            ),
            "policy id pattern": lambda value: value.update(policy_id="Bad Policy"),
            "policy hash pattern": lambda value: value.update(policy_sha256="0"),
        }
        for label, change in cases.items():
            with self.subTest(label=label):
                value = json.loads(json.dumps(valid))
                change(value)
                self.assertTrue(validate_local_schema(value, schema), label)
                self.assertFalse(ordinary_draft202012_valid(value, schema), label)

        for field, replacement in (
            ("observed", None),
            ("output_sha256", None),
            ("rollout_sha256", None),
            ("error", "not-null"),
        ):
            with self.subTest(pass_field=field):
                self.assertTrue(
                    mutated(lambda value, f=field, r=replacement: value.update({f: r}))
                )
        for field, replacement in (
            ("output_sha256", "0" * 64),
            ("rollout_sha256", "0" * 64),
            ("error", ""),
        ):
            with self.subTest(halt_field=field):

                def halt_mutation(
                    value: dict, f: str = field, r: object = replacement
                ) -> None:
                    value.update(
                        status="halt",
                        observed=None,
                        output_sha256=None,
                        rollout_sha256=None,
                        error="halt",
                    )
                    value[f] = r

                self.assertTrue(mutated(halt_mutation))

        workspace = self.envelope(self.run_dispatch(route="builder"))["receipt"]
        self.assertEqual(validate_local_schema(workspace, schema), [])
        self.assertTrue(ordinary_draft202012_valid(workspace, schema))
        for label, change in (
            ("missing network", lambda policy: policy.pop("network_access")),
            ("network enabled", lambda policy: policy.update(network_access=True)),
            ("missing exclusion", lambda policy: policy.pop("exclude_slash_tmp")),
            (
                "wrong exclusion type",
                lambda policy: policy.update(exclude_tmpdir_env_var="false"),
            ),
            ("missing roots", lambda policy: policy.pop("writable_roots")),
            (
                "nonempty roots",
                lambda policy: policy.update(writable_roots=["/unexpected"]),
            ),
            ("extra policy key", lambda policy: policy.update(unexpected=True)),
        ):
            with self.subTest(workspace_policy=label):
                value = json.loads(json.dumps(workspace))
                change(value["observed"]["sandbox_policy"])
                self.assertTrue(validate_local_schema(value, schema), label)
                self.assertFalse(ordinary_draft202012_valid(value, schema), label)

        self.write_policy(overrides={"scout": ("gpt-5.6", "none")})
        custom = self.envelope(self.run_dispatch())["receipt"]
        self.assertEqual(custom["policy_kind"], "custom")
        self.assertEqual(validate_local_schema(custom, schema), [])
        self.assertTrue(ordinary_draft202012_valid(custom, schema))

        for label, sandbox, policy in (
            (
                "workspace with read-only policy",
                "workspace-write",
                {"type": "read-only"},
            ),
            (
                "read-only with workspace policy",
                "read-only",
                {
                    "type": "workspace-write",
                    "network_access": False,
                    "exclude_slash_tmp": False,
                    "exclude_tmpdir_env_var": False,
                },
            ),
        ):
            with self.subTest(cross_branch_policy=label):
                value = json.loads(json.dumps(valid))
                value["observed"]["sandbox"] = sandbox
                value["observed"]["sandbox_policy"] = policy
                self.assertTrue(validate_local_schema(value, schema), label)
                self.assertFalse(ordinary_draft202012_valid(value, schema), label)

        # This published pattern—not an optional format checker—carries the
        # deterministic receipt profile. The independent ordinary validator is
        # intentionally format-disabled.
        valid_datetimes = (
            "0000-02-29t00:00:00z",
            "2000-02-29t23:59:59z",
            "2026-07-12t12:34:56.123+05:30",
            "2026-12-31t23:59:59-23:59",
        )
        invalid_datetimes = (
            "1900-02-29t12:00:00z",
            "2100-02-29t12:00:00z",
            "2026-04-31t12:00:00z",
            "2026-02-30t12:00:00z",
            "2026-07-12t24:00:00z",
            "2026-07-12t12:60:00z",
            # The deterministic profile rejects both a historically known
            # leap-second spelling and arbitrary :60 values.
            "2016-12-31t23:59:60z",
            "2026-07-12t12:34:60z",
            "2026-12-31t23:59:60-23:59",
            "2026-07-12t12:34:56+24:00",
            "2026-07-12t12:34:56+00:60",
            "2026-07-12t12:34:56+05:30:01",
        )
        for timestamp in valid_datetimes:
            with self.subTest(valid_timestamp=timestamp):
                value = json.loads(json.dumps(valid))
                value["started_at"] = timestamp
                self.assertEqual(validate_local_schema(value, schema), [])
                self.assertTrue(ordinary_draft202012_valid(value, schema))
                self.assertTrue(is_rfc3339_datetime(timestamp))
        for timestamp in invalid_datetimes:
            with self.subTest(invalid_timestamp=timestamp):
                value = json.loads(json.dumps(valid))
                value["started_at"] = timestamp
                self.assertTrue(validate_local_schema(value, schema))
                self.assertFalse(ordinary_draft202012_valid(value, schema))
                self.assertFalse(is_rfc3339_datetime(timestamp))
        self.assertTrue(
            is_canonical_rfc4122_uuid("123e4567-e89b-42d3-a456-426614174000")
        )
        for invalid in (
            "123E4567-E89B-42D3-A456-426614174000",
            "123e4567-e89b-02d3-a456-426614174000",
        ):
            self.assertFalse(is_canonical_rfc4122_uuid(invalid))

    def test_workdir_and_codex_home_must_be_disjoint(self) -> None:
        nested_work = self.home / "work"
        nested_work.mkdir()
        for workdir in (self.home, self.base, nested_work):
            with self.subTest(workdir=workdir):
                if (self.home / "argv.json").exists():
                    (self.home / "argv.json").unlink()
                result = self.run_dispatch(workdir=workdir)
                self.assertEqual(result.returncode, 2)
                self.assertIn("must not contain", json.loads(result.stdout)["error"])
                self.assertFalse((self.home / "argv.json").exists())

    def test_prompt_file_contract(self) -> None:
        valid = self.base / "prompt.txt"
        valid.write_text("prompt-file task", encoding="utf-8")
        valid.chmod(0o600)
        self.assertEqual(self.run_dispatch(prompt_file=valid).returncode, 0)

        cases = []
        symlink = self.base / "prompt-link"
        symlink.symlink_to(valid)
        cases.append(symlink)
        directory = self.base / "prompt-dir"
        directory.mkdir()
        cases.append(directory)
        fifo = self.base / "prompt-fifo"
        os.mkfifo(fifo, 0o600)
        cases.append(fifo)
        permissive = self.base / "prompt-permissive"
        permissive.write_text("task")
        permissive.chmod(0o644)
        cases.append(permissive)
        invalid_utf8 = self.base / "prompt-invalid"
        invalid_utf8.write_bytes(b"\xff")
        invalid_utf8.chmod(0o600)
        cases.append(invalid_utf8)
        oversized = self.base / "prompt-oversized"
        oversized.write_bytes(b"x" * (256 * 1024 + 1))
        oversized.chmod(0o600)
        cases.append(oversized)
        for path in cases:
            with self.subTest(path=path.name):
                argv_record = self.home / "argv.json"
                if argv_record.exists():
                    argv_record.unlink()
                result = self.run_dispatch(
                    prompt_file=path,
                    harness_timeout=5 if path == fifo else 15,
                )
                self.assertEqual(result.returncode, 2)
                self.assertFalse(argv_record.exists())

    def test_zero_and_negative_timeout_halt(self) -> None:
        for timeout in (0, -1):
            result = self.run_dispatch(timeout=timeout)
            self.assertEqual(result.returncode, 2)
            self.assertIn(
                "timeout must be positive", json.loads(result.stdout)["error"]
            )

    def test_probe_with_missing_codex_is_structured_halt(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "CODEX_HOME": str(self.home),
                "CODEX_TIER_CODEX_BIN": str(self.base / "missing-codex"),
            }
        )
        result = subprocess.run(
            [str(self.launcher), "probe"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "halt")

    def test_codex_home_mode_is_not_silently_changed(self) -> None:
        self.home.chmod(0o755)
        result = self.run_dispatch()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.home.stat().st_mode & 0o777, 0o755)

    def test_fifth_dispatch_succeeds_after_slot_release(self) -> None:
        lock_dir = self.home / "tier-routing" / "locks"
        lock_dir.mkdir(parents=True, mode=0o700)
        handles = []
        for index in range(4):
            handle = (lock_dir / f"dispatch-{index}.lock").open("a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handles.append(handle)
        released = handles.pop()
        fcntl.flock(released.fileno(), fcntl.LOCK_UN)
        released.close()
        result = self.run_dispatch()
        for handle in handles:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        self.assertEqual(result.returncode, 0)

    def test_concurrency_cap_stops_before_model_call(self) -> None:
        lock_dir = self.home / "tier-routing" / "locks"
        lock_dir.mkdir(parents=True, mode=0o700)
        handles = []
        try:
            for index in range(4):
                handle = (lock_dir / f"dispatch-{index}.lock").open("a+")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                handles.append(handle)
            result = self.run_dispatch()
        finally:
            for handle in handles:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
        receipt = self.envelope(result)["receipt"]
        self.assertEqual(result.returncode, 2)
        self.assertIn("concurrency cap", receipt["error"])
        self.assertFalse((self.home / "argv.json").exists())

    def test_workspace_writer_lock_stops_same_workdir_before_model_call(self) -> None:
        lock_path = self.writer_lock_path(self.workdir)
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path.parent.chmod(0o700)
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_dispatch(route="builder")
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        receipt = self.envelope(result)["receipt"]
        self.assertEqual(result.returncode, 2)
        self.assertIn("workspace writer lock", receipt["error"])
        self.assertFalse((self.home / "argv.json").exists())

    def test_surgeon_worktree_preconditions_halt_before_model_call(self) -> None:
        cases: list[tuple[str, Path, str]] = []

        primary, _ = self.linked_worktree()
        cases.append(("primary", primary, "linked Git worktree"))

        _, tracked_dirty = self.linked_worktree()
        (tracked_dirty / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        cases.append(("tracked dirty", tracked_dirty, "must be clean"))

        _, untracked_dirty = self.linked_worktree()
        (untracked_dirty / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        cases.append(("untracked dirty", untracked_dirty, "must be clean"))

        _, no_head = self.linked_worktree()
        self.git(
            "-C",
            str(no_head),
            "symbolic-ref",
            "HEAD",
            f"refs/heads/missing-{uuid.uuid4().hex}",
        )
        cases.append(("no HEAD", no_head, "valid HEAD"))

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
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "gitlink",
        )
        cases.append(("submodule", submodule, "submodules"))

        _, linked = self.linked_worktree()
        nested = linked / "nested"
        nested.mkdir()
        cases.append(("non-root", nested, "worktree root"))

        for label, workdir, marker in cases:
            with self.subTest(case=label):
                (self.home / "argv.json").unlink(missing_ok=True)
                result = self.run_dispatch(route="surgeon", workdir=workdir)
                receipt = self.envelope(result)["receipt"]
                self.assertEqual(result.returncode, 2)
                self.assertIn(marker, receipt["error"])
                self.assertIsNone(receipt["workspace_precondition"])
                self.assertFalse((self.home / "argv.json").exists())
                with CODEX_TIER.WorkspaceWriterLock(self.home, workdir):
                    pass

    def test_surgeon_writer_lock_precedes_worktree_preflight(self) -> None:
        _, linked = self.linked_worktree()
        (linked / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        lock_path = self.writer_lock_path(linked)
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path.parent.chmod(0o700)
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_dispatch(route="surgeon", workdir=linked)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        receipt = self.envelope(result)["receipt"]
        self.assertEqual(result.returncode, 2)
        self.assertIn("workspace writer lock", receipt["error"])
        self.assertNotIn("must be clean", receipt["error"])
        self.assertIsNone(receipt["workspace_precondition"])
        self.assertFalse((self.home / "argv.json").exists())

    def test_workspace_writer_lock_alias_stops_before_model_call(self) -> None:
        alias = self.base / "work-alias"
        alias.symlink_to(self.workdir, target_is_directory=True)
        lock_path = self.writer_lock_path(self.workdir)
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path.parent.chmod(0o700)
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_dispatch(route="builder", workdir=alias)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        receipt = self.envelope(result)["receipt"]
        self.assertEqual(result.returncode, 2)
        self.assertIn("workspace writer lock", receipt["error"])
        self.assertFalse((self.home / "argv.json").exists())

    def test_workspace_writer_lock_allows_other_workdir_and_read_only_route(
        self,
    ) -> None:
        lock_path = self.writer_lock_path(self.workdir)
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path.parent.chmod(0o700)
        other_workdir = self.base / "other-work"
        other_workdir.mkdir()
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(
                self.run_dispatch(route="builder", workdir=other_workdir).returncode, 0
            )
            self.assertEqual(self.run_dispatch(route="scout").returncode, 0)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def test_workspace_writer_lock_releases_after_dispatch_failure(self) -> None:
        failed = self.run_dispatch(route="builder", mode="nonzero")
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(self.run_dispatch(route="builder").returncode, 0)

    def test_workspace_writer_lock_releases_for_in_process_reacquire(self) -> None:
        with CODEX_TIER.WorkspaceWriterLock(self.home, self.workdir):
            with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "already held"):
                with CODEX_TIER.WorkspaceWriterLock(self.home, self.workdir):
                    pass
        with CODEX_TIER.WorkspaceWriterLock(self.home, self.workdir):
            pass

    def test_workspace_writer_lock_is_shared_across_codex_homes(self) -> None:
        other_home = self.base / "other-codex-home"
        other_home.mkdir(mode=0o700)
        holder = CODEX_TIER.WorkspaceWriterLock(other_home, self.workdir)
        contender = CODEX_TIER.WorkspaceWriterLock(self.home, self.workdir)
        self.assertEqual(holder._path, contender._path)
        with holder:
            result = self.run_dispatch(route="builder")
        receipt = self.envelope(result)["receipt"]
        self.assertEqual(result.returncode, 2)
        self.assertIn("workspace writer lock", receipt["error"])
        self.assertFalse((self.home / "argv.json").exists())

    def test_workspace_writer_lock_rejects_unsafe_shared_state(self) -> None:
        target = self.base / "lock-target"
        target.mkdir(mode=0o700)
        linked = self.base / "linked-lock-directory"
        linked.symlink_to(target, target_is_directory=True)
        with (
            mock.patch.object(
                CODEX_TIER, "workspace_writer_lock_dir", return_value=linked
            ),
            self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "symlinked"),
        ):
            with CODEX_TIER.WorkspaceWriterLock(self.home, self.workdir):
                pass

        permissive = self.base / "permissive-lock-directory"
        permissive.mkdir(mode=0o700)
        permissive.chmod(0o755)
        with (
            mock.patch.object(
                CODEX_TIER, "workspace_writer_lock_dir", return_value=permissive
            ),
            self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "mode 0700"),
        ):
            with CODEX_TIER.WorkspaceWriterLock(self.home, self.workdir):
                pass

        special = self.base / "special-lock-directory"
        special.mkdir(mode=0o700)
        with mock.patch.object(
            CODEX_TIER, "workspace_writer_lock_dir", return_value=special
        ):
            lock = CODEX_TIER.WorkspaceWriterLock(self.home, self.workdir)
            os.mkfifo(lock._path, mode=0o600)
            with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "regular file"):
                with lock:
                    pass

    def test_workspace_writer_lock_converts_setup_errors(self) -> None:
        missing = self.base / "missing-workdir"
        with self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "writer lock workdir"):
            CODEX_TIER.WorkspaceWriterLock(self.home, missing)

        lock = CODEX_TIER.WorkspaceWriterLock(self.home, self.workdir)
        real_open = os.open
        real_close = os.close
        descriptors: list[int] = []

        def recording_open(*args: object, **kwargs: object) -> int:
            descriptor = real_open(*args, **kwargs)  # type: ignore[arg-type]
            descriptors.append(descriptor)
            return descriptor

        with (
            mock.patch.object(CODEX_TIER.os, "open", side_effect=recording_open),
            mock.patch.object(
                CODEX_TIER.os, "fdopen", side_effect=OSError("synthetic fdopen failure")
            ),
            mock.patch.object(CODEX_TIER.os, "close", wraps=real_close) as close,
            self.assertRaisesRegex(CODEX_TIER.DispatchHalt, "open writer lock handle"),
        ):
            lock.__enter__()
        self.assertEqual(len(descriptors), 1)
        close.assert_any_call(descriptors[0])
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])

        lock_dir = self.base / "collision-lock-directory"
        lock_dir.write_text("collision", encoding="utf-8")
        with (
            mock.patch.object(
                CODEX_TIER, "workspace_writer_lock_dir", return_value=lock_dir
            ),
            self.assertRaisesRegex(
                CODEX_TIER.DispatchHalt, "workspace writer lock directory"
            ),
        ):
            with CODEX_TIER.WorkspaceWriterLock(self.home, self.workdir):
                pass

    def test_tampered_ledger_stops_before_model_call(self) -> None:
        first = self.run_dispatch()
        self.assertEqual(first.returncode, 0)
        ledger = self.home / "tier-routing" / "ledger.jsonl"
        entry = json.loads(ledger.read_text())
        entry["receipt"]["route_requested"] = "architect"
        ledger.write_text(json.dumps(entry) + "\n")
        (self.home / "argv.json").unlink()
        result = self.run_dispatch()
        self.assertEqual(result.returncode, 2)
        self.assertIn("ledger is invalid", json.loads(result.stdout)["error"])
        self.assertFalse((self.home / "argv.json").exists())


if __name__ == "__main__":
    unittest.main()
