<#
.SYNOPSIS
Store an API key for one data-analyst source in Claude Code user settings (Windows PowerShell).

.DESCRIPTION
- Prompts for the key with hidden input. The key is never echoed, logged, or put on a command line.
- Checks the key with the source's API (one small request) unless -NoVerify.
- Writes "env": {"<KEY_VARIABLE>": "..."} into %USERPROFILE%\.claude\settings.json (or
  $env:CLAUDE_CONFIG_DIR\settings.json), keeping every other setting; the previous file is saved as .bak.
- The skill reads settings.json directly, so the key works at once.
Not for claude.ai: the web sandbox has no settings file; use the manual-download route there.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -Source fred
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [switch]$NoVerify,
    [string]$Settings
)
$ErrorActionPreference = 'Stop'
function Fail([string]$Message) { [Console]::Error.WriteLine("ERROR: $Message"); exit 1 }

if (-not $Settings) {
    $cfg = $env:CLAUDE_CONFIG_DIR
    if (-not $cfg) { $cfg = Join-Path $HOME '.claude' }
    $Settings = Join-Path $cfg 'settings.json'
}
function Find-Python {
    foreach ($name in 'python', 'python3', 'py') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $saved = $ErrorActionPreference; $ErrorActionPreference = 'SilentlyContinue'
        & $cmd.Source -c 'import json, sys' *> $null
        $ok = ($LASTEXITCODE -eq 0); $ErrorActionPreference = $saved
        if ($ok) { return $cmd.Source }
    }
    return $null
}
$py = Find-Python
if (-not $py) { Fail 'Python 3 is required. Install it from https://www.python.org/downloads/ and run this again.' }
$helper = Join-Path $PSScriptRoot 'scripts\setup_key.py'

$infoJson = & $py $helper info $Source
if ($LASTEXITCODE -ne 0) { Fail "Source '$Source' does not use an API key (or is unknown). Nothing to set up." }
$info = $infoJson | ConvertFrom-Json
Write-Host "$($info.title): the key goes in $($info.env). Get a free key at $($info.signup_url)"

if ([Console]::IsInputRedirected) {
    $plain = [Console]::In.ReadLine()
} else {
    $secure = Read-Host -Prompt "Enter your $($info.env) (input is hidden)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
if ($null -eq $plain) { $plain = '' }
$plain = $plain -replace '\s', ''
if (-not $plain) { Fail 'No key entered. Nothing was changed.' }
$env:DATA_ANALYST_SETUP_KEY = $plain
Remove-Variable plain, secure -ErrorAction SilentlyContinue
# Paired values that are not secret (e.g. AQS_EMAIL for EPA AQS)
$extra = @{}
foreach ($p in $info.extra.PSObject.Properties) {
    $v = Read-Host -Prompt "Enter $($p.Value) (sent with the key as '$($p.Name)')"
    if ($v) { $extra[$p.Value] = $v.Trim() }
}
$env:DATA_ANALYST_SETUP_EXTRA = ($extra | ConvertTo-Json -Compress)

try {
    if (-not $NoVerify) {
        Write-Host -NoNewline "Checking the key with $($info.title)... "
        & $py $helper verify $Source | Out-Host
        $rc = $LASTEXITCODE
        switch ($rc) {
            0 { Write-Host 'OK.' }
            4 { Write-Host 'rejected.' }
            5 { Write-Host 'could not reach the API (network error).' }
            default { Write-Host 'the check was not conclusive; the key may still be valid.' }
        }
        if ($rc -ne 0) {
            $answer = 'n'
            if (-not [Console]::IsInputRedirected) { $answer = Read-Host -Prompt 'Save the key anyway? [y/N]' }
            if ($answer -notmatch '^(y|yes)$') { Fail 'Key not saved. Nothing was changed.' }
        }
    }
    & $py $helper save $Source $Settings | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail 'Could not update the settings file.' }
} finally {
    Remove-Item Env:\DATA_ANALYST_SETUP_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:\DATA_ANALYST_SETUP_EXTRA -ErrorAction SilentlyContinue
}
Write-Host 'Done. Ask Claude to run the request again; the skill reads the key from settings.json at once.'
