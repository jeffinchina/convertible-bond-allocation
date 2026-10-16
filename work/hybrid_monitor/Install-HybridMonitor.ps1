param(
    [switch]$SkipCliInstall,
    [switch]$SkipScheduledTask
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$runner = (Resolve-Path -LiteralPath (Join-Path $root 'Run-HybridMonitor.ps1')).Path
$runtime = Join-Path $root 'runtime'

if (-not $SkipCliInstall) {
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    & npm.cmd install --prefix $runtime '@openai/codex'
    if ($LASTEXITCODE -ne 0) { throw 'Codex CLI 安装失败。' }
}

if (-not $SkipScheduledTask) {
    $taskName = '可转债配债-混合监控'
    $reminderTaskName = '可转债配债-申购提醒'
    $powerShell = (Get-Command pwsh.exe).Source
    $arguments = "-NoProfile -File `"$runner`" -Scheduled"
    $reminderRunner = (Resolve-Path -LiteralPath (Join-Path $root 'Run-SubscriptionReminder.ps1')).Path
    $reminderArguments = "-NoProfile -File `"$reminderRunner`" -Scheduled"
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute $powerShell -Argument $arguments -WorkingDirectory $root
    $reminderAction = New-ScheduledTaskAction -Execute $powerShell -Argument $reminderArguments -WorkingDirectory $root
    $weeklyTrigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '08:30'
    # InteractiveToken tasks cannot start while the user is logged out. The
    # logon trigger gives the stateful monitor a chance to safely catch up;
    # state.json and event keys prevent duplicate reports and event emails.
    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($weeklyTrigger, $logonTrigger) -Settings $settings -Principal $principal -Description '交易日零模型检索三层官方公告；无新增直发邮件；仅事件日调用Codex深度分析。' -Force | Out-Null
    Write-Output "已创建计划任务：$taskName（工作日 08:30 + $currentUser 登录时补跑）"

    $reminder0850 = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '08:50'
    $reminder0950 = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '09:50'
    $reminderLogon = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    $reminderSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName $reminderTaskName -Action $reminderAction -Trigger @($reminder0850, $reminder0950, $reminderLogon) -Settings $reminderSettings -Principal $principal -Description '正式发行公告确认申购日后，于申购日08:50和09:50发送两次新债申购提醒；登录时补发当日未完成批次。' -Force | Out-Null
    Write-Output "已创建计划任务：$reminderTaskName（工作日 08:50、09:50 + $currentUser 登录时补跑）"
}

Write-Output '安装完成。建议先执行一次：Run-HybridMonitor.ps1 -DryRun'
