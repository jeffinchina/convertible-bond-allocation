param(
    [Parameter(Mandatory = $true)]
    [string]$EvidencePath,
    [switch]$NoSend
)

$ErrorActionPreference = 'Stop'
if ($null -ne $PSStyle) {
    $PSStyle.OutputRendering = 'PlainText'
}
$root = $PSScriptRoot
$workspace = (Resolve-Path -LiteralPath (Join-Path $root '..\..')).Path
$evidence = (Resolve-Path -LiteralPath $EvidencePath).Path
$configPath = (Resolve-Path -LiteralPath (Join-Path $root 'config.json')).Path
$promptTemplate = (Resolve-Path -LiteralPath (Join-Path $root 'event-analysis-prompt.md')).Path
$runtimeCli = Join-Path $root 'runtime\node_modules\@openai\codex\bin\codex.js'
$outputSchema = (Resolve-Path -LiteralPath (Join-Path $root 'analysis-result-schema.json')).Path
$resultPath = Join-Path (Split-Path -Parent $evidence) 'analysis-result.json'
$sendScript = (Resolve-Path -LiteralPath (Join-Path $root '..\Send-QQMail.ps1')).Path
$config = Get-Content -LiteralPath $configPath -Raw -Encoding utf8 | ConvertFrom-Json
$analysisConfig = $config.eventAnalysis
$modelCandidates = @(
    $analysisConfig.modelCandidates |
        ForEach-Object { ([string]$_).Trim() } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Select-Object -Unique
)
if ($modelCandidates.Count -eq 0) {
    $modelCandidates = @('gpt-6-astra', 'gpt-5.6-sol')
}
$reasoningEffort = ([string]$analysisConfig.reasoningEffort).Trim()
if ([string]::IsNullOrWhiteSpace($reasoningEffort)) {
    $reasoningEffort = 'high'
}

$entry = Get-Content -LiteralPath $evidence -Raw -Encoding utf8 | ConvertFrom-Json
$evidenceFiles = if ($null -ne $entry.evidencePaths) { @($entry.evidencePaths) } else { @($evidence) }
$inventoryHtml = if ($null -ne $entry.inventoryHtml) { [string]$entry.inventoryHtml } else { '' }
$calculationHtmlByCode = @{}
$companyProfileByCode = @{}

function Add-CompanyProfileFirstRow {
    param(
        [Parameter(Mandatory = $true)][string]$Html,
        [Parameter(Mandatory = $true)]$Profile
    )
    if ($Html -match 'data-generated=["'']company-profile["'']') {
        return $Html
    }
    $headingIndex = $Html.IndexOf('关键数据与计算', [System.StringComparison]::Ordinal)
    if ($headingIndex -lt 0) {
        throw '正式发行分析邮件缺少“关键数据与计算”章节，无法注入行业和主营业务。'
    }
    $tableIndex = $Html.IndexOf('<table', $headingIndex, [System.StringComparison]::OrdinalIgnoreCase)
    if ($tableIndex -lt 0) {
        throw '正式发行分析邮件的“关键数据与计算”章节缺少表格。'
    }
    $headerEnd = $Html.IndexOf('</thead>', $tableIndex, [System.StringComparison]::OrdinalIgnoreCase)
    if ($headerEnd -ge 0) {
        $insertAt = $headerEnd + '</thead>'.Length
    } else {
        $headerEnd = $Html.IndexOf('</tr>', $tableIndex, [System.StringComparison]::OrdinalIgnoreCase)
        if ($headerEnd -lt 0) {
            throw '正式发行分析邮件的关键数据表缺少表头行。'
        }
        $insertAt = $headerEnd + '</tr>'.Length
    }
    $industry = [System.Net.WebUtility]::HtmlEncode([string]$Profile.industry)
    $mainBusiness = [System.Net.WebUtility]::HtmlEncode([string]$Profile.mainBusiness)
    $reportDate = [System.Net.WebUtility]::HtmlEncode([string]$Profile.mainBusinessReportDate)
    $row = "<tr data-generated=`"company-profile`"><td>行业｜主营业务</td><td>$industry<br>$mainBusiness</td><td>$reportDate 最新披露主营构成；每项不超过10字</td></tr>"
    return $Html.Insert($insertAt, $row)
}

foreach ($evidenceFile in $evidenceFiles) {
    $pack = Get-Content -LiteralPath $evidenceFile -Raw -Encoding utf8 | ConvertFrom-Json
    $code = [string]$pack.candidate.code
    $profile = $pack.companyProfile
    if ($null -eq $profile -or $profile.status -ne 'complete') {
        $missingProfile = if ($null -ne $profile) { @($profile.missingFields) -join ', ' } else { 'companyProfile' }
        throw "事件行业和主营业务数据不完整，停止生成分析邮件：$code；$missingProfile"
    }
    if ([string]::IsNullOrWhiteSpace([string]$profile.industry) -or [string]::IsNullOrWhiteSpace([string]$profile.mainBusiness)) {
        throw "事件行业和主营业务存在空值，停止生成分析邮件：$code"
    }
    if (([string]$profile.industry).Length -gt 10 -or ([string]$profile.mainBusiness).Length -gt 10) {
        throw "事件行业或主营业务超过10个字符，停止生成分析邮件：$code"
    }
    $companyProfileByCode[$code] = $profile
    if ($null -eq $pack.calculation -or $pack.calculation.status -ne 'complete') {
        $missing = if ($null -ne $pack.calculation) { @($pack.calculation.missingFields) -join ', ' } else { 'calculation' }
        throw "事件关键计算数据不完整，停止生成分析邮件：$code；$missing"
    }
    $calculationHtmlByCode[$code] = [string]$pack.calculation.html
}

$globalCli = Get-Command codex.exe -CommandType Application -ErrorAction SilentlyContinue
if ($null -ne $globalCli) {
    $codexCommand = $globalCli.Source
    $codexPrefixArguments = @()
    $codexLauncher = 'global'
} elseif (Test-Path -LiteralPath $runtimeCli) {
    $nodeCommand = Get-Command node -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $nodeCommand) {
        throw '未找到全局 Codex CLI，且项目内置 Codex CLI 所需的 Node.js 不可用。'
    }
    $codexCommand = $nodeCommand.Source
    $codexPrefixArguments = @($runtimeCli)
    $codexLauncher = 'bundled'
} else {
    throw '未找到可用的 Codex CLI。请先运行 Install-HybridMonitor.ps1。'
}

$instructions = Get-Content -LiteralPath $promptTemplate -Raw
$prompt = @"
$instructions

本次事件清单或唯一证据包：$evidence
请现在完成分析并按要求只输出 JSON。
"@

function Test-ModelCompatibilityFailure {
    param([string]$OutputText)
    # Metadata warnings can accompany unrelated network/quota failures. Only
    # explicit error lines are allowed to trigger a different model request.
    $errorLines = @($OutputText -split '\r?\n' | Where-Object {
        $_ -match '(?i)\bERROR\b'
    }) -join "`n"
    return $errorLines -match '(?i)(model_not_found|model[^\r\n]{0,120}(?:not supported|not available)|not supported when using Codex|requires a newer version of Codex)'
}
$selectedModel = $null
$attemptSummaries = @()
$env:NO_COLOR = '1'

foreach ($model in $modelCandidates) {
    if (Test-Path -LiteralPath $resultPath) {
        Remove-Item -LiteralPath $resultPath -Force
    }
    $codexArguments = @(
        $codexPrefixArguments
        '--model', $model
        '-c', "model_reasoning_effort=`"$reasoningEffort`""
        '--cd', $workspace
        '--sandbox', 'workspace-write'
        '--ask-for-approval', 'never'
        '--search'
        'exec'
        '--skip-git-repo-check'
        '--ephemeral'
        '--output-schema', $outputSchema
        '--output-last-message', $resultPath
        '-'
    )
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $attemptOutput = @($prompt | & $codexCommand @codexArguments 2>&1)
        $attemptExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $attemptText = ($attemptOutput | Out-String).Trim()
    $attemptSummaries += "$model=$attemptExitCode"
    Write-Output "事件分析模型尝试：$model（$codexLauncher CLI），退出码 $attemptExitCode"
    if ($attemptExitCode -eq 0 -and (Test-Path -LiteralPath $resultPath)) {
        $selectedModel = $model
        break
    }

    $modelCompatibilityFailure = Test-ModelCompatibilityFailure -OutputText $attemptText
    if (-not $modelCompatibilityFailure) {
        $detail = if ($attemptText.Length -gt 3000) {
            $attemptText.Substring($attemptText.Length - 3000)
        } else {
            $attemptText
        }
        throw "Codex 事件分析失败（模型 $model，退出码 $attemptExitCode）：$detail"
    }
}

if ([string]::IsNullOrWhiteSpace($selectedModel)) {
    throw "Codex 事件分析没有兼容模型（$($attemptSummaries -join '；')）。请更新 Codex CLI 或调整 config.json 的 eventAnalysis.modelCandidates。"
}
Write-Output "事件分析采用模型：$selectedModel（reasoning=$reasoningEffort）"

$rawResult = Get-Content -LiteralPath $resultPath -Raw -Encoding utf8
$result = $rawResult | ConvertFrom-Json
$emails = @($result.emails)
if ($emails.Count -lt 1) { throw 'Codex 返回结果没有邮件。' }
$sendResults = @()
foreach ($email in $emails) {
    if ([string]::IsNullOrWhiteSpace([string]$email.subject) -or [string]::IsNullOrWhiteSpace([string]$email.htmlBody)) {
        throw 'Codex 返回结果缺少 subject 或 htmlBody。'
    }
    $allowedPrefix = if ($email.stage -eq 'registration') {
        '【可转债配债】同意注册公告：'
    } elseif ($email.stage -eq 'formal') {
        '【可转债配债】正式发行公告：'
    } else {
        throw "Codex 返回了无效阶段：$($email.stage)"
    }
    if (-not ([string]$email.subject).StartsWith($allowedPrefix)) {
        throw "邮件标题不符合固定格式：$($email.subject)"
    }
    $code = [string]$email.code
    $marker = "[[CALCULATION_TABLE:$code]]"
    if (-not ([string]$email.htmlBody).Contains($marker)) {
        throw "分析邮件缺少可信计算占位符：$marker"
    }
    if (-not $calculationHtmlByCode.ContainsKey($code)) {
        throw "没有找到证券代码 $code 的可信计算结果"
    }
    $body = [string]$email.htmlBody
    $wrappedMarker = "<h3>$marker</h3>"
    if ($body.Contains($wrappedMarker)) {
        $body = $body.Replace($wrappedMarker, $calculationHtmlByCode[$code])
    } else {
        $body = $body.Replace($marker, $calculationHtmlByCode[$code])
    }
    $email.htmlBody = $body
    if (-not [string]::IsNullOrWhiteSpace($inventoryHtml)) {
        $email.htmlBody = ([string]$email.htmlBody) + $inventoryHtml
    }
    $safeCode = ([string]$email.code) -replace '[^0-9A-Za-z_-]', '_'
    $htmlPath = Join-Path (Split-Path -Parent $evidence) "analysis-email-$safeCode-$($email.stage).html"
    $email.htmlBody | Set-Content -LiteralPath $htmlPath -Encoding utf8
    if ($NoSend) {
        $sendResults += "preview:$htmlPath"
    } else {
        $sendResult = & pwsh.exe -NoProfile -File $sendScript `
            -Subject ([string]$email.subject) -BodyFile $htmlPath -Html
        if ($LASTEXITCODE -ne 0) { throw '深度分析已完成，但邮件发送失败。' }
        $sendResults += ($sendResult | Out-String).Trim()
    }
}

foreach ($evidenceFile in $evidenceFiles) {
    $pack = Get-Content -LiteralPath $evidenceFile -Raw -Encoding utf8 | ConvertFrom-Json
    $pack.analysisStatus = if ($NoSend) { 'preview' } else { 'sent' }
    $pack | Add-Member -NotePropertyName analysisResultPath -NotePropertyValue $resultPath -Force
    $pack | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $evidenceFile -Encoding utf8
}

[PSCustomObject]@{
    ok = $true
    emailCount = $emails.Count
    subjects = @($emails | ForEach-Object { $_.subject })
    evidenceEntry = $evidence
    result = $resultPath
    emailResults = $sendResults
} | ConvertTo-Json -Depth 8 -Compress
