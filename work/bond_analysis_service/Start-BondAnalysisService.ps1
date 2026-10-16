param(
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$serviceDir = $PSScriptRoot
$serviceScript = Join-Path $serviceDir 'service.py'
$url = "http://127.0.0.1:$Port/"
$healthUrl = "http://127.0.0.1:$Port/health"

$running = $false
try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
    $running = [bool]$health.ok
} catch {
    $running = $false
}

if (-not $running) {
    $python = (Get-Command py.exe -ErrorAction Stop).Source
    $stdout = Join-Path $serviceDir 'service-stdout.log'
    $stderr = Join-Path $serviceDir 'service-stderr.log'
    Start-Process -FilePath $python `
        -ArgumentList @('-3', $serviceScript, '--serve', '--host', '127.0.0.1', '--port', [string]$Port) `
        -WorkingDirectory $serviceDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr | Out-Null

    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 400
        try {
            $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
            $running = [bool]$health.ok
        } catch {
            $running = $false
        }
    } while (-not $running -and [DateTime]::UtcNow -lt $deadline)
}

if (-not $running) {
    throw "配债分析服务启动失败，请查看 $serviceDir\service-stderr.log"
}

Start-Process $url
