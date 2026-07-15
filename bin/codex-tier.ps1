# Project: codex-tier-routing
# Original project by: 0xTitanas
# Canonical source: https://github.com/0xTitanas/codex-tier-routing
# Copyright: 2026 0xTitanas
# SPDX-License-Identifier: MIT

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Stop-Unsafe([string]$Message) {
    throw "codex-tier: $Message"
}

$script:CurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$script:TrustedOwnerSids = @(
    $script:CurrentSid,
    'S-1-5-18',
    'S-1-5-32-544',
    'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
)
$script:TrustedAclSids = @($script:TrustedOwnerSids) + 'S-1-3-4'
$script:ReplaceRights = [uint32](0x10000000 -bor 0x00080000 -bor 0x00040000 -bor 0x00010000 -bor 0x40)
$script:WriteRights = [uint32]($script:ReplaceRights -bor 0x40000000 -bor 0x02 -bor 0x04 -bor 0x10 -bor 0x100)

function Assert-SafeAcl([string]$Path, [string]$Label, [bool]$Final) {
    try {
        $acl = Get-Acl -LiteralPath $Path
        $descriptor = [Security.AccessControl.RawSecurityDescriptor]::new(
            $acl.GetSecurityDescriptorBinaryForm(), 0
        )
    } catch {
        Stop-Unsafe "unable to inspect the Windows ACL for $Label"
    }
    if ($null -eq $descriptor.Owner -or $script:TrustedOwnerSids -notcontains $descriptor.Owner.Value) {
        Stop-Unsafe "$Label has an untrusted Windows owner"
    }
    if ($null -eq $descriptor.DiscretionaryAcl) {
        Stop-Unsafe "$Label has an unverifiable Windows ACL"
    }
    $dangerous = if ($Final) { $script:WriteRights } else { $script:ReplaceRights }
    foreach ($ace in $descriptor.DiscretionaryAcl) {
        if (([int]$ace.AceFlags -band [int][Security.AccessControl.AceFlags]::InheritOnly) -ne 0) {
            continue
        }
        if (([uint32]$ace.AccessMask -band $dangerous) -eq 0) {
            continue
        }
        if ($ace -isnot [Security.AccessControl.QualifiedAce]) {
            Stop-Unsafe "$Label has an unverifiable Windows ACE"
        }
        if ($ace.AceQualifier -ne [Security.AccessControl.AceQualifier]::AccessAllowed) {
            continue
        }
        if ($null -eq $ace.SecurityIdentifier -or $script:TrustedAclSids -notcontains $ace.SecurityIdentifier.Value) {
            Stop-Unsafe "$Label grants write access outside the trusted Windows identities"
        }
    }
}

function Assert-SafePath([string]$Path, [string]$Label) {
    if ([string]::IsNullOrWhiteSpace($Path) -or
        $Path.StartsWith('\\') -or
        $Path.StartsWith('\\?\') -or
        $Path.StartsWith('\\.\') -or
        $Path -notmatch '^[A-Za-z]:[\\/]') {
        Stop-Unsafe "$Label must use an absolute local drive path"
    }
    $normalized = $Path.Replace('/', '\')
    if ($normalized.Substring(2).Contains(':')) {
        Stop-Unsafe "$Label contains an alternate data stream"
    }
    $parts = @($normalized.Substring(3).Split([char]'\') | Where-Object { $_ -ne '' })
    foreach ($part in $parts) {
        if ($part -eq '.' -or $part -eq '..' -or $part.EndsWith('.') -or $part.EndsWith(' ')) {
            Stop-Unsafe "$Label contains an unsafe Windows path component"
        }
    }
    $current = $normalized.Substring(0, 3)
    $chain = @($current)
    foreach ($part in $parts) {
        $current = Join-Path $current $part
        $chain += $current
    }
    for ($index = 0; $index -lt $chain.Count; $index++) {
        try {
            $item = Get-Item -LiteralPath $chain[$index] -Force
        } catch {
            Stop-Unsafe "$Label is missing: $($chain[$index])"
        }
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            Stop-Unsafe "$Label contains a Windows reparse point"
        }
        if ($index -lt $chain.Count - 1 -and -not $item.PSIsContainer) {
            Stop-Unsafe "$Label contains a non-directory component"
        }
        Assert-SafeAcl $item.FullName "$Label component" ($index -eq $chain.Count - 1)
    }
    return (Get-Item -LiteralPath $normalized -Force).FullName
}

function Get-Python {
    $candidates = @()
    if ($env:CODEX_TIER_PYTHON_BIN) {
        $candidates += $env:CODEX_TIER_PYTHON_BIN
    }
    foreach ($root in @(
        'HKCU:\Software\Python\PythonCore',
        'HKLM:\Software\Python\PythonCore',
        'HKLM:\Software\WOW6432Node\Python\PythonCore'
    )) {
        if (Test-Path -LiteralPath $root) {
            Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue |
                Sort-Object PSChildName -Descending |
                ForEach-Object {
                    $install = Get-ItemProperty -LiteralPath (Join-Path $_.PSPath 'InstallPath') -ErrorAction SilentlyContinue
                    if ($install -and $install.'(default)') {
                        $candidates += Join-Path $install.'(default)' 'python.exe'
                    }
                }
        }
    }
    foreach ($name in @('python.exe', 'python3.exe')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
        if ($command) {
            $candidates += $command.Source
        }
    }
    foreach ($candidate in $candidates | Select-Object -Unique) {
        try {
            $trusted = Assert-SafePath $candidate 'Python executable'
            $item = Get-Item -LiteralPath $trusted -Force
            if ($item.PSIsContainer) {
                throw 'not a file'
            }
            & $item.FullName -I -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'
            if ($LASTEXITCODE -eq 0) {
                return $item.FullName
            }
        } catch {
            if ($env:CODEX_TIER_PYTHON_BIN) {
                throw
            }
        }
    }
    Stop-Unsafe 'Python 3.11 or newer is unavailable'
}

foreach ($name in @(
    'PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'PYTHONINSPECT',
    'PYTHONUSERBASE', 'PYTHONEXECUTABLE', '__PYVENV_LAUNCHER__',
    'LD_PRELOAD', 'LD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES',
    'DYLD_LIBRARY_PATH'
)) {
    Remove-Item "Env:$name" -ErrorAction SilentlyContinue
}

$launcher = Assert-SafePath $PSCommandPath 'dispatcher launcher'
$runtime = Assert-SafePath (Join-Path ([IO.Path]::GetDirectoryName($launcher)) 'codex-tier.py') 'dispatcher runtime'
$python = Get-Python
$env:CODEX_TIER_LAUNCHER_PATH = $launcher
& $python -I $runtime @args
exit $LASTEXITCODE
