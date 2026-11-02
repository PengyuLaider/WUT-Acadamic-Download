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
  powershell -NoProfile -ExecutionPolicy Bypass -File campus_net.ps1 -Action status -Json -UseSystemProxy
  powershell -NoProfile -ExecutionPolicy Bypass -File campus_net.ps1 -Action vpn-url -Url "https://kns.cnki.net/" -Json
  powershell -NoProfile -ExecutionPolicy Bypass -File campus_net.ps1 -Action open -Url "https://doi.org/10.1038/nature12373" -Vpn

.NOTES
  status 的校园网/公网 IP 探测默认直连（HttpWebRequest.Proxy=$null），绕过 Clash 等
  Windows 系统代理；需要走系统代理时显式加 -UseSystemProxy。
#>
[CmdletBinding()]
param(
    [ValidateSet('status', 'login', 'vpn-url', 'open', 'carsi', 'save-cred')]
    [string]$Action = 'status',

    [string]$Config = '',
    [string]$Url = '',
    [switch]$Vpn,
    # status 探测默认绕过 Clash/系统代理；加此开关恢复走系统代理（会把出口判成代理节点）
    [switch]$UseSystemProxy,
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

# status 探测的代理偏好：-UseSystemProxy 显式指定时优先；否则由 config 的 proxy_mode 决定
# （默认 direct，直连探测，避免 Clash 等系统代理把出口 IP 变成东京节点导致校园网误判）。
$script:UseSystemProxyPreference = if ($PSBoundParameters.ContainsKey('UseSystemProxy')) { [bool]$UseSystemProxy } else { $null }

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
    # 内置 WHUT/CERNET 网段（与 Get-BuiltinCampusCidrs 一致）；campus_cidrs 支持 config 覆盖/追加
    campus_cidrs = @('202.114.48.0/20', '202.114.88.0/21', '218.197.96.0/19')
    campus_cidrs_mode = 'append'            # append = 内置网段 + 本列表；replace = 只用本列表
    campus_private_cidrs = @('10.82.0.0/16', '10.83.0.0/16')  # 校园 LAN 私网（弱证据）
    campus_private_cidrs_mode = 'append'    # 同上；私网网段只作弱证据
    proxy_mode = 'direct'                   # direct = status 探测直连；system = 走系统代理；-UseSystemProxy 优先
    cernet = [ordered]@{
        lookup_enabled = $true
        timeout_seconds = 8                 # 单次查询超时上限 8s；失败只记录证据，不影响判定
        lookup_urls = @('https://ipinfo.io/{ip}/json', 'https://api.bgpview.io/ip/{ip}')
        expected_asns = @(4538)             # CERNET AS4538
        org_markers = @('CERNET', 'China Education and Research Network', '教育网', '中国教育和科研计算机网')
    }
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

# ---------------------------------------------------------------- 通用取值 / 校园网判定辅助
function Get-SettingValue([string]$Name, $Default = $null) {
    try {
        if ($null -ne $settings -and $settings -is [System.Collections.IDictionary] -and $settings.Contains($Name)) {
            return $settings[$Name]
        }
        if ($null -ne $settings) {
            $prop = $settings.PSObject.Properties[$Name]
            if ($prop) { return $prop.Value }
        }
    } catch { }
    return $Default
}

function Get-SectionSetting([string]$Section, [string]$Name, $Default = $null) {
    $sec = Get-SettingValue $Section $null
    if ($null -eq $sec) { return $Default }
    try {
        if ($sec -is [System.Collections.IDictionary] -and $sec.Contains($Name)) { return $sec[$Name] }
        $prop = $sec.PSObject.Properties[$Name]
        if ($prop) { return $prop.Value }
    } catch { }
    return $Default
}

function Get-BuiltinCampusCidrs {
    # WHUT/CERNET 内置网段：覆盖 202.114.56.2、202.114.89.79、202.114.49.228、
    # 218.197.100.87、218.197.104.131、218.197.111.6 等已知学校地址。
    return @('202.114.48.0/20', '202.114.88.0/21', '218.197.96.0/19')
}

function Get-BuiltinCampusPrivateCidrs {
    # 校园 LAN 私网：本机网关 10.83.255.254，图书馆看到的 rqIp 常见 10.82.x。
    # 私网可能与外单位重叠，仅作弱证据（on_campus_reason=campus_lan_private / library_rqip）。
    return @('10.82.0.0/16', '10.83.0.0/16')
}

function Merge-CampusCidrList($Builtin, $User, [string]$Mode) {
    $builtinList = @()
    foreach ($c in @($Builtin)) {
        if ($c -and ($builtinList -notcontains [string]$c)) { $builtinList += [string]$c }
    }
    $userList = @()
    foreach ($c in @($User)) {
        if ($c -and ($userList -notcontains [string]$c)) { $userList += [string]$c }
    }
    if ($Mode -eq 'replace') {
        if ($userList.Count -gt 0) { return $userList }
        return @()
    }
    $merged = @($builtinList)
    foreach ($c in $userList) {
        if ($merged -notcontains $c) { $merged += $c }
    }
    return $merged
}

function Get-CampusCidrSet {
    $mode = [string](Get-SettingValue 'campus_cidrs_mode' 'append')
    $user = Get-SettingValue 'campus_cidrs' @()
    return @(Merge-CampusCidrList (Get-BuiltinCampusCidrs) $user $mode)
}

function Get-CampusPrivateCidrSet {
    $mode = [string](Get-SettingValue 'campus_private_cidrs_mode' 'append')
    $user = Get-SettingValue 'campus_private_cidrs' @()
    return @(Merge-CampusCidrList (Get-BuiltinCampusPrivateCidrs) $user $mode)
}

function Get-UseSystemProxy {
    # 优先级：-UseSystemProxy / 脚本变量 > config.proxy_mode > 默认直连
    try {
        $existing = Get-Variable -Name UseSystemProxyPreference -Scope Script -ErrorAction SilentlyContinue
        if ($existing -and $null -ne $existing.Value) { return [bool]$existing.Value }
    } catch { }
    return ([string](Get-SettingValue 'proxy_mode' 'direct') -eq 'system')
}

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

function Read-HttpResponseText($Response, [int]$MaxChars = 1048576) {
    $text = ''
    if ($null -eq $Response) { return $text }
    $stream = $null
    $reader = $null
    try {
        $stream = $Response.GetResponseStream()
        if ($null -eq $stream) { return $text }
        $encoding = [Text.Encoding]::UTF8
        try {
            $charset = [string]$Response.CharacterSet
            if ($charset) { $encoding = [Text.Encoding]::GetEncoding($charset) }
        } catch { $encoding = [Text.Encoding]::UTF8 }
        $reader = New-Object System.IO.StreamReader($stream, $encoding)
        $text = $reader.ReadToEnd()
        if ($text.Length -gt $MaxChars) { $text = $text.Substring(0, $MaxChars) }
    } catch {
    } finally {
        if ($reader) { try { $reader.Dispose() } catch { } }
        if ($stream) { try { $stream.Dispose() } catch { } }
    }
    return $text
}

function New-HttpProbeResult([string]$Uri) {
    return [ordered]@{
        url = $Uri; ok = $false; status = $null; final_url = $null; redirected = $false
        body = ''; error = $null; used_system_proxy = $false; elapsed_ms = $null
    }
}

function Invoke-DirectHttpRequest {
    <#
      status 探测用 GET。
      默认 Proxy=$null 直连，绕过 Clash / WinINET 系统代理（否则出口 IP 会变成代理节点，
      校园网判定永远失败）；-UseProxy 显式恢复系统代理。
      不抛异常：失败时 ok=$false、error 有文本，调用方据此优雅降级。
    #>
    [CmdletBinding()]
    param(
        [string]$Uri,
        [int]$TimeoutSec = 12,
        [switch]$UseProxy,
        [int]$MaxRedirections = 8,
        [string]$UserAgent = 'Mozilla/5.0 campus-library-papers status probe'
    )
    $result = New-HttpProbeResult $Uri
    if ([string]::IsNullOrWhiteSpace($Uri)) { $result.error = 'empty uri'; return $result }
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $request = $null
    $response = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create($Uri)
        $request.Method = 'GET'
        $request.UserAgent = $UserAgent
        $request.AllowAutoRedirect = $true
        $request.MaximumAutomaticRedirections = [Math]::Max(0, $MaxRedirections)
        $timeoutMs = [int][Math]::Max(1000, $TimeoutSec * 1000)
        $request.Timeout = $timeoutMs
        $request.ReadWriteTimeout = $timeoutMs
        try {
            $request.AutomaticDecompression = [System.Net.DecompressionMethods]::GZip -bor [System.Net.DecompressionMethods]::Deflate
        } catch { }
        if ($UseProxy) {
            # 显式恢复系统代理（IE/WinINET 设置，Clash 等通常改这里）
            try {
                $proxy = [System.Net.WebRequest]::GetSystemWebProxy()
                if ($proxy) {
                    try { $proxy.Credentials = [System.Net.CredentialCache]::DefaultCredentials } catch { }
                    $request.Proxy = $proxy
                } else {
                    $request.Proxy = [System.Net.WebRequest]::DefaultWebProxy
                }
            } catch { }
            $result.used_system_proxy = $true
        } else {
            # Proxy=$null 即直连：不读取 IE/Clash 系统代理设置
            $request.Proxy = $null
        }
        $response = $request.GetResponse()
        $result.status = [int]$response.StatusCode
        try { $result.final_url = $response.ResponseUri.AbsoluteUri } catch { }
        $result.body = Read-HttpResponseText $response
        $result.ok = ($result.status -ge 200 -and $result.status -lt 400)
    } catch [System.Net.WebException] {
        $webResponse = $_.Exception.Response
        if ($webResponse) {
            $response = $webResponse
            try { $result.status = [int]$webResponse.StatusCode } catch { }
            try { $result.final_url = $webResponse.ResponseUri.AbsoluteUri } catch { }
            $result.body = Read-HttpResponseText $webResponse
        }
        $result.error = $_.Exception.Message
    } catch {
        $result.error = $_.Exception.Message
    } finally {
        if ($response) { try { $response.Close() } catch { } }
        $watch.Stop()
        $result.elapsed_ms = [int]$watch.ElapsedMilliseconds
    }
    if ($result.final_url) {
        try {
            $a = [Uri]$Uri
            $b = [Uri]$result.final_url
            $result.redirected = ($a.Scheme -ne $b.Scheme) -or ($a.Host -ne $b.Host) -or ($a.AbsolutePath -ne $b.AbsolutePath)
        } catch { }
    }
    return $result
}

function Test-Url([string]$Target, [int]$TimeoutSec = 20) {
    # 保持既有返回结构；底层改为可直连的 Invoke-DirectHttpRequest（默认绕过系统代理）
    $useProxy = Get-UseSystemProxy
    $probe = Invoke-DirectHttpRequest -Uri $Target -TimeoutSec $TimeoutSec -UseProxy:$useProxy
    $content = [string]$probe.body
    if ($null -eq $content) { $content = '' }
    return [ordered]@{
        url = $Target
        ok = [bool]$probe.ok
        status = $probe.status
        final_url = $probe.final_url
        redirected = [bool]$probe.redirected
        error = $probe.error
        body_head = $content.Substring(0, [Math]::Min(1200, $content.Length))
        # body_full 供登录流程解析隐藏字段/验证码；限制 512 KB 防止异常大页面
        body_full = $content.Substring(0, [Math]::Min(524288, $content.Length))
        elapsed_ms = $probe.elapsed_ms
    }
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
    # 公网 IP 探测：默认直连（绕过 Clash 等系统代理），失败时优雅返回 ok=$false
    $useProxy = Get-UseSystemProxy
    $ipv4Pattern = '(\d{1,3}(?:\.\d{1,3}){3})'
    $ipv6Pattern = '((?:[0-9a-fA-F]{1,4}:){2,}[0-9a-fA-F]{1,4})'
    foreach ($probe in @(Get-SettingValue 'public_ip_probes' @())) {
        if (-not $probe) { continue }
        try {
            $item = [ordered]@{
                probe = [string]$probe; ip = $null; family = $null; ok = $false
                source = 'probe'; error = $null; used_system_proxy = [bool]$useProxy
            }
            $res = Invoke-DirectHttpRequest -Uri ([string]$probe) -TimeoutSec 12 -UseProxy:$useProxy
            $item.used_system_proxy = [bool]$res.used_system_proxy
            if (-not $res.ok) {
                $item.error = if ($res.error) { [string]$res.error } else { "http_status_$($res.status)" }
                continue
            }
            $text = ([string]$res.body).Trim()
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
    return [ordered]@{
        probe = $null; ip = $null; family = $null; ok = $false
        source = 'probe'; error = 'all public IP probes failed'; used_system_proxy = [bool]$useProxy
    }
}

function Get-CaptivePortalState {
    # 强制门户探测同样直连（默认绕过系统代理）；失败只记录，不抛异常
    $useProxy = Get-UseSystemProxy
    $state = [ordered]@{ detected = $false; probes = @() }
    foreach ($probe in @(Get-SettingValue 'captive_portal_probes' @())) {
        if (-not $probe) { continue }
        $item = [ordered]@{ url = $probe.url; expected = $probe.expect; ok = $false; status = $null; redirected = $false; final_url = $null; error = $null }
        try {
            $res = Invoke-DirectHttpRequest -Uri ([string]$probe.url) -TimeoutSec 10 -MaxRedirections 5 -UseProxy:$useProxy
            $item.status = $res.status
            $item.final_url = $res.final_url
            $item.error = $res.error
            $text = [string]$res.body
            if ($probe.expect -eq '204') {
                $item.ok = ($item.status -eq 204)
            } else {
                $item.ok = ($text -match [regex]::Escape([string]$probe.expect))
            }
            if ($item.final_url -and $item.final_url -notmatch [regex]::Escape(([Uri]$probe.url).Host)) { $item.redirected = $true }
            # 强制门户常以 3xx + 登录页形式出现（允许自动重定向时可能以 200 落地）
            if ($item.status -in @(301, 302, 303, 307, 308)) { $item.redirected = $true }
        } catch {
            $item.error = $_.Exception.Message
        }
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

function Test-NonPublicIpv4([string]$Ip) {
    # 私网 / 保留 / 文档测试网段：不是真实公网出口，不做 CERNET AS 查询
    try {
        $bytes = [System.Net.IPAddress]::Parse($Ip).GetAddressBytes()
        if ($bytes.Length -ne 4) { return $false }
        if ($bytes[0] -eq 10) { return $true }
        if ($bytes[0] -eq 127) { return $true }
        if ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) { return $true }
        if ($bytes[0] -eq 192 -and $bytes[1] -eq 168) { return $true }
        if ($bytes[0] -eq 169 -and $bytes[1] -eq 254) { return $true }
        if ($bytes[0] -eq 100 -and $bytes[1] -ge 64 -and $bytes[1] -le 127) { return $true }
        if ($bytes[0] -eq 192 -and $bytes[1] -eq 0 -and $bytes[2] -eq 2) { return $true }   # TEST-NET-1
        if ($bytes[0] -eq 198 -and $bytes[1] -eq 51 -and $bytes[2] -eq 100) { return $true } # TEST-NET-2
        if ($bytes[0] -eq 203 -and $bytes[1] -eq 0 -and $bytes[2] -eq 113) { return $true }   # TEST-NET-3
        return $false
    } catch { return $false }
}

function Get-RqIpFromText([string]$Text) {
    # 图书馆页面常由服务端写入 rqIp（如 var rqIp = '10.82.x.x'），是校园内部出口的弱证据
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $match = [regex]::Match($Text, 'rqIp\s*[=:]\s*[''"]([^''"]+)[''"]')
    if ($match.Success) { return $match.Groups[1].Value }
    return $null
}

function ConvertFrom-CernetPayload {
    # 从 ipinfo.io / api.bgpview.io 的响应里提取 ASN 与组织名（解析失败不抛异常）
    param([string]$Text)
    $info = [ordered]@{ asn = $null; asn_name = $null; org = $null; parse = 'none'; error = $null }
    if ([string]::IsNullOrWhiteSpace($Text)) { return $info }
    $json = $null
    try {
        $json = $Text.Trim() | ConvertFrom-Json -ErrorAction Stop
        $info.parse = 'json'
    } catch {
        $info.error = $_.Exception.Message
    }
    if ($json) {
        # ipinfo.io: {"org":"AS4538 China Education and Research Network Center"}
        try {
            $org = [string]$json.org
            if ($org) {
                $info.org = $org
                $match = [regex]::Match($org, 'AS\s?(\d{2,6})')
                if ($match.Success) {
                    $info.asn = [int]$match.Groups[1].Value
                    $info.asn_name = ($org -replace '^\s*AS\s?\d{2,6}\s*', '').Trim()
                }
            }
        } catch { }
        # 某些接口把 asn 放在对象里：{"asn":{"asn":4538,"name":"..."}}
        if (-not $info.asn) {
            try {
                $asnValue = $json.asn
                if ($null -ne $asnValue) {
                    if ($asnValue -is [string] -or $asnValue -is [int] -or $asnValue -is [long]) {
                        $match = [regex]::Match([string]$asnValue, 'AS?\s?(\d{2,6})')
                        if ($match.Success) { $info.asn = [int]$match.Groups[1].Value }
                    } else {
                        try { if ($asnValue.asn) { $info.asn = [int]$asnValue.asn } } catch { }
                        try { if (-not $info.asn_name) { $info.asn_name = [string]$asnValue.name } } catch { }
                    }
                }
            } catch { }
        }
        # api.bgpview.io: {"data":{"prefixes":[{"asn":{"asn":4538,"name":"CERNET"}}]}}
        if (-not $info.asn) {
            try {
                foreach ($prefix in @($json.data.prefixes)) {
                    if ($null -eq $prefix) { continue }
                    try { if ($prefix.asn.asn) { $info.asn = [int]$prefix.asn.asn } } catch { }
                    if ($info.asn) {
                        try { $info.asn_name = [string]$prefix.asn.name } catch { }
                        if (-not $info.asn_name) { try { $info.asn_name = [string]$prefix.asn.description } catch { } }
                        break
                    }
                }
            } catch { }
        }
        if ($info.asn -and -not $info.asn_name) {
            foreach ($name in @('name', 'description', 'asn_name')) {
                try {
                    $value = [string]$json.$name
                    if ($value) { $info.asn_name = $value; break }
                } catch { }
            }
        }
    }
    if (-not $info.asn) {
        # 兜底：纯文本里的 AS 号
        $match = [regex]::Match($Text, 'AS\s?(\d{2,6})')
        if ($match.Success) { $info.asn = [int]$match.Groups[1].Value }
        if (-not $info.org) {
            $matchOrg = [regex]::Match($Text, '"org"\s*:\s*"([^"]+)"')
            if ($matchOrg.Success) { $info.org = $matchOrg.Groups[1].Value }
        }
    }
    return $info
}

function Get-CernetLookupTimeout {
    # 验收要求单次查询超时 ≤8s
    $timeout = 8
    try {
        $raw = Get-SectionSetting 'cernet' 'timeout_seconds' 8
        if ($raw) { $timeout = [int]$raw }
    } catch { $timeout = 8 }
    if ($timeout -le 0) { $timeout = 8 }
    if ($timeout -gt 8) { $timeout = 8 }
    return $timeout
}

function Get-CernetEvidence([string]$Ip) {
    # 公网 IP 的 CERNET AS 证据：仅作判定证据，任何失败都返回 null 而不是抛异常
    $evidence = [ordered]@{
        checked = $false; ip = $Ip; asn = $null; asn_name = $null; org = $null
        is_cernet = $null; source = $null; error = $null; lookups = @(); elapsed_ms = $null
    }
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        if ([string]::IsNullOrWhiteSpace($Ip)) { $evidence.error = 'no_public_ip'; return $evidence }
        if ($Ip -match ':') { $evidence.error = 'ipv6_lookup_not_supported'; return $evidence }
        if ($Ip -notmatch '^\d{1,3}(?:\.\d{1,3}){3}$') { $evidence.error = 'invalid_ip'; return $evidence }
        if (Test-NonPublicIpv4 $Ip) { $evidence.error = 'private_or_reserved_ip'; return $evidence }
        if (-not [bool](Get-SectionSetting 'cernet' 'lookup_enabled' $true)) {
            $evidence.error = 'lookup_disabled'
            return $evidence
        }
        $timeout = Get-CernetLookupTimeout
        $urls = @(Get-SectionSetting 'cernet' 'lookup_urls' @('https://ipinfo.io/{ip}/json', 'https://api.bgpview.io/ip/{ip}'))
        $expectedAsns = @()
        foreach ($value in @(Get-SectionSetting 'cernet' 'expected_asns' @(4538))) {
            try { if ($null -ne $value) { $expectedAsns += [int]$value } } catch { }
        }
        if ($expectedAsns.Count -eq 0) { $expectedAsns = @(4538) }
        $markers = @(Get-SectionSetting 'cernet' 'org_markers' @('CERNET', 'China Education and Research Network', '教育网', '中国教育和科研计算机网'))
        $errors = @()
        foreach ($template in $urls) {
            if (-not $template) { continue }
            $url = ([string]$template).Replace('{ip}', $Ip)
            $lookup = [ordered]@{ url = $url; ok = $false; status = $null; asn = $null; asn_name = $null; error = $null }
            try {
                # 必须直连：走系统代理会拿到代理节点的 AS，证据无效
                $res = Invoke-DirectHttpRequest -Uri $url -TimeoutSec $timeout -UseProxy:$false
                $lookup.status = $res.status
                if ($res.ok) {
                    $parsed = ConvertFrom-CernetPayload ([string]$res.body)
                    $lookup.asn = $parsed.asn
                    $lookup.asn_name = $parsed.asn_name
                    if ($parsed.asn) {
                        $lookup.ok = $true
                        $evidence.asn = $parsed.asn
                        $evidence.asn_name = $parsed.asn_name
                        $evidence.org = $parsed.org
                        $evidence.source = $url
                    } else {
                        $lookup.error = 'asn_not_found'
                        $errors += "$url => asn_not_found"
                    }
                } else {
                    $lookup.error = if ($res.error) { [string]$res.error } else { "http_status_$($res.status)" }
                    $errors += "$url => $($lookup.error)"
                }
            } catch {
                $lookup.error = $_.Exception.Message
                $errors += "$url => $($lookup.error)"
            }
            $evidence.lookups += $lookup
            if ($evidence.asn) { break }
        }
        $evidence.checked = $true
        if ($evidence.asn) {
            $asnNumber = [int]$evidence.asn
            $isCernet = $null
            if ($expectedAsns -contains $asnNumber) { $isCernet = $true }
            if ($isCernet -ne $true -and $evidence.asn_name) {
                foreach ($marker in $markers) {
                    if ($marker -and ([string]$evidence.asn_name).ToLowerInvariant().Contains(([string]$marker).ToLowerInvariant())) {
                        $isCernet = $true
                        break
                    }
                }
            }
            if ($null -eq $isCernet) { $isCernet = $false }  # ASN 已知且不是 CERNET
            $evidence.is_cernet = $isCernet
        } else {
            $evidence.error = if ($errors.Count -gt 0) { ($errors -join '; ') } else { 'lookup_failed' }
        }
    } catch {
        $evidence.error = $_.Exception.Message
    } finally {
        $watch.Stop()
        $evidence.elapsed_ms = [int]$watch.ElapsedMilliseconds
    }
    return $evidence
}

function Format-StatusValue($Value) {
    # 人类可读输出：空值统一显示 unknown，不留下空白
    if ($null -eq $Value) { return 'unknown' }
    if ($Value -is [bool]) { if ($Value) { return 'true' } return 'false' }
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) { return 'unknown' }
    return $text
}

function Ensure-ResultField($Object, [string]$Name, $Default = $null) {
    if ($null -eq $Object) { return }
    try {
        if ($Object -is [System.Collections.IDictionary]) {
            if (-not $Object.Contains($Name)) { $Object[$Name] = $Default }
        } elseif ($null -eq $Object.PSObject.Properties[$Name]) {
            $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Default -Force
        }
    } catch { }
}

function Invoke-WebVpnProbe {
    # 用一条重写 URL 探测 WebVPN 是否已登录：302 -> /login 表示未登录；200 表示已登录。
    # 关键：未登录访问 /login 也会下发同名 ticket，所以不能只看 Cookie 是否存在。
    $probe = [ordered]@{
        target = $null; rewritten = $null; status = $null; redirect = $null
        logged_in = $null; method = $null; error = $null
    }
    $target = [string](Get-SectionSetting 'webvpn' 'probe_target' '')
    if (-not $target) { return $probe }
    $probe.target = $target
    $script = Join-Path $ScriptDir 'webvpn_url.py'
    $python = Resolve-Python
    if (-not $python -or -not (Test-Path -LiteralPath $script)) {
        $probe.error = '需要 python 生成 WebVPN 探测 URL（设置 PYTHON 或 DSH_PYTHON）'
        return $probe
    }
    try {
        $out = & $python $script --base ([string](Get-SectionSetting 'webvpn' 'base' '')) $target 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { $probe.error = 'webvpn_url.py 生成探测 URL 失败'; return $probe }
        $probe.rewritten = ([string]($out | Select-Object -Last 1)).Trim()
    } catch {
        $probe.error = $_.Exception.Message
        return $probe
    }
    if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
        try {
            $curlArgs = @('-s', '-o', 'NUL', '-w', '%{http_code}|%{redirect_url}', '--max-time', '20')
            # 与 status 探测保持一致：默认直连，忽略 HTTP(S)_PROXY 环境变量
            if (-not (Get-UseSystemProxy)) { $curlArgs += '--noproxy'; $curlArgs += '*' }
            $curlArgs += $probe.rewritten
            $raw = & curl.exe @curlArgs 2>$null
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
    $useProxy = Get-UseSystemProxy
    $effectiveCidrs = @(Get-CampusCidrSet)
    $effectivePrivateCidrs = @(Get-CampusPrivateCidrSet)

    $localIps = @(Get-LocalIPv4)
    $cidrHits = @()
    $privateHits = @()
    foreach ($entry in $localIps) {
        if (-not $entry -or -not $entry.IPAddress) { continue }
        $ip = [string]$entry.IPAddress
        foreach ($cidr in $effectiveCidrs) {
            if ($cidr -and (Test-CidrContains $ip ([string]$cidr))) {
                $cidrHits += [ordered]@{ ip = $ip; cidr = [string]$cidr; iface = $entry.InterfaceAlias; source = 'local_ip' }
            }
        }
        foreach ($cidr in $effectivePrivateCidrs) {
            if ($cidr -and (Test-CidrContains $ip ([string]$cidr))) {
                $privateHits += [ordered]@{ ip = $ip; cidr = [string]$cidr; iface = $entry.InterfaceAlias; source = 'local_ip' }
            }
        }
    }

    $probes = @()
    foreach ($probe in @(Get-SettingValue 'on_campus_probes' @())) {
        if ($probe -and $probe.url) { $probes += (Test-CampusProbe $probe) }
    }

    $captive = Get-CaptivePortalState
    if ($null -eq $captive) { $captive = [ordered]@{ detected = $false; probes = @() } }
    Ensure-ResultField $captive 'detected' $false
    Ensure-ResultField $captive 'probes' @()
    $captiveDetected = $false
    try { $captiveDetected = [bool]$captive.detected } catch { }
    $captiveProbes = @()
    try { if ($captive.probes) { $captiveProbes = @($captive.probes) } } catch { }

    $publicIp = Get-PublicIP
    if ($null -eq $publicIp) { $publicIp = [ordered]@{} }
    foreach ($field in @('probe', 'ip', 'family', 'ok', 'source', 'error', 'used_system_proxy')) {
        $default = $false
        if ($field -in @('probe', 'ip', 'family', 'source', 'error')) { $default = $null }
        Ensure-ResultField $publicIp $field $default
    }

    # 公网 IP 探测都失败时，回退读图书馆页面服务端写入的 rqIp（弱证据，但通常就是出口 IP）
    $rqIpSource = $null
    if (-not [bool]$publicIp.ok) {
        foreach ($probe in $probes) {
            if (-not $probe.url) { continue }
            $pageText = [string]$probe.body_head
            $rqIp = Get-RqIpFromText $pageText
            if (-not $rqIp) {
                # rqIp 常出现在 1200 字符之后：必要时重新抓整页
                $page = Test-Url $probe.url
                $rqIp = Get-RqIpFromText ([string]$page.body_full)
            }
            if ($rqIp) {
                $publicIp = [ordered]@{
                    probe = $probe.url; ip = $rqIp; ok = $true
                    family = if ($rqIp -match ':') { 'IPv6' } else { 'IPv4' }
                    source = 'probe-rqIp'; error = $null; used_system_proxy = [bool]$useProxy
                }
                $rqIpSource = $probe.url
                break
            }
        }
    }

    $publicIpText = if ([bool]$publicIp.ok) { [string]$publicIp.ip } else { '' }
    $publicIpPrivate = [bool]($publicIpText -and (Test-NonPublicIpv4 $publicIpText))
    $publicCidrHits = @()
    $rqIpHit = $null
    if ($publicIpText) {
        foreach ($cidr in $effectiveCidrs) {
            if (Test-CidrContains $publicIpText ([string]$cidr)) {
                $publicCidrHits += [ordered]@{ ip = $publicIpText; cidr = [string]$cidr; iface = ''; source = 'public_ip' }
            }
        }
        if ($publicIpPrivate) {
            foreach ($cidr in $effectivePrivateCidrs) {
                if (Test-CidrContains $publicIpText ([string]$cidr)) { $rqIpHit = [string]$cidr; break }
            }
        }
    }

    # CERNET AS 证据（仅公网 IP、仅直连；失败不影响判定）
    $cernet = [ordered]@{
        checked = $false; ip = $null; asn = $null; asn_name = $null; org = $null
        is_cernet = $null; source = $null; error = $null; lookups = @(); elapsed_ms = $null
    }
    if ($publicIpText) {
        $cernet.ip = $publicIpText
        if ($publicIpPrivate) {
            $cernet.error = 'private_or_reserved_ip'
        } elseif ($publicCidrHits.Count -gt 0) {
            # 先比 CIDR：已命中校园网段就无需再查 AS，节省一次网络往返
            $cernet.error = 'skipped_public_ip_cidr_hit'
        } else {
            $cernet = Get-CernetEvidence $publicIpText
        }
    } else {
        $cernet.error = 'no_public_ip'
    }
    if ($null -eq $cernet) { $cernet = [ordered]@{} }
    foreach ($field in @('checked', 'ip', 'asn', 'asn_name', 'org', 'is_cernet', 'source', 'error', 'lookups', 'elapsed_ms')) {
        $default = $null
        if ($field -eq 'checked') { $default = $false }
        if ($field -eq 'lookups') { $default = @() }
        Ensure-ResultField $cernet $field $default
    }

    # 代理状态：扩展既有结构，明确本机探测是否绕过系统代理
    $proxy = Get-ProxyState
    if ($null -eq $proxy) { $proxy = [ordered]@{} }
    foreach ($field in @('env_http_proxy', 'env_https_proxy', 'no_proxy', 'registry')) {
        Ensure-ResultField $proxy $field $null
    }
    $proxyMode = if ($useProxy) { 'system' } else { 'direct' }
    try {
        $proxy['use_system_proxy'] = [bool]$useProxy
        $proxy['mode'] = $proxyMode
        $proxy['bypassed'] = (-not $useProxy)
        $proxy['note'] = if ($useProxy) {
            'status 探测使用系统代理（可能把出口判成代理节点，校园网判定可能失效）'
        } else {
            'status 探测使用直连（HttpWebRequest.Proxy=$null），绕过 Clash 等系统代理'
        }
    } catch { }

    # WebVPN：保留独立 HTTP 会话语义，同时给出 vpn_state 文本
    $vpnEnabled = [bool](Get-SectionSetting 'webvpn' 'enabled' $false)
    $vpnBase = [string](Get-SectionSetting 'webvpn' 'base' '')
    $vpnCookieEnv = [string](Get-SectionSetting 'webvpn' 'cookie_env' '')
    $vpn = [ordered]@{
        enabled = $vpnEnabled; base = $vpnBase; reachable = $false; logged_in = $null
        status = $null; final_url = $null; error = $null; cookie_env_set = $false
        probe = $null; http_probe_logged_in = $null
    }
    $vpn.cookie_env_set = [bool]($vpnCookieEnv -and (Get-Item -Path "Env:$vpnCookieEnv" -ErrorAction SilentlyContinue))
    if ($vpnEnabled -and $vpnBase) {
        $vpnRes = Test-Url ($vpnBase.TrimEnd('/') + '/')
        $vpn.reachable = [bool]$vpnRes.ok
        $vpn.status = $vpnRes.status
        $vpn.final_url = $vpnRes.final_url
        $vpn.error = $vpnRes.error
        # 更强的判据：重写 URL 是否 302 到 /login
        $vpn.probe = Invoke-WebVpnProbe
        if ($vpn.probe) {
            try { $vpn.http_probe_logged_in = $vpn.probe.logged_in } catch { }
        }
        if ($null -ne $vpn.http_probe_logged_in) { $vpn.logged_in = $vpn.http_probe_logged_in }
    }
    # CLI 独立 HTTP 探测不能代表当前浏览器会话，保持既有 webvpn.logged_in=null 语义
    $vpn.logged_in = $null

    $carsiEnabled = [bool](Get-SectionSetting 'carsi' 'enabled' $false)
    $carsiPortal = [string](Get-SectionSetting 'carsi' 'portal' '')
    $carsi = [ordered]@{
        enabled = $carsiEnabled; portal = $carsiPortal
        idp_entity_id = [string](Get-SectionSetting 'carsi' 'idp_entity_id' ''); reachable = $null
    }
    if ($carsiEnabled -and $carsiPortal) {
        $carsiRes = Test-Url $carsiPortal
        $carsi.reachable = [bool]$carsiRes.ok
    }

    # ---------------- 校园网判定（on_campus / on_campus_reason）
    $authenticatedProbes = @($probes | Where-Object { $_.authenticated -eq $true })
    $loginWallProbes = @($probes | Where-Object { $_.login_wall -eq $true -or $_.authenticated -eq $false })
    $onCampus = $null
    $onCampusReason = 'unknown'
    $onCampusEvidence = @()
    if ($cidrHits.Count -gt 0) {
        $onCampus = $true; $onCampusReason = 'campus_cidr'
        $onCampusEvidence += "local_ip=$($cidrHits[0].ip) in $($cidrHits[0].cidr)"
    } elseif ($authenticatedProbes.Count -gt 0) {
        $onCampus = $true; $onCampusReason = 'probe'
        $onCampusEvidence += "probe_authenticated=$($authenticatedProbes[0].url)"
    } elseif ($publicCidrHits.Count -gt 0) {
        $onCampus = $true; $onCampusReason = 'public_ip_cidr'
        $onCampusEvidence += "public_ip=$publicIpText in $($publicCidrHits[0].cidr)"
    } elseif ($publicIpPrivate -and $rqIpHit) {
        $onCampus = $null; $onCampusReason = 'library_rqip'
        $onCampusEvidence += "rqIp=$publicIpText in $rqIpHit"
    } elseif ($privateHits.Count -gt 0) {
        $onCampus = $null; $onCampusReason = 'campus_lan_private'
        $onCampusEvidence += "local_private_ip=$($privateHits[0].ip) in $($privateHits[0].cidr)"
    } elseif ($cernet.is_cernet -eq $true) {
        $onCampus = $null; $onCampusReason = 'cernet_as'
        $onCampusEvidence += "public_ip=$publicIpText AS$($cernet.asn)"
    } elseif ($loginWallProbes.Count -gt 0) {
        $onCampus = $false; $onCampusReason = 'login_wall'
        $onCampusEvidence += "probe_login_wall=$($loginWallProbes[0].url)"
    } elseif ($captiveDetected) {
        $onCampus = $false; $onCampusReason = 'captive_portal'
        $onCampusEvidence += 'captive_portal_detected'
    } elseif ($cernet.checked -and $cernet.is_cernet -eq $false) {
        $onCampus = $false; $onCampusReason = 'cernet_as_negative'
        $onCampusEvidence += "public_ip=$publicIpText AS$($cernet.asn) not CERNET"
    } else {
        $onCampus = $null; $onCampusReason = 'unknown'
        if ($publicIpText) { $onCampusEvidence += "public_ip=$publicIpText 未命中校园网段且无 CERNET 证据" }
        else { $onCampusEvidence += '无可用公网 IP 或探测证据' }
    }

    # ---------------- VPN 状态
    $vpnConnected = $null
    $vpnState = 'unknown'
    if ($onCampus -eq $true) {
        $vpnConnected = $false
        $vpnState = 'campus_direct'
    } elseif (-not $vpnEnabled) {
        $vpnState = 'disabled'
    } elseif (-not $vpnBase) {
        $vpnState = 'not_configured'
    } elseif (-not $vpn.reachable) {
        $vpnState = 'webvpn_unreachable'
    } elseif ($vpn.http_probe_logged_in -eq $true) {
        $vpnConnected = $true
        $vpnState = 'webvpn_http_session_logged_in'
    } elseif ($vpn.http_probe_logged_in -eq $false) {
        if ($onCampus -eq $false) {
            $vpnConnected = $false
            $vpnState = 'webvpn_login_required'
        } else {
            # 独立 HTTP 会话未登录，但不能否定用户浏览器已登录
            $vpnState = 'webvpn_login_required_independent_session'
        }
    } elseif ($vpn.probe) {
        $vpnState = 'webvpn_probe_inconclusive'
    } else {
        $vpnState = 'browser_session_not_observed'
    }

    $recommendation = 'oa_or_vpn'
    if ($onCampus -eq $true) { $recommendation = 'direct' }
    elseif ($vpnConnected -eq $true) { $recommendation = 'vpn' }

    $campusCidrHits = @($cidrHits) + @($publicCidrHits)
    $privateCidrHits = @($privateHits)
    if ($publicIpPrivate -and $rqIpHit) {
        $privateCidrHits += [ordered]@{ ip = $publicIpText; cidr = $rqIpHit; iface = ''; source = 'rqIp' }
    }

    return [ordered]@{
        probe_scope = 'independent_http_session'
        browser_session = 'not_observed'
        checked_at = (Get-Date).ToString('o')
        config_path = [string](Get-SettingValue '_config_path' '')
        school = [string](Get-SectionSetting 'school' 'name' '')
        on_campus = $onCampus
        on_campus_reason = $onCampusReason
        on_campus_evidence = @($onCampusEvidence)
        vpn_connected = $vpnConnected
        vpn_state = $vpnState
        captive_portal_detected = $captiveDetected
        recommendation = $recommendation
        proxy = $proxy
        local_ipv4 = @($localIps)
        campus_cidrs = @($effectiveCidrs)
        campus_private_cidrs = @($effectivePrivateCidrs)
        campus_cidr_hits = @($campusCidrHits)
        campus_private_cidr_hits = @($privateCidrHits)
        public_ip = $publicIp
        cernet = $cernet
        campus_probes = @($probes)
        captive_portal_probes = @($captiveProbes)
        webvpn = $vpn
        carsi = $carsi
        hints = @(
            'on_campus=true 表示网络位置（内置 WHUT 网段 / CERNET AS4538 / 受限探测命中），不代表目标论文有订阅权限；仍需逐篇核验',
            'on_campus=false -> 优先 CARSI（carsi.portal）按库做机构登录；WebVPN 作备选；纯 OA 论文可直接脚本下载',
            'on_campus=null -> 证据不足，按校外默认处理：先 OA，再 CARSI/WebVPN；可补充 campus_cidrs / on_campus_probes',
            'vpn_connected=null 表示无法判断浏览器会话；用户已登录时直接继续原浏览器，不重复要求登录',
            'status 探测默认直连（HttpWebRequest.Proxy=$null）绕过 Clash 等系统代理；要临时走系统代理加 -UseSystemProxy',
            'campus_private_cidrs（10.82/16、10.83/16）为校园 LAN 弱证据，可能与其他单位私网重叠',
            'CERNET AS 只说明出口属于教育网，可能对应其他高校；WHUT 判定优先看 campus_cidrs'
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
        $localIpText = 'unknown'
        if (@($status.local_ipv4).Count -gt 0) {
            $localIpText = (@($status.local_ipv4 | ForEach-Object { $_.IPAddress }) -join ', ')
        }
        $hitItems = @($status.campus_cidr_hits) + @($status.campus_private_cidr_hits)
        $campusHitText = 'none'
        if ($hitItems.Count -gt 0) {
            $campusHitText = (@($hitItems | ForEach-Object { "$($_.ip)@$($_.cidr)" }) -join ', ')
        }
        $cernetText = 'unknown'
        if ($status.cernet -and $status.cernet.checked) {
            $cernetText = "$(Format-StatusValue $status.cernet.asn) $(Format-StatusValue $status.cernet.asn_name) (is_cernet=$(Format-StatusValue $status.cernet.is_cernet))"
        }
        $human = @(
            "网络状态：",
            "  on_campus        = $(Format-StatusValue $status.on_campus)",
            "  on_campus_reason = $(Format-StatusValue $status.on_campus_reason)",
            "  vpn_connected    = $(Format-StatusValue $status.vpn_connected)",
            "  vpn_state        = $(Format-StatusValue $status.vpn_state)",
            "  captive_portal   = $(Format-StatusValue $status.captive_portal_detected)",
            "  recommendation   = $(Format-StatusValue $status.recommendation)",
            "  公网 IP          = $(Format-StatusValue $status.public_ip.ip)",
            "  CERNET AS        = $cernetText",
            "  系统代理         = $(Format-StatusValue $status.proxy.use_system_proxy)",
            "  校园网命中       = $campusHitText",
            "  本地 IPv4        = $localIpText"
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
    'carsi' {
        # CARSI 按数据库做机构登录：这里只打开学校 CARSI 门户，具体数据库由用户选择
        $portal = [string](Get-SectionSetting 'carsi' 'portal' '')
        if (-not $portal) {
            Write-Result ([ordered]@{ status = 'error'; reason = '配置缺少 carsi.portal' })
            $exitCode = 2
        } else {
            try {
                Start-Process $portal
                Write-Result ([ordered]@{ status = 'opened'; url = $portal;
                    note = '已打开 CARSI 门户；请选择目标数据库并用学校账号登录，验证码由用户完成' }) "已在默认浏览器打开 CARSI：$portal"
            } catch {
                Write-Result ([ordered]@{ status = 'error'; reason = "无法打开浏览器：$($_.Exception.Message)" })
                $exitCode = 1
            }
        }
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
