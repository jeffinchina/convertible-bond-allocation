param(
    [string[]]$Recipients
)

$ErrorActionPreference = 'Stop'

$configPath = Join-Path $PSScriptRoot 'qqmail-config.json'

function Get-NormalizedMailAddresses {
    param([Parameter(Mandatory = $true)][string[]]$Values)

    $result = New-Object System.Collections.Generic.List[string]
    foreach ($value in $Values) {
        foreach ($part in ([string]$value -split '[;,\r\n]+')) {
            $candidate = $part.Trim()
            if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
            if ($candidate -match '[\r\n]') { throw '邮箱地址不能包含换行符。' }
            try {
                $parsed = [System.Net.Mail.MailAddress]::new($candidate)
            } catch {
                throw '存在格式无效的邮箱地址。'
            }
            if ($parsed.Address -ne $candidate) { throw '邮箱地址必须使用纯地址格式。' }
            if (-not $result.Contains($parsed.Address)) { [void]$result.Add($parsed.Address) }
        }
    }
    return @($result)
}

$existingConfig = $null
if (Test-Path -LiteralPath $configPath) {
    try {
        $existingConfig = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    } catch {
        $existingConfig = $null
    }
}

if ($PSBoundParameters.ContainsKey('Recipients')) {
    if ($null -eq $existingConfig) {
        throw 'QQ 邮箱尚未配置，无法只更新收件人。请先运行图形配置。'
    }
    if ([string]::IsNullOrWhiteSpace([string]$existingConfig.encryptedAuthCode)) {
        throw '现有配置缺少加密授权码，无法安全迁移。'
    }
    $normalizedRecipients = @(Get-NormalizedMailAddresses -Values $Recipients)
    if ($normalizedRecipients.Count -eq 0) { throw '至少需要一个有效收件邮箱。' }
    if ($null -ne $existingConfig.PSObject.Properties['to']) {
        $existingConfig.to = @($normalizedRecipients)
    } else {
        $existingConfig | Add-Member -NotePropertyName to -NotePropertyValue @($normalizedRecipients)
    }
    $tempConfigPath = "$configPath.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        $existingConfig | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $tempConfigPath -Encoding utf8
        Move-Item -LiteralPath $tempConfigPath -Destination $configPath -Force
    } finally {
        if (Test-Path -LiteralPath $tempConfigPath) { Remove-Item -LiteralPath $tempConfigPath -Force }
    }
    [PSCustomObject]@{
        ok = $true
        configured = $true
        recipientCount = $normalizedRecipients.Count
        encryptedAuthCodePreserved = $true
    } | ConvertTo-Json -Compress
    exit 0
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$senderDefault = if ($null -ne $existingConfig -and -not [string]::IsNullOrWhiteSpace([string]$existingConfig.from)) {
    [string]$existingConfig.from
} elseif ($null -ne $existingConfig -and -not [string]::IsNullOrWhiteSpace([string]$existingConfig.username)) {
    [string]$existingConfig.username
} else {
    ''
}
$recipientDefaults = if ($null -ne $existingConfig -and $null -ne $existingConfig.to) {
    @($existingConfig.to)
} elseif ($null -ne $existingConfig -and $null -ne $existingConfig.recipients) {
    @($existingConfig.recipients)
} elseif ($null -ne $existingConfig -and $null -ne $existingConfig.recipient) {
    @($existingConfig.recipient)
} else {
    @()
}

$form = New-Object System.Windows.Forms.Form
$form.Text = '配置可转债日报 QQ 邮箱发送'
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true
$form.ShowInTaskbar = $true
$form.ClientSize = New-Object System.Drawing.Size(560, 370)
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false

$title = New-Object System.Windows.Forms.Label
$title.Text = '请输入新生成的 QQ 邮箱授权码'
$title.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 13, [System.Drawing.FontStyle]::Bold)
$title.Location = New-Object System.Drawing.Point(28, 22)
$title.AutoSize = $true
$form.Controls.Add($title)

$description = New-Object System.Windows.Forms.Label
$description.Text = '发件人与收件人仅保存在本机配置中；授权码使用 Windows 当前用户加密。不要填写 QQ 登录密码。'
$description.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
$description.Location = New-Object System.Drawing.Point(30, 63)
$description.Size = New-Object System.Drawing.Size(495, 42)
$form.Controls.Add($description)

$senderLabel = New-Object System.Windows.Forms.Label
$senderLabel.Text = '发件邮箱：'
$senderLabel.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10)
$senderLabel.Location = New-Object System.Drawing.Point(30, 111)
$senderLabel.AutoSize = $true
$form.Controls.Add($senderLabel)

$senderBox = New-Object System.Windows.Forms.TextBox
$senderBox.Location = New-Object System.Drawing.Point(120, 107)
$senderBox.Size = New-Object System.Drawing.Size(404, 30)
$senderBox.Font = New-Object System.Drawing.Font('Consolas', 10)
$senderBox.Text = $senderDefault
$form.Controls.Add($senderBox)

$recipientLabel = New-Object System.Windows.Forms.Label
$recipientLabel.Text = '收件邮箱：'
$recipientLabel.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10)
$recipientLabel.Location = New-Object System.Drawing.Point(30, 153)
$recipientLabel.AutoSize = $true
$form.Controls.Add($recipientLabel)

$recipientBox = New-Object System.Windows.Forms.TextBox
$recipientBox.Location = New-Object System.Drawing.Point(120, 149)
$recipientBox.Size = New-Object System.Drawing.Size(404, 54)
$recipientBox.Font = New-Object System.Drawing.Font('Consolas', 10)
$recipientBox.Multiline = $true
$recipientBox.Text = (@($recipientDefaults) | ForEach-Object { [string]$_ }) -join '; '
$form.Controls.Add($recipientBox)

$recipientHint = New-Object System.Windows.Forms.Label
$recipientHint.Text = '多个地址可用分号、逗号或换行分隔。'
$recipientHint.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 8)
$recipientHint.ForeColor = [System.Drawing.Color]::DimGray
$recipientHint.Location = New-Object System.Drawing.Point(120, 206)
$recipientHint.AutoSize = $true
$form.Controls.Add($recipientHint)

$authLabel = New-Object System.Windows.Forms.Label
$authLabel.Text = '授权码：'
$authLabel.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10)
$authLabel.Location = New-Object System.Drawing.Point(30, 241)
$authLabel.AutoSize = $true
$form.Controls.Add($authLabel)

$authBox = New-Object System.Windows.Forms.TextBox
$authBox.Location = New-Object System.Drawing.Point(120, 237)
$authBox.Size = New-Object System.Drawing.Size(404, 30)
$authBox.Font = New-Object System.Drawing.Font('Consolas', 11)
$authBox.UseSystemPasswordChar = $true
$form.Controls.Add($authBox)

$saveButton = New-Object System.Windows.Forms.Button
$saveButton.Text = '加密保存'
$saveButton.Location = New-Object System.Drawing.Point(326, 301)
$saveButton.Size = New-Object System.Drawing.Size(95, 34)
$saveButton.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
$form.Controls.Add($saveButton)

$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Text = '取消'
$cancelButton.Location = New-Object System.Drawing.Point(429, 301)
$cancelButton.Size = New-Object System.Drawing.Size(95, 34)
$cancelButton.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
$form.Controls.Add($cancelButton)

$form.AcceptButton = $saveButton
$form.CancelButton = $cancelButton

$saveButton.Add_Click({
    $sender = $senderBox.Text.Trim()
    try {
        $senderAddress = [System.Net.Mail.MailAddress]::new($sender)
        if ($senderAddress.Address -ne $sender) { throw 'invalid' }
        $configuredRecipients = @(Get-NormalizedMailAddresses -Values @($recipientBox.Text))
        if ($configuredRecipients.Count -eq 0) { throw 'invalid' }
    } catch {
        [System.Windows.Forms.MessageBox]::Show('请填写有效的发件邮箱和至少一个收件邮箱。', '邮箱配置无效', 'OK', 'Warning') | Out-Null
        return
    }

    $authCode = $authBox.Text.Trim()
    if ([string]::IsNullOrWhiteSpace($authCode)) {
        [System.Windows.Forms.MessageBox]::Show('请输入 QQ 邮箱授权码。', '缺少授权码', 'OK', 'Warning') | Out-Null
        return
    }

    $secure = ConvertTo-SecureString -String $authCode -AsPlainText -Force
    $encrypted = ConvertFrom-SecureString -SecureString $secure
    $config = [ordered]@{
        smtpServer = 'smtp.qq.com'
        smtpPort = 587
        useSsl = $true
        username = $sender
        from = $sender
        to = @($configuredRecipients)
        encryptedAuthCode = $encrypted
        encryption = 'Windows DPAPI CurrentUser'
        configuredAt = (Get-Date).ToString('o')
    }

    $config | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding utf8
    $authBox.Clear()
    [System.Windows.Forms.MessageBox]::Show('授权码已加密保存。现在可以进行发送测试。', '配置成功', 'OK', 'Information') | Out-Null
    $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $form.Close()
})

$cancelButton.Add_Click({
    $form.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $form.Close()
})

$authBox.Select()
$result = $form.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output '{"ok":true,"configured":true}'
} else {
    Write-Output '{"ok":false,"configured":false,"reason":"cancelled"}'
}
