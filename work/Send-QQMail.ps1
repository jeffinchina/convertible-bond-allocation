param(
    [Parameter(Mandatory = $true)]
    [string]$Subject,

    [Parameter(Mandatory = $true, ParameterSetName = 'Body')]
    [string]$Body,

    [Parameter(Mandatory = $true, ParameterSetName = 'BodyFile')]
    [string]$BodyFile,

    [switch]$Html
)

$ErrorActionPreference = 'Stop'
$configPath = Join-Path $PSScriptRoot 'qqmail-config.json'
$sentLogPath = Join-Path $PSScriptRoot 'qqmail-sent-log.json'

function Read-SmtpResponse {
    param([Parameter(Mandatory = $true)][System.IO.StreamReader]$Reader)

    $lines = New-Object System.Collections.Generic.List[string]
    do {
        $line = $Reader.ReadLine()
        if ($null -eq $line) { throw 'SMTP 服务器在返回响应前关闭了连接。' }
        [void]$lines.Add($line)
        $continued = $line.Length -ge 4 -and $line[3] -eq '-'
    } while ($continued)

    $last = $lines[$lines.Count - 1]
    if ($last.Length -lt 3 -or $last.Substring(0, 3) -notmatch '^\d{3}$') {
        throw "SMTP 返回了无法识别的响应：$($lines -join ' | ')"
    }
    [PSCustomObject]@{
        Code = [int]$last.Substring(0, 3)
        Text = ($lines -join ' | ')
    }
}

function Assert-SmtpResponse {
    param(
        [Parameter(Mandatory = $true)]$Response,
        [Parameter(Mandatory = $true)][int[]]$AllowedCodes,
        [Parameter(Mandatory = $true)][string]$Stage
    )
    if ($AllowedCodes -notcontains [int]$Response.Code) {
        throw "$Stage 失败：$($Response.Text)"
    }
}

function Send-SmtpLine {
    param(
        [Parameter(Mandatory = $true)][System.IO.StreamWriter]$Writer,
        [Parameter(Mandatory = $true)][System.IO.StreamReader]$Reader,
        [Parameter(Mandatory = $true)][string]$Line,
        [Parameter(Mandatory = $true)][int[]]$AllowedCodes,
        [Parameter(Mandatory = $true)][string]$Stage
    )
    $Writer.WriteLine($Line)
    $Writer.Flush()
    $response = Read-SmtpResponse -Reader $Reader
    Assert-SmtpResponse -Response $response -AllowedCodes $AllowedCodes -Stage $Stage
    $response
}

function Send-QqSmtpMessage {
    param(
        [Parameter(Mandatory = $true)][string]$Server,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$Username,
        [Parameter(Mandatory = $true)][string]$AuthCode,
        [Parameter(Mandatory = $true)][string]$From,
        [Parameter(Mandatory = $true)][string[]]$Recipients,
        [Parameter(Mandatory = $true)][string]$Subject,
        [Parameter(Mandatory = $true)][string]$MessageBody,
        [Parameter(Mandatory = $true)][bool]$IsHtml
    )

    if ($Subject -match '[\r\n]') { throw '邮件主题不能包含换行符。' }
    foreach ($recipient in $Recipients) {
        if ($recipient -match '[\r\n]') { throw '收件地址不能包含换行符。' }
    }

    $tcp = [System.Net.Sockets.TcpClient]::new()
    $ssl = $null
    $reader = $null
    $writer = $null
    try {
        $tcp.ReceiveTimeout = 30000
        $tcp.SendTimeout = 30000
        $tcp.Connect($Server, $Port)
        $network = $tcp.GetStream()
        $reader = [System.IO.StreamReader]::new($network, [System.Text.Encoding]::ASCII, $false, 1024, $true)
        $writer = [System.IO.StreamWriter]::new($network, [System.Text.Encoding]::ASCII, 1024, $true)
        $writer.NewLine = "`r`n"
        $writer.AutoFlush = $true

        $greeting = Read-SmtpResponse -Reader $reader
        Assert-SmtpResponse -Response $greeting -AllowedCodes @(220) -Stage '连接 SMTP 服务器'
        Send-SmtpLine -Writer $writer -Reader $reader -Line 'EHLO localhost' -AllowedCodes @(250) -Stage 'SMTP EHLO' | Out-Null
        Send-SmtpLine -Writer $writer -Reader $reader -Line 'STARTTLS' -AllowedCodes @(220) -Stage 'SMTP STARTTLS' | Out-Null

        $reader.Dispose()
        $writer.Dispose()
        $reader = $null
        $writer = $null
        $ssl = [System.Net.Security.SslStream]::new($network, $false)
        $ssl.AuthenticateAsClient(
            $Server,
            $null,
            [System.Security.Authentication.SslProtocols]::Tls12,
            $true
        )
        $reader = [System.IO.StreamReader]::new($ssl, [System.Text.Encoding]::ASCII, $false, 1024, $true)
        $writer = [System.IO.StreamWriter]::new($ssl, [System.Text.Encoding]::ASCII, 1024, $true)
        $writer.NewLine = "`r`n"
        $writer.AutoFlush = $true

        Send-SmtpLine -Writer $writer -Reader $reader -Line 'EHLO localhost' -AllowedCodes @(250) -Stage '加密 SMTP EHLO' | Out-Null
        Send-SmtpLine -Writer $writer -Reader $reader -Line 'AUTH LOGIN' -AllowedCodes @(334) -Stage '开始 SMTP 登录' | Out-Null
        $encodedUsername = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($Username))
        Send-SmtpLine -Writer $writer -Reader $reader -Line $encodedUsername -AllowedCodes @(334) -Stage '提交 SMTP 用户名' | Out-Null
        $encodedAuthCode = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($AuthCode))
        Send-SmtpLine -Writer $writer -Reader $reader -Line $encodedAuthCode -AllowedCodes @(235) -Stage 'SMTP 身份验证' | Out-Null

        Send-SmtpLine -Writer $writer -Reader $reader -Line "MAIL FROM:<$From>" -AllowedCodes @(250) -Stage '设置发件人' | Out-Null
        foreach ($recipient in $Recipients) {
            Send-SmtpLine -Writer $writer -Reader $reader -Line "RCPT TO:<$recipient>" -AllowedCodes @(250, 251) -Stage '设置收件人' | Out-Null
        }
        Send-SmtpLine -Writer $writer -Reader $reader -Line 'DATA' -AllowedCodes @(354) -Stage '开始提交邮件正文' | Out-Null

        $subjectHeader = '=?UTF-8?B?' + [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($Subject)) + '?='
        $contentType = if ($IsHtml) { 'text/html' } else { 'text/plain' }
        $bodyBase64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($MessageBody))
        $bodyLines = if ($bodyBase64.Length -gt 0) {
            [regex]::Matches($bodyBase64, '.{1,76}') | ForEach-Object { $_.Value }
        } else {
            @('')
        }
        $dateHeader = (Get-Date).ToString('ddd, dd MMM yyyy HH:mm:ss zzz', [System.Globalization.CultureInfo]::GetCultureInfo('en-US'))
        $messageId = '<' + [Guid]::NewGuid().ToString('N') + '@qqmail.local>'
        $headers = @(
            "From: <$From>"
            "To: $($Recipients -join ', ')"
            "Subject: $subjectHeader"
            "Date: $dateHeader"
            "Message-ID: $messageId"
            'MIME-Version: 1.0'
            "Content-Type: $contentType; charset=UTF-8"
            'Content-Transfer-Encoding: base64'
        ) -join "`r`n"
        $payload = $headers + "`r`n`r`n" + ($bodyLines -join "`r`n") + "`r`n.`r`n"
        $writer.Write($payload)
        $writer.Flush()
        $dataResponse = Read-SmtpResponse -Reader $reader
        Assert-SmtpResponse -Response $dataResponse -AllowedCodes @(250) -Stage '提交邮件'
        try { Send-SmtpLine -Writer $writer -Reader $reader -Line 'QUIT' -AllowedCodes @(221) -Stage '结束 SMTP 会话' | Out-Null } catch { }
    } finally {
        if ($null -ne $reader) { $reader.Dispose() }
        if ($null -ne $writer) { $writer.Dispose() }
        if ($null -ne $ssl) { $ssl.Dispose() }
        $tcp.Dispose()
    }
}

function Send-QqSmtpMessageWithPython {
    param(
        [Parameter(Mandatory = $true)][string]$Server,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$Username,
        [Parameter(Mandatory = $true)][string]$AuthCode,
        [Parameter(Mandatory = $true)][string]$From,
        [Parameter(Mandatory = $true)][string[]]$Recipients,
        [Parameter(Mandatory = $true)][string]$Subject,
        [Parameter(Mandatory = $true)][string]$MessageBody,
        [Parameter(Mandatory = $true)][bool]$IsHtml
    )

    $helper = Join-Path $PSScriptRoot 'Send-QQMail.py'
    if (-not (Test-Path -LiteralPath $helper)) { throw "Python SMTP 辅助脚本不存在：$helper" }
    $request = [ordered]@{
        server = $Server
        port = $Port
        username = $Username
        authCodeB64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($AuthCode))
        from = $From
        recipients = @($Recipients)
        subjectB64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($Subject))
        bodyB64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($MessageBody))
        isHtml = $IsHtml
    } | ConvertTo-Json -Depth 5 -Compress

    $nativePreferenceWasSet = Test-Path variable:PSNativeCommandUseErrorActionPreference
    if ($nativePreferenceWasSet) {
        $oldNativePreference = $PSNativeCommandUseErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $false
    }
    try {
        $output = $request | & py $helper 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        if ($nativePreferenceWasSet) { $PSNativeCommandUseErrorActionPreference = $oldNativePreference }
    }
    $outputText = (@($output) | ForEach-Object { [string]$_ }) -join "`n"
    if ($exitCode -ne 0) { throw "Python SMTP 发送失败：$outputText" }
    $result = ($outputText.Trim().Split("`n")[-1]) | ConvertFrom-Json
    if (-not [bool]$result.ok) { throw "Python SMTP 未返回成功状态：$outputText" }
    return $result
}

if (-not (Test-Path -LiteralPath $configPath)) {
    throw 'QQ 邮箱尚未配置。请先运行 Configure-QQMail.ps1。'
}

$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$configuredRecipients = if ($null -ne $config.to) {
    @($config.to)
} elseif ($null -ne $config.recipients) {
    @($config.recipients)
} elseif ($null -ne $config.recipient) {
    @($config.recipient)
} else {
    @()
}
$recipients = @(
    $configuredRecipients |
        ForEach-Object { ([string]$_).Trim() } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Sort-Object -Unique
)
if ($recipients.Count -eq 0) {
    throw 'QQ 邮箱配置中没有有效收件人。请在 to 数组中配置至少一个地址。'
}

if ($PSCmdlet.ParameterSetName -eq 'BodyFile') {
    $resolvedBodyFile = (Resolve-Path -LiteralPath $BodyFile).Path
    $messageBody = Get-Content -LiteralPath $resolvedBodyFile -Raw
} else {
    $messageBody = $Body
}

$recipientKey = $recipients -join ';'
$hashBytes = [System.Text.Encoding]::UTF8.GetBytes("$recipientKey`n$Subject`n$messageBody")
$messageHash = [Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData($hashBytes))
$sendLedgerMutex = [System.Threading.Mutex]::new($false, 'Local\ConvertibleBondAllocation.QQMail.SendLedger.v1')
$sendLedgerLockAcquired = $false
$secure = $null
$credential = $null
$authCode = $null

try {
    try {
        $sendLedgerLockAcquired = $sendLedgerMutex.WaitOne([TimeSpan]::FromSeconds(45))
    } catch [System.Threading.AbandonedMutexException] {
        # The previous owner exited without releasing the mutex. Windows transfers
        # ownership to this process, so the ledger can be checked safely here.
        $sendLedgerLockAcquired = $true
    }
    if (-not $sendLedgerLockAcquired) {
        throw '等待邮件发送账本锁超时，请稍后重试。'
    }

    $sentLog = if (Test-Path -LiteralPath $sentLogPath) {
        @(Get-Content -LiteralPath $sentLogPath -Raw | ConvertFrom-Json)
    } else {
        @()
    }

    $existing = $sentLog | Where-Object { $_.messageHash -eq $messageHash } | Select-Object -First 1
    if ($null -ne $existing) {
        [PSCustomObject]@{
            ok = $true
            duplicate = $true
            subject = $Subject
            sentAt = $existing.sentAt
        } | ConvertTo-Json -Compress
        return
    }

    $secure = ConvertTo-SecureString -String $config.encryptedAuthCode
    $credential = New-Object System.Management.Automation.PSCredential($config.username, $secure)
    $authCode = $credential.GetNetworkCredential().Password

    $transportResult = Send-QqSmtpMessageWithPython `
        -Server ([string]$config.smtpServer) `
        -Port ([int]$config.smtpPort) `
        -Username ([string]$config.username) `
        -AuthCode $authCode `
        -From ([string]$config.from) `
        -Recipients $recipients `
        -Subject $Subject `
        -MessageBody $messageBody `
        -IsHtml ([bool]$Html)

    $sentAt = (Get-Date).ToString('o')
    $sentLog = @($sentLog) + [PSCustomObject]@{
        messageHash = $messageHash
        subject = $Subject
        sentAt = $sentAt
        messageId = [string]$transportResult.messageId
        serverAccepted = [bool]$transportResult.serverAccepted
    }
    $sentLog | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $sentLogPath -Encoding utf8

    [PSCustomObject]@{
        ok = $true
        duplicate = $false
        subject = $Subject
        sentAt = $sentAt
        messageId = [string]$transportResult.messageId
        serverAccepted = [bool]$transportResult.serverAccepted
    } | ConvertTo-Json -Compress
} catch {
    $messages = New-Object System.Collections.Generic.List[string]
    $current = $_.Exception
    while ($null -ne $current) {
        if (-not [string]::IsNullOrWhiteSpace($current.Message)) {
            [void]$messages.Add($current.Message)
        }
        $current = $current.InnerException
    }
    $errorResult = [PSCustomObject]@{
        ok = $false
        duplicate = $false
        errorType = $_.Exception.GetType().FullName
        error = ($messages -join ' | ')
    } | ConvertTo-Json -Compress
    [Console]::Error.WriteLine($errorResult)
    exit 1
} finally {
    $authCode = $null
    $credential = $null
    $secure = $null
    if ($sendLedgerLockAcquired) {
        $sendLedgerMutex.ReleaseMutex()
    }
    $sendLedgerMutex.Dispose()
}
