param(
    [switch]$DryRun,
    [switch]$Strict,
    [string]$CodexHome
)

# Project: codex-tier-routing
# Original project by: 0xTitanas
# Canonical source: https://github.com/0xTitanas/codex-tier-routing
# Copyright: 2026 0xTitanas
# SPDX-License-Identifier: MIT

# Tests replace this literal only in a temporary copied installer.
$INSTALL_TEST_BUILD = $false
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Fail([string]$Message) {
    throw "install.ps1: $Message"
}

function Get-UserPathState {
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $false)
    if ($null -eq $key) {
        return [pscustomobject]@{
            Exists = $false
            Value = ''
            Kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
        }
    }
    try {
        $exists = @($key.GetValueNames()) -contains 'Path'
        if (-not $exists) {
            return [pscustomobject]@{
                Exists = $false
                Value = ''
                Kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
            }
        }
        $kind = $key.GetValueKind('Path')
        if ($kind -notin @(
            [Microsoft.Win32.RegistryValueKind]::String,
            [Microsoft.Win32.RegistryValueKind]::ExpandString
        )) {
            Fail 'the user Path registry value is not a string'
        }
        return [pscustomobject]@{
            Exists = $true
            Value = [string]$key.GetValue(
                'Path',
                '',
                [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
            )
            Kind = $kind
        }
    } finally {
        $key.Dispose()
    }
}

function Set-UserPathState(
    [string]$Value,
    [Microsoft.Win32.RegistryValueKind]$Kind,
    [bool]$Exists
) {
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    if ($null -eq $key) {
        Fail 'unable to open the user environment registry key'
    }
    try {
        if ($Exists) {
            $key.SetValue('Path', $Value, $Kind)
        } else {
            $key.DeleteValue('Path', $false)
        }
    } finally {
        $key.Dispose()
    }
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
$script:CreatedDirectories = New-Object Collections.Generic.List[string]

function Invoke-TestFailure([string]$Point) {
    if ($INSTALL_TEST_BUILD -and $env:CODEX_TIER_INSTALL_TEST_FAIL_POINT -eq $Point) {
        Fail "injected installer failure at $Point"
    }
}

function Assert-SafeAcl([string]$Path, [string]$Label, [bool]$Final) {
    try {
        $acl = Get-Acl -LiteralPath $Path
        $sections = [Security.AccessControl.AccessControlSections]::Owner -bor [Security.AccessControl.AccessControlSections]::Access
        $descriptor = New-Object Security.AccessControl.RawSecurityDescriptor($acl.GetSecurityDescriptorSddlForm($sections))
    } catch {
        Fail "unable to inspect the Windows ACL for $Label"
    }
    if ($null -eq $descriptor.Owner -or $script:TrustedOwnerSids -notcontains $descriptor.Owner.Value) {
        Fail "$Label has an untrusted Windows owner"
    }
    if ($null -eq $descriptor.DiscretionaryAcl) {
        Fail "$Label has an unverifiable Windows ACL"
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
            Fail "$Label has an unverifiable Windows ACE"
        }
        if ($ace.AceQualifier -ne [Security.AccessControl.AceQualifier]::AccessAllowed) {
            continue
        }
        if ($null -eq $ace.SecurityIdentifier -or $script:TrustedAclSids -notcontains $ace.SecurityIdentifier.Value) {
            Fail "$Label grants write access outside the trusted Windows identities"
        }
    }
}

function Get-PathChain([string]$Path, [string]$Label) {
    if ([string]::IsNullOrWhiteSpace($Path) -or
        $Path.StartsWith('\\') -or
        $Path.StartsWith('\\?\') -or
        $Path.StartsWith('\\.\') -or
        $Path -notmatch '^[A-Za-z]:[\\/]') {
        Fail "$Label must use an absolute local drive path"
    }
    $normalized = $Path.Replace('/', '\')
    if ($normalized.Substring(2).Contains(':')) {
        Fail "$Label contains an alternate data stream"
    }
    $parts = @($normalized.Substring(3).Split([char]'\') | Where-Object { $_ -ne '' })
    foreach ($part in $parts) {
        if ($part -eq '.' -or $part -eq '..' -or $part.EndsWith('.') -or $part.EndsWith(' ')) {
            Fail "$Label contains an unsafe Windows path component"
        }
    }
    $current = $normalized.Substring(0, 3)
    $chain = @($current)
    foreach ($part in $parts) {
        $current = Join-Path $current $part
        $chain += $current
    }
    return $chain
}

function Assert-SafePath(
    [string]$Path,
    [string]$Label,
    [switch]$AllowMissing,
    [switch]$RequireDirectory,
    [switch]$RequireFile
) {
    $chain = @(Get-PathChain $Path $Label)
    $missing = $false
    for ($index = 0; $index -lt $chain.Count; $index++) {
        try {
            $item = Get-Item -LiteralPath $chain[$index] -Force
        } catch {
            if (-not $AllowMissing) {
                Fail "$Label is missing: $($chain[$index])"
            }
            $missing = $true
            continue
        }
        if ($missing) {
            Fail "$Label has an object beneath a missing ancestor"
        }
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            Fail "$Label contains a Windows reparse point"
        }
        $final = $index -eq $chain.Count - 1
        if (-not $final -and -not $item.PSIsContainer) {
            Fail "$Label contains a non-directory component"
        }
        if ($final -and $RequireDirectory -and -not $item.PSIsContainer) {
            Fail "$Label is not a directory"
        }
        if ($final -and $RequireFile -and $item.PSIsContainer) {
            Fail "$Label is not a file"
        }
        Assert-SafeAcl $item.FullName "$Label component" $final
    }
    return $chain[-1]
}

function Set-PrivateAcl([string]$Path, [bool]$Directory) {
    $owner = New-Object Security.Principal.SecurityIdentifier($script:CurrentSid)
    if ($Directory) {
        $security = New-Object Security.AccessControl.DirectorySecurity
        $security.SetOwner($owner)
        $security.SetAccessRuleProtection($true, $false)
        $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
        foreach ($sidValue in @($script:CurrentSid, 'S-1-5-18', 'S-1-5-32-544')) {
            $sid = New-Object Security.Principal.SecurityIdentifier($sidValue)
            $rule = New-Object Security.AccessControl.FileSystemAccessRule(
                $sid,
                [Security.AccessControl.FileSystemRights]::FullControl,
                $inheritance,
                [Security.AccessControl.PropagationFlags]::None,
                [Security.AccessControl.AccessControlType]::Allow
            )
            [void]$security.AddAccessRule($rule)
        }
        [IO.Directory]::SetAccessControl($Path, $security)
    } else {
        $security = New-Object Security.AccessControl.FileSecurity
        $security.SetOwner($owner)
        $security.SetAccessRuleProtection($true, $false)
        foreach ($sidValue in @($script:CurrentSid, 'S-1-5-18', 'S-1-5-32-544')) {
            $sid = New-Object Security.Principal.SecurityIdentifier($sidValue)
            $rule = New-Object Security.AccessControl.FileSystemAccessRule(
                $sid,
                [Security.AccessControl.FileSystemRights]::FullControl,
                [Security.AccessControl.AccessControlType]::Allow
            )
            [void]$security.AddAccessRule($rule)
        }
        [IO.File]::SetAccessControl($Path, $security)
    }
    Assert-SafeAcl $Path 'private installation object' $true
}

function New-PrivateDirectoryChain([string]$Path) {
    $chain = @(Get-PathChain $Path 'private directory')
    for ($index = 0; $index -lt $chain.Count; $index++) {
        $candidate = $chain[$index]
        if (Test-Path -LiteralPath $candidate) {
            $item = Get-Item -LiteralPath $candidate -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or -not $item.PSIsContainer) {
                Fail "private directory path is unsafe: $candidate"
            }
            Assert-SafeAcl $item.FullName 'private directory component' ($index -eq $chain.Count - 1)
            continue
        }
        [IO.Directory]::CreateDirectory($candidate) | Out-Null
        Set-PrivateAcl $candidate $true
        $script:CreatedDirectories.Add($candidate)
    }
    Assert-SafePath $Path 'private directory' -RequireDirectory | Out-Null
}

function Get-Sha256([string]$Path) {
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        } finally {
            $sha.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

function Assert-SafeTree([string]$Path, [string]$Label) {
    $root = Assert-SafePath $Path $Label
    $stack = New-Object Collections.Generic.Stack[string]
    $stack.Push($root)
    while ($stack.Count -gt 0) {
        $current = $stack.Pop()
        $item = Get-Item -LiteralPath $current -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            Fail "$Label contains a Windows reparse point"
        }
        Assert-SafeAcl $item.FullName $Label $true
        if ($item.PSIsContainer) {
            foreach ($child in Get-ChildItem -LiteralPath $item.FullName -Force) {
                if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    Fail "$Label contains a Windows reparse point"
                }
                if (-not $child.PSIsContainer -and ($child.Attributes -band [IO.FileAttributes]::Directory) -ne 0) {
                    Fail "$Label contains an unsupported object"
                }
                $stack.Push($child.FullName)
            }
        }
    }
}

function Copy-Private([string]$Source, [string]$Destination) {
    $sourceItem = Get-Item -LiteralPath $Source -Force
    if ($sourceItem.PSIsContainer) {
        New-PrivateDirectoryChain $Destination
        foreach ($child in Get-ChildItem -LiteralPath $Source -Force) {
            Copy-Private $child.FullName (Join-Path $Destination $child.Name)
        }
        return
    }
    [IO.File]::Copy($Source, $Destination, $false)
    Set-PrivateAcl $Destination $false
    if ((Get-Sha256 $Source) -ne (Get-Sha256 $Destination)) {
        Fail "staged file verification failed: $Source"
    }
}

function Move-SafePath([string]$Source, [string]$Destination) {
    $item = Get-Item -LiteralPath $Source -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        Fail "refusing to move a reparse point: $Source"
    }
    if ($item.PSIsContainer) {
        [IO.Directory]::Move($Source, $Destination)
    } else {
        [IO.File]::Move($Source, $Destination)
    }
}

function Remove-SafeTree([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        Fail "refusing to remove a reparse point: $Path"
    }
    if ($item.PSIsContainer) {
        foreach ($child in Get-ChildItem -LiteralPath $item.FullName -Force) {
            Remove-SafeTree $child.FullName
        }
        [IO.Directory]::Delete($item.FullName, $false)
    } else {
        [IO.File]::Delete($item.FullName)
    }
}

function Remove-CreatedEmptyDirectories {
    for ($index = $script:CreatedDirectories.Count - 1; $index -ge 0; $index--) {
        $path = $script:CreatedDirectories[$index]
        if (Test-Path -LiteralPath $path) {
            $item = Get-Item -LiteralPath $path -Force
            if ($item.PSIsContainer -and
                ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0 -and
                -not (Get-ChildItem -LiteralPath $path -Force | Select-Object -First 1)) {
                [IO.Directory]::Delete($path, $false)
            }
        }
    }
}

if (-not $PSBoundParameters.ContainsKey('CodexHome')) {
    if (Test-Path Env:CODEX_HOME) {
        $CodexHome = $env:CODEX_HOME
    } else {
        $CodexHome = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex'
    }
}
$CodexHome = Assert-SafePath $CodexHome 'Codex home' -AllowMissing
$root = Assert-SafePath $PSScriptRoot 'installer source root' -RequireDirectory
$agentNames = @('architect.toml', 'builder.toml', 'grunt.toml', 'reviewer.toml', 'scout.toml', 'surgeon.toml')
$entries = @()
foreach ($agent in $agentNames) {
    $entries += [pscustomobject]@{
        Source = Join-Path $root "agents\$agent"
        Destination = Join-Path $CodexHome "agents\$agent"
        Relative = "agents\$agent"
        Stage = $null
        Applied = $false
        HadOriginal = $false
    }
}
foreach ($artifact in @(
    @('skills\model-routing', 'skills\model-routing'),
    @('bin\codex-tier.cmd', 'bin\codex-tier.cmd'),
    @('bin\codex-tier-launcher.ps1', 'bin\codex-tier-launcher.ps1'),
    @('bin\codex-tier.py', 'bin\codex-tier.py')
)) {
    $entries += [pscustomobject]@{
        Source = Join-Path $root $artifact[0]
        Destination = Join-Path $CodexHome $artifact[1]
        Relative = $artifact[1]
        Stage = $null
        Applied = $false
        HadOriginal = $false
    }
}
foreach ($entry in $entries) {
    Assert-SafeTree $entry.Source 'installer source'
}

if ($DryRun) {
    Write-Output 'Dry run enabled: no filesystem changes will be made.'
    foreach ($entry in $entries) {
        Write-Output "Would install: $($entry.Source) -> $($entry.Destination)"
    }
    if ($Strict) {
        Write-Output 'Would enable strict routing'
    }
    exit 0
}

$oldUserPathState = Get-UserPathState
$oldUserPath = $oldUserPathState.Value
$pathChanged = $false
$transactionRoot = $null
$snapshotRoot = $null
$backupLeaf = $null
$strictCleanupNeeded = $false
$tierRoutingExisted = Test-Path -LiteralPath (Join-Path $CodexHome 'tier-routing')
$previousCodexHome = $env:CODEX_HOME
$hadProcessCodexHome = Test-Path Env:CODEX_HOME
$committed = $false

try {
    New-PrivateDirectoryChain $CodexHome
    $transactionRoot = Join-Path $CodexHome ('.codex-tier-routing-transaction.' + [guid]::NewGuid().ToString('N'))
    New-PrivateDirectoryChain $transactionRoot
    $stageRoot = Join-Path $transactionRoot 'stage'
    $snapshotRoot = Join-Path $transactionRoot 'snapshot'
    New-PrivateDirectoryChain $stageRoot
    New-PrivateDirectoryChain $snapshotRoot

    for ($index = 0; $index -lt $entries.Count; $index++) {
        $entry = $entries[$index]
        $entry.Stage = Join-Path $stageRoot ([string]$index)
        Copy-Private $entry.Source $entry.Stage
        Assert-SafeTree $entry.Stage 'staged installation artifact'

        $parent = [IO.Path]::GetDirectoryName($entry.Destination)
        New-PrivateDirectoryChain $parent
        if (Test-Path -LiteralPath $entry.Destination) {
            Assert-SafeTree $entry.Destination 'replacement destination'
            $entry.HadOriginal = $true
        }
    }

    for ($index = 0; $index -lt $entries.Count; $index++) {
        $entry = $entries[$index]
        $entry.Applied = $true
        if ($entry.HadOriginal) {
            $original = Join-Path $snapshotRoot $entry.Relative
            New-PrivateDirectoryChain ([IO.Path]::GetDirectoryName($original))
            Move-SafePath $entry.Destination $original
        }
        Invoke-TestFailure "after-original-$($index + 1)"
        Move-SafePath $entry.Stage $entry.Destination
        Assert-SafeTree $entry.Destination 'installed artifact'
        Invoke-TestFailure "after-replacement-$($index + 1)"
    }

    $env:CODEX_HOME = $CodexHome
    $launcher = Join-Path $CodexHome 'bin\codex-tier-launcher.ps1'
    $versionOutput = & $launcher --version 2>&1
    if ($LASTEXITCODE -ne 0 -or @($versionOutput).Count -ne 1 -or $versionOutput -notmatch '^codex-tier 1\.1\.0-rc\.1$') {
        Fail 'installed launcher verification failed'
    }

    if ($Strict) {
        $manifest = Join-Path $CodexHome 'tier-routing\strict-config.json'
        $manifestExisted = Test-Path -LiteralPath $manifest
        $strictOutput = & $launcher strict-enable 2>&1
        $strictExit = $LASTEXITCODE
        if (-not $manifestExisted -and (Test-Path -LiteralPath $manifest)) {
            $strictCleanupNeeded = $true
        }
        if ($strictExit -ne 0) {
            Fail "strict routing could not be enabled: $($strictOutput -join ' ')"
        }
        Write-Output $strictOutput
        Invoke-TestFailure 'strict-enable-succeeded'
    }

    $bin = Join-Path $CodexHome 'bin'
    $pathParts = @($oldUserPath -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $alreadyOnPath = $false
    foreach ($part in $pathParts) {
        if ([string]::Equals($part.TrimEnd('\'), $bin.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
            $alreadyOnPath = $true
            break
        }
    }
    if (-not $alreadyOnPath) {
        $pathChanged = $true
        Set-UserPathState (($pathParts + $bin) -join ';') $oldUserPathState.Kind $true
    }

    $hasOriginals = [bool]($entries | Where-Object { $_.HadOriginal } | Select-Object -First 1)
    if ($hasOriginals) {
        $backupParent = Join-Path $CodexHome '.codex-tier-routing-backups'
        New-PrivateDirectoryChain $backupParent
        $backupLeaf = Join-Path $backupParent ((Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmss') + '-' + [guid]::NewGuid().ToString('N'))
        New-PrivateDirectoryChain $backupLeaf
        $promotedSnapshot = Join-Path $backupLeaf 'snapshot'
        [IO.Directory]::Move($snapshotRoot, $promotedSnapshot)
        $snapshotRoot = $promotedSnapshot
        Invoke-TestFailure 'backup-promotion'
    }

    $committed = $true
    $strictCleanupNeeded = $false
    if (Test-Path -LiteralPath $transactionRoot) {
        Remove-SafeTree $transactionRoot
    }
    Write-Output "Installed codex-tier into $CodexHome"
    if ($backupLeaf) {
        Write-Output "Preserved replaced artifacts in $backupLeaf"
    }
} catch {
    $failure = $_
    $rollbackFailed = $false
    if ($strictCleanupNeeded) {
        try {
            $strictLauncher = Join-Path $CodexHome 'bin\codex-tier-launcher.ps1'
            & $strictLauncher strict-disable | Out-Null
            if ($LASTEXITCODE -ne 0) {
                $rollbackFailed = $true
            }
        } catch {
            $rollbackFailed = $true
        }
    }
    for ($index = $entries.Count - 1; $index -ge 0; $index--) {
        $entry = $entries[$index]
        if (-not $entry.Applied) {
            continue
        }
        try {
            if (Test-Path -LiteralPath $entry.Destination) {
                Remove-SafeTree $entry.Destination
            }
            if ($entry.HadOriginal) {
                $original = Join-Path $snapshotRoot $entry.Relative
                if (-not (Test-Path -LiteralPath $original)) {
                    Fail "transaction backup is missing: $original"
                }
                New-PrivateDirectoryChain ([IO.Path]::GetDirectoryName($entry.Destination))
                Move-SafePath $original $entry.Destination
            }
        } catch {
            $rollbackFailed = $true
        }
    }
    if ($pathChanged) {
        try {
            Set-UserPathState $oldUserPath $oldUserPathState.Kind $oldUserPathState.Exists
        } catch {
            $rollbackFailed = $true
        }
    }
    if (-not $rollbackFailed) {
        try {
            if ($backupLeaf -and (Test-Path -LiteralPath $backupLeaf)) {
                Remove-SafeTree $backupLeaf
            }
            if ($transactionRoot -and (Test-Path -LiteralPath $transactionRoot)) {
                Remove-SafeTree $transactionRoot
            }
            if (-not $tierRoutingExisted) {
                $tierRouting = Join-Path $CodexHome 'tier-routing'
                if ((Test-Path -LiteralPath $tierRouting) -and
                    -not (Get-ChildItem -LiteralPath $tierRouting -Force | Select-Object -First 1)) {
                    [IO.Directory]::Delete($tierRouting, $false)
                }
            }
            Remove-CreatedEmptyDirectories
        } catch {
            $rollbackFailed = $true
        }
    }
    if ($rollbackFailed) {
        throw "install.ps1: rollback incomplete; recovery state was preserved. Original failure: $failure"
    }
    throw $failure
} finally {
    if ($hadProcessCodexHome) {
        $env:CODEX_HOME = $previousCodexHome
    } else {
        Remove-Item Env:CODEX_HOME -ErrorAction SilentlyContinue
    }
}

if (-not $committed) {
    exit 1
}
