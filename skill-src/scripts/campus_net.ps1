#requires -Version 5.1
<#
.SYNOPSIS
  校园网 / 数字图书馆网络状态检测与登录辅助（Windows PowerShell）。

.DESCRIPTION
  给 DSH skill「campus-library-papers」使用。核心动作：
    status   检测：本地 IP、公网 IP、强制门户、校内探测页、WebVPN 网关、代理
    login    学校统一身份认证（CAS / 表单门户）登录辅助；验证码/二次验证转人工
    vpn-url  把一个普通网址重写成学校 WebVPN 网址（调用 scripts/webvpn_url.py）
    open     用默认浏览器打开网址（可 -Vpn 先转 WebVPN），用于人工下载
    save-cred 用 Windows DPAPI 加密保存账号（不落明文），供 login 使用

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File campus_net.ps1 -Action status -Json
  powershell -NoProfile -ExecutionPolicy Bypass -File campus_net.ps1 -Action vpn-url -Url "https://kns.cnki.net/" -Json
  powershell -NoProfile -ExecutionPolicy Bypass -File campus_net.ps1 -Action open -Url "https://doi.org/10.1038/nature12373" -Vpn
#>
[CmdletBinding()]
param(
    [ValidateSet('status', 'login', 'vpn-url', 'open', 'save-cred')]
    [string]$Action = 'status',

    [string]$Config = '',
    [string]$Url = '',
    [switch]$Vpn,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
# 只启用 TLS 1.2+（TLS 1.0/1.1 已弃用）
try {
    $tls = [Net.SecurityProtocolType]::Tls12
    if ([Enum]::GetNames([Net.SecurityProtocolType]) -contains 'Tls13') {
        $tls = $tls -bor [Net.SecurityProtocolType]::Tls13
    }
    [Net.ServicePointManager]::SecurityProtocol = $tls
} catch { }
# 让 Windows PowerShell 5.1 在管道/重定向时也按 UTF-8 输出中文
try {
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
    $OutputEncoding = [Text.Encoding]::UTF8
} catch { }

$ScriptDir = Split-Path -Parent $PSCommandPath
$SkillDir = Split-Path -Parent $ScriptDir
$DefaultConfig = Join-Path $SkillDir 'config\config.json'
$ExampleConfig = Join-Path $SkillDir 'config\config.example.json'

# ---------------------------------------------------------------- 配置
$DefaultSettings = [ordered]@{
    school = [ordered]@{ name = ''; domains = @() }
    email = ''
    download_dir = 'papers'
    on_campus_probes = @()
    captive_portal_probes = @(
        [ordered]@{ url = 'http://www.msftconnecttest.com/connecttest.txt'; expect = 'Microsoft Connect Test' },
        [ordered]@{ url = 'http://connect.rom.miui.com/generate_204'; expect = '204' }
    )
    public_ip_probes = @(
        'https://api.ipify.org?format=json',
        'https://ifconfig.me/ip',
        'https://myip.ipip.net',
        'https://ip.useragentinfo.com/json'
    )
    webvpn = [ordered]@{
        enabled = $true
        base = 'https://webvpn.whut.edu.cn'
        login_url = 'https://webvpn.whut.edu.cn/login?cas_login=true'
        cookie_env = 'WEBVPN_COOKIE'
        probe_target = 'https://lib.whut.edu.cn/'
        login_markers = @('登录', 'login', '统一身份认证', 'cas')
    }
    auth = [ordered]@{
        mode = 'manual'                       # cas | manual | form | none
        login_url = ''
        manual_open_url = ''
        open_browser_on_manual = $true
        username_field = 'username'
        password_field = 'password'
        extra_fields = [ordered]@{}
        hidden_fields = @()                 # 额外需要从登录页解析的隐藏字段名
        success_url_contains = @()
        success_body_contains = @()
        failure_body_contains = @('用户名或密码', '密码错误', '验证码错误', 'invalid credential')
        captcha_markers = @('captcha', '验证码', 'kaptcha')
        manual_verify_hint = '登录页需要验证码/二次验证，请在浏览器中手动完成登录后再运行 -Action status。'
    }
    credentials = [ordered]@{ dpapi_file = '' }
    carsi = [ordered]@{
        enabled = $true
        portal = 'http://carsi.whut.edu.cn/'
        idp_entity_id = 'https://idp.whut.edu.cn/idp/shibboleth'
    }
    campus_cidrs = @()
}

function Write-Diag([string]$Message) {
    # 诊断信息一律走 stderr，避免 -Json 时污染 stdout（PowerShell 5.1 会把 Write-Warning 并进 stdout）
    try { [Console]::Error.WriteLine("WARNING: $Message") }
    catch { Write-Verbose "WARNING: $Message" }
}

function Read-JsonFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop)
    } catch {
        Write-Diag "读取配置失败：$Path（$($_.Exception.Message)）"
        return $null
    }
}

function Merge-Settings($Base, $Override) {
    if ($null -eq $Override) { return $Base }
    foreach ($prop in $Override.PSObject.Properties) {
        $name = $prop.Name
        if ($Base.Contains($name) -and $Base[$name] -is [System.Collections.IDictionary] -and
            $prop.Value -is [pscustomobject]) {
            $nested = [ordered]@{}
            foreach ($k in $Base[$name].Keys) { $nested[$k] = $Base[$name][$k] }
            Merge-Settings $nested $prop.Value | Out-Null
            $Base[$name] = $nested
        } else {
            $Base[$name] = $prop.Value
        }
    }
    return $Base
}

$settings = [ordered]@{}
foreach ($k in $DefaultSettings.Keys) { $settings[$k] = $DefaultSettings[$k] }

$configPath = if ($Config) { $Config } elseif (Test-Path -LiteralPath $DefaultConfig) { $DefaultConfig } elseif (Test-Path -LiteralPath $ExampleConfig) { $ExampleConfig } else { '' }
if ($Config -and -not (Test-Path -LiteralPath $Config)) {
    throw '指定的配置文件不存在；请检查 -Config 路径'
}
if ($configPath) {
    $loaded = Read-JsonFile $configPath
    if (-not $loaded) { throw '配置读取失败，请修复 JSON 后重试' }
    Merge-Settings $settings $loaded | Out-Null
}
$settings._config_path = $configPath

function Write-Result($Object, [string]$HumanText) {
    if ($Json) {
        $Object | ConvertTo-Json -Depth 10
    } else {
        if ($HumanText) { Write-Output $HumanText }
        else { $Object | ConvertTo-Json -Depth 10 }
    }
}

function Get-FinalUrl($Response, [string]$Fallback) {
    # PowerShell 5.1 用 BaseResponse.ResponseUri，PowerShell 7 用 RequestMessage.RequestUri
    try { $u = $Response.BaseResponse.ResponseUri; if ($u) { return $u.AbsoluteUri } } catch { }
    try { $u = $Response.BaseResponse.RequestMessage.RequestUri; if ($u) { return $u.AbsoluteUri } } catch { }
    return $Fallback
}

function Test-Url([string]$Target, [int]$TimeoutSec = 20) {
    $result = [ordered]@{
        url = $Target; ok = $false; status = $null; final_url = $null
        redirected = $false; error = $null; body_head = ''; body_full = ''; elapsed_ms = $null
    }
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        $response = Invoke-WebRequest -Uri $Target -UseBasicParsing -TimeoutSec $TimeoutSec `
            -MaximumRedirection 8 -UserAgent 'Mozilla/5.0 campus-library-papers status probe'
        $result.ok = $true
        $result.status = [int]$response.StatusCode
        $result.final_url = Get-FinalUrl $response $Target
        $content = ''
        if ($response.Content -is [byte[]]) {
            $content = [Text.Encoding]::UTF8.GetString($response.Content)
        } elseif ($response.Content) {
            $content = [string]$response.Content
        }
        $result.body_head = $content.Substring(0, [Math]::Min(1200, $content.Length))
        # body_full 供登录流程解析隐藏字段/验证码；限制 512 KB 防止异常大页面
        $result.body_full = $content.Substring(0, [Math]::Min(524288, $content.Length))
    } catch [System.Net.WebException] {
        $response = $_.Exception.Response
        if ($response -and $response.StatusCode) { $result.status = [int]$response.StatusCode }
        $result.error = $_.Exception.Message
    } catch {
        $result.error = $_.Exception.Message
        # PowerShell 7 的 HttpResponseException 不是 WebException，这里兜底取状态码
        try {
            $statusCode = $_.Exception.Response.StatusCode
            if ($statusCode) { $result.status = [int]$statusCode }
        } catch { }
    }
    $watch.Stop()
    $result.elapsed_ms = [int]$watch.ElapsedMilliseconds
    if ($result.final_url) {
        $a = [Uri]$Target; $b = [Uri]$result.final_url
        $result.redirected = ($a.Scheme -ne $b.Scheme) -or ($a.Host -ne $b.Host) -or ($a.AbsolutePath -ne $b.AbsolutePath)
    }
    return $result
}

function Get-LocalIPv4 {
    $addresses = @()
    try {
        $addresses = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
            Select-Object IPAddress, InterfaceAlias, PrefixLength
    } catch {
        try {
            $addresses = [Net.Dns]::GetHostAddresses([Net.Dns]::GetHostName()) |
                Where-Object { $_.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork } |
                ForEach-Object { [pscustomobject]@{ IPAddress = $_.IPAddressToString; InterfaceAlias = ''; PrefixLength = $null } }
        } catch { }
    }
    return @($addresses)
}

function Get-ProxyState {
    $state = [ordered]@{ env_http_proxy = $env:HTTP_PROXY; env_https_proxy = $env:HTTPS_PROXY; no_proxy = $env:NO_PROXY; registry = $null }
    try {
        $ie = Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
        $state.registry = [ordered]@{ enabled = [bool]$ie.ProxyEnable; server = $ie.ProxyServer; pac = $ie.AutoConfigURL }
    } catch { }
    return $state
}

function Get-PublicIP {
    $ipv4Pattern = '(\d{1,3}(?:\.\d{1,3}){3})'
    $ipv6Pattern = '((?:[0-9a-fA-F]{1,4}:){2,}[0-9a-fA-F]{1,4})'
    foreach ($probe in @($settings.public_ip_probes)) {
        if (-not $probe) { continue }
        try {
            $item = [ordered]@{ probe = $probe; ip = $null; family = $null; ok = $false }
            $response = Invoke-WebRequest -Uri $probe -UseBasicParsing -TimeoutSec 12 -UserAgent 'campus-library-papers status probe'
            $text = if ($response.Content -is [byte[]]) { [Text.Encoding]::UTF8.GetString($response.Content) } else { [string]$response.Content }
            $text = $text.Trim()
            if ($text -match '"ip"\s*:\s*"([^"]+)"') {
                $item.ip = $Matches[1]; $item.ok = $true
            } elseif ($text -match $ipv4Pattern) {
                $item.ip = $Matches[1]; $item.ok = $true
            } elseif ($text -match $ipv6Pattern) {
                $item.ip = $Matches[1]; $item.ok = $true
            }
            if ($item.ok) {
                $item.family = if ($item.ip -match ':') { 'IPv6' } else { 'IPv4' }
                return $item
            }
        } catch { }
    }
    return [ordered]@{ probe = $null; ip = $null; family = $null; ok = $false }
}

function Get-CaptivePortalState {
    $state = [ordered]@{ detected = $false; probes = @() }
    foreach ($probe in @($settings.captive_portal_probes)) {
        if (-not $probe) { continue }
        $item = [ordered]@{ url = $probe.url; expected = $probe.expect; ok = $false; status = $null; redirected = $false; final_url = $null; error = $null }
        try {
            $response = Invoke-WebRequest -Uri $probe.url -UseBasicParsing -TimeoutSec 10 -MaximumRedirection 5 `
                -UserAgent 'Mozilla/5.0 campus-library-papers captive probe'
            $item.status = [int]$response.StatusCode
            $item.final_url = Get-FinalUrl $response $probe.url
            $text = if ($response.Content -is [byte[]]) { [Text.Encoding]::UTF8.GetString($response.Content) } else { [string]$response.Content }
            if ($probe.expect -eq '204') {
                $item.ok = ($item.status -eq 204)
            } else {
                $item.ok = ($text -match [regex]::Escape([string]$probe.expect))
            }
            if ($item.final_url -and $item.final_url -notmatch [regex]::Escape(([Uri]$probe.url).Host)) { $item.redirected = $true }
        } catch [System.Net.WebException] {
            $response = $_.Exception.Response
            if ($response -and $response.StatusCode) { $item.status = [int]$response.StatusCode }
            $item.error = $_.Exception.Message
            # 强制门户常以 3xx/4xx + 登录页形式出现
            if ($item.status -in @(301, 302, 303, 307, 308)) { $item.redirected = $true }
        } catch { $item.error = $_.Exception.Message }
        # 只有"被劫持/被要求认证"才算强制门户；单纯探测超时不可达不算，避免误报
        $portalLike = $item.redirected -or ($item.status -in @(301, 302, 303, 307, 308)) -or ($item.status -eq 200 -and -not $item.ok -and [string]$probe.expect -ne '204')
        if ($portalLike) { $state.detected = $true }
        $state.probes += $item
    }
    return $state
}

function Test-CampusProbe($Probe) {
    $item = [ordered]@{
        name = $Probe.name; url = $Probe.url; ok = $false; authenticated = $null
        status = $null; final_url = $null; redirected = $false
        matched_markers = @(); missing_markers = @(); login_wall = $false; error = $null
        body_head = ''
    }
    if (-not $Probe.url) { return $item }
    $res = Test-Url $Probe.url
    $item.status = $res.status
    $item.final_url = $res.final_url
    $item.redirected = $res.redirected
    $item.error = $res.error
    $item.body_head = [string]$res.body_head
    $body = if ($res.body_full) { [string]$res.body_full } else { [string]$res.body_head }
    $lower = $body.ToLowerInvariant()

    foreach ($marker in @($Probe.must_contain)) {
        if ($marker -and $body -notmatch [regex]::Escape([string]$marker)) { $item.missing_markers += $marker }
    }
    foreach ($marker in @($Probe.must_not_contain)) {
        if ($marker -and $body -match [regex]::Escape([string]$marker)) { $item.login_wall = $true }
    }
    foreach ($marker in @($Probe.login_markers)) {
        if ($marker -and $lower.Contains(([string]$marker).ToLowerInvariant())) { $item.login_wall = $true }
    }
    $item.ok = [bool]($res.ok -and $res.status -eq 200)
    # Public library homepages and negative-marker absence prove reachability only.
    if ($Probe.proves_campus_access -eq $true -and @($Probe.must_contain).Count -gt 0) {
        if ($item.ok -and -not $item.redirected -and -not $item.login_wall -and $item.missing_markers.Count -eq 0) {
            $item.authenticated = $true
        } elseif ($item.login_wall) {
            $item.authenticated = $false
        }
    }
    return $item
}

function Test-CidrContains([string]$Ip, [string]$Cidr) {
    try {
        if (-not $Cidr.Contains('/')) { return $Ip -eq $Cidr }
        $parts = $Cidr.Split('/')
        $network = [System.Net.IPAddress]::Parse($parts[0])
        $bits = [int]$parts[1]
        if ($bits -lt 0 -or $bits -gt ($network.GetAddressBytes().Length * 8)) { return $false }
        $ipBytes = [System.Net.IPAddress]::Parse($Ip).GetAddressBytes()
        $netBytes = $network.GetAddressBytes()
        if ($ipBytes.Length -ne $netBytes.Length) { return $false }
        $fullBytes = [Math]::Floor($bits / 8)
        for ($i = 0; $i -lt $fullBytes; $i++) {
            if ($ipBytes[$i] -ne $netBytes[$i]) { return $false }
        }
        $remaining = $bits % 8
        if ($remaining -gt 0) {
            $mask = [byte](0xFF -band (0xFF -shl (8 - $remaining)))
            if (($ipBytes[$fullBytes] -band $mask) -ne ($netBytes[$fullBytes] -band $mask)) { return $false }
        }
        return $true
    } catch { return $false }
}

function Invoke-WebVpnProbe {
    # 用一条重写 URL 探测 WebVPN 是否已登录：302 -> /login 表示未登录；200 表示已登录。
    # 关键：未登录访问 /login 也会下发同名 ticket，所以不能只看 Cookie 是否存在。
    $probe = [ordered]@{
        target = $null; rewritten = $null; status = $null; redirect = $null
        logged_in = $null; method = $null; error = $null
    }
    $target = [string]$settings.webvpn.probe_target
    if (-not $target) { return $probe }
    $probe.target = $target
    $script = Join-Path $ScriptDir 'webvpn_url.py'
    $python = Resolve-Python
    if (-not $python -or -not (Test-Path -LiteralPath $script)) {
        $probe.error = '需要 python 生成 WebVPN 探测 URL（设置 PYTHON 或 DSH_PYTHON）'
        return $probe
    }
    try {
        $out = & $python $script --base ([string]$settings.webvpn.base) $target 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { $probe.error = 'webvpn_url.py 生成探测 URL 失败'; return $probe }
        $probe.rewritten = ([string]($out | Select-Object -Last 1)).Trim()
    } catch {
        $probe.error = $_.Exception.Message
        return $probe
    }
    if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
        try {
            $raw = & curl.exe -s -o NUL -w '%{http_code}|%{redirect_url}' --max-time 20 $probe.rewritten 2>$null
            $probe.method = 'curl'
            $parts = ([string]$raw).Split('|')
            if ($parts.Count -ge 1 -and $parts[0] -match '^\d+$') { $probe.status = [int]$parts[0] }
            if ($parts.Count -ge 2) { $probe.redirect = $parts[1] }
        } catch {
            $probe.error = $_.Exception.Message
        }
    } else {
        $res = Test-Url $probe.rewritten
        $probe.method = 'Invoke-WebRequest'
        $probe.status = $res.status
        $probe.redirect = $res.final_url
    }
    if ($probe.status -eq 200) { $probe.logged_in = $null } # HTTP 200 alone is not authentication evidence
    elseif ($probe.status -in @(301, 302, 303, 307, 308)) {
        if ($probe.redirect -match '/login') { $probe.logged_in = $false }
    }
    return $probe
}

function Get-NetworkStatus {
    $localIps = @(Get-LocalIPv4)
    $cidrHits = @()
    foreach ($entry in $localIps) {
        foreach ($cidr in @($settings.campus_cidrs)) {
            if ($cidr -and (Test-CidrContains $entry.IPAddress ([string]$cidr))) {
                $cidrHits += [ordered]@{ ip = $entry.IPAddress; cidr = $cidr; iface = $entry.InterfaceAlias }
            }
        }
    }

    $probes = @()
    foreach ($probe in @($settings.on_campus_probes)) {
        if ($probe -and $probe.url) { $probes += (Test-CampusProbe $probe) }
    }

    $captive = Get-CaptivePortalState
    $publicIp = Get-PublicIP
    $proxy = Get-ProxyState
    # 公网 IP 探测都失败时，回退读图书馆首页服务端写入的 rqIp（弱证据，但通常就是出口 IP）
    if (-not $publicIp.ok) {
        foreach ($probe in $probes) {
            if (-not $probe.url) { continue }
            $pageText = [string]$probe.body_head
            # rqIp 常出现在 1200 字符之后：必要时重新抓整页（Test-Url.body_full）
            if ($pageText -notmatch 'rqIp') {
                $page = Test-Url $probe.url
                $pageText = [string]$page.body_full
            }
            $match = [regex]::Match($pageText, "rqIp\s*=\s*'([^']+)'")
            if (-not $match.Success) { $match = [regex]::Match($pageText, 'rqIp\s*=\s*"([^"]+)"') }
            if ($match.Success) {
                $ip = $match.Groups[1].Value
                $publicIp = [ordered]@{
                    probe = $probe.url; ip = $ip; ok = $true
                    family = if ($ip -match ':') { 'IPv6' } else { 'IPv4' }
                    source = 'probe-rqIp'
                }
                break
            }
        }
    }

    $vpn = [ordered]@{ enabled = [bool]$settings.webvpn.enabled; base = $settings.webvpn.base; reachable = $false; logged_in = $null; status = $null; final_url = $null; error = $null; cookie_env_set = $false; probe = $null }
    $vpn.cookie_env_set = [bool](Get-Item -Path "Env:$($settings.webvpn.cookie_env)" -ErrorAction SilentlyContinue)
    if ($vpn.enabled -and $vpn.base) {
        $vpnRes = Test-Url ($vpn.base.TrimEnd('/') + '/')
        $vpn.reachable = [bool]$vpnRes.ok
        $vpn.status = $vpnRes.status
        $vpn.final_url = $vpnRes.final_url
        $vpn.error = $vpnRes.error
        if ($vpnRes.ok) {
            $bodyLower = ([string]$vpnRes.body_head).ToLowerInvariant()
            $hasLogin = $false
            foreach ($marker in @($settings.webvpn.login_markers)) {
                if ($marker -and $bodyLower.Contains(([string]$marker).ToLowerInvariant())) { $hasLogin = $true; break }
            }
            $vpn.logged_in = $null # This HTTP session is independent of the browser
        }
        # 更强的判据：重写 URL 是否 302 到 /login
        $vpn.probe = Invoke-WebVpnProbe
        if ($vpn.probe -and $vpn.probe.logged_in -ne $null) { $vpn.logged_in = $vpn.probe.logged_in }
    }

    $carsi = [ordered]@{ enabled = [bool]$settings.carsi.enabled; portal = $settings.carsi.portal; idp_entity_id = $settings.carsi.idp_entity_id; reachable = $null }
    if ($carsi.enabled -and $carsi.portal) {
        $carsiRes = Test-Url ([string]$carsi.portal)
        $carsi.reachable = [bool]$carsiRes.ok
    }

    $onCampus = $null
    $authenticatedProbes = @($probes | Where-Object { $_.authenticated -eq $true })
    if ($authenticatedProbes.Count -gt 0) { $onCampus = $true }
    elseif ($probes.Count -gt 0 -and ($probes | Where-Object { $_.authenticated -eq $false -or $_.login_wall })) { $onCampus = $false }

    $vpnConnected = $null
    # CLI HTTP probes never establish the current browser session status.
    $vpn.http_probe_logged_in = $vpn.logged_in
    $vpn.logged_in = $null

    $recommendation = 'unknown'
    if ($onCampus -eq $true) { $recommendation = 'direct' }
    elseif ($vpnConnected -eq $true) { $recommendation = 'vpn' }
    elseif ($onCampus -eq $false) { $recommendation = 'oa_or_vpn' }

    return [ordered]@{
        probe_scope = 'independent_http_session'
        browser_session = 'not_observed'
        checked_at = (Get-Date).ToString('o')
        config_path = $settings._config_path
        school = $settings.school.name
        on_campus = $onCampus
        vpn_connected = $vpnConnected
        captive_portal_detected = $captive.detected
        recommendation = $recommendation
        local_ipv4 = @($localIps)
        campus_cidr_hits = @($cidrHits)
        campus_probes = @($probes)
        captive_portal_probes = @($captive.probes)
        webvpn = $vpn
        carsi = $carsi
        public_ip = $publicIp
        proxy = $proxy
        hints = @(
            'on_campus=true 仅说明已配置的受限探测页通过；目标论文订阅权限仍需逐篇核验',
            'on_campus=false -> 优先 CARSI（carsi.portal）按库做机构登录；WebVPN 作备选；纯 OA 论文可直接脚本下载',
            'vpn_connected=null：脚本不能判断浏览器会话；用户已登录时直接继续原浏览器，不重复要求登录',
            'on_campus=null -> 探测页无法判断，请人工确认或补充 on_campus_probes / campus_cidrs'
        )
    }
}

# ---------------------------------------------------------------- 登录
function Get-DpapiCredentialPath {
    if ($settings.credentials.dpapi_file) { return [Environment]::ExpandEnvironmentVariables($settings.credentials.dpapi_file) }
    $dir = Join-Path $env:USERPROFILE '.dsh\campus-papers'
    return (Join-Path $dir 'cred.xml')
}

function Save-DpapiCredential {
    $path = Get-DpapiCredentialPath
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $path) | Out-Null
    $user = $env:CAMPUS_USERNAME
    $secure = $null
    if ($user -and $env:CAMPUS_PASSWORD) {
        $secure = ConvertTo-SecureString -String $env:CAMPUS_PASSWORD -AsPlainText -Force
    } else {
        $user = Read-Host '校园网/统一身份认证账号'
        if (-not $user) { throw '账号不能为空' }
        $secure = Read-Host '密码（输入不回显）' -AsSecureString
    }
    $encrypted = ConvertFrom-SecureString -SecureString $secure
    $payload = [ordered]@{ username = $user; password = $encrypted; saved_at = (Get-Date).ToString('o') }
    ($payload | ConvertTo-Json) | Set-Content -LiteralPath $path -Encoding UTF8
    Write-Result ([ordered]@{ status = 'saved'; path = $path; note = '密码用 Windows DPAPI 加密，只能当前 Windows 用户在本机解密' }) "已保存凭据（DPAPI 加密）：$path"
}

function Get-CredentialPair {
    $user = $env:CAMPUS_USERNAME
    $pass = $env:CAMPUS_PASSWORD
    if ($user -and $pass) { return @{ username = $user; password = $pass; source = 'env' } }
    $path = Get-DpapiCredentialPath
    if (Test-Path -LiteralPath $path) {
        $saved = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        try {
            $secure = ConvertTo-SecureString -String $saved.password
            $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            try {
                $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
            } finally {
                [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            }
            return @{ username = [string]$saved.username; password = $plain; source = 'dpapi' }
        } catch {
            throw "凭据文件无法解密（是否换过 Windows 用户？）：$path"
        }
    }
    return $null
}

function Get-HiddenFormFields([string]$Html) {
    $fields = @{}
    foreach ($match in [regex]::Matches($Html, '(?is)<input[^>]+>')) {
        $tag = $match.Value
        if ($tag -notmatch '(?i)type\s*=\s*["'']hidden["'']') { continue }
        $nameMatch = [regex]::Match($tag, '(?i)name\s*=\s*["'']([^"'']+)["'']')
        if (-not $nameMatch.Success) { continue }
        $valueMatch = [regex]::Match($tag, '(?i)value\s*=\s*["'']([^"'']*)["'']')
        $fields[$nameMatch.Groups[1].Value] = if ($valueMatch.Success) { $valueMatch.Groups[1].Value } else { '' }
    }
    return $fields
}

function Invoke-Login {
    $auth = $settings.auth
    if ($auth.mode -eq 'none' -or -not $auth.login_url) {
        return [ordered]@{ status = 'skipped'; reason = '未配置 auth.login_url（config.json 的 auth 段）' }
    }
    if ($auth.mode -eq 'manual') {
        # 统一认证带图形验证码/JS 加密密码时，只能人工在浏览器完成
        $target = $auth.login_url
        if ($auth.manual_open_url) { $target = [string]$auth.manual_open_url }
        $opened = $false
        if ($auth.open_browser_on_manual) {
            try { Start-Process $target; $opened = $true } catch { }
        }
        return [ordered]@{
            status = 'manual_required'
            reason = '学校统一认证需要人工完成（验证码/扫码/页面 JS 加密密码），agent 不代填'
            login_url = $target
            browser_opened = $opened
            hint = $auth.manual_verify_hint
            next_step = '请在浏览器完成登录并保持登录状态，回复“已登录”后在同一浏览器访问目标论文核验权限。'
        }
    }
    $cred = Get-CredentialPair
    if (-not $cred) {
        return [ordered]@{
            status = 'manual_required'
            reason = '没有可用凭据：设置 CAMPUS_USERNAME / CAMPUS_PASSWORD 环境变量，或先运行 -Action save-cred'
            hint = '也可以在浏览器里手动登录，再运行 -Action status 验证'
        }
    }

    $loginPage = Test-Url $auth.login_url
    if (-not $loginPage.ok) {
        return [ordered]@{ status = 'login_page_failed'; url = $auth.login_url; error = $loginPage.error; status_code = $loginPage.status }
    }
    # 登录页的隐藏字段/验证码往往在 1200 字符之后，这里用完整正文解析
    $pageBody = if ($loginPage.body_full) { [string]$loginPage.body_full } else { [string]$loginPage.body_head }
    $lower = $pageBody.ToLowerInvariant()
    foreach ($marker in @($auth.captcha_markers)) {
        if ($marker -and $lower.Contains(([string]$marker).ToLowerInvariant())) {
            return [ordered]@{
                status = 'manual_required'
                reason = '登录页疑似需要验证码/二次验证（命中 captcha 标记：' + $marker + '）'
                hint = $auth.manual_verify_hint
                login_url = $auth.login_url
            }
        }
    }

    $hidden = Get-HiddenFormFields $pageBody
    foreach ($name in @($auth.hidden_fields)) {
        if ($name -and -not $hidden.ContainsKey($name)) {
            return [ordered]@{
                status = 'config_incomplete'
                reason = "登录页里没找到隐藏字段 '$name'，请检查 auth.login_url 是否为登录表单页"
                hidden_fields_found = @($hidden.Keys)
            }
        }
    }

    $form = @{}
    foreach ($key in $hidden.Keys) { $form[$key] = $hidden[$key] }
    $extraFields = $auth.extra_fields
    if ($extraFields -is [System.Collections.IDictionary]) {
        foreach ($key in $extraFields.Keys) { $form[$key] = [string]$extraFields[$key] }
    } elseif ($extraFields) {
        foreach ($prop in $extraFields.PSObject.Properties) { $form[$prop.Name] = [string]$prop.Value }
    }
    $form[$auth.username_field] = $cred.username
    $form[$auth.password_field] = $cred.password

    $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
    $body = ($form.GetEnumerator() | ForEach-Object {
        '{0}={1}' -f [Uri]::EscapeDataString([string]$_.Key), [Uri]::EscapeDataString([string]$_.Value)
    }) -join '&'
    $result = [ordered]@{
        status = 'unknown'; login_url = $auth.login_url; final_url = $null
        status_code = $null; credential_source = $cred.source; error = $null
    }
    try {
        $response = Invoke-WebRequest -Uri $auth.login_url -Method Post -Body $body -UseBasicParsing `
            -ContentType 'application/x-www-form-urlencoded' -WebSession $session -MaximumRedirection 10 -TimeoutSec 30 `
            -UserAgent 'Mozilla/5.0 campus-library-papers login helper'
        $result.status_code = [int]$response.StatusCode
        $result.final_url = Get-FinalUrl $response $auth.login_url
        $text = if ($response.Content -is [byte[]]) { [Text.Encoding]::UTF8.GetString($response.Content) } else { [string]$response.Content }
    } catch [System.Net.WebException] {
        $response = $_.Exception.Response
        if ($response) { $result.status_code = [int]$response.StatusCode }
        $result.error = $_.Exception.Message
        $text = ''
    } catch {
        $result.error = $_.Exception.Message
        $text = ''
    }

    $success = $false
    foreach ($pattern in @($auth.success_url_contains)) {
        if ($pattern -and $result.final_url -and $result.final_url -match [regex]::Escape([string]$pattern)) { $success = $true }
    }
    foreach ($pattern in @($auth.success_body_contains)) {
        if ($pattern -and $text -match [regex]::Escape([string]$pattern)) { $success = $true }
    }
    foreach ($pattern in @($auth.failure_body_contains)) {
        if ($pattern -and $text -match [regex]::Escape([string]$pattern)) { $success = $false; break }
    }
    $result.status = if ($success) { 'success' } elseif ($result.error) { 'error' } else { 'unverified' }
    $result.next_step = switch ($result.status) {
        'success' { '运行 -Action status 确认；之后直接访问数据库时若仍跳登录，检查成功判定配置或改用浏览器人工登录。' }
        'unverified' { '登录请求已提交但无法判定结果：请用浏览器打开校园网首页确认，再运行 -Action status。' }
        default { '登录失败：核对 auth 字段名、隐藏字段、验证码；必要时改为浏览器人工登录。' }
    }
    return $result
}

# ---------------------------------------------------------------- WebVPN 重写
function Resolve-Python {
    foreach ($candidate in @($env:DSH_PYTHON, $env:PYTHON, 'python', 'py')) {
        if (-not $candidate) { continue }
        try {
            $cmd = Get-Command $candidate -ErrorAction Stop
            return $cmd.Source
        } catch { }
    }
    return $null
}

function Invoke-WebVpnRewrite {
    if (-not $Url) { return [ordered]@{ status = 'error'; reason = '缺少 -Url 参数' } }
    $script = Join-Path $ScriptDir 'webvpn_url.py'
    if (-not (Test-Path -LiteralPath $script)) { return [ordered]@{ status = 'error'; reason = "缺少脚本：$script" } }
    $python = Resolve-Python
    if (-not $python) {
        return [ordered]@{ status = 'error'; reason = '找不到 python，请设置 PYTHON 或 DSH_PYTHON 环境变量' }
    }
    $base = [string]$settings.webvpn.base
    if (-not $base) { return [ordered]@{ status = 'error'; reason = 'config.json 的 webvpn.base 为空' } }
    $output = & $python $script --base $base $Url 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        return [ordered]@{ status = 'error'; reason = ($output -join "`n"); exit_code = $code }
    }
    return [ordered]@{ status = 'ok'; source = $Url; webvpn = ($output | Select-Object -Last 1); gateway = $base }
}

# ---------------------------------------------------------------- 主流程
$exitCode = 0
switch ($Action) {
    'status' {
        $status = Get-NetworkStatus
        $human = @(
            "网络状态：",
            "  on_campus        = $($status.on_campus)",
            "  vpn_connected    = $($status.vpn_connected)",
            "  captive_portal   = $($status.captive_portal_detected)",
            "  recommendation   = $($status.recommendation)",
            "  公网 IP          = $($status.public_ip.ip)",
            "  本地 IPv4        = $(@($status.local_ipv4 | ForEach-Object { $_.IPAddress }) -join ', ')"
        ) -join "`n"
        Write-Result $status $human
    }
    'login' {
        $result = Invoke-Login
        Write-Result $result "登录结果：$($result.status) $($result.reason)$($result.hint)$($result.next_step)"
        if ($result.status -in @('login_page_failed', 'error')) { $exitCode = 1 }
    }
    'save-cred' {
        Save-DpapiCredential
    }
    'vpn-url' {
        $result = Invoke-WebVpnRewrite
        Write-Result $result $(if ($result.webvpn) { $result.webvpn } else { $result.reason })
        if ($result.status -ne 'ok') { $exitCode = 2 }
    }
    'open' {
        if (-not $Url) {
            Write-Result ([ordered]@{ status = 'error'; reason = '缺少 -Url 参数' })
            $exitCode = 2
        } else {
            $target = $Url
            if ($Vpn) {
                $rewritten = Invoke-WebVpnRewrite
                if ($rewritten.status -ne 'ok') {
                    Write-Result ([ordered]@{ status = 'error'; reason = "WebVPN 重写失败：$($rewritten.reason)" })
                    $exitCode = 2
                    break
                }
                $target = [string]$rewritten.webvpn
            }
            try {
                Start-Process $target
                Write-Result ([ordered]@{ status = 'opened'; url = $target; vpn = [bool]$Vpn }) "已在默认浏览器打开：$target"
            } catch {
                Write-Result ([ordered]@{ status = 'error'; reason = $_.Exception.Message; url = $target })
                $exitCode = 1
            }
        }
    }
}
exit $exitCode
