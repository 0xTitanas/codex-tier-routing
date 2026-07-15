"""
Fail-closed local Codex tier dispatcher.

This wrapper fills the portability gap on Codex surfaces that cannot select a
registered custom agent. It invokes the stock ``codex exec`` command with a
fixed route, then verifies the route against Codex's host-written rollout.
Worker output is returned as untrusted data and never supplies attestation.
"""

from __future__ import annotations

import argparse
import copy
from contextlib import nullcontext
import difflib
import hashlib
import json
import os
import re
import selectors
import signal
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl


SCHEMA = "codex-tier-routing/local-dispatch-receipt/v2"
SURGEON_SCHEMA = "codex-tier-routing/local-dispatch-receipt/v3"
POLICY_SCHEMA = "codex-tier-routing/policy/v1"
POLICY_V2_SCHEMA = "codex-tier-routing/policy/v2"
PROJECT_VERSION = "1.1.0-rc.1"
DEFAULT_POLICY_ID = "default-v1"
CUSTOM_POLICY_ID = "custom-v1"
DEFAULT_POLICY_V2_ID = "default-v2"
CUSTOM_POLICY_V2_ID = "custom-v2"
MAX_POLICY_BYTES = 64 * 1024
MAX_RECEIPT_CHECK_BYTES = 2 * 1024 * 1024
ACCEPTED_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
GIT_OBJECT_ID_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
LEDGER_REF_PATTERN = re.compile(r"local-ledger://[0-9a-f]{64}\Z")
MODEL_CATALOG_URL = "https://developers.openai.com/api/docs/models"
MAX_PROMPT_BYTES = 256 * 1024
MAX_CONTROL_STDOUT_BYTES = 256 * 1024
MAX_CONTROL_STDERR_BYTES = 256 * 1024
MAX_DISPATCH_STDOUT_BYTES = 16 * 1024 * 1024
MAX_DISPATCH_STDERR_BYTES = 2 * 1024 * 1024
MAX_ROLLOUT_BYTES = 32 * 1024 * 1024
MAX_LEDGER_ENTRY_BYTES = 1024 * 1024
MAX_LEDGER_BYTES = 128 * 1024 * 1024
MAX_LEDGER_ENTRIES = 250_000
MAX_ROLLOUT_SCAN_ENTRIES = 250_000
DEFAULT_TIMEOUT_SECONDS = 900
ROLLOUT_WAIT_SECONDS = 5.0
STRICT_MANIFEST_SCHEMA = "codex-tier-routing/strict-config/v1"


@dataclass(frozen=True)
class Route:
    model: str
    effort: str
    sandbox: str


@dataclass(frozen=True)
class RoutingPolicy:
    """Validated model/effort selections for the fixed route boundaries."""

    kind: str
    policy_id: str
    sha256: str
    input_sha256: str
    document: dict[str, Any]
    routes: dict[str, Route]


@dataclass(frozen=True)
class CodexLaunch:
    """Validated entrypoint and the runtime used instead of its shebang."""

    entrypoint: Path
    runtime: Path

    def command(self, *arguments: str) -> list[str]:
        if self.runtime == self.entrypoint:
            return [str(self.entrypoint), *arguments]
        return [str(self.runtime), str(self.entrypoint), *arguments]

    @property
    def method(self) -> str:
        return (
            "native" if self.runtime == self.entrypoint else "explicit-shebang-runtime"
        )


ROUTES: dict[str, Route] = {
    "scout": Route("gpt-5.6-terra", "low", "read-only"),
    "grunt": Route("gpt-5.6-luna", "low", "workspace-write"),
    "builder": Route("gpt-5.6-terra", "medium", "workspace-write"),
    "reviewer": Route("gpt-5.6-sol", "high", "read-only"),
    "architect": Route("gpt-5.6-sol", "xhigh", "read-only"),
}
SURGEON_ROUTE = Route("gpt-5.6-sol", "high", "workspace-write")

RECEIPT_FIELDS = {
    "schema",
    "receipt_id",
    "mechanism",
    "claim_class",
    "route_requested",
    "route_resolved",
    "policy_kind",
    "policy_id",
    "policy_sha256",
    "policy_input_sha256",
    "policy_document",
    "requested",
    "dispatcher_launcher",
    "dispatcher_launcher_sha256",
    "dispatcher_runtime",
    "dispatcher_runtime_sha256",
    "dispatcher_python",
    "dispatcher_python_sha256",
    "codex_executable",
    "codex_executable_sha256",
    "codex_runtime",
    "codex_runtime_sha256",
    "codex_launch_method",
    "observed",
    "status",
    "started_at",
    "finished_at",
    "output_sha256",
    "rollout_sha256",
    "error",
}
SURGEON_RECEIPT_FIELDS = RECEIPT_FIELDS | {"workspace_precondition"}


def policy_payload(policy_id: str, routes: dict[str, Route]) -> dict[str, Any]:
    return {
        "schema": POLICY_SCHEMA,
        "policy_id": policy_id,
        "routes": {
            name: {"model": route.model, "effort": route.effort}
            for name, route in routes.items()
        },
    }


def effective_policy_document(
    policy_id: str,
    routes: dict[str, Route],
    *,
    schema: str = POLICY_SCHEMA,
) -> dict[str, Any]:
    return {
        "schema": schema,
        "policy_id": policy_id,
        "routes": {
            name: {
                "model": route.model,
                "effort": route.effort,
                "sandbox": route.sandbox,
                "approval_policy": "never",
            }
            for name, route in routes.items()
        },
    }


def default_policy() -> RoutingPolicy:
    source = policy_payload(DEFAULT_POLICY_ID, ROUTES)
    document = effective_policy_document(DEFAULT_POLICY_ID, ROUTES)
    return RoutingPolicy(
        kind="builtin",
        policy_id=DEFAULT_POLICY_ID,
        sha256=sha256_bytes(canonical_json(document)),
        input_sha256=sha256_bytes(canonical_json(source)),
        document=document,
        routes=dict(ROUTES),
    )


def surgeon_policy(policy: RoutingPolicy) -> RoutingPolicy:
    routes = {**policy.routes, "surgeon": SURGEON_ROUTE}
    policy_id = (
        DEFAULT_POLICY_V2_ID if policy.kind == "builtin" else CUSTOM_POLICY_V2_ID
    )
    document = effective_policy_document(policy_id, routes, schema=POLICY_V2_SCHEMA)
    return RoutingPolicy(
        kind=policy.kind,
        policy_id=policy_id,
        sha256=sha256_bytes(canonical_json(document)),
        input_sha256=policy.input_sha256,
        document=document,
        routes=routes,
    )


class DispatchHalt(RuntimeError):
    """A fail-closed dispatch outcome."""


if os.name == "nt":
    _ADVAPI32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.GetCurrentProcess.restype = wintypes.HANDLE
    _KERNEL32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    _KERNEL32.GetFileAttributesW.restype = wintypes.DWORD
    _KERNEL32.MoveFileExW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    _KERNEL32.MoveFileExW.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32.LocalFree.argtypes = [wintypes.HLOCAL]
    _KERNEL32.LocalFree.restype = wintypes.HLOCAL
    _ADVAPI32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _ADVAPI32.OpenProcessToken.restype = wintypes.BOOL
    _ADVAPI32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _ADVAPI32.GetTokenInformation.restype = wintypes.BOOL
    _ADVAPI32.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    _ADVAPI32.ConvertSidToStringSidW.restype = wintypes.BOOL

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", wintypes.LPVOID), ("attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("user", _SidAndAttributes)]

    class _AceHeader(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_ubyte),
            ("flags", ctypes.c_ubyte),
            ("size", wintypes.WORD),
        ]

    class _AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("ace_count", wintypes.DWORD),
            ("acl_bytes_in_use", wintypes.DWORD),
            ("acl_bytes_free", wintypes.DWORD),
        ]

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("internal", ctypes.c_size_t),
            ("internal_high", ctypes.c_size_t),
            ("offset", wintypes.DWORD),
            ("offset_high", wintypes.DWORD),
            ("event", wintypes.HANDLE),
        ]

    class _JobBasicLimits(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time", ctypes.c_int64),
            ("per_job_user_time", ctypes.c_int64),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set", ctypes.c_size_t),
            ("maximum_working_set", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_operation_count", ctypes.c_uint64),
            ("write_operation_count", ctypes.c_uint64),
            ("other_operation_count", ctypes.c_uint64),
            ("read_transfer_count", ctypes.c_uint64),
            ("write_transfer_count", ctypes.c_uint64),
            ("other_transfer_count", ctypes.c_uint64),
        ]

    class _JobExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", _JobBasicLimits),
            ("io", _IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t),
            ("peak_job_memory", ctypes.c_size_t),
        ]

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("thread_id", wintypes.DWORD),
            ("owner_process_id", wintypes.DWORD),
            ("base_priority", wintypes.LONG),
            ("priority_delta", wintypes.LONG),
            ("flags", wintypes.DWORD),
        ]

    _ADVAPI32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    _ADVAPI32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _ADVAPI32.GetAclInformation.argtypes = [
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    _ADVAPI32.GetAclInformation.restype = wintypes.BOOL
    _ADVAPI32.GetAce.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    _ADVAPI32.GetAce.restype = wintypes.BOOL
    _ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    _ADVAPI32.GetSecurityDescriptorDacl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _ADVAPI32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _ADVAPI32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    _ADVAPI32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    _KERNEL32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _KERNEL32.LockFileEx.restype = wintypes.BOOL
    _KERNEL32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _KERNEL32.UnlockFileEx.restype = wintypes.BOOL
    _KERNEL32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _KERNEL32.CreateJobObjectW.restype = wintypes.HANDLE
    _KERNEL32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _KERNEL32.SetInformationJobObject.restype = wintypes.BOOL
    _KERNEL32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _KERNEL32.AssignProcessToJobObject.restype = wintypes.BOOL
    _KERNEL32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _KERNEL32.TerminateJobObject.restype = wintypes.BOOL
    _KERNEL32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _KERNEL32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _KERNEL32.Thread32First.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ThreadEntry32),
    ]
    _KERNEL32.Thread32First.restype = wintypes.BOOL
    _KERNEL32.Thread32Next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ThreadEntry32),
    ]
    _KERNEL32.Thread32Next.restype = wintypes.BOOL
    _KERNEL32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _KERNEL32.OpenThread.restype = wintypes.HANDLE
    _KERNEL32.ResumeThread.argtypes = [wintypes.HANDLE]
    _KERNEL32.ResumeThread.restype = wintypes.DWORD


_WINDOWS_TRUSTED_OWNER_SIDS = {
    "S-1-5-18",  # LocalSystem
    "S-1-5-32-544",  # BUILTIN\\Administrators
    # NT SERVICE\\TrustedInstaller, the normal owner of protected OS roots.
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
}
_WINDOWS_REPARSE_POINT = 0x400
_WINDOWS_DIRECTORY = 0x10
_WINDOWS_CREATE_SUSPENDED = 0x00000004
_WINDOWS_SAFE_NAME_SURROGATE_TAGS = {0xA0000003, 0xA000000C}
_WINDOWS_INHERIT_ONLY_ACE = 0x08
_WINDOWS_ALLOWED_ACE = 0x00
_WINDOWS_DENIED_ACES = {0x01, 0x06, 0x0A, 0x0C}
_WINDOWS_REPLACE_RIGHTS = 0x10000000 | 0x00080000 | 0x00040000 | 0x00010000 | 0x40
_WINDOWS_WRITE_RIGHTS = (
    _WINDOWS_REPLACE_RIGHTS | 0x40000000 | 0x02 | 0x04 | 0x10 | 0x100
)
_WINDOWS_CURRENT_SID: str | None = None


def _windows_error(operation: str, code: int | None = None) -> OSError:
    error = ctypes.get_last_error() if code is None else code
    return OSError(error, f"{operation}: {ctypes.FormatError(error).strip()}")


def _windows_sid_string(sid: int | ctypes.c_void_p) -> str:
    converted = wintypes.LPWSTR()
    if not _ADVAPI32.ConvertSidToStringSidW(sid, ctypes.byref(converted)):
        raise _windows_error("unable to convert Windows SID")
    try:
        return converted.value
    finally:
        _KERNEL32.LocalFree(converted)


def _windows_current_sid() -> str:
    global _WINDOWS_CURRENT_SID
    if _WINDOWS_CURRENT_SID is not None:
        return _WINDOWS_CURRENT_SID
    token = wintypes.HANDLE()
    if not _ADVAPI32.OpenProcessToken(
        _KERNEL32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
    ):
        raise _windows_error("unable to inspect the current Windows identity")
    try:
        required = wintypes.DWORD()
        _ADVAPI32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        if required.value == 0:
            raise _windows_error("unable to size the current Windows identity")
        buffer = ctypes.create_string_buffer(required.value)
        if not _ADVAPI32.GetTokenInformation(
            token, 1, buffer, required, ctypes.byref(required)
        ):
            raise _windows_error("unable to read the current Windows identity")
        user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        _WINDOWS_CURRENT_SID = _windows_sid_string(user.user.sid)
        return _WINDOWS_CURRENT_SID
    finally:
        _KERNEL32.CloseHandle(token)


def _windows_trusted_owner_sids() -> set[str]:
    return {*_WINDOWS_TRUSTED_OWNER_SIDS, _windows_current_sid()}


def _windows_trusted_acl_sids() -> set[str]:
    # OWNER RIGHTS is safe only because the object's actual owner is checked
    # against the narrower owner allowlist before any ACE is accepted.
    return {*_windows_trusted_owner_sids(), "S-1-3-4"}


def _windows_attributes(path: Path, *, label: str) -> int:
    attributes = _KERNEL32.GetFileAttributesW(str(path))
    if attributes == 0xFFFFFFFF:
        raise DispatchHalt(f"unable to inspect {label}: {_windows_error(label)}")
    return attributes


def _windows_validate_acl_ace(
    address: int, *, label: str, dangerous: int, trusted: set[str]
) -> None:
    header = _AceHeader.from_address(address)
    if header.flags & _WINDOWS_INHERIT_ONLY_ACE:
        return
    mask = ctypes.c_uint32.from_address(address + 4).value
    if not mask & dangerous or header.type in _WINDOWS_DENIED_ACES:
        return
    if header.type != _WINDOWS_ALLOWED_ACE:
        raise DispatchHalt(f"{label} has an unverifiable Windows ACE")
    sid = _windows_sid_string(address + 8)
    if sid not in trusted:
        inherited = "inherited " if header.flags & 0x10 else ""
        raise DispatchHalt(
            f"{label} grants {inherited}write access outside the trusted Windows identities"
        )


def _windows_acl(path: Path, *, label: str, final: bool) -> None:
    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    result = _ADVAPI32.GetNamedSecurityInfoW(
        str(path),
        1,
        0x00000001 | 0x00000004,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise DispatchHalt(
            f"unable to inspect Windows ACL for {label}: {_windows_error(label, result)}"
        )
    try:
        if not owner or _windows_sid_string(owner) not in _windows_trusted_owner_sids():
            raise DispatchHalt(f"{label} has an untrusted Windows owner")
        if not dacl:
            raise DispatchHalt(f"{label} has an unverifiable Windows ACL")
        information = _AclSizeInformation()
        if not _ADVAPI32.GetAclInformation(
            dacl,
            ctypes.byref(information),
            ctypes.sizeof(information),
            2,
        ):
            raise DispatchHalt(f"unable to inspect Windows ACL entries for {label}")
        dangerous = _WINDOWS_WRITE_RIGHTS if final else _WINDOWS_REPLACE_RIGHTS
        trusted = _windows_trusted_acl_sids()
        for index in range(information.ace_count):
            ace = wintypes.LPVOID()
            if not _ADVAPI32.GetAce(dacl, index, ctypes.byref(ace)):
                raise DispatchHalt(f"unable to inspect Windows ACL entry for {label}")
            _windows_validate_acl_ace(
                ace.value, label=label, dangerous=dangerous, trusted=trusted
            )
    finally:
        if descriptor:
            _KERNEL32.LocalFree(descriptor)


def _windows_lexical_chain(path: Path, *, label: str) -> list[Path]:
    raw = str(path)
    if raw.startswith(("\\\\", "\\?\\", "\\.\\")):
        raise DispatchHalt(f"{label} must use a local drive path")
    drive, remainder = os.path.splitdrive(raw)
    if len(drive) != 2 or drive[1] != ":" or not remainder.startswith(("\\", "/")):
        raise DispatchHalt(f"{label} must be an absolute local drive path")
    components = [value for value in re.split(r"[\\/]+", remainder) if value]
    for component in components:
        if (
            component in {".", ".."}
            or component.endswith((" ", "."))
            or ":" in component
        ):
            raise DispatchHalt(f"{label} contains an unsafe Windows path component")
    current = Path(f"{drive}\\")
    chain = [current]
    for component in components:
        current /= component
        chain.append(current)
    return chain


def _windows_validate_path(
    path: Path,
    *,
    label: str,
    require_directory: bool = False,
    require_file: bool = False,
    allow_reparse: bool = False,
) -> Path:
    chain = _windows_lexical_chain(path, label=label)
    for index, component in enumerate(chain):
        attributes = _windows_attributes(component, label=label)
        if attributes & _WINDOWS_REPARSE_POINT:
            tag = getattr(os.lstat(component), "st_reparse_tag", None)
            if not allow_reparse or tag not in _WINDOWS_SAFE_NAME_SURROGATE_TAGS:
                raise DispatchHalt(f"{label} contains a Windows reparse point")
        final = index == len(chain) - 1
        if final and require_directory and not attributes & _WINDOWS_DIRECTORY:
            raise DispatchHalt(f"{label} is not a directory")
        if final and require_file and attributes & _WINDOWS_DIRECTORY:
            raise DispatchHalt(f"{label} is not a regular file")
        _windows_acl(component, label=f"{label} component {component}", final=final)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise DispatchHalt(f"unable to resolve {label}: {path}") from exc
    resolved_text = str(resolved)
    if resolved_text.startswith("\\\\?\\"):
        resolved_text = resolved_text[4:]
        if not re.match(r"^[A-Za-z]:[\\\\/]", resolved_text):
            raise DispatchHalt(f"resolved {label} must use a local drive path")
        resolved = Path(resolved_text)
    if os.path.normcase(str(resolved)) != os.path.normcase(str(path.absolute())):
        # A non-reparse spelling can still expand an 8.3 alias. Validate the
        # canonical target independently before accepting that identity.
        resolved_chain = _windows_lexical_chain(resolved, label=label)
        for index, component in enumerate(resolved_chain):
            attributes = _windows_attributes(component, label=label)
            if attributes & _WINDOWS_REPARSE_POINT:
                raise DispatchHalt(f"resolved {label} contains a Windows reparse point")
            _windows_acl(
                component,
                label=f"resolved {label} component {component}",
                final=index == len(resolved_chain) - 1,
            )
    return resolved


def _windows_secure_private(path: Path) -> None:
    flags = "OICI" if path.is_dir() else ""
    sid = _windows_current_sid()
    sddl = f"D:P(A;{flags};FA;;;SY)(A;{flags};FA;;;BA)(A;{flags};FA;;;{sid})"
    descriptor = wintypes.LPVOID()
    if not _ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise DispatchHalt("unable to build a private Windows ACL")
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = wintypes.LPVOID()
        if (
            not _ADVAPI32.GetSecurityDescriptorDacl(
                descriptor,
                ctypes.byref(present),
                ctypes.byref(dacl),
                ctypes.byref(defaulted),
            )
            or not present
        ):
            raise DispatchHalt("unable to read a private Windows ACL")
        result = _ADVAPI32.SetNamedSecurityInfoW(
            str(path), 1, 0x00000004 | 0x80000000, None, None, dacl, None
        )
        if result:
            raise DispatchHalt(
                f"unable to apply a private Windows ACL: {_windows_error(str(path), result)}"
            )
    finally:
        _KERNEL32.LocalFree(descriptor)


def _stat_is_reparse(details: os.stat_result) -> bool:
    return bool(getattr(details, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT)


def _path_is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    return bool(
        os.name == "nt"
        and os.path.lexists(path)
        and _windows_attributes(path, label="path") & _WINDOWS_REPARSE_POINT
    )


def paths_overlap(left: Path, right: Path) -> bool:
    """Return whether either resolved path contains the other."""
    left_value = os.path.normcase(os.path.realpath(left))
    right_value = os.path.normcase(os.path.realpath(right))
    try:
        return os.path.commonpath((left_value, right_value)) in {
            left_value,
            right_value,
        }
    except ValueError:
        return False


def paths_same(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
            os.path.realpath(right)
        )


def resolve_workdir(value: str) -> Path:
    candidate = Path(value).expanduser()
    if os.name == "nt":
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        return _windows_validate_path(
            candidate, label="workdir", require_directory=True
        )
    return candidate.resolve()


def _lock_file(handle: Any, *, exclusive: bool, nonblocking: bool = False) -> None:
    if os.name != "nt":
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if nonblocking:
            operation |= fcntl.LOCK_NB
        fcntl.flock(handle.fileno(), operation)
        return
    overlapped = _Overlapped()
    flags = (0x00000002 if exclusive else 0) | (0x00000001 if nonblocking else 0)
    if _KERNEL32.LockFileEx(
        msvcrt.get_osfhandle(handle.fileno()),
        flags,
        0,
        1,
        0,
        ctypes.byref(overlapped),
    ):
        return
    error = ctypes.get_last_error()
    if nonblocking and error in {32, 33, 158}:
        raise BlockingIOError(error, "Windows file lock is already held")
    raise _windows_error("unable to acquire Windows file lock", error)


def _unlock_file(handle: Any) -> None:
    if os.name != "nt":
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    overlapped = _Overlapped()
    if not _KERNEL32.UnlockFileEx(
        msvcrt.get_osfhandle(handle.fileno()),
        0,
        1,
        0,
        ctypes.byref(overlapped),
    ):
        raise _windows_error("unable to release Windows file lock")


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_codex_home() -> Path:
    """Return the one canonical, safe Codex home used by this invocation."""
    if "CODEX_HOME" not in os.environ:
        default = Path.home() / ".codex"
        return preflight_home(default, lexical=str(default))
    configured = os.environ["CODEX_HOME"]
    # Check lexical absoluteness before expansion: accepting ``~`` or a
    # relative spelling would make an environment override context-dependent.
    if not configured or not Path(configured).is_absolute():
        raise DispatchHalt("CODEX_HOME must be an absolute path")
    candidate = Path(configured)
    return preflight_home(candidate, lexical=configured)


def child_environment(home: Path) -> dict[str, str]:
    """Launch Codex with a narrow, explicit environment.

    ``HOME`` and ``CODEX_HOME`` deliberately retain the installed Codex
    authentication/session boundary. Environment-only API credentials, loader
    variables, language startup hooks, and arbitrary caller state are not
    forwarded. ``PATH`` remains available for sandboxed project tools, but the
    dispatcher resolves and validates the Codex entrypoint and its shebang
    runtime explicitly before launch rather than trusting that path.
    """
    configured_home = os.environ.get("USERPROFILE" if os.name == "nt" else "HOME")
    if not configured_home or not Path(configured_home).is_absolute():
        raise DispatchHalt("user home must be an absolute path")
    user_home = validate_executable_chain(
        Path(configured_home), label="HOME path", require_directory=True
    )
    environment = {"HOME": str(user_home), "CODEX_HOME": str(home)}
    allowed = ["PATH", "TERM", "LANG", "LC_ALL"]
    if os.name == "nt":
        allowed.extend(
            [
                "USERPROFILE",
                "HOMEDRIVE",
                "HOMEPATH",
                "SystemRoot",
                "WINDIR",
                "COMSPEC",
                "PATHEXT",
                "TEMP",
                "TMP",
                "APPDATA",
                "LOCALAPPDATA",
            ]
        )
    for name in allowed:
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value
    return environment


def dispatcher_launch_chain() -> tuple[Path, Path, Path]:
    """Return the validated launcher, Python source, and interpreter paths."""
    configured = os.environ.get("CODEX_TIER_LAUNCHER_PATH")
    if not configured or not Path(configured).is_absolute():
        raise DispatchHalt("dispatch must be invoked through the installed launcher")
    launcher = validate_executable_chain(
        Path(configured), label="dispatcher launcher path"
    )
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise DispatchHalt("dispatcher launcher is not an executable regular file")
    runtime = validate_executable_chain(
        Path(__file__).resolve(strict=True), label="dispatcher runtime path"
    )
    try:
        expected_runtime = launcher.with_name("codex-tier.py").resolve(strict=True)
    except OSError as exc:
        raise DispatchHalt("dispatcher launcher runtime is unavailable") from exc
    if runtime != expected_runtime:
        raise DispatchHalt("dispatcher launcher and runtime are not installed siblings")
    interpreter = validate_executable_chain(
        Path(sys.executable), label="Python interpreter path"
    )
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise DispatchHalt("Python interpreter is not an executable regular file")
    return launcher, runtime, interpreter


def validate_ledger_lines(handle: Any) -> str | None:
    previous_hash = None
    line_number = 0
    handle.seek(0)
    while True:
        line = handle.readline(MAX_LEDGER_ENTRY_BYTES + 1)
        if not line:
            break
        if len(line) > MAX_LEDGER_ENTRY_BYTES:
            raise DispatchHalt("local receipt ledger entry exceeds the size limit")
        if not line.strip():
            continue
        line_number += 1
        if line_number > MAX_LEDGER_ENTRIES:
            raise DispatchHalt("local receipt ledger exceeds the entry limit")
        try:
            previous = json.loads(line)
            if not isinstance(previous, dict):
                raise TypeError("ledger entry is not an object")
            entry_hash = previous.pop("entry_hash")
            if previous.get("previous_hash") != previous_hash:
                raise ValueError("broken previous hash")
            if sha256_bytes(canonical_json(previous)) != entry_hash:
                raise ValueError("entry hash mismatch")
            previous_hash = entry_hash
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise DispatchHalt(
                f"local receipt ledger is invalid at entry {line_number}"
            ) from exc
    return previous_hash


def ensure_private_dir(path: Path) -> None:
    if os.name == "nt":
        if path.exists():
            _windows_validate_path(
                path, label="private directory", require_directory=True
            )
        else:
            missing = []
            current = path
            while not current.exists():
                missing.append(current)
                current = current.parent
            _windows_validate_path(
                current, label="private directory parent", require_directory=True
            )
            for directory in reversed(missing):
                try:
                    directory.mkdir()
                except FileExistsError:
                    _windows_validate_path(
                        directory,
                        label="concurrently created private directory",
                        require_directory=True,
                    )
                else:
                    _windows_secure_private(directory)
        _windows_secure_private(path)
        _windows_validate_path(path, label="private directory", require_directory=True)
        return
    if path.is_symlink():
        raise DispatchHalt(f"refusing symlinked private directory: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_uid != os.geteuid():
        raise DispatchHalt(
            f"private directory is not owned by the current identity: {path}"
        )
    os.chmod(path, 0o700)
    reject_darwin_allow_acl(path, label="private directory")


def reject_darwin_allow_acl(path: Path, *, label: str) -> None:
    """Reject macOS ACL grants while permitting normal deny-only home ACLs."""
    if sys.platform != "darwin":
        return
    try:
        result = subprocess.run(
            ["/bin/ls", "-lde", str(path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DispatchHalt(f"unable to inspect ACLs for {label}") from exc
    if result.returncode != 0:
        raise DispatchHalt(f"unable to inspect ACLs for {label}")
    if re.search(r"(?m)^\s*\d+:.*\sallow\s", result.stdout):
        raise DispatchHalt(f"refusing ACL allow entry on {label}")


def preflight_home(path: Path, *, lexical: str | None = None) -> Path:
    """Validate the complete lexical home chain before resolving the path."""
    raw = str(path) if lexical is None else lexical
    if os.name == "nt":
        candidate = Path(raw)
        if not raw or not candidate.is_absolute():
            raise DispatchHalt("CODEX_HOME must be an absolute path")
        return _windows_validate_path(
            candidate, label="Codex home", require_directory=True
        )
    if not raw or not Path(raw).is_absolute():
        raise DispatchHalt("CODEX_HOME must be an absolute path")
    components = [component for component in raw.split("/") if component]
    if any(component in {".", ".."} for component in components):
        raise DispatchHalt("CODEX_HOME must not contain . or .. components")

    current = Path("/")
    chain = [current]
    for component in components:
        current /= component
        chain.append(current)

    effective_uid = os.geteuid()
    for component in chain:
        try:
            details = os.lstat(component)
        except OSError as exc:
            raise DispatchHalt(
                f"Codex home must be an existing directory: {component}"
            ) from exc
        if stat.S_ISLNK(details.st_mode):
            raise DispatchHalt(f"refusing symlinked Codex home component: {component}")
        if not stat.S_ISDIR(details.st_mode):
            raise DispatchHalt(f"Codex home component is not a directory: {component}")
        if details.st_uid not in {0, effective_uid}:
            raise DispatchHalt(
                f"Codex home component has an untrusted owner: {component}"
            )
        reject_darwin_allow_acl(component, label="Codex home component")
        mode = stat.S_IMODE(details.st_mode)
        if mode & 0o022 and not mode & stat.S_ISVTX:
            raise DispatchHalt(
                f"Codex home component is non-sticky group/world-writable: {component}"
            )

    home_details = os.lstat(path)
    if home_details.st_uid != effective_uid:
        raise DispatchHalt("Codex home is not owned by the current identity")
    if stat.S_IMODE(home_details.st_mode) & 0o022:
        raise DispatchHalt("Codex home is group/world-writable")
    return path.resolve()


def validate_executable_chain(
    path: Path,
    *,
    label: str,
    require_directory: bool = False,
) -> Path:
    """Validate complete lexical/resolved ancestry for a local runtime path.

    Root-owned and current-user-owned components are trusted. World-writable
    components are always rejected. Group write is accepted only on a component
    owned by the invoking user; this is necessary for common owner-managed
    Homebrew and user-private-group layouts and does not extend trust beyond the
    same operating-system identity. Symlinks are allowed only when both their
    lexical containment and resolved target chains pass these checks.
    """
    if os.name == "nt":
        return _windows_validate_path(
            path,
            label=label,
            require_directory=require_directory,
            require_file=not require_directory,
            allow_reparse=True,
        )
    raw = str(path)
    if not raw or not path.is_absolute():
        raise DispatchHalt(f"{label} must be an absolute path")
    components = [component for component in raw.split("/") if component]
    if any(component in {".", ".."} for component in components):
        raise DispatchHalt(f"{label} must not contain . or .. components")

    def validate_chain(candidate: Path, chain_label: str) -> None:
        effective_uid = os.geteuid()
        current = Path("/")
        chain = [current]
        for component in [part for part in str(candidate).split("/") if part]:
            current /= component
            chain.append(current)
        for current in chain:
            try:
                status = current.lstat()
            except OSError as exc:
                raise DispatchHalt(
                    f"{chain_label} component is missing: {current}"
                ) from exc
            if status.st_uid not in {0, effective_uid}:
                raise DispatchHalt(
                    f"refusing foreign-owned {chain_label} component: {current}"
                )
            if stat.S_ISLNK(status.st_mode):
                continue
            mode = stat.S_IMODE(status.st_mode)
            sticky_directory = stat.S_ISDIR(status.st_mode) and bool(
                mode & stat.S_ISVTX
            )
            if mode & 0o002 and not sticky_directory:
                raise DispatchHalt(
                    f"refusing world-writable {chain_label} component: {current}"
                )
            if status.st_uid != effective_uid and mode & 0o020 and not sticky_directory:
                raise DispatchHalt(
                    f"refusing group-writable foreign {chain_label} component: {current}"
                )
            reject_darwin_allow_acl(current, label=f"{chain_label} component")

    validate_chain(path, label)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise DispatchHalt(f"unable to resolve {label}: {path}") from exc
    validate_chain(resolved, f"resolved {label}")
    if require_directory and not resolved.is_dir():
        raise DispatchHalt(f"{label} is not a directory")
    return resolved


def preflight_ledger(home: Path) -> None:
    ledger_dir = home / "tier-routing"
    ensure_private_dir(ledger_dir)
    ledger = ledger_dir / "ledger.jsonl"
    if ledger.is_symlink() or (
        os.name == "nt"
        and os.path.lexists(ledger)
        and _windows_attributes(ledger, label="local receipt ledger")
        & _WINDOWS_REPARSE_POINT
    ):
        raise DispatchHalt("refusing symlinked local receipt ledger")
    if ledger.exists():
        current_owner_regular(ledger, label="local receipt ledger")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(ledger, flags)
        except OSError as exc:
            raise DispatchHalt(
                f"unable to open local receipt ledger safely: {type(exc).__name__}"
            ) from exc
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            _lock_file(handle, exclusive=False)
            try:
                if os.fstat(handle.fileno()).st_size > MAX_LEDGER_BYTES:
                    raise DispatchHalt(
                        "local receipt ledger exceeds the total size limit"
                    )
                validate_ledger_lines(handle)
            finally:
                _unlock_file(handle)


def _read_shebang(path: Path) -> list[str] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            first_line = handle.readline(4097)
    except OSError as exc:
        raise DispatchHalt(
            f"unable to inspect Codex entrypoint: {type(exc).__name__}"
        ) from exc
    if not first_line.startswith(b"#!"):
        return None
    if len(first_line) > 4096 and not first_line.endswith(b"\n"):
        raise DispatchHalt("Codex entrypoint has an oversized shebang")
    try:
        text = first_line[2:].decode("utf-8").strip()
        tokens = shlex.split(text, posix=True)
    except (UnicodeError, ValueError) as exc:
        raise DispatchHalt("Codex entrypoint has an invalid shebang") from exc
    if not tokens:
        raise DispatchHalt("Codex entrypoint has an empty shebang")
    return tokens


def _runtime_from_path(name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", name):
        raise DispatchHalt("Codex shebang runtime name is invalid")
    executable = shutil.which(name, path=os.environ.get("PATH"))
    if not executable:
        raise DispatchHalt(f"Codex shebang runtime is unavailable: {name}")
    return validate_executable_chain(
        Path(executable), label="Codex shebang runtime path"
    )


def resolve_codex() -> CodexLaunch:
    configured = os.environ.get("CODEX_TIER_CODEX_BIN")
    executable = configured or shutil.which("codex", path=os.environ.get("PATH"))
    if not executable:
        raise DispatchHalt("codex executable is unavailable")
    entrypoint = validate_executable_chain(
        Path(executable).expanduser(), label="Codex executable path"
    )
    if not entrypoint.is_file() or not os.access(entrypoint, os.X_OK):
        raise DispatchHalt("codex executable is not an executable regular file")
    shebang = _read_shebang(entrypoint)
    if shebang is None:
        return CodexLaunch(entrypoint=entrypoint, runtime=entrypoint)

    interpreter = shebang[0]
    if interpreter == "/usr/bin/env":
        if len(shebang) != 2:
            raise DispatchHalt(
                "Codex /usr/bin/env shebang must name exactly one runtime"
            )
        runtime = _runtime_from_path(shebang[1])
    else:
        if len(shebang) != 1 or not Path(interpreter).is_absolute():
            raise DispatchHalt(
                "Codex shebang must use one absolute runtime or /usr/bin/env NAME"
            )
        runtime = validate_executable_chain(
            Path(interpreter), label="Codex shebang runtime path"
        )
    if not runtime.is_file() or not os.access(runtime, os.X_OK):
        raise DispatchHalt("Codex shebang runtime is not an executable regular file")
    return CodexLaunch(entrypoint=entrypoint, runtime=runtime)


def codex_version(launch: CodexLaunch, home: Path) -> str:
    try:
        result = run_process_bounded(
            launch.command("--version"),
            input_bytes=b"",
            cwd=None,
            env=child_environment(home),
            timeout=15,
            stdout_limit=MAX_CONTROL_STDOUT_BYTES,
            stderr_limit=MAX_CONTROL_STDERR_BYTES,
        )
    except DispatchHalt as exc:
        raise DispatchHalt(f"unable to inspect codex version: {exc}") from exc
    if result.returncode != 0:
        raise DispatchHalt("codex --version failed")
    return result.stdout.strip() or result.stderr.strip()


def native_multi_agent_enabled(
    launch: CodexLaunch,
    home: Path,
    *,
    cwd: Path | None = None,
    force_disabled: bool = False,
) -> bool:
    arguments: list[str] = []
    if force_disabled:
        arguments.extend(["-c", "features.multi_agent=false"])
    arguments.extend(["features", "list"])
    try:
        result = run_process_bounded(
            launch.command(*arguments),
            input_bytes=b"",
            cwd=cwd,
            env=child_environment(home),
            timeout=15,
            stdout_limit=MAX_CONTROL_STDOUT_BYTES,
            stderr_limit=MAX_CONTROL_STDERR_BYTES,
        )
    except DispatchHalt as exc:
        raise DispatchHalt(
            f"unable to inspect native multi-agent state: {exc}"
        ) from exc
    if result.returncode != 0:
        raise DispatchHalt("codex features list failed")
    matches = [
        line.split()[-1]
        for line in result.stdout.splitlines()
        if line.split() and line.split()[0] == "multi_agent"
    ]
    if len(matches) != 1 or matches[0] not in {"true", "false"}:
        raise DispatchHalt("codex did not report one native multi-agent feature state")
    return matches[0] == "true"


def reject_profile_overrides(home: Path) -> None:
    for path in sorted(home.glob("*.config.toml")):
        current_owner_regular(path, label="Codex profile config")
        try:
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise DispatchHalt(f"Codex profile config is invalid: {path.name}") from exc
        value = parsed.get("features", {}).get("multi_agent")
        if value is not None and value is not False:
            raise DispatchHalt(
                f"profile {path.name} overrides native multi-agent; set it to false first"
            )


def strict_paths(home: Path) -> tuple[Path, Path, Path]:
    state_dir = home / "tier-routing"
    ensure_private_dir(state_dir)
    return (
        home / "config.toml",
        state_dir / "strict-config.json",
        state_dir / "strict-config.backup",
    )


def current_owner_regular(path: Path, *, label: str) -> os.stat_result:
    if os.name == "nt":
        resolved = _windows_validate_path(path, label=label, require_file=True)
        return resolved.stat()
    if path.is_symlink():
        raise DispatchHalt(f"refusing symlinked {label}")
    details = path.stat()
    if not path.is_file() or details.st_uid != os.geteuid():
        raise DispatchHalt(f"{label} is not a current-owner regular file")
    if stat.S_IMODE(details.st_mode) & 0o022:
        raise DispatchHalt(f"{label} is group/world-writable")
    reject_darwin_allow_acl(path, label=label)
    return details


def windows_sandbox_mode(home: Path) -> str:
    """Preserve an explicit elevated sandbox, otherwise use the native fallback."""
    config = home / "config.toml"
    if _path_is_link(config):
        raise DispatchHalt("refusing reparse-point config.toml")
    if not config.exists():
        return "unelevated"
    current_owner_regular(config, label="config.toml")
    try:
        parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise DispatchHalt("unable to read the Windows sandbox configuration") from exc
    windows = parsed.get("windows", {})
    if not isinstance(windows, dict):
        raise DispatchHalt("Windows sandbox configuration must be a table")
    mode = windows.get("sandbox", "unelevated")
    if mode not in {"elevated", "unelevated"}:
        raise DispatchHalt("Windows sandbox must be elevated or unelevated")
    return mode


def private_existing_dir(path: Path, *, label: str) -> None:
    """Require an existing owner-only directory without changing its mode."""
    if os.name == "nt":
        _windows_validate_path(path, label=label, require_directory=True)
        return
    if path.is_symlink():
        raise DispatchHalt(f"refusing symlinked {label}")
    try:
        details = os.lstat(path)
    except OSError as exc:
        raise DispatchHalt(f"unable to inspect {label}") from exc
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
        raise DispatchHalt(f"{label} is not a current-owner directory")
    if stat.S_IMODE(details.st_mode) & 0o077:
        raise DispatchHalt(
            f"{label} must not grant group/other permissions (use mode 0700)"
        )
    reject_darwin_allow_acl(path, label=label)


def parse_policy(raw: bytes) -> RoutingPolicy:
    """Parse one exact, deliberately narrow policy contract."""
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise DispatchHalt("routing policy must be valid UTF-8 TOML") from exc
    if not isinstance(value, dict) or set(value) != {"schema", "routes"}:
        raise DispatchHalt(
            "routing policy top-level keys must be exactly schema and routes"
        )
    if value["schema"] != POLICY_SCHEMA:
        raise DispatchHalt(f"routing policy schema must be {POLICY_SCHEMA}")
    configured = value["routes"]
    expected_names = set(ROUTES)
    if not isinstance(configured, dict) or set(configured) != expected_names:
        raise DispatchHalt(
            "routing policy routes must be exactly: "
            + ", ".join(sorted(expected_names))
        )
    routes: dict[str, Route] = {}
    for name in ROUTES:
        selection = configured[name]
        if not isinstance(selection, dict) or set(selection) != {"model", "effort"}:
            raise DispatchHalt(
                f"routing policy route {name} must contain exactly model and effort"
            )
        model = selection["model"]
        effort = selection["effort"]
        if not isinstance(model, str) or not MODEL_ID_PATTERN.fullmatch(model):
            raise DispatchHalt(
                f"routing policy route {name} model must match "
                "[A-Za-z0-9][A-Za-z0-9._-]{0,127}; account model access "
                "cannot be checked offline, so run a bounded canary after validation"
            )
        if not isinstance(effort, str) or effort not in ACCEPTED_EFFORTS:
            raise DispatchHalt(
                f"routing policy route {name} effort must be one of: "
                + ", ".join(ACCEPTED_EFFORTS)
            )
        routes[name] = Route(model, effort, ROUTES[name].sandbox)
    document = effective_policy_document(CUSTOM_POLICY_ID, routes)
    return RoutingPolicy(
        kind="custom",
        policy_id=CUSTOM_POLICY_ID,
        sha256=sha256_bytes(canonical_json(document)),
        input_sha256=sha256_bytes(raw),
        document=document,
        routes=routes,
    )


def load_policy(home: Path) -> RoutingPolicy:
    """Load the fixed policy path or return the backwards-compatible default."""
    state_dir = home / "tier-routing"
    path = state_dir / "policy.toml"
    try:
        os.lstat(state_dir)
    except FileNotFoundError:
        return default_policy()
    private_existing_dir(state_dir, label="routing policy directory")
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return default_policy()
    except OSError as exc:
        raise DispatchHalt("unable to inspect routing policy") from exc
    if stat.S_ISLNK(details.st_mode) or _stat_is_reparse(details):
        raise DispatchHalt("refusing symlinked routing policy")
    expected = current_owner_regular(path, label="routing policy")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DispatchHalt(
            f"unable to open routing policy safely: {type(exc).__name__}"
        ) from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise DispatchHalt("routing policy is not a current-owner regular file")
        if os.name == "nt" and (
            details.st_dev != expected.st_dev or details.st_ino != expected.st_ino
        ):
            raise DispatchHalt("routing policy changed during safe open")
        if os.name != "nt" and details.st_uid != os.geteuid():
            raise DispatchHalt("routing policy is not a current-owner regular file")
        if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077:
            raise DispatchHalt(
                "routing policy must not grant group/other permissions (use mode 0600)"
            )
        if details.st_size > MAX_POLICY_BYTES:
            raise DispatchHalt(
                f"routing policy exceeds the {MAX_POLICY_BYTES}-byte limit"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(MAX_POLICY_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > MAX_POLICY_BYTES:
        raise DispatchHalt(f"routing policy exceeds the {MAX_POLICY_BYTES}-byte limit")
    reject_darwin_allow_acl(path, label="routing policy")
    return parse_policy(raw)


def assert_policy_unchanged(initial: RoutingPolicy, home: Path) -> None:
    try:
        current = load_policy(home)
    except DispatchHalt as exc:
        raise DispatchHalt("routing policy became invalid during dispatch") from exc
    if (
        current.kind != initial.kind
        or current.policy_id != initial.policy_id
        or current.sha256 != initial.sha256
        or current.input_sha256 != initial.input_sha256
        or current.document != initial.document
        or current.routes != initial.routes
    ):
        raise DispatchHalt("routing policy changed during dispatch")


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.codex-tier-{uuid.uuid4().hex}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(temporary, flags, mode)
        if os.name == "nt":
            _windows_secure_private(temporary)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            if os.name != "nt":
                os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            if not _KERNEL32.MoveFileExW(str(temporary), str(path), 0x1 | 0x8):
                raise _windows_error("unable to replace private Windows file")
            _windows_validate_path(
                path, label="strict configuration", require_file=True
            )
        else:
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise DispatchHalt(
            f"unable to update strict configuration: {type(exc).__name__}"
        ) from exc


def strict_candidate(original: bytes) -> bytes:
    try:
        text = original.decode("utf-8")
        parsed = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise DispatchHalt("config.toml is not valid UTF-8 TOML") from exc
    features = parsed.get("features")
    if features is not None and not isinstance(features, dict):
        raise DispatchHalt("config.toml features value is not a table")
    lines = text.splitlines(keepends=True)
    headers = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(r"[ \t]*\[features\][ \t]*(?:#.*?)?(?:\r?\n)?", line)
    ]
    newline = "\r\n" if "\r\n" in text else "\n"
    if not headers:
        if features is not None:
            raise DispatchHalt(
                "config.toml defines features without a simple [features] table; edit manually"
            )
        prefix = "" if not text or text.endswith(("\n", "\r")) else newline
        block = (
            f"{prefix}{newline if text else ''}"
            "# codex-tier-routing strict mode: native subagent spawning disabled"
            f"{newline}[features]{newline}multi_agent = false{newline}"
        )
        candidate = (text + block).encode()
    elif len(headers) == 1:
        start = headers[0]
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if re.match(r"[ \t]*\[", lines[index]):
                end = index
                break
        assignments = []
        pattern = re.compile(
            r"^([ \t]*multi_agent[ \t]*=[ \t]*)(true|false)([ \t]*(?:#.*?)?)(\r?\n)?$"
        )
        for index in range(start + 1, end):
            match = pattern.fullmatch(lines[index])
            if match:
                assignments.append((index, match))
        if len(assignments) > 1:
            raise DispatchHalt("config.toml contains multiple multi_agent assignments")
        if assignments:
            index, match = assignments[0]
            lines[index] = (
                f"{match.group(1)}false{match.group(3)}{match.group(4) or ''}"
            )
        else:
            lines.insert(start + 1, f"multi_agent = false{newline}")
        candidate = "".join(lines).encode()
    else:
        raise DispatchHalt("config.toml contains multiple [features] tables")
    try:
        reparsed = tomllib.loads(candidate.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise DispatchHalt("strict configuration candidate is invalid") from exc
    if reparsed.get("features", {}).get("multi_agent") is not False:
        raise DispatchHalt("strict configuration candidate did not disable multi_agent")
    return candidate


def strict_toml(raw: bytes) -> dict[str, Any]:
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise DispatchHalt("managed strict configuration has drifted") from exc
    if not isinstance(value, dict):
        raise DispatchHalt("managed strict configuration has drifted")
    return value


def additive_line_mapping(base: bytes, current: bytes) -> list[int] | None:
    """Map unchanged base lines into a version containing line-only additions."""
    base_lines = base.splitlines(keepends=True)
    current_lines = current.splitlines(keepends=True)
    mapping: list[int] = []
    cursor = 0
    for base_index, line in enumerate(base_lines):
        while cursor < len(current_lines) and current_lines[cursor] != line:
            final_unterminated_line = (
                base_index == len(base_lines) - 1
                and not line.endswith((b"\n", b"\r"))
                and current_lines[cursor].rstrip(b"\r\n") == line
            )
            if final_unterminated_line:
                break
            cursor += 1
        if cursor == len(current_lines):
            return None
        mapping.append(cursor)
        cursor += 1
    return mapping


def strict_candidate_is_intact(candidate: bytes, current: bytes) -> bool:
    try:
        parsed = strict_toml(current)
    except DispatchHalt:
        return False
    features = parsed.get("features")
    return (
        additive_line_mapping(candidate, current) is not None
        and isinstance(features, dict)
        and features.get("multi_agent") is False
    )


def strict_target_matches(original: bytes, current: bytes) -> bool:
    """Recognize an already-restored config plus unrelated line additions."""
    if additive_line_mapping(original, current) is None:
        return False
    try:
        original_value = strict_toml(original)
        current_value = strict_toml(current)
    except DispatchHalt:
        return False
    original_features = original_value.get("features")
    current_features = current_value.get("features")
    original_has_target = isinstance(original_features, dict) and (
        "multi_agent" in original_features
    )
    current_has_target = isinstance(current_features, dict) and (
        "multi_agent" in current_features
    )
    if original_has_target:
        return current_has_target and (
            current_features["multi_agent"] == original_features["multi_agent"]
        )
    return not current_has_target


def restore_strict_additions(
    original: bytes, candidate: bytes, current: bytes
) -> bytes:
    """Reverse the one managed line edit while preserving additive TOML drift."""
    original_lines = original.splitlines(keepends=True)
    candidate_lines = candidate.splitlines(keepends=True)
    current_lines = current.splitlines(keepends=True)
    mapping = additive_line_mapping(candidate, current)
    changes = [
        opcode
        for opcode in difflib.SequenceMatcher(
            a=original_lines, b=candidate_lines, autojunk=False
        ).get_opcodes()
        if opcode[0] != "equal"
    ]
    if mapping is None or len(changes) != 1:
        raise DispatchHalt(
            "managed strict configuration has drifted; refusing to overwrite it"
        )
    _, original_start, original_end, candidate_start, candidate_end = changes[0]
    if candidate_start == candidate_end:
        raise DispatchHalt("strict configuration recovery delta is invalid")
    positions = mapping[candidate_start:candidate_end]
    if positions != list(range(positions[0], positions[0] + len(positions))):
        raise DispatchHalt(
            "managed strict configuration has drifted; refusing to overwrite it"
        )
    replacement = list(original_lines[original_start:original_end])
    managed_ending = current_lines[positions[-1]][
        len(current_lines[positions[-1]].rstrip(b"\r\n")) :
    ]
    if replacement and managed_ending and not replacement[-1].endswith((b"\n", b"\r")):
        replacement[-1] += managed_ending
    restored_lines = (
        current_lines[: positions[0]] + replacement + current_lines[positions[-1] + 1 :]
    )
    restored = b"".join(restored_lines)
    current_value = strict_toml(current)
    original_value = strict_toml(original)
    expected = copy.deepcopy(current_value)
    original_features = original_value.get("features")
    expected_features = expected.get("features")
    if isinstance(original_features, dict) and "multi_agent" in original_features:
        if not isinstance(expected_features, dict):
            raise DispatchHalt("managed strict configuration has drifted")
        expected_features["multi_agent"] = original_features["multi_agent"]
    elif isinstance(expected_features, dict):
        expected_features.pop("multi_agent", None)
        if not expected_features and "features" not in original_value:
            expected.pop("features")
    if strict_toml(restored) != expected:
        raise DispatchHalt(
            "managed strict configuration has drifted; refusing to overwrite it"
        )
    return restored


def strict_managed_versions(
    manifest: dict[str, Any], backup: Path
) -> tuple[bytes, bytes]:
    current_owner_regular(backup, label="strict-mode backup")
    original = backup.read_bytes()
    if sha256_bytes(original) != manifest["before_sha256"]:
        raise DispatchHalt("strict-mode backup hash mismatch")
    candidate = strict_candidate(original)
    if sha256_bytes(candidate) != manifest["after_sha256"]:
        raise DispatchHalt("strict-mode managed candidate hash mismatch")
    return original, candidate


def read_strict_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    current_owner_regular(path, label="strict-mode manifest")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DispatchHalt("strict-mode manifest is invalid") from exc
    required = {
        "schema",
        "before_exists",
        "before_mode",
        "before_sha256",
        "after_sha256",
        "state",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema") != STRICT_MANIFEST_SCHEMA
        or value.get("state") not in {"pending", "active", "disabling"}
    ):
        raise DispatchHalt("strict-mode manifest is invalid")
    return value


def strict_enable(_: argparse.Namespace, home: Path | None = None) -> int:
    home = resolve_codex_home() if home is None else home
    executable = resolve_codex()
    config = home / "config.toml"
    # exists() is false for dangling links, so reject either form before every
    # config existence, read, or recovery decision below.
    if _path_is_link(config):
        raise DispatchHalt("refusing symlinked config.toml")
    _, manifest_path, backup = strict_paths(home)
    reject_profile_overrides(home)
    manifest = read_strict_manifest(manifest_path)
    if manifest is not None:
        if manifest["state"] == "disabling":
            strict_disable(_, home)
            return strict_enable(_, home)
        _, managed = strict_managed_versions(manifest, backup)
        if config.exists():
            current_owner_regular(config, label="config.toml")
        current = config.read_bytes() if config.exists() else b""
        current_hash = sha256_bytes(current)
        managed_is_intact = strict_candidate_is_intact(managed, current)
        if manifest["state"] == "pending" and managed_is_intact:
            if native_multi_agent_enabled(executable, home):
                raise DispatchHalt("pending strict configuration is not effective")
            manifest["state"] = "active"
            atomic_write(manifest_path, canonical_json(manifest) + b"\n", 0o600)
        elif (
            manifest["state"] == "pending" and current_hash == manifest["before_sha256"]
        ):
            manifest_path.unlink()
            backup.unlink()
            return strict_enable(_, home)
        elif not managed_is_intact:
            raise DispatchHalt("managed strict configuration has drifted")
        if native_multi_agent_enabled(executable, home):
            raise DispatchHalt("managed strict configuration is not effective")
        print(
            json.dumps(
                {"status": "pass", "strict_mode": "already_active"}, sort_keys=True
            )
        )
        return 0
    before_exists = config.exists()
    if before_exists:
        details = current_owner_regular(config, label="config.toml")
        original = config.read_bytes()
        mode = stat.S_IMODE(details.st_mode)
    else:
        original = b""
        mode = 0o600
    candidate = strict_candidate(original)
    if candidate == original and not native_multi_agent_enabled(executable, home):
        print(
            json.dumps(
                {"status": "pass", "strict_mode": "already_effective_unmanaged"},
                sort_keys=True,
            )
        )
        return 0
    if backup.exists():
        current_owner_regular(backup, label="strict-mode backup")
        if backup.read_bytes() != original:
            raise DispatchHalt(
                "orphaned strict-mode backup does not match current config"
            )
        backup.unlink()
    atomic_write(backup, original, 0o600)
    manifest = {
        "schema": STRICT_MANIFEST_SCHEMA,
        "before_exists": before_exists,
        "before_mode": mode if before_exists else None,
        "before_sha256": sha256_bytes(original),
        "after_sha256": sha256_bytes(candidate),
        "state": "pending",
    }
    atomic_write(manifest_path, canonical_json(manifest) + b"\n", 0o600)
    try:
        atomic_write(config, candidate, mode)
        if native_multi_agent_enabled(executable, home):
            raise DispatchHalt("Codex still reports native multi-agent enabled")
        manifest["state"] = "active"
        atomic_write(manifest_path, canonical_json(manifest) + b"\n", 0o600)
    except BaseException as cause:
        rollback_blocked = False
        try:
            if _path_is_link(config):
                rollback_blocked = True
                current_exists = True
                current = b""
            elif config.exists():
                current_exists = True
                current_owner_regular(config, label="config.toml")
                current = config.read_bytes()
            else:
                current_exists = False
                current = b""
        except (OSError, DispatchHalt):
            rollback_blocked = True
            current_exists = config.exists() or _path_is_link(config)
            current = b""

        current_hash = sha256_bytes(current)
        if (
            not rollback_blocked
            and current_exists
            and current_hash == sha256_bytes(candidate)
        ):
            if before_exists:
                atomic_write(config, original, mode)
            else:
                try:
                    config.unlink()
                except FileNotFoundError:
                    pass
        elif (
            not rollback_blocked
            and current_exists == before_exists
            and current_hash == sha256_bytes(original)
        ):
            pass
        else:
            rollback_blocked = True

        if rollback_blocked:
            raise DispatchHalt(
                "strict enable failed and config.toml changed concurrently; "
                "preserved strict recovery records"
            ) from cause

        try:
            backup.unlink()
        except FileNotFoundError:
            pass
        try:
            manifest_path.unlink()
        except FileNotFoundError:
            pass
        raise
    print(
        json.dumps(
            {"status": "pass", "strict_mode": "enabled", "restart_required": True},
            sort_keys=True,
        )
    )
    return 0


def strict_disable(_: argparse.Namespace, home: Path | None = None) -> int:
    home = resolve_codex_home() if home is None else home
    # This check must precede strict_paths(): strict-disable is also a
    # recovery operation, and strict_paths creates/chmods tier-routing.
    # is_symlink covers both live and dangling links.
    config = home / "config.toml"
    if _path_is_link(config):
        raise DispatchHalt("refusing symlinked config.toml")
    config, manifest_path, backup = strict_paths(home)
    manifest = read_strict_manifest(manifest_path)
    if manifest is None:
        print(
            json.dumps({"status": "pass", "strict_mode": "not_managed"}, sort_keys=True)
        )
        return 0
    original, managed = strict_managed_versions(manifest, backup)
    if _path_is_link(config):
        raise DispatchHalt("refusing symlinked config.toml")
    if config.exists():
        current_details = current_owner_regular(config, label="config.toml")
        current_mode = stat.S_IMODE(current_details.st_mode)
    else:
        current_mode = 0o600
    current = config.read_bytes() if config.exists() else b""
    if manifest["state"] == "active":
        if not strict_candidate_is_intact(managed, current):
            raise DispatchHalt(
                "managed strict configuration has drifted; refusing to overwrite it"
            )
        manifest["state"] = "disabling"
        atomic_write(manifest_path, canonical_json(manifest) + b"\n", 0o600)
    elif not (
        strict_candidate_is_intact(managed, current)
        or strict_target_matches(original, current)
    ):
        raise DispatchHalt(
            "managed strict configuration has drifted; refusing to overwrite it"
        )

    # Re-read after recording the disabling state so changes already visible at
    # this recovery boundary are preserved or rejected.
    if _path_is_link(config):
        raise DispatchHalt("refusing symlinked config.toml")
    if config.exists():
        current_details = current_owner_regular(config, label="config.toml")
        current_mode = stat.S_IMODE(current_details.st_mode)
    current = config.read_bytes() if config.exists() else b""
    if strict_candidate_is_intact(managed, current):
        restored = restore_strict_additions(original, managed, current)
    elif strict_target_matches(original, current):
        restored = current
    else:
        raise DispatchHalt(
            "managed strict configuration has drifted; refusing to overwrite it"
        )
    if restored != current:
        if restored or manifest["before_exists"]:
            mode = (
                manifest["before_mode"] if manifest["before_exists"] else current_mode
            )
            atomic_write(config, restored, mode)
        else:
            try:
                config.unlink()
            except FileNotFoundError:
                pass
    manifest_path.unlink()
    backup.unlink()
    print(
        json.dumps(
            {"status": "pass", "strict_mode": "disabled", "restart_required": True},
            sort_keys=True,
        )
    )
    return 0


class Slot:
    """Cross-process four-slot cap using non-blocking advisory locks."""

    def __init__(self, home: Path):
        self._dir = home / "tier-routing" / "locks"
        self._file = None

    def __enter__(self) -> "Slot":
        ensure_private_dir(self._dir)
        for index in range(4):
            path = self._dir / f"dispatch-{index}.lock"
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags, 0o600)
            except OSError as exc:
                raise DispatchHalt(
                    f"unable to open dispatch lock safely: {type(exc).__name__}"
                ) from exc
            handle = os.fdopen(descriptor, "a+")
            if os.name == "nt":
                _windows_secure_private(path)
                current_owner_regular(path, label="dispatch lock")
            else:
                os.fchmod(handle.fileno(), 0o600)
            try:
                _lock_file(handle, exclusive=True, nonblocking=True)
            except BlockingIOError:
                handle.close()
                continue
            self._file = handle
            return self
        raise DispatchHalt("concurrency cap reached (four active dispatches)")

    def __exit__(self, *_: object) -> None:
        if self._file is not None:
            try:
                _unlock_file(self._file)
            finally:
                self._file.close()


def workspace_writer_lock_dir() -> Path:
    """Return the environment-independent lock namespace for this OS identity."""
    if os.name == "nt":
        configured = os.environ.get("LOCALAPPDATA")
        if not configured or not Path(configured).is_absolute():
            raise DispatchHalt("LOCALAPPDATA must be an absolute path")
        base = _windows_validate_path(
            Path(configured), label="LOCALAPPDATA", require_directory=True
        )
        return base / f"codex-tier-routing-{_windows_current_sid()}-locks"
    return Path("/tmp") / f"codex-tier-routing-{os.geteuid()}-locks"


class WorkspaceWriterLock:
    """Per-workdir exclusive writer lock using the resolved directory identity."""

    def __init__(self, home: Path, workdir: Path):
        self._dir = workspace_writer_lock_dir()
        try:
            details = workdir.stat()
        except OSError as exc:
            raise DispatchHalt(
                f"unable to inspect writer lock workdir safely: {type(exc).__name__}"
            ) from exc
        self._path = (
            self._dir / f"workspace-write-{details.st_dev}-{details.st_ino}.lock"
        )
        self._file = None

    def __enter__(self) -> "WorkspaceWriterLock":
        try:
            if os.name == "nt":
                try:
                    self._dir.mkdir()
                except FileExistsError:
                    pass
                _windows_secure_private(self._dir)
            else:
                try:
                    self._dir.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            private_existing_dir(self._dir, label="workspace writer lock directory")
        except DispatchHalt:
            raise
        except OSError as exc:
            raise DispatchHalt(
                "unable to prepare workspace writer lock directory safely: "
                f"{type(exc).__name__}"
            ) from exc
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self._path, flags, 0o600)
        except OSError as exc:
            raise DispatchHalt(
                f"unable to open workspace writer lock safely: {type(exc).__name__}"
            ) from exc
        try:
            details = os.fstat(descriptor)
            if os.name == "nt":
                _windows_secure_private(self._path)
                expected = current_owner_regular(
                    self._path, label="workspace writer lock"
                )
                if (
                    details.st_dev != expected.st_dev
                    or details.st_ino != expected.st_ino
                ):
                    raise DispatchHalt("workspace writer lock changed during safe open")
            else:
                if (
                    not stat.S_ISREG(details.st_mode)
                    or details.st_uid != os.geteuid()
                    or details.st_nlink != 1
                    or stat.S_IMODE(details.st_mode) & 0o077
                ):
                    raise DispatchHalt(
                        "workspace writer lock is not a private current-owner regular file"
                    )
                os.fchmod(descriptor, 0o600)
        except DispatchHalt:
            os.close(descriptor)
            raise
        except OSError as exc:
            os.close(descriptor)
            raise DispatchHalt(
                f"unable to inspect workspace writer lock safely: {type(exc).__name__}"
            ) from exc
        try:
            handle = os.fdopen(descriptor, "a+")
        except OSError as exc:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise DispatchHalt(
                f"unable to open writer lock handle safely: {type(exc).__name__}"
            ) from exc
        try:
            _lock_file(handle, exclusive=True, nonblocking=True)
        except BlockingIOError as exc:
            handle.close()
            raise DispatchHalt(
                "workspace writer lock already held for this workdir"
            ) from exc
        except OSError as exc:
            handle.close()
            raise DispatchHalt(
                f"unable to acquire workspace writer lock safely: {type(exc).__name__}"
            ) from exc
        self._file = handle
        return self

    def __exit__(self, *_: object) -> None:
        if self._file is not None:
            try:
                _unlock_file(self._file)
            finally:
                self._file.close()


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file is not None:
        path = Path(args.prompt_file).expanduser()
        descriptor = None
        try:
            if os.name == "nt":
                if _path_is_link(path):
                    raise DispatchHalt("prompt file must not be a reparse point")
                path = path.absolute()
                expected = current_owner_regular(path, label="prompt file")
                descriptor = os.open(path, os.O_RDONLY)
                details = os.fstat(descriptor)
                if (
                    details.st_dev != expected.st_dev
                    or details.st_ino != expected.st_ino
                ):
                    raise DispatchHalt("prompt file changed during safe open")
                raw = os.read(descriptor, MAX_PROMPT_BYTES + 1)
            else:
                descriptor = os.open(
                    path,
                    os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                )
                details = os.fstat(descriptor)
                if not stat.S_ISREG(details.st_mode):
                    raise DispatchHalt("prompt file must be regular")
                if details.st_uid != os.geteuid() or details.st_mode & 0o077:
                    raise DispatchHalt(
                        "prompt file must be current-owner and owner-only"
                    )
                flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
                fcntl.fcntl(descriptor, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
                raw = os.read(descriptor, MAX_PROMPT_BYTES + 1)
        except DispatchHalt:
            raise
        except OSError as exc:
            raise DispatchHalt(
                f"unable to read prompt file: {type(exc).__name__}"
            ) from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        try:
            prompt = raw.decode("utf-8")
        except UnicodeError as exc:
            raise DispatchHalt("prompt file must be UTF-8") from exc
    else:
        try:
            raw = sys.stdin.buffer.read(MAX_PROMPT_BYTES + 1)
            prompt = raw.decode("utf-8")
        except UnicodeError as exc:
            raise DispatchHalt("stdin prompt must be UTF-8") from exc

    encoded = prompt.encode("utf-8")
    if not prompt.strip():
        raise DispatchHalt("prompt is empty")
    if b"\x00" in encoded:
        raise DispatchHalt("prompt contains a NUL byte")
    if len(encoded) > MAX_PROMPT_BYTES:
        raise DispatchHalt(f"prompt exceeds {MAX_PROMPT_BYTES} bytes")
    return prompt


def parse_exec_stream(raw: str) -> tuple[str, str]:
    thread_ids: list[str] = []
    messages: list[str] = []
    terminal = False
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DispatchHalt("codex exec emitted malformed JSONL") from exc
        if not isinstance(event, dict):
            raise DispatchHalt("codex exec emitted a non-object event")
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                thread_ids.append(thread_id)
        elif event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    messages.append(text)
        elif event.get("type") == "turn.completed":
            terminal = True

    if len(thread_ids) != 1:
        raise DispatchHalt("codex exec did not emit exactly one thread identity")
    try:
        parsed_thread = uuid.UUID(thread_ids[0])
    except ValueError as exc:
        raise DispatchHalt("codex exec emitted an invalid thread identity") from exc
    if str(parsed_thread) != thread_ids[0].lower():
        raise DispatchHalt("codex exec emitted a non-canonical thread identity")
    if not terminal:
        raise DispatchHalt("codex exec did not emit a terminal turn event")
    if not messages:
        raise DispatchHalt("codex exec did not emit a final agent message")
    return thread_ids[0], "\n".join(messages)


def scan_rollout_candidates(
    sessions: Path,
    thread_id: str,
    earliest_mtime_ns: int,
    deadline: float,
) -> list[Path]:
    """Scan the documented YYYY/MM/DD layout without materializing the tree."""
    target_suffix = f"{thread_id}.jsonl"
    expected_component = {
        0: re.compile(r"^[0-9]{4}$"),
        1: re.compile(r"^[0-9]{2}$"),
        2: re.compile(r"^[0-9]{2}$"),
    }
    stack = [(sessions, 0)]
    scanned = 0
    matches: list[Path] = []
    while stack:
        if time.monotonic() >= deadline:
            raise DispatchHalt("rollout candidate scan timed out")
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > MAX_ROLLOUT_SCAN_ENTRIES:
                        raise DispatchHalt(
                            "rollout candidate scan exceeds the entry limit"
                        )
                    if time.monotonic() >= deadline:
                        raise DispatchHalt("rollout candidate scan timed out")
                    if entry.is_symlink() or _stat_is_reparse(
                        entry.stat(follow_symlinks=False)
                    ):
                        continue
                    if depth < 3:
                        if not expected_component[depth].fullmatch(entry.name):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append((Path(entry.path), depth + 1))
                        continue
                    if not (
                        entry.name.startswith("rollout-")
                        and entry.name.endswith(target_suffix)
                        and entry.is_file(follow_symlinks=False)
                    ):
                        continue
                    details = entry.stat(follow_symlinks=False)
                    if details.st_mtime_ns < earliest_mtime_ns - 1_000_000_000:
                        continue
                    matches.append(Path(entry.path))
                    if len(matches) > 1:
                        raise DispatchHalt(
                            "multiple rollout records matched the child session"
                        )
        except DispatchHalt:
            raise
        except OSError as exc:
            raise DispatchHalt(
                f"unable to inspect rollout candidates: {type(exc).__name__}"
            ) from exc
    return matches


def find_rollout(home: Path, thread_id: str, earliest_mtime_ns: int) -> Path:
    sessions = home / "sessions"
    deadline = time.monotonic() + ROLLOUT_WAIT_SECONDS
    matches: list[Path] = []
    if sessions.exists() or _path_is_link(sessions):
        if os.name == "nt":
            _windows_validate_path(
                sessions, label="Codex sessions directory", require_directory=True
            )
        else:
            try:
                details = os.lstat(sessions)
            except OSError as exc:
                raise DispatchHalt(
                    "unable to inspect Codex sessions directory"
                ) from exc
            if (
                stat.S_ISLNK(details.st_mode)
                or not stat.S_ISDIR(details.st_mode)
                or details.st_uid != os.geteuid()
                or stat.S_IMODE(details.st_mode) & 0o022
            ):
                raise DispatchHalt("Codex sessions directory is unsafe")
            reject_darwin_allow_acl(sessions, label="Codex sessions directory")
    while time.monotonic() < deadline:
        if sessions.is_dir():
            matches = scan_rollout_candidates(
                sessions, thread_id, earliest_mtime_ns, deadline
            )
        if len(matches) == 1:
            break
        time.sleep(0.05)
    if len(matches) != 1:
        raise DispatchHalt("host-written rollout record is unavailable")
    path = matches[0]
    validate_rollout_path(home, path)
    return path


def validate_rollout_path(home: Path, path: Path) -> None:
    """Require an owner-controlled, non-writable chain from home to rollout."""
    if os.name == "nt":
        home_value = os.path.normcase(os.path.realpath(home))
        path_value = os.path.normcase(os.path.realpath(path))
        try:
            if os.path.commonpath((home_value, path_value)) != home_value:
                raise DispatchHalt("rollout record is outside the Codex home")
        except ValueError as exc:
            raise DispatchHalt("rollout record is outside the Codex home") from exc
        relative = Path(os.path.relpath(path_value, home_value))
        current = home
        for index, component in enumerate(relative.parts):
            current /= component
            _windows_validate_path(
                current,
                label="rollout provenance path",
                require_file=index == len(relative.parts) - 1,
                require_directory=index != len(relative.parts) - 1,
            )
        return
    try:
        relative = path.relative_to(home)
    except ValueError as exc:
        raise DispatchHalt("rollout record is outside the Codex home") from exc
    current = home
    for index, component in enumerate(relative.parts):
        current /= component
        try:
            details = os.lstat(current)
        except OSError as exc:
            raise DispatchHalt("unable to inspect rollout provenance path") from exc
        if stat.S_ISLNK(details.st_mode):
            raise DispatchHalt("rollout provenance path contains a symlink")
        is_final = index == len(relative.parts) - 1
        expected_type = stat.S_ISREG if is_final else stat.S_ISDIR
        if not expected_type(details.st_mode):
            raise DispatchHalt("rollout provenance path has an unexpected object type")
        if details.st_uid != os.geteuid():
            raise DispatchHalt(
                "rollout provenance path is not owned by the current identity"
            )
        if stat.S_IMODE(details.st_mode) & 0o022:
            raise DispatchHalt("rollout provenance path is group/world-writable")
        reject_darwin_allow_acl(current, label="rollout provenance path")


def read_rollout(path: Path) -> bytes:
    try:
        expected = current_owner_regular(path, label="rollout record")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        details = os.fstat(descriptor)
        unsafe = not stat.S_ISREG(details.st_mode)
        if os.name == "nt":
            unsafe = (
                unsafe
                or details.st_dev != expected.st_dev
                or details.st_ino != expected.st_ino
            )
        else:
            unsafe = (
                unsafe
                or details.st_uid != os.geteuid()
                or stat.S_IMODE(details.st_mode) & 0o022
            )
        if unsafe:
            os.close(descriptor)
            raise DispatchHalt("rollout record is not a current-owner regular file")
        if details.st_size > MAX_ROLLOUT_BYTES:
            os.close(descriptor)
            raise DispatchHalt(f"rollout record exceeds {MAX_ROLLOUT_BYTES} bytes")
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read(MAX_ROLLOUT_BYTES + 1)
        if len(raw) > MAX_ROLLOUT_BYTES:
            raise DispatchHalt(f"rollout record exceeds {MAX_ROLLOUT_BYTES} bytes")
        return raw
    except DispatchHalt:
        raise
    except OSError as exc:
        raise DispatchHalt(
            f"unable to read rollout record: {type(exc).__name__}"
        ) from exc


def load_observation(path: Path) -> tuple[dict[str, Any], bytes]:
    deadline = time.monotonic() + ROLLOUT_WAIT_SECONDS
    last_error = "rollout record is incomplete"
    while time.monotonic() < deadline:
        raw = read_rollout(path)
        metas = []
        turns = []
        malformed = False
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed = True
                break
            if not isinstance(event, dict) or not isinstance(
                event.get("payload"), dict
            ):
                continue
            if event.get("type") == "session_meta":
                metas.append(event["payload"])
            elif event.get("type") == "turn_context":
                turns.append(event["payload"])
        if len(metas) > 1 or len(turns) > 1:
            raise DispatchHalt(
                "rollout record must contain one session metadata and one turn context"
            )
        if not malformed and len(metas) == 1 and len(turns) == 1:
            return {"meta": metas[0], "turn": turns[0]}, raw
        last_error = (
            "rollout record contains malformed JSONL"
            if malformed
            else "rollout record is incomplete"
        )
        time.sleep(0.05)
    raise DispatchHalt(last_error)


def sandbox_name(turn: dict[str, Any]) -> str | None:
    policy = turn.get("sandbox_policy")
    return policy.get("type") if isinstance(policy, dict) else None


def verify_observation(
    observation: dict[str, Any],
    *,
    thread_id: str,
    route: Route,
    workdir: Path,
) -> dict[str, Any]:
    meta = observation["meta"]
    turn = observation["turn"]
    observed = {
        "session_id": meta.get("session_id") or meta.get("id"),
        "source": meta.get("source"),
        "originator": meta.get("originator"),
        "model_provider": meta.get("model_provider"),
        "model": turn.get("model"),
        "effort": turn.get("effort"),
        "sandbox": sandbox_name(turn),
        "sandbox_policy": turn.get("sandbox_policy"),
        "approval_policy": turn.get("approval_policy"),
        "cwd": turn.get("cwd"),
        "session_cwd": meta.get("cwd"),
        "cli_version": meta.get("cli_version"),
    }
    if observed["cli_version"] is not None and not isinstance(
        observed["cli_version"], str
    ):
        raise DispatchHalt("host observation mismatch: cli_version")
    expected = {
        "session_id": thread_id,
        "source": "exec",
        "originator": "codex_exec",
        "model_provider": "openai",
        "model": route.model,
        "effort": route.effort,
        "sandbox": route.sandbox,
        "approval_policy": "never",
        "cwd": str(workdir),
        "session_cwd": str(workdir),
    }
    mismatches = [
        key
        for key, expected_value in expected.items()
        if key not in {"cwd", "session_cwd"} and observed.get(key) != expected_value
    ]
    for key in ("cwd", "session_cwd"):
        value = observed.get(key)
        if not isinstance(value, str) or not paths_same(Path(value), workdir):
            mismatches.append(key)
        else:
            observed[key] = str(workdir)
    if mismatches:
        raise DispatchHalt(
            "host observation mismatch: " + ", ".join(sorted(mismatches))
        )
    policy = observed["sandbox_policy"]
    if not isinstance(policy, dict):
        raise DispatchHalt("host observation mismatch: sandbox_policy")
    if route.sandbox == "read-only" and policy != {"type": "read-only"}:
        raise DispatchHalt("host observation mismatch: sandbox_policy")
    if route.sandbox == "workspace-write":
        allowed_keys = {
            "type",
            "network_access",
            "exclude_slash_tmp",
            "exclude_tmpdir_env_var",
        }
        if (
            set(policy) != allowed_keys
            or policy.get("type") != "workspace-write"
            or policy.get("network_access") is not False
            or not isinstance(policy.get("exclude_slash_tmp"), bool)
            or not isinstance(policy.get("exclude_tmpdir_env_var"), bool)
        ):
            raise DispatchHalt("host observation mismatch: sandbox_policy")
        # Codex 0.144.1 serializes an empty workspace-write `writable_roots`
        # list by omitting the key. Any present key is rejected above. Make
        # that verified canonical-empty state explicit in the normalized
        # receipt so offline consumers do not have to infer it from omission.
        observed["sandbox_policy"] = {**policy, "writable_roots": []}
    return observed


def ledger_append(home: Path, receipt: dict[str, Any]) -> str:
    ledger_dir = home / "tier-routing"
    ensure_private_dir(ledger_dir)
    ledger = ledger_dir / "ledger.jsonl"
    if _path_is_link(ledger):
        raise DispatchHalt("refusing symlinked local receipt ledger")
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(ledger, flags, 0o600)
    except OSError as exc:
        raise DispatchHalt(
            f"unable to open local receipt ledger safely: {type(exc).__name__}"
        ) from exc
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        if os.name == "nt":
            _windows_secure_private(ledger)
            current_owner_regular(ledger, label="local receipt ledger")
        else:
            os.fchmod(handle.fileno(), 0o600)
        reject_darwin_allow_acl(ledger, label="local receipt ledger")
        _lock_file(handle, exclusive=True)
        try:
            if os.fstat(handle.fileno()).st_size > MAX_LEDGER_BYTES:
                raise DispatchHalt("local receipt ledger exceeds the total size limit")
            previous_hash = validate_ledger_lines(handle)
            entry = {"previous_hash": previous_hash, "receipt": receipt}
            entry_hash = sha256_bytes(canonical_json(entry))
            entry["entry_hash"] = entry_hash
            serialized = json.dumps(entry, sort_keys=True) + "\n"
            serialized_size = len(serialized.encode("utf-8"))
            if serialized_size > MAX_LEDGER_ENTRY_BYTES:
                raise DispatchHalt("local receipt ledger entry exceeds the size limit")
            if os.fstat(handle.fileno()).st_size + serialized_size > MAX_LEDGER_BYTES:
                raise DispatchHalt(
                    "local receipt ledger would exceed the total size limit; "
                    "checkpoint and rotate it before dispatching again"
                )
            handle.seek(0, os.SEEK_END)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            _unlock_file(handle)
    return f"local-ledger://{entry_hash}"


def base_receipt(
    route_name: str,
    route: Route,
    policy: RoutingPolicy,
    workdir: Path,
    launch: CodexLaunch,
) -> dict[str, Any]:
    launcher, dispatcher_runtime, interpreter = dispatcher_launch_chain()
    receipt = {
        "schema": SURGEON_SCHEMA if route_name == "surgeon" else SCHEMA,
        "receipt_id": str(uuid.uuid4()),
        "mechanism": "codex-exec-local",
        "claim_class": "dispatcher_configured_and_host_observed",
        "route_requested": route_name,
        "route_resolved": route_name,
        "policy_kind": policy.kind,
        "policy_id": policy.policy_id,
        "policy_sha256": policy.sha256,
        "policy_input_sha256": policy.input_sha256,
        "policy_document": policy.document,
        "requested": {
            "model": route.model,
            "effort": route.effort,
            "sandbox": route.sandbox,
            "approval_policy": "never",
            "cwd": str(workdir),
        },
        "dispatcher_launcher": str(launcher),
        "dispatcher_launcher_sha256": sha256_file(launcher),
        "dispatcher_runtime": str(dispatcher_runtime),
        "dispatcher_runtime_sha256": sha256_file(dispatcher_runtime),
        "dispatcher_python": str(interpreter),
        "dispatcher_python_sha256": sha256_file(interpreter),
        "codex_executable": str(launch.entrypoint),
        "codex_executable_sha256": sha256_file(launch.entrypoint),
        "codex_runtime": str(launch.runtime),
        "codex_runtime_sha256": sha256_file(launch.runtime),
        "codex_launch_method": launch.method,
        "observed": None,
        "status": "halt",
        "started_at": utc_now(),
        "finished_at": None,
        "output_sha256": None,
        "rollout_sha256": None,
        "error": None,
    }
    if route_name == "surgeon":
        receipt["workspace_precondition"] = None
    return receipt


def verify_policy_binding(receipt: dict[str, Any]) -> None:
    """Verify the self-contained policy/request/observation relationship."""
    schema = receipt.get("schema")
    if schema == SCHEMA:
        policy_schema = POLICY_SCHEMA
        default_id = DEFAULT_POLICY_ID
        custom_id = CUSTOM_POLICY_ID
        fixed_routes = ROUTES
    elif schema == SURGEON_SCHEMA:
        policy_schema = POLICY_V2_SCHEMA
        default_id = DEFAULT_POLICY_V2_ID
        custom_id = CUSTOM_POLICY_V2_ID
        fixed_routes = {**ROUTES, "surgeon": SURGEON_ROUTE}
    else:
        raise DispatchHalt("receipt schema is unsupported")

    document = receipt.get("policy_document")
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "policy_id",
        "routes",
    }:
        raise DispatchHalt("receipt policy document is unavailable")
    if sha256_bytes(canonical_json(document)) != receipt.get("policy_sha256"):
        raise DispatchHalt("receipt policy document hash mismatch")
    if document.get("schema") != policy_schema:
        raise DispatchHalt("receipt policy document schema mismatch")
    if document.get("policy_id") != receipt.get("policy_id"):
        raise DispatchHalt("receipt policy identifier mismatch")
    kind = receipt.get("policy_kind")
    if kind not in {"builtin", "custom"}:
        raise DispatchHalt("receipt policy kind is invalid")
    expected_id = default_id if kind == "builtin" else custom_id
    if receipt.get("policy_id") != expected_id:
        raise DispatchHalt("receipt policy kind and identifier mismatch")
    if not isinstance(
        receipt.get("policy_input_sha256"), str
    ) or not SHA256_PATTERN.fullmatch(receipt["policy_input_sha256"]):
        raise DispatchHalt("receipt policy input hash is invalid")
    route_name = receipt.get("route_requested")
    if route_name != receipt.get("route_resolved"):
        raise DispatchHalt("receipt route resolution mismatch")
    if schema == SURGEON_SCHEMA and route_name != "surgeon":
        raise DispatchHalt("receipt v3 route must be surgeon")
    routes = document.get("routes")
    if not isinstance(routes, dict) or set(routes) != set(fixed_routes):
        raise DispatchHalt("receipt policy route set mismatch")
    for name, fixed in fixed_routes.items():
        selection = routes[name]
        if not isinstance(selection, dict) or set(selection) != {
            "model",
            "effort",
            "sandbox",
            "approval_policy",
        }:
            raise DispatchHalt(f"receipt policy route is invalid: {name}")
        if not isinstance(selection["model"], str) or not MODEL_ID_PATTERN.fullmatch(
            selection["model"]
        ):
            raise DispatchHalt(f"receipt policy model is invalid: {name}")
        if selection["effort"] not in ACCEPTED_EFFORTS:
            raise DispatchHalt(f"receipt policy effort is invalid: {name}")
        if selection["sandbox"] != fixed.sandbox:
            raise DispatchHalt(f"receipt policy sandbox mismatch: {name}")
        if selection["approval_policy"] != "never":
            raise DispatchHalt(f"receipt policy approval mismatch: {name}")
        if name == "surgeon" and (
            selection["model"] != fixed.model or selection["effort"] != fixed.effort
        ):
            raise DispatchHalt("receipt surgeon policy tuple mismatch")
    route = routes.get(route_name) if isinstance(routes, dict) else None
    requested = receipt.get("requested")
    if not isinstance(route, dict) or not isinstance(requested, dict):
        raise DispatchHalt("receipt policy route binding is unavailable")
    for key in ("model", "effort", "sandbox", "approval_policy"):
        if requested.get(key) != route.get(key):
            raise DispatchHalt(f"receipt policy request mismatch: {key}")
    observed = receipt.get("observed")
    if observed is not None:
        if not isinstance(observed, dict):
            raise DispatchHalt("receipt observation is invalid")
        for key in ("model", "effort", "sandbox", "approval_policy"):
            if observed.get(key) != requested.get(key):
                raise DispatchHalt(f"receipt policy observation mismatch: {key}")


def _assign_windows_job(process: subprocess.Popen[bytes]) -> None:
    job = _KERNEL32.CreateJobObjectW(None, None)
    if not job:
        process.kill()
        raise DispatchHalt("unable to create a Windows process job")
    limits = _JobExtendedLimits()
    limits.basic.limit_flags = 0x00002000
    if not _KERNEL32.SetInformationJobObject(
        job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ) or not _KERNEL32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
        error = ctypes.get_last_error()
        process.kill()
        _KERNEL32.CloseHandle(job)
        raise DispatchHalt(
            f"unable to contain the Windows process tree: {_windows_error('job', error)}"
        )
    process._codex_tier_job = job  # type: ignore[attr-defined]


def _resume_windows_process(process: subprocess.Popen[bytes]) -> None:
    snapshot = _KERNEL32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise _windows_error("unable to inspect the suspended Windows process")
    try:
        entry = _ThreadEntry32()
        entry.size = ctypes.sizeof(entry)
        found = _KERNEL32.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.owner_process_id == process.pid:
                thread = _KERNEL32.OpenThread(0x0002, False, entry.thread_id)
                if not thread:
                    raise _windows_error("unable to open the suspended Windows thread")
                try:
                    if _KERNEL32.ResumeThread(thread) == 0xFFFFFFFF:
                        raise _windows_error(
                            "unable to resume the contained Windows process"
                        )
                    return
                finally:
                    _KERNEL32.CloseHandle(thread)
            found = _KERNEL32.Thread32Next(snapshot, ctypes.byref(entry))
        raise DispatchHalt("unable to find the suspended Windows process thread")
    finally:
        _KERNEL32.CloseHandle(snapshot)


def _close_windows_job(
    process: subprocess.Popen[bytes], *, terminate: bool = False
) -> None:
    job = getattr(process, "_codex_tier_job", None)
    if not job:
        return
    if terminate:
        _KERNEL32.TerminateJobObject(job, 1)
    _KERNEL32.CloseHandle(job)
    process._codex_tier_job = None  # type: ignore[attr-defined]


def reap_process_group(process: subprocess.Popen[bytes]) -> None:
    """Tear down a child group without swallowing a second control-flow exit."""
    if os.name == "nt":
        _close_windows_job(process, terminate=True)
        try:
            process.kill()
        except OSError:
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_windows_process_bounded(
    command: list[str],
    *,
    task_input: Any,
    cwd: Path | None,
    env: dict[str, str],
    timeout: int,
    stdout_limit: int,
    stderr_limit: int,
    operation: str,
    suffix: str,
) -> tuple[subprocess.Popen[bytes], int, bytes, bytes]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=task_input,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | _WINDOWS_CREATE_SUSPENDED),
    )
    try:
        _assign_windows_job(process)
        _resume_windows_process(process)
    except BaseException:
        reap_process_group(process)
        raise
    if process.stdout is None or process.stderr is None:
        reap_process_group(process)
        raise DispatchHalt(f"{operation} did not expose bounded streams")

    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": stdout_limit, "stderr": stderr_limit}
    overflow: list[str] = []
    overflow_event = threading.Event()

    def read_stream(name: str, stream: Any) -> None:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            buffers[name].extend(chunk)
            if len(buffers[name]) > limits[name]:
                overflow.append(name)
                overflow_event.set()
                return

    readers = [
        threading.Thread(target=read_stream, args=(name, stream), daemon=True)
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if overflow_event.wait(0.01):
            name = overflow[0]
            reap_process_group(process)
            raise DispatchHalt(
                f"{operation} {name} exceeded {limits[name]} bytes{suffix}"
            )
        if time.monotonic() >= deadline:
            reap_process_group(process)
            raise DispatchHalt(f"{operation} timed out{suffix}")
    returncode = process.wait()
    _close_windows_job(process)
    for reader in readers:
        reader.join(timeout=5)
    if any(reader.is_alive() for reader in readers):
        reap_process_group(process)
        raise DispatchHalt(f"{operation} stream cleanup failed{suffix}")
    process.stdout.close()
    process.stderr.close()
    if overflow:
        name = overflow[0]
        raise DispatchHalt(f"{operation} {name} exceeded {limits[name]} bytes{suffix}")
    return process, returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"])


def run_process_bounded(
    command: list[str],
    *,
    input_bytes: bytes,
    cwd: Path | None,
    env: dict[str, str],
    timeout: int,
    stdout_limit: int,
    stderr_limit: int,
    operation: str = "Codex control command",
    ambiguous_on_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one process while bounding both captured streams in memory."""
    process: subprocess.Popen[bytes] | None = None
    suffix = "; completion state is ambiguous" if ambiguous_on_failure else ""
    try:
        with tempfile.TemporaryFile(
            mode="w+b", dir=None if os.name == "nt" else "/tmp"
        ) as task_input:
            task_input.write(input_bytes)
            task_input.seek(0)
            if os.name == "nt":
                process, returncode, stdout_bytes, stderr_bytes = (
                    _run_windows_process_bounded(
                        command,
                        task_input=task_input,
                        cwd=cwd,
                        env=env,
                        timeout=timeout,
                        stdout_limit=stdout_limit,
                        stderr_limit=stderr_limit,
                        operation=operation,
                        suffix=suffix,
                    )
                )
            else:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdin=task_input,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    start_new_session=True,
                )
                if process.stdout is None or process.stderr is None:
                    raise DispatchHalt(f"{operation} did not expose bounded streams")

                buffers = {"stdout": bytearray(), "stderr": bytearray()}
                limits = {"stdout": stdout_limit, "stderr": stderr_limit}
                selector = selectors.DefaultSelector()
                try:
                    for name, stream in (
                        ("stdout", process.stdout),
                        ("stderr", process.stderr),
                    ):
                        os.set_blocking(stream.fileno(), False)
                        selector.register(stream, selectors.EVENT_READ, name)
                    deadline = time.monotonic() + timeout
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            reap_process_group(process)
                            raise DispatchHalt(f"{operation} timed out{suffix}")
                        events = selector.select(min(remaining, 0.25))
                        for key, _ in events:
                            name = key.data
                            try:
                                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                            except BlockingIOError:
                                continue
                            if not chunk:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                                continue
                            buffers[name].extend(chunk)
                            if len(buffers[name]) > limits[name]:
                                reap_process_group(process)
                                raise DispatchHalt(
                                    f"{operation} {name} exceeded "
                                    f"{limits[name]} bytes{suffix}"
                                )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        reap_process_group(process)
                        raise DispatchHalt(f"{operation} timed out{suffix}")
                    try:
                        returncode = process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired as exc:
                        reap_process_group(process)
                        raise DispatchHalt(f"{operation} timed out{suffix}") from exc
                finally:
                    selector.close()
                stdout_bytes = bytes(buffers["stdout"])
                stderr_bytes = bytes(buffers["stderr"])
        try:
            stdout = stdout_bytes.decode("utf-8")
            stderr = stderr_bytes.decode("utf-8")
        except UnicodeError as exc:
            if process is not None:
                reap_process_group(process)
            raise DispatchHalt(f"{operation} emitted non-UTF-8 output{suffix}") from exc
    except DispatchHalt:
        if process is not None:
            reap_process_group(process)
        raise
    except BaseException as exc:
        if process is not None:
            reap_process_group(process)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise DispatchHalt(f"{operation} failed: {type(exc).__name__}{suffix}") from exc
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def run_codex(
    command: list[str], prompt: str, timeout: int, workdir: Path, home: Path
) -> subprocess.CompletedProcess[str]:
    return run_process_bounded(
        command,
        input_bytes=prompt.encode("utf-8"),
        cwd=workdir,
        env=child_environment(home),
        timeout=timeout,
        stdout_limit=MAX_DISPATCH_STDOUT_BYTES,
        stderr_limit=MAX_DISPATCH_STDERR_BYTES,
        operation="codex exec",
        ambiguous_on_failure=True,
    )


def resolve_git() -> Path:
    executable = shutil.which("git", path=os.environ.get("PATH"))
    if executable is None or not Path(executable).is_absolute():
        raise DispatchHalt("Git executable is unavailable")
    resolved = validate_executable_chain(Path(executable), label="Git executable path")
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise DispatchHalt("Git executable is not an executable regular file")
    return resolved


def git_preflight_output(
    executable: Path,
    workdir: Path,
    home: Path,
    *arguments: str,
    failure: str,
    input_bytes: bytes = b"",
    stdout_limit: int = MAX_CONTROL_STDOUT_BYTES,
) -> str:
    result = run_process_bounded(
        [
            str(executable),
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(workdir),
            *arguments,
        ],
        input_bytes=input_bytes,
        cwd=workdir,
        env={
            **child_environment(home),
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
        timeout=10,
        stdout_limit=stdout_limit,
        stderr_limit=MAX_CONTROL_STDERR_BYTES,
        operation="surgeon Git preflight",
    )
    if result.returncode != 0:
        raise DispatchHalt(failure)
    return result.stdout


def reject_active_git_filters(executable: Path, workdir: Path, home: Path) -> None:
    staged = git_preflight_output(
        executable,
        workdir,
        home,
        "ls-files",
        "--cached",
        "--stage",
        "-z",
        failure="unable to inspect surgeon Git index",
        stdout_limit=MAX_DISPATCH_STDOUT_BYTES,
    )
    paths: list[str] = []
    for entry in staged.rstrip("\0").split("\0") if staged else ():
        metadata, separator, path = entry.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or not path:
            raise DispatchHalt("surgeon Git index output is malformed")
        mode, _, stage = fields
        if stage != "0":
            raise DispatchHalt("surgeon worktree must not have unmerged index entries")
        if mode == "160000":
            raise DispatchHalt("surgeon worktrees with submodules are not supported")
        paths.append(path)

    if not paths:
        return
    attributes = git_preflight_output(
        executable,
        workdir,
        home,
        "check-attr",
        "-z",
        "--stdin",
        "--all",
        failure="unable to inspect surgeon Git filter attributes",
        input_bytes=("\0".join(paths) + "\0").encode(),
        stdout_limit=MAX_DISPATCH_STDOUT_BYTES,
    )
    fields = attributes.rstrip("\0").split("\0") if attributes else []
    if len(fields) % 3:
        raise DispatchHalt("surgeon Git attribute output is malformed")
    for index in range(0, len(fields), 3):
        attribute = fields[index + 1]
        if attribute == "filter":
            raise DispatchHalt(
                "surgeon worktree uses Git filter attributes; refusing unsafe status scan"
            )


def surgeon_workspace_precondition(workdir: Path, home: Path) -> dict[str, str]:
    if os.name == "nt":
        _windows_validate_path(workdir, label="surgeon workdir", require_directory=True)
    executable = resolve_git()

    root_raw = git_preflight_output(
        executable,
        workdir,
        home,
        "rev-parse",
        "--show-toplevel",
        failure="surgeon workdir must be the root of a linked Git worktree",
    ).strip()
    if not root_raw or "\n" in root_raw:
        raise DispatchHalt("surgeon workdir must be a Git worktree root")
    try:
        root = Path(root_raw).resolve(strict=True)
    except OSError as exc:
        raise DispatchHalt("surgeon workdir must be a Git worktree root") from exc
    if not paths_same(root, workdir):
        raise DispatchHalt("surgeon workdir must be a Git worktree root")

    git_paths = []
    for argument in ("--git-dir", "--git-common-dir"):
        raw = git_preflight_output(
            executable,
            workdir,
            home,
            "rev-parse",
            argument,
            failure="surgeon workdir must be a linked Git worktree",
        ).strip()
        if not raw or "\n" in raw:
            raise DispatchHalt("surgeon workdir must be a linked Git worktree")
        candidate = Path(raw)
        try:
            git_paths.append(
                (candidate if candidate.is_absolute() else workdir / candidate).resolve(
                    strict=True
                )
            )
        except OSError as exc:
            raise DispatchHalt("surgeon workdir must be a linked Git worktree") from exc
    if os.name == "nt":
        for git_path in git_paths:
            _windows_validate_path(
                git_path, label="surgeon Git metadata", require_directory=True
            )
    if paths_same(git_paths[0], git_paths[1]):
        raise DispatchHalt("surgeon workdir must be a linked Git worktree")

    starting_head = git_preflight_output(
        executable,
        workdir,
        home,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        failure="surgeon worktree must have a valid HEAD",
    ).strip()
    if GIT_OBJECT_ID_PATTERN.fullmatch(starting_head) is None:
        raise DispatchHalt("surgeon worktree must have a valid Git HEAD object ID")

    reject_active_git_filters(executable, workdir, home)
    status = git_preflight_output(
        executable,
        workdir,
        home,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=all",
        "--no-renames",
        failure="unable to inspect surgeon worktree status",
    )
    if status:
        raise DispatchHalt("surgeon worktree must be clean, including untracked files")

    current_head = git_preflight_output(
        executable,
        workdir,
        home,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        failure="surgeon worktree HEAD changed during preflight",
    ).strip()
    if current_head != starting_head:
        raise DispatchHalt("surgeon worktree HEAD changed during preflight")
    return {
        "kind": "clean-linked-git-worktree",
        "starting_head": starting_head,
    }


def dispatch(args: argparse.Namespace, home: Path | None = None) -> int:
    home = resolve_codex_home() if home is None else home
    active_policy = load_policy(home)
    is_surgeon = args.route == "surgeon"
    receipt_policy = surgeon_policy(active_policy) if is_surgeon else active_policy
    route = SURGEON_ROUTE if is_surgeon else active_policy.routes[args.route]
    workdir = resolve_workdir(args.workdir)
    if not workdir.is_dir():
        raise DispatchHalt("workdir must be an existing directory")
    prompt = read_prompt(args)
    if paths_overlap(home, workdir):
        raise DispatchHalt("workdir and Codex home must not contain one another")
    launch = resolve_codex()
    if native_multi_agent_enabled(launch, home, cwd=workdir):
        raise DispatchHalt(
            "native multi-agent spawning is enabled; run codex-tier strict-enable and restart Codex"
        )
    if native_multi_agent_enabled(launch, home, cwd=workdir, force_disabled=True):
        raise DispatchHalt(
            "Codex did not honor the child native multi-agent disable override"
        )
    preflight_ledger(home)
    receipt = base_receipt(args.route, route, receipt_policy, workdir, launch)
    verify_policy_binding(receipt)
    windows_sandbox = (
        ["-c", f'windows.sandbox="{windows_sandbox_mode(home)}"']
        if os.name == "nt" and route.sandbox == "workspace-write"
        else []
    )
    output = ""
    try:
        writer_lock = (
            WorkspaceWriterLock(home, workdir)
            if route.sandbox == "workspace-write"
            else nullcontext()
        )
        with Slot(home), writer_lock:
            assert_policy_unchanged(active_policy, home)
            if is_surgeon:
                receipt["workspace_precondition"] = surgeon_workspace_precondition(
                    workdir, home
                )
                verify_policy_binding(receipt)
            dispatch_started_ns = time.time_ns()
            command = launch.command(
                "-a",
                "never",
                "exec",
                "--strict-config",
                "--json",
                "-m",
                route.model,
                "-c",
                f'model_reasoning_effort="{route.effort}"',
                "-c",
                'approval_policy="never"',
                "-c",
                "features.multi_agent=false",
                "-c",
                "sandbox_workspace_write.network_access=false",
                "-c",
                "sandbox_workspace_write.writable_roots=[]",
                *windows_sandbox,
                "-s",
                route.sandbox,
                "-C",
                str(workdir),
                "-",
            )
            dispatch_error: DispatchHalt | None = None
            try:
                result = run_codex(command, prompt, args.timeout, workdir, home)
            except DispatchHalt as exc:
                dispatch_error = exc
                result = None
            assert_policy_unchanged(active_policy, home)
            if dispatch_error is not None:
                raise dispatch_error
            assert result is not None
            if (
                sha256_file(Path(receipt["dispatcher_launcher"]))
                != receipt["dispatcher_launcher_sha256"]
            ):
                raise DispatchHalt("dispatcher launcher changed during dispatch")
            if (
                sha256_file(Path(receipt["dispatcher_runtime"]))
                != receipt["dispatcher_runtime_sha256"]
            ):
                raise DispatchHalt("dispatcher runtime changed during dispatch")
            if sha256_file(launch.entrypoint) != receipt["codex_executable_sha256"]:
                raise DispatchHalt("codex executable changed during dispatch")
            if sha256_file(launch.runtime) != receipt["codex_runtime_sha256"]:
                raise DispatchHalt("codex runtime changed during dispatch")
            if (
                sha256_file(Path(sys.executable).resolve(strict=True))
                != receipt["dispatcher_python_sha256"]
            ):
                raise DispatchHalt("dispatcher Python changed during dispatch")
            if result.returncode != 0:
                raise DispatchHalt(f"codex exec exited nonzero ({result.returncode})")
            thread_id, output = parse_exec_stream(result.stdout)
            rollout_path = find_rollout(home, thread_id, dispatch_started_ns)
            observation, rollout_raw = load_observation(rollout_path)
            receipt["observed"] = verify_observation(
                observation,
                thread_id=thread_id,
                route=route,
                workdir=workdir,
            )
            verify_policy_binding(receipt)
            receipt["status"] = "pass"
            receipt["output_sha256"] = sha256_bytes(output.encode())
            receipt["rollout_sha256"] = sha256_bytes(rollout_raw)
    except DispatchHalt as exc:
        receipt["error"] = str(exc)
    receipt["finished_at"] = utc_now()
    receipt_ref = ledger_append(home, receipt)
    envelope = {
        "receipt": receipt,
        "receipt_ref": receipt_ref,
        "result": output if receipt["status"] == "pass" else None,
    }
    print(json.dumps(envelope, sort_keys=True))
    return 0 if receipt["status"] == "pass" else 2


def probe(_: argparse.Namespace, home: Path | None = None) -> int:
    try:
        home = resolve_codex_home() if home is None else home
        policy = load_policy(home)
        launch = resolve_codex()
        launcher, dispatcher_runtime, interpreter = dispatcher_launch_chain()
        native_enabled = native_multi_agent_enabled(launch, home)
        payload = {
            "status": "halt" if native_enabled else "ready",
            "schema": SCHEMA,
            "dispatcher_version": PROJECT_VERSION,
            "dispatcher_launcher": str(launcher),
            "dispatcher_runtime": str(dispatcher_runtime),
            "dispatcher_python": str(interpreter),
            "codex_executable": str(launch.entrypoint),
            "codex_runtime": str(launch.runtime),
            "codex_launch_method": launch.method,
            "codex_version": codex_version(launch, home),
            "codex_home": str(home),
            "native_multi_agent_enabled": native_enabled,
            "policy": {
                "kind": policy.kind,
                "policy_id": policy.policy_id,
                "policy_sha256": policy.sha256,
                "policy_input_sha256": policy.input_sha256,
                "model_access": "not_checked",
                "accepted_efforts": list(ACCEPTED_EFFORTS),
            },
            "routes": {
                name: {
                    "model": value.model,
                    "effort": value.effort,
                    "sandbox": value.sandbox,
                }
                for name, value in policy.routes.items()
            },
            "experimental_routes": {
                "surgeon": {
                    "model": SURGEON_ROUTE.model,
                    "effort": SURGEON_ROUTE.effort,
                    "sandbox": SURGEON_ROUTE.sandbox,
                    "configurable": False,
                }
            },
        }
        if native_enabled:
            payload["error"] = (
                "native multi-agent spawning is enabled; run codex-tier strict-enable and restart Codex"
            )
        print(json.dumps(payload, sort_keys=True))
        return 2 if native_enabled else 0
    except DispatchHalt as exc:
        print(json.dumps({"status": "halt", "error": str(exc)}, sort_keys=True))
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "halt",
                    "error": f"unexpected dispatcher failure: {type(exc).__name__}",
                },
                sort_keys=True,
            )
        )
        return 2


def policy_check(_: argparse.Namespace, home: Path | None = None) -> int:
    """Validate and report the active policy without invoking Codex."""
    home = resolve_codex_home() if home is None else home
    policy = load_policy(home)
    print(
        json.dumps(
            {
                "status": "pass",
                "schema": POLICY_SCHEMA,
                "dispatcher_version": PROJECT_VERSION,
                "policy_kind": policy.kind,
                "policy_id": policy.policy_id,
                "policy_sha256": policy.sha256,
                "policy_input_sha256": policy.input_sha256,
                "policy_document": policy.document,
                "model_access": "not_checked",
                "model_catalog": MODEL_CATALOG_URL,
                "model_access_note": (
                    "Run one bounded dispatch canary per changed route; this Codex "
                    "CLI does not expose a trusted account-model entitlement list."
                ),
                "accepted_efforts": list(ACCEPTED_EFFORTS),
                "routes": {
                    name: {
                        "model": route.model,
                        "effort": route.effort,
                        "sandbox": route.sandbox,
                    }
                    for name, route in policy.routes.items()
                },
                "experimental_routes": {
                    "surgeon": {
                        "model": SURGEON_ROUTE.model,
                        "effort": SURGEON_ROUTE.effort,
                        "sandbox": SURGEON_ROUTE.sandbox,
                        "configurable": False,
                    }
                },
            },
            sort_keys=True,
        )
    )
    return 0


def read_receipt_check_input(args: argparse.Namespace) -> bytes:
    if args.receipt_file is None:
        raw = sys.stdin.buffer.read(MAX_RECEIPT_CHECK_BYTES + 1)
    else:
        path = Path(args.receipt_file).expanduser()
        if not path.is_absolute():
            raise DispatchHalt("receipt file path must be absolute")
        if _path_is_link(path):
            raise DispatchHalt(
                "receipt input path must not be a symlink or reparse point"
            )
        path = validate_executable_chain(path, label="receipt input path")
        details = current_owner_regular(path, label="receipt input")
        if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077:
            raise DispatchHalt(
                "receipt input must not grant group/other permissions (use mode 0600)"
            )
        if details.st_size > MAX_RECEIPT_CHECK_BYTES:
            raise DispatchHalt("receipt input exceeds the size limit")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != details.st_dev
                or opened.st_ino != details.st_ino
                or opened.st_size > MAX_RECEIPT_CHECK_BYTES
                or (
                    os.name != "nt"
                    and (
                        opened.st_uid != os.geteuid()
                        or stat.S_IMODE(opened.st_mode) & 0o077
                    )
                )
            ):
                os.close(descriptor)
                raise DispatchHalt("receipt input changed or became unsafe")
            with os.fdopen(descriptor, "rb") as handle:
                raw = handle.read(MAX_RECEIPT_CHECK_BYTES + 1)
        except OSError as exc:
            raise DispatchHalt(
                f"unable to read receipt input safely: {type(exc).__name__}"
            ) from exc
    if len(raw) > MAX_RECEIPT_CHECK_BYTES:
        raise DispatchHalt("receipt input exceeds the size limit")
    if not raw.strip():
        raise DispatchHalt("receipt input is empty")
    return raw


def verify_receipt_envelope(envelope: Any) -> dict[str, Any]:
    """Check critical envelope semantics after structural JSON Schema validation."""
    if not isinstance(envelope, dict) or set(envelope) != {
        "receipt",
        "receipt_ref",
        "result",
    }:
        raise DispatchHalt("receipt envelope fields are invalid")
    receipt = envelope["receipt"]
    if not isinstance(receipt, dict):
        raise DispatchHalt("receipt fields are invalid")
    schema = receipt.get("schema")
    if schema == SCHEMA:
        expected_fields = RECEIPT_FIELDS
    elif schema == SURGEON_SCHEMA:
        expected_fields = SURGEON_RECEIPT_FIELDS
    else:
        raise DispatchHalt(f"receipt schema must be {SCHEMA} or {SURGEON_SCHEMA}")
    if set(receipt) != expected_fields:
        raise DispatchHalt("receipt fields are invalid")
    if receipt["mechanism"] != "codex-exec-local":
        raise DispatchHalt("receipt mechanism mismatch")
    if receipt["claim_class"] != "dispatcher_configured_and_host_observed":
        raise DispatchHalt("receipt claim class mismatch")
    try:
        receipt_uuid = str(uuid.UUID(receipt["receipt_id"]))
    except (AttributeError, TypeError, ValueError) as exc:
        raise DispatchHalt("receipt identifier is invalid") from exc
    if receipt_uuid != receipt["receipt_id"]:
        raise DispatchHalt("receipt identifier is not canonical")
    if not isinstance(envelope["receipt_ref"], str) or not LEDGER_REF_PATTERN.fullmatch(
        envelope["receipt_ref"]
    ):
        raise DispatchHalt("receipt reference is invalid")
    for field in (
        "policy_sha256",
        "policy_input_sha256",
        "dispatcher_launcher_sha256",
        "dispatcher_runtime_sha256",
        "dispatcher_python_sha256",
        "codex_executable_sha256",
        "codex_runtime_sha256",
    ):
        if not isinstance(receipt[field], str) or not SHA256_PATTERN.fullmatch(
            receipt[field]
        ):
            raise DispatchHalt(f"receipt hash is invalid: {field}")
    for field in (
        "dispatcher_launcher",
        "dispatcher_runtime",
        "dispatcher_python",
        "codex_executable",
        "codex_runtime",
        "started_at",
        "finished_at",
    ):
        if not isinstance(receipt[field], str) or not receipt[field]:
            raise DispatchHalt(f"receipt field is invalid: {field}")
    if receipt["codex_launch_method"] not in {
        "native",
        "explicit-shebang-runtime",
    }:
        raise DispatchHalt("receipt launch method is invalid")
    requested = receipt["requested"]
    if not isinstance(requested, dict) or set(requested) != {
        "model",
        "effort",
        "sandbox",
        "approval_policy",
        "cwd",
    }:
        raise DispatchHalt("receipt requested fields are invalid")
    if not isinstance(requested["cwd"], str) or not requested["cwd"]:
        raise DispatchHalt("receipt requested cwd is invalid")
    verify_policy_binding(receipt)

    status = receipt["status"]
    if schema == SURGEON_SCHEMA:
        precondition = receipt["workspace_precondition"]
        if precondition is None:
            if status != "halt":
                raise DispatchHalt(
                    "passing surgeon receipt requires a workspace precondition"
                )
        elif (
            not isinstance(precondition, dict)
            or set(precondition) != {"kind", "starting_head"}
            or precondition.get("kind") != "clean-linked-git-worktree"
            or not isinstance(precondition.get("starting_head"), str)
            or GIT_OBJECT_ID_PATTERN.fullmatch(precondition["starting_head"]) is None
        ):
            raise DispatchHalt("surgeon workspace precondition is invalid")
    if status == "pass":
        if not isinstance(envelope["result"], str):
            raise DispatchHalt("passing receipt result must be a string")
        if receipt["error"] is not None:
            raise DispatchHalt("passing receipt error must be null")
        for field in ("output_sha256", "rollout_sha256"):
            if not isinstance(receipt[field], str) or not SHA256_PATTERN.fullmatch(
                receipt[field]
            ):
                raise DispatchHalt(f"passing receipt hash is invalid: {field}")
        if sha256_bytes(envelope["result"].encode()) != receipt["output_sha256"]:
            raise DispatchHalt("receipt output hash mismatch")
        observed = receipt["observed"]
        if not isinstance(observed, dict):
            raise DispatchHalt("passing receipt observation is unavailable")
        required_observed = {
            "session_id",
            "source",
            "originator",
            "model_provider",
            "model",
            "effort",
            "sandbox",
            "sandbox_policy",
            "approval_policy",
            "cwd",
            "session_cwd",
            "cli_version",
        }
        if set(observed) != required_observed:
            raise DispatchHalt("receipt observed fields are invalid")
        if (
            observed["source"] != "exec"
            or observed["originator"] != "codex_exec"
            or observed["model_provider"] != "openai"
            or observed["cwd"] != requested["cwd"]
            or observed["session_cwd"] != requested["cwd"]
            or not isinstance(observed["session_id"], str)
            or not observed["session_id"]
            or (
                observed["cli_version"] is not None
                and not isinstance(observed["cli_version"], str)
            )
        ):
            raise DispatchHalt("receipt host observation identity mismatch")
        sandbox_policy = observed["sandbox_policy"]
        if requested["sandbox"] == "read-only":
            if sandbox_policy != {"type": "read-only"}:
                raise DispatchHalt("receipt read-only sandbox policy mismatch")
        else:
            if (
                not isinstance(sandbox_policy, dict)
                or set(sandbox_policy)
                != {
                    "type",
                    "writable_roots",
                    "network_access",
                    "exclude_slash_tmp",
                    "exclude_tmpdir_env_var",
                }
                or sandbox_policy.get("type") != "workspace-write"
                or sandbox_policy.get("writable_roots") != []
                or sandbox_policy.get("network_access") is not False
                or not isinstance(sandbox_policy.get("exclude_slash_tmp"), bool)
                or not isinstance(sandbox_policy.get("exclude_tmpdir_env_var"), bool)
            ):
                raise DispatchHalt("receipt workspace sandbox policy mismatch")
    elif status == "halt":
        if envelope["result"] is not None:
            raise DispatchHalt("halt receipt result must be null")
        if not isinstance(receipt["error"], str) or not receipt["error"]:
            raise DispatchHalt("halt receipt error must be non-empty")
        if (
            receipt["output_sha256"] is not None
            or receipt["rollout_sha256"] is not None
        ):
            raise DispatchHalt("halt receipt output hashes must be null")
        if receipt["observed"] is not None and not isinstance(
            receipt["observed"], dict
        ):
            raise DispatchHalt("halt receipt observation is invalid")
    else:
        raise DispatchHalt("receipt status is invalid")
    return receipt


def receipt_check(args: argparse.Namespace, home: Path | None = None) -> int:
    del home
    raw = read_receipt_check_input(args)
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DispatchHalt("receipt input must be one UTF-8 JSON envelope") from exc
    receipt = verify_receipt_envelope(envelope)
    print(
        json.dumps(
            {
                "status": "pass",
                "check": "critical_semantics",
                "receipt_id": receipt["receipt_id"],
                "receipt_ref": envelope["receipt_ref"],
                "limitations": [
                    "validate the envelope against the published JSON Schemas separately",
                    "ledger membership and provider-side model identity are not verified",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="codex-tier")
    result.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {PROJECT_VERSION}",
    )
    subparsers = result.add_subparsers(dest="command", required=True)
    probe_parser = subparsers.add_parser(
        "probe", help="inspect local capability without a model call"
    )
    probe_parser.set_defaults(func=probe)

    policy_parser = subparsers.add_parser(
        "policy-check",
        help="validate the active model/effort policy without a model call",
    )
    policy_parser.set_defaults(func=policy_check)

    receipt_parser = subparsers.add_parser(
        "receipt-check",
        help="check critical receipt semantics without a model call",
    )
    receipt_parser.add_argument(
        "--receipt-file",
        help="read one owner-only envelope file; defaults to stdin",
    )
    receipt_parser.set_defaults(func=receipt_check)

    strict_enable_parser = subparsers.add_parser(
        "strict-enable", help="disable native Codex subagent spawning in user config"
    )
    strict_enable_parser.set_defaults(func=strict_enable)

    strict_disable_parser = subparsers.add_parser(
        "strict-disable",
        help="restore the strict setting while preserving unrelated additions",
    )
    strict_disable_parser.set_defaults(func=strict_disable)

    dispatch_parser = subparsers.add_parser(
        "dispatch", help="run one host-observed local route"
    )
    dispatch_parser.add_argument(
        "--route", required=True, choices=sorted((*ROUTES, "surgeon"))
    )
    dispatch_parser.add_argument("--workdir", required=True)
    dispatch_parser.add_argument(
        "--prompt-file", help="read the task from a regular file; defaults to stdin"
    )
    dispatch_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    dispatch_parser.set_defaults(func=dispatch)
    return result


def _validate_interpreter() -> None:
    """Repeat the launcher's interpreter checks as defense in depth."""
    if sys.version_info < (3, 11):
        raise DispatchHalt("Python 3.11 or newer is required")
    resolved = validate_executable_chain(
        Path(sys.executable), label="Python interpreter path"
    )
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise DispatchHalt("Python interpreter is not an executable regular file")


def main() -> int:
    # The sibling shell launcher performs this validation before the first
    # candidate execution. Repeat it here so direct Python invocation also
    # fails closed, while making no claim that a post-start check can protect
    # against an interpreter that was already malicious.
    _validate_interpreter()
    args = parser().parse_args()
    try:
        home = resolve_codex_home()
        if getattr(args, "timeout", 1) <= 0:
            print(json.dumps({"status": "halt", "error": "timeout must be positive"}))
            return 2
        return args.func(args, home)
    except DispatchHalt as exc:
        print(json.dumps({"status": "halt", "error": str(exc)}, sort_keys=True))
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "halt",
                    "error": f"unexpected dispatcher failure: {type(exc).__name__}",
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
