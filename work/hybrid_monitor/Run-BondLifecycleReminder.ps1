param(
    [switch]$DryRun,
    [switch]$Scheduled,
    [string]$AsOf
)

$ErrorActionPreference = 'Stop'
if ($null -ne $PSStyle) {
    $PSStyle.OutputRendering = 'PlainText'
}
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$root = $PSScriptRoot
$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$log = Join-Path $logDir "bond-lifecycle-reminder-$stamp.log"
$arguments = @((Join-Path $root 'lifecycle_reminder.py'))
if ($DryRun) { $arguments += '--dry-run' }
if (-not [string]::IsNullOrWhiteSpace($AsOf)) { $arguments += @('--as-of', $AsOf) }

try {
    $ErrorActionPreference = 'Continue'
    $output = & py @arguments 2>&1
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    $output | Tee-Object -FilePath $log
    if ($exitCode -ne 0) {
        throw "出签/上市提醒退出码：$exitCode"
    }
} catch {
    $ErrorActionPreference = 'Stop'
    $_ | Out-String | Add-Content -LiteralPath $log -Encoding utf8
    throw
}
