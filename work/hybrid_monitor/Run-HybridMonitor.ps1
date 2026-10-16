param(
    [switch]$DryRun,
    [switch]$NoModel,
    [switch]$ReplayReported,
    [switch]$Scheduled,
    [string]$AsOf,
    [string]$WindowStart
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

if ($Scheduled) {
    $chinaTimeZone = [System.TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time')
    $nowBeijing = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $chinaTimeZone)
    $todayCutoff = $nowBeijing.Date.AddHours(8).AddMinutes(30)
    $todayCutoffOffset = [DateTimeOffset]::new($todayCutoff, [TimeSpan]::FromHours(8))

    if ($nowBeijing -lt $todayCutoff) {
        Write-Output '计划运行跳过：北京时间尚未到当日 08:30。'
        exit 0
    }

    $statePath = Join-Path $root 'state.json'
    if (Test-Path -LiteralPath $statePath) {
        $state = Get-Content -LiteralPath $statePath -Raw -Encoding utf8 | ConvertFrom-Json
        $lastSuccessfulText = [string]$state.lastSuccessfulCutoff
        if (-not [string]::IsNullOrWhiteSpace($lastSuccessfulText)) {
            try {
                $lastSuccessfulCutoff = [DateTimeOffset]::Parse(
                    $lastSuccessfulText,
                    [System.Globalization.CultureInfo]::InvariantCulture
                )
                if ($lastSuccessfulCutoff -ge $todayCutoffOffset) {
                    Write-Output "计划运行跳过：今日 08:30 截止点已成功处理（$lastSuccessfulText）。"
                    exit 0
                }
            } catch [System.FormatException] {
                Write-Warning "state.json 的 lastSuccessfulCutoff 无法解析，继续执行监控：$lastSuccessfulText"
            }
        }
    }
}

$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$log = Join-Path $logDir "run-$stamp.log"
$arguments = @((Join-Path $root 'monitor.py'))
if ($DryRun) { $arguments += '--dry-run' }
if ($NoModel) { $arguments += '--no-model' }
if ($ReplayReported) { $arguments += '--replay-reported' }
if (-not [string]::IsNullOrWhiteSpace($AsOf)) { $arguments += @('--as-of', $AsOf) }
if (-not [string]::IsNullOrWhiteSpace($WindowStart)) { $arguments += @('--window-start', $WindowStart) }

try {
    # Windows PowerShell 5.1 turns native stderr into non-terminating ErrorRecord
    # objects. Temporarily continue so the complete Python traceback reaches the
    # log instead of stopping after its first line.
    $ErrorActionPreference = 'Continue'
    $output = & py @arguments 2>&1
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    $output | Tee-Object -FilePath $log
    if ($exitCode -ne 0) {
        throw "混合监控退出码：$exitCode"
    }
} catch {
    $ErrorActionPreference = 'Stop'
    $_ | Out-String | Add-Content -LiteralPath $log -Encoding utf8
    throw
}
