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
$promptTemplate = (Resolve-Path -LiteralPath (Join-Path $root 'event-analysis-prompt.md')).Path
$runtimeCli = Join-Path $root 'runtime\node_modules\@openai\codex\bin\codex.js'
$outputSchema = (Resolve-Path -LiteralPath (Join-Path $root 'analysis-result-schema.json')).Path
$resultPath = Join-Path (Split-Path -Parent $evidence) 'analysis-result.json'
$sendScript = (Resolve-Path -LiteralPath (Join-Path $root '..\Send-QQMail.ps1')).Path

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

if (-not (Test-Path -LiteralPath $runtimeCli)) {
    throw '尚未安装独立 Codex CLI 运行时。请先运行 Install-HybridMonitor.ps1。'
}

$instructions = Get-Content -LiteralPath $promptTemplate -Raw
$prompt = @"
$instructions

本次事件清单或唯一证据包：$evidence
请现在完成分析并按要求只输出 JSON。
"@

$prompt | & node $runtimeCli `
    --cd $workspace `
    --sandbox workspace-write `
    --ask-for-approval never `
    --search `
    exec `
    --skip-git-repo-check `
    --ephemeral `
    --output-schema $outputSchema `
    --output-last-message $resultPath `
    -

if ($LASTEXITCODE -ne 0) {
    throw "Codex 事件分析失败，退出码：$LASTEXITCODE"
}

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
