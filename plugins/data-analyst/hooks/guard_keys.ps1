# Runs guard_keys.py (the data-analyst key guard) with the first Python that works: python3, python, py.
# Windows PowerShell wrapper for Codex (commandWindows). A missing interpreter allows the call.
$ProgressPreference = 'SilentlyContinue'
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
$in = [Console]::In.ReadToEnd()
$py = Join-Path $PSScriptRoot 'guard_keys.py'
foreach ($c in 'python3', 'python', 'py') {
    if (-not (Get-Command $c -CommandType Application -ErrorAction SilentlyContinue)) { continue }
    $out = $in | & $c -I $py --stdout-reason 2>$null
    $code = $LASTEXITCODE
    if ($code -eq 2) { [Console]::Error.WriteLine(($out -join "`n")); exit 2 }
    if ($code -eq 0) { exit 0 }
}
exit 0
