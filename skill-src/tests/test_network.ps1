#requires -Version 5.1
# 离线回归测试：只解析并加载 campus_net.ps1 的函数定义，不执行主流程、不联网。
# 原有 12 项断言保留；新增覆盖：内置 WHUT 网段、CIDR 边界、公网 IP/CERNET 证据、
# 代理绕过（直连/系统代理可 mock）、字段非空、UTF-8 BOM、人类输出 unknown、config.example。
$ErrorActionPreference = 'Stop'
$scriptFile = Join-Path $PSScriptRoot '..\scripts\campus_net.ps1'
$scriptPath = (Resolve-Path $scriptFile).Path
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
# Load function definitions only: never execute CLI entrypoint or make HTTP requests.
$ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false) | ForEach-Object {
    Invoke-Expression $_.Extent.Text
}

$count = 0
function Assert-Equal($actual, $expected, $name) {
    if ($actual -ne $expected) { throw "FAIL $name : actual=$actual expected=$expected" }
    $script:count++
}
function Assert-True($actual, $name) {
    if (-not $actual) { throw "FAIL $name : expected truthy, actual=$actual" }
    $script:count++
}
function Assert-False($actual, $name) {
    if ($actual) { throw "FAIL $name : expected falsy, actual=$actual" }
    $script:count++
}
function Assert-NotNull($actual, $name) {
    if ($null -eq $actual) { throw "FAIL $name : expected non-null" }
    $script:count++
}
function Assert-Contains($Collection, $Item, $name) {
    $found = $false
    foreach ($x in @($Collection)) { if ($x -eq $Item) { $found = $true; break } }
    if (-not $found) { throw "FAIL $name : '$Item' not found in '$(@($Collection) -join ', ')'" }
    $script:count++
}
function Assert-Match([string]$Text, [string]$Pattern, $name) {
    if ($Text -notmatch $Pattern) { throw "FAIL $name : pattern '$Pattern' not matched" }
    $script:count++
}

# ================= 1. 静态检查：BOM、命令行参数兼容 =================
$bytes = [IO.File]::ReadAllBytes($scriptPath)
Assert-True ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) 'campus_net.ps1 is UTF-8 with BOM'
$paramNames = @($ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
Assert-Contains $paramNames 'UseSystemProxy' 'UseSystemProxy switch exists'
Assert-Contains $paramNames 'Action' 'Action parameter kept'

# ================= 2. 离线 mock 基础设施 =================
$script:httpCalls = @()
$script:httpOk = $true
$script:httpStatus = 200
$script:httpBody = 'University Library'
$script:httpFinalUrl = 'https://lib.example.org'
$script:httpError = $null
$script:publicIpEnabled = $true
$script:publicIpBody = '{"ip":"192.0.2.1"}'
$script:cernetOk = $false
$script:cernetBody = ''
$script:cernetStatus = 200
$script:cernetError = 'mock cernet unavailable'
$script:localIps = @()
$script:captiveDetected = $false
$script:webvpnProbeLoggedIn = $false

function New-MockHttpResult {
    param([string]$Uri, [bool]$Ok, $Status, [string]$Body, [string]$FinalUrl, [string]$ErrorText, [bool]$UsedProxy)
    return [ordered]@{
        url = $Uri; ok = $Ok; status = $Status; final_url = $FinalUrl; redirected = ($FinalUrl -ne $Uri)
        body = $Body; error = $ErrorText; used_system_proxy = $UsedProxy; elapsed_ms = 1
    }
}

# 代理绕过可 mock：记录每次请求是否 -UseProxy，并按 URL 路由到公网 IP / CERNET / 普通页面
function Invoke-DirectHttpRequest {
    param([string]$Uri, [int]$TimeoutSec = 12, [switch]$UseProxy, [int]$MaxRedirections = 8, [string]$UserAgent = '')
    $script:httpCalls += [ordered]@{ uri = $Uri; use_proxy = [bool]$UseProxy; timeout = $TimeoutSec }
    $usedProxy = [bool]$UseProxy
    if ($Uri -match 'ipinfo\.io|bgpview\.io') {
        return New-MockHttpResult -Uri $Uri -Ok $script:cernetOk -Status $script:cernetStatus -Body $script:cernetBody -FinalUrl $Uri -ErrorText $script:cernetError -UsedProxy $usedProxy
    }
    if ($Uri -match 'ipify|ifconfig|ipip|ip\.useragentinfo') {
        if (-not $script:publicIpEnabled) {
            return New-MockHttpResult -Uri $Uri -Ok $false -Status $null -Body '' -FinalUrl $Uri -ErrorText 'mock public ip unavailable' -UsedProxy $usedProxy
        }
        return New-MockHttpResult -Uri $Uri -Ok $true -Status 200 -Body $script:publicIpBody -FinalUrl $Uri -ErrorText $null -UsedProxy $usedProxy
    }
    return New-MockHttpResult -Uri $Uri -Ok ([bool]$script:httpOk) -Status $script:httpStatus -Body $script:httpBody -FinalUrl $script:httpFinalUrl -ErrorText $script:httpError -UsedProxy $usedProxy
}

function Get-LocalIPv4 { return @($script:localIps) }
function Get-CaptivePortalState { return [ordered]@{ detected = [bool]$script:captiveDetected; probes = @() } }
function Get-ProxyState {
    return [ordered]@{
        env_http_proxy = $null; env_https_proxy = $null; no_proxy = $null
        registry = [ordered]@{ enabled = $false; server = $null; pac = $null }
    }
}
function Invoke-WebVpnProbe { return [ordered]@{ logged_in = $script:webvpnProbeLoggedIn; target = 'mock'; status = $null } }

function New-StatusSettings {
    param(
        [string[]]$CampusCidrs = @(),
        [string]$CampusCidrsMode = 'append',
        [string[]]$PrivateCidrs = @(),
        [string]$PrivateCidrsMode = 'append',
        $Probes = @(),
        [bool]$WebVpnEnabled = $false
    )
    $script:settings = [ordered]@{
        school = [ordered]@{ name = 'test' }
        _config_path = 'test'
        campus_cidrs = @($CampusCidrs)
        campus_cidrs_mode = $CampusCidrsMode
        campus_private_cidrs = @($PrivateCidrs)
        campus_private_cidrs_mode = $PrivateCidrsMode
        proxy_mode = 'direct'
        on_campus_probes = @($Probes)
        captive_portal_probes = @()
        public_ip_probes = @('https://api.ipify.org?format=json')
        cernet = [ordered]@{
            lookup_enabled = $true
            timeout_seconds = 8
            lookup_urls = @('https://ipinfo.io/{ip}/json', 'https://api.bgpview.io/ip/{ip}')
            expected_asns = @(4538)
            org_markers = @('CERNET', 'China Education and Research Network')
        }
        webvpn = [ordered]@{
            enabled = $WebVpnEnabled; base = 'https://webvpn.example.edu.cn'
            cookie_env = 'CAMPUS_TEST_NOT_SET'; login_markers = @('login'); probe_target = ''
        }
        carsi = [ordered]@{ enabled = $false; portal = ''; idp_entity_id = '' }
    }
}

function Reset-MockState {
    $script:httpCalls = @()
    $script:httpOk = $true
    $script:httpStatus = 200
    $script:httpBody = 'University Library'
    $script:httpFinalUrl = 'https://lib.example.org'
    $script:httpError = $null
    $script:publicIpEnabled = $true
    $script:publicIpBody = '{"ip":"192.0.2.1"}'
    $script:cernetOk = $false
    $script:cernetBody = ''
    $script:cernetStatus = 200
    $script:cernetError = 'mock cernet unavailable'
    $script:localIps = @()
    $script:captiveDetected = $false
    $script:webvpnProbeLoggedIn = $false
    $script:UseSystemProxyPreference = $false
}

function New-Status {
    param($Probes = @(), [bool]$WebVpnEnabled = $false)
    New-StatusSettings -Probes $Probes -WebVpnEnabled $WebVpnEnabled
    return Get-NetworkStatus
}

Reset-MockState
New-StatusSettings
$public = [pscustomobject]@{ name = 'public'; url = 'https://lib.example.org'; must_contain = @('University'); must_not_contain = @('Denied'); login_markers = @() }

# ================= 3. 原有 12 项断言（行为保持） =================
Assert-Equal (Test-CampusProbe $public).authenticated $null 'public page is not campus evidence'
$restricted = [pscustomobject]@{ name = 'restricted'; url = 'https://lib.example.org'; must_contain = @('University'); must_not_contain = @('Denied'); login_markers = @(); proves_campus_access = $true }
Assert-Equal (Test-CampusProbe $restricted).authenticated $true 'explicit restricted probe positive marker'
$script:httpOk = $false; $script:httpStatus = 403
Assert-Equal (Test-CampusProbe $restricted).authenticated $null '403 is not success'
$script:httpOk = $true; $script:httpStatus = 200; $script:httpBody = 'Denied'
Assert-Equal (Test-CampusProbe $restricted).authenticated $false 'explicit denial'
Assert-Equal (Test-CidrContains '10.1.2.3' '10.0.0.0/9') $true 'partial-byte CIDR positive'
Assert-Equal (Test-CidrContains '10.200.2.3' '10.0.0.0/9') $false 'partial-byte CIDR negative'
Assert-Equal (Test-CidrContains '10.1.2.3' '10.0.0.0/33') $false 'invalid mask'
Assert-Equal (Test-CidrContains '10.1.2.3' '0.0.0.0/0') $true 'zero-bit mask'
# The browser session must remain unknown even when the independent HTTP probe is unauthenticated.
Reset-MockState
$status = New-Status -Probes @($public) -WebVpnEnabled $true
Assert-Equal $status.on_campus $null 'public homepage leaves campus unknown'
Assert-Equal $status.vpn_connected $null 'independent HTTP does not invalidate browser session'
Assert-Equal $status.webvpn.http_probe_logged_in $false 'HTTP probe result remains available'
Assert-Equal $status.browser_session 'not_observed' 'scope explicit'

# ================= 4. 内置 WHUT 网段与 config 覆盖/追加 =================
Reset-MockState
New-StatusSettings
$builtinCidrs = @(Get-CampusCidrSet)
Assert-Equal $builtinCidrs.Count 3 'three built-in WHUT CIDRs'
Assert-Contains $builtinCidrs '202.114.48.0/20' 'built-in 202.114.48.0/20'
Assert-Contains $builtinCidrs '202.114.88.0/21' 'built-in 202.114.88.0/21'
Assert-Contains $builtinCidrs '218.197.96.0/19' 'built-in 218.197.96.0/19'
$builtinPrivate = @(Get-CampusPrivateCidrSet)
Assert-Contains $builtinPrivate '10.82.0.0/16' 'built-in campus private 10.82/16'
Assert-Contains $builtinPrivate '10.83.0.0/16' 'built-in campus private 10.83/16'
New-StatusSettings -CampusCidrs @('203.0.113.0/24')
$appendCidrs = @(Get-CampusCidrSet)
Assert-Equal $appendCidrs.Count 4 'append mode keeps built-ins and adds config CIDR'
Assert-Contains $appendCidrs '203.0.113.0/24' 'append mode includes custom CIDR'
New-StatusSettings -CampusCidrs @('203.0.113.0/24') -CampusCidrsMode 'replace'
$replaceCidrs = @(Get-CampusCidrSet)
Assert-Equal $replaceCidrs.Count 1 'replace mode drops built-ins'
Assert-Equal $replaceCidrs[0] '203.0.113.0/24' 'replace mode keeps only custom CIDR'
New-StatusSettings -CampusCidrs @('202.114.48.0/20')
Assert-Equal (@(Get-CampusCidrSet)).Count 3 'append mode dedupes duplicate CIDR'
New-StatusSettings -CampusCidrs @('203.0.113.0/24') -CampusCidrsMode 'replace'
Assert-Equal (@(Get-CampusCidrSet)).Count 1 'replace mode with one entry'
New-StatusSettings

# ================= 5. CIDR 边界与已知 WHUT 地址 =================
$cidrCases = @(
    @{ ip = '202.114.48.0'; cidr = '202.114.48.0/20'; expected = $true },
    @{ ip = '202.114.63.255'; cidr = '202.114.48.0/20'; expected = $true },
    @{ ip = '202.114.64.0'; cidr = '202.114.48.0/20'; expected = $false },
    @{ ip = '202.114.47.255'; cidr = '202.114.48.0/20'; expected = $false },
    @{ ip = '202.114.88.0'; cidr = '202.114.88.0/21'; expected = $true },
    @{ ip = '202.114.95.255'; cidr = '202.114.88.0/21'; expected = $true },
    @{ ip = '202.114.96.0'; cidr = '202.114.88.0/21'; expected = $false },
    @{ ip = '218.197.96.0'; cidr = '218.197.96.0/19'; expected = $true },
    @{ ip = '218.197.127.255'; cidr = '218.197.96.0/19'; expected = $true },
    @{ ip = '218.197.128.0'; cidr = '218.197.96.0/19'; expected = $false },
    @{ ip = '202.114.56.2'; cidr = '202.114.48.0/20'; expected = $true },
    @{ ip = '202.114.89.79'; cidr = '202.114.88.0/21'; expected = $true }
)
foreach ($case in $cidrCases) {
    Assert-Equal (Test-CidrContains $case.ip $case.cidr) $case.expected "CIDR boundary $($case.ip) in $($case.cidr)"
}
foreach ($ip in @('202.114.56.2', '202.114.89.79', '202.114.49.228', '218.197.100.87', '218.197.104.131', '218.197.111.6')) {
    $hit = $false
    foreach ($cidr in @(Get-CampusCidrSet)) { if (Test-CidrContains $ip ([string]$cidr)) { $hit = $true; break } }
    Assert-True $hit "known WHUT address $ip matched by built-in CIDRs"
}
foreach ($ip in @('8.8.8.8', '8.8.4.4', '172.16.0.1')) {
    $hit = $false
    foreach ($cidr in @(Get-CampusCidrSet)) { if (Test-CidrContains $ip ([string]$cidr)) { $hit = $true; break } }
    Assert-False $hit "non-campus IP $ip not matched by built-in CIDRs"
}

# ================= 6. 非公网 IP 辅助函数 =================
Assert-True (Test-NonPublicIpv4 '10.82.1.2') '10.82.x is non-public'
Assert-True (Test-NonPublicIpv4 '192.168.1.1') '192.168.x is non-public'
Assert-True (Test-NonPublicIpv4 '192.0.2.1') 'TEST-NET-1 is non-public'
Assert-False (Test-NonPublicIpv4 '218.197.100.87') 'WHUT public IP is public'
Assert-False (Test-NonPublicIpv4 '8.8.8.8') '8.8.8.8 is public'
Assert-False (Test-NonPublicIpv4 'not-an-ip') 'invalid IP is not public'

# ================= 7. on_campus 端到端判定 =================
# 7.1 本地 IP 落在内置 WHUT 网段
Reset-MockState
$script:localIps = @([pscustomobject]@{ IPAddress = '202.114.56.2'; InterfaceAlias = 'Wi-Fi'; PrefixLength = 24 })
$status = New-Status -Probes @($public)
Assert-Equal $status.on_campus $true 'local WHUT IP => on campus'
Assert-Equal $status.on_campus_reason 'campus_cidr' 'reason campus_cidr'
Assert-Equal $status.vpn_connected $false 'campus direct => VPN not used'
Assert-Equal $status.vpn_state 'campus_direct' 'vpn_state campus_direct'
Assert-Equal $status.recommendation 'direct' 'recommendation direct on campus'
Assert-True (@($status.campus_cidr_hits).Count -ge 1) 'campus_cidr_hits populated'

# 7.2 另一已知 WHUT 地址
Reset-MockState
$script:localIps = @([pscustomobject]@{ IPAddress = '218.197.100.87'; InterfaceAlias = 'Ethernet'; PrefixLength = 24 })
$status = New-Status
Assert-Equal $status.on_campus $true 'local 218.197.100.87 => on campus'
Assert-Equal $status.on_campus_reason 'campus_cidr' 'reason campus_cidr for 218.197.x'

# 7.3 公网出口 IP 命中 WHUT 网段
Reset-MockState
$script:publicIpBody = '{"ip":"218.197.100.87"}'
$status = New-Status
Assert-Equal $status.on_campus $true 'public egress in WHUT CIDR => on campus'
Assert-Equal $status.on_campus_reason 'public_ip_cidr' 'reason public_ip_cidr'
$publicHits = @($status.campus_cidr_hits)
Assert-Equal $publicHits[0].source 'public_ip' 'public ip hit source recorded'

# 7.3b 报告中的校园出口 218.197.141.140 不在内置 /19，但 CERNET AS4538 可判 true
Reset-MockState
$script:publicIpBody = '{"ip":"218.197.141.140"}'
$script:cernetOk = $true
$script:cernetBody = '{"ip":"218.197.141.140","org":"AS4538 China Education and Research Network Center"}'
$status = New-Status
Assert-Equal $status.on_campus $null 'CERNET egress => on campus (insufficient evidence)'
Assert-Equal $status.on_campus_reason 'cernet_as' 'reason cernet_as'
Assert-Equal $status.cernet.asn 4538 'cernet AS recorded'

# 7.4 非校园出口（中国移动 AS4134）=> false
Reset-MockState
$script:publicIpBody = '{"ip":"8.8.4.4"}'
$script:cernetOk = $true
$script:cernetBody = '{"ip":"8.8.4.4","org":"AS4134 Chinanet"}'
$status = New-Status
Assert-Equal $status.on_campus $false 'China Mobile egress => not on campus'
Assert-Equal $status.on_campus_reason 'cernet_as_negative' 'reason cernet_as_negative'
Assert-Equal $status.cernet.is_cernet $false 'cernet evidence false for AS4134'

# 7.5 公网 IP 未命中且 CERNET 证据失败 => unknown（优雅降级）
Reset-MockState
$script:publicIpBody = '{"ip":"8.8.8.8"}'
$status = New-Status
Assert-Equal $status.on_campus $null 'no evidence => on_campus null'
Assert-Equal $status.on_campus_reason 'unknown' 'reason unknown'
Assert-Equal $status.cernet.checked $true 'cernet lookup attempted'
Assert-Equal $status.cernet.is_cernet $null 'cernet evidence null on failure'

# 7.6 公网 IP 探测失败时用图书馆 rqIp（10.82.x）判定
Reset-MockState
$script:publicIpEnabled = $false
$script:httpBody = "var rqIp = '10.82.3.4'; <html>University Library</html>"
$rqProbe = [pscustomobject]@{ name = 'lib'; url = 'https://lib.example.org/'; must_contain = @('University'); must_not_contain = @(); login_markers = @(); proves_campus_access = $false }
$status = New-Status -Probes @($rqProbe)
Assert-Equal $status.public_ip.source 'probe-rqIp' 'rqIp fallback source'
Assert-Equal $status.public_ip.ip '10.82.3.4' 'rqIp fallback value'
Assert-Equal $status.on_campus $null 'campus rqIp => on campus (insufficient evidence)'
Assert-Equal $status.on_campus_reason 'library_rqip' 'reason library_rqip'

# 7.7 本地校园私网 IP
Reset-MockState
$script:localIps = @([pscustomobject]@{ IPAddress = '10.83.5.6'; InterfaceAlias = 'Ethernet'; PrefixLength = 16 })
$status = New-Status
Assert-Equal $status.on_campus $null 'campus LAN private IP => on campus (insufficient evidence)'
Assert-Equal $status.on_campus_reason 'campus_lan_private' 'reason campus_lan_private'

# 7.8 强制门户
Reset-MockState
$script:captiveDetected = $true
$status = New-Status
Assert-Equal $status.on_campus $false 'captive portal => not on campus'
Assert-Equal $status.on_campus_reason 'captive_portal' 'reason captive_portal'

# 7.9 登录墙
Reset-MockState
$script:httpBody = 'Please sign in to continue'
$loginProbe = [pscustomobject]@{ name = 'lib'; url = 'https://lib.example.org/'; must_contain = @(); must_not_contain = @(); login_markers = @('Sign in') }
$status = New-Status -Probes @($loginProbe)
Assert-Equal $status.on_campus $false 'login wall => not on campus'
Assert-Equal $status.on_campus_reason 'login_wall' 'reason login_wall'

# ================= 8. vpn_connected / vpn_state =================
# 8.1 校园网直连 + WebVPN 已配置 => 不需要 VPN
Reset-MockState
$script:localIps = @([pscustomobject]@{ IPAddress = '202.114.56.2'; InterfaceAlias = 'Wi-Fi'; PrefixLength = 24 })
$status = New-Status -WebVpnEnabled $true
Assert-Equal $status.vpn_connected $false 'campus direct with webvpn configured => vpn false'
Assert-Equal $status.vpn_state 'campus_direct' 'vpn_state campus_direct with webvpn enabled'

# 8.2 校外 + WebVPN HTTP 会话已登录 => vpn true
Reset-MockState
$script:publicIpBody = '{"ip":"8.8.4.4"}'
$script:cernetOk = $true; $script:cernetBody = '{"org":"AS4134 Chinanet"}'
$script:webvpnProbeLoggedIn = $true
$status = New-Status -WebVpnEnabled $true
Assert-Equal $status.on_campus $false 'off campus for vpn login scenario'
Assert-Equal $status.vpn_connected $true 'webvpn http session logged in => vpn true'
Assert-Equal $status.vpn_state 'webvpn_http_session_logged_in' 'vpn_state logged in'
Assert-Equal $status.recommendation 'vpn' 'recommendation vpn'

# 8.3 校外 + WebVPN 要求重新登录 => vpn false
Reset-MockState
$script:publicIpBody = '{"ip":"8.8.4.4"}'
$script:cernetOk = $true; $script:cernetBody = '{"org":"AS4134 Chinanet"}'
$script:webvpnProbeLoggedIn = $false
$status = New-Status -WebVpnEnabled $true
Assert-Equal $status.vpn_connected $false 'webvpn login required => vpn false'
Assert-Equal $status.vpn_state 'webvpn_login_required' 'vpn_state login required'
Assert-Equal $status.recommendation 'oa_or_vpn' 'recommendation oa_or_vpn off campus'

# ================= 9. 结果字段完整性 / JSON / 人类输出 =================
Reset-MockState
$script:publicIpBody = '{"ip":"218.197.100.87"}'
$status = New-Status -Probes @($public) -WebVpnEnabled $true
$requiredFields = @('on_campus', 'on_campus_reason', 'vpn_connected', 'vpn_state', 'recommendation', 'public_ip', 'campus_cidr_hits', 'campus_probes', 'cernet', 'proxy')
foreach ($field in $requiredFields) {
    Assert-True ($status.Contains($field)) "status JSON field present: $field"
}
Assert-True ($status.on_campus -is [bool]) 'on_campus is bool, never empty'
Assert-True ($status.on_campus_reason -is [string] -and $status.on_campus_reason.Length -gt 0) 'on_campus_reason non-empty'
Assert-True ($status.vpn_state -is [string] -and $status.vpn_state.Length -gt 0) 'vpn_state non-empty'
Assert-True (@('direct', 'vpn', 'oa_or_vpn') -contains $status.recommendation) 'recommendation is a concrete path'
Assert-True ($status.public_ip -and $status.public_ip.Contains('ip') -and $status.public_ip.ip) 'public_ip populated'
Assert-True ($status.cernet -and $status.cernet.Contains('is_cernet')) 'cernet evidence field present'
Assert-True ($status.proxy -and $status.proxy.Contains('use_system_proxy') -and $status.proxy.use_system_proxy -is [bool]) 'proxy state has bool use_system_proxy'
Assert-Equal $status.proxy.mode 'direct' 'proxy mode direct by default'
Assert-True (@($status.on_campus_evidence).Count -gt 0) 'on_campus_evidence non-empty'
Assert-True (@($status.campus_cidr_hits).Count -gt 0) 'campus_cidr_hits non-empty for campus scenario'

$statusJson = $status | ConvertTo-Json -Depth 10
foreach ($field in @('on_campus', 'on_campus_reason', 'vpn_connected', 'vpn_state', 'recommendation', 'public_ip', 'campus_cidr_hits', 'campus_probes', 'cernet', 'proxy')) {
    Assert-Match $statusJson ('"' + $field + '"\s*:') "JSON contains $field"
}
$roundTrip = $statusJson | ConvertFrom-Json
Assert-True ($roundTrip.PSObject.Properties.Name -contains 'on_campus_reason') 'JSON round-trip keeps on_campus_reason'
Assert-Equal $roundTrip.on_campus $true 'JSON round-trip keeps on_campus value'
Assert-Equal (Format-StatusValue $null) 'unknown' 'human output null -> unknown'
Assert-Equal (Format-StatusValue '') 'unknown' 'human output empty -> unknown'
Assert-Equal (Format-StatusValue $false) 'false' 'human output false -> false'
Assert-Equal (Format-StatusValue $true) 'true' 'human output true -> true'
Assert-Equal (Format-StatusValue 'direct') 'direct' 'human output text passthrough'
$scriptSource = [IO.File]::ReadAllText($scriptPath, [Text.Encoding]::UTF8)
Assert-Match $scriptSource 'on_campus\s*=\s*\$\(Format-StatusValue' 'human status line never blank'
Assert-Match $scriptSource 'on_campus_reason\s*=\s*\$\(Format-StatusValue' 'human on_campus_reason never blank'
Assert-Match $scriptSource '-UseSystemProxy' 'UseSystemProxy documented'

# ================= 10. 代理绕过（可 mock） =================
# 10.1 默认直连
Reset-MockState
New-StatusSettings
$status = New-Status -Probes @($public)
Assert-Equal $status.proxy.use_system_proxy $false 'status proxy defaults to direct'
$proxyCalls = @($script:httpCalls | Where-Object { $_.use_proxy -eq $true })
Assert-Equal $proxyCalls.Count 0 'status probes bypass system proxy by default'

# 10.2 Get-PublicIP 直连
Reset-MockState
New-StatusSettings
$script:UseSystemProxyPreference = $false
$ipDirect = Get-PublicIP
Assert-Equal $ipDirect.ok $true 'mock public ip resolved'
Assert-Equal $script:httpCalls[0].use_proxy $false 'public IP probe direct'
Assert-Equal $ipDirect.used_system_proxy $false 'public ip records direct mode'

# 10.3 -UseSystemProxy 显式恢复系统代理
$script:UseSystemProxyPreference = $true
$ipProxy = Get-PublicIP
Assert-Equal $script:httpCalls[1].use_proxy $true 'public IP probe uses system proxy when requested'
Assert-Equal $ipProxy.used_system_proxy $true 'public ip records system proxy mode'

# 10.4 status + 系统代理：探测可走代理，但 CERNET 证据必须仍直连
Reset-MockState
$script:publicIpBody = '{"ip":"8.8.8.8"}'
$script:UseSystemProxyPreference = $true
$status = New-Status -Probes @($public)
Assert-Equal $status.proxy.use_system_proxy $true 'status reports system proxy mode'
$proxiedCalls = @($script:httpCalls | Where-Object { $_.use_proxy -eq $true })
Assert-True ($proxiedCalls.Count -ge 1) 'status probes can use system proxy when requested'
$cernetProxied = @($script:httpCalls | Where-Object { $_.uri -match 'ipinfo|bgpview' -and $_.use_proxy -eq $true })
Assert-Equal $cernetProxied.Count 0 'cernet evidence always direct'
Reset-MockState

# ================= 11. CERNET AS 证据解析 =================
New-StatusSettings
$parsedIpinfo = ConvertFrom-CernetPayload '{"ip":"218.197.100.87","org":"AS4538 China Education and Research Network Center"}'
Assert-Equal $parsedIpinfo.asn 4538 'ipinfo payload parses ASN'
Assert-Match $parsedIpinfo.asn_name 'China Education' 'ipinfo payload parses org name'
$parsedBgpview = ConvertFrom-CernetPayload '{"status":"ok","data":{"prefixes":[{"asn":{"asn":4538,"name":"CERNET","description":"China Education and Research Network"}}]}}'
Assert-Equal $parsedBgpview.asn 4538 'bgpview payload parses ASN'
Assert-Match $parsedBgpview.asn_name 'CERNET' 'bgpview payload parses name'

Reset-MockState
New-StatusSettings
$script:cernetOk = $true
$script:cernetBody = '{"ip":"218.197.100.87","org":"AS4538 China Education and Research Network Center"}'
$evidence = Get-CernetEvidence '218.197.100.87'
Assert-Equal $evidence.checked $true 'cernet evidence checked'
Assert-Equal $evidence.is_cernet $true 'AS4538 identified as CERNET'
Assert-Equal $evidence.asn 4538 'evidence carries ASN'
Assert-Match $evidence.source 'ipinfo' 'evidence source recorded'

Reset-MockState
New-StatusSettings
$script:cernetOk = $true
$script:cernetBody = '{"ip":"8.8.4.4","org":"AS4134 Chinanet"}'
$evidence = Get-CernetEvidence '8.8.4.4'
Assert-Equal $evidence.is_cernet $false 'AS4134 is not CERNET'

Reset-MockState
New-StatusSettings
$evidence = Get-CernetEvidence '8.8.8.8'
Assert-Equal $evidence.checked $true 'failed lookups still checked'
Assert-Equal $evidence.is_cernet $null 'failed lookups return null evidence'
Assert-NotNull $evidence.error 'failed lookups record error text'

Reset-MockState
New-StatusSettings
$callsBefore = @($script:httpCalls).Count
$evidence = Get-CernetEvidence '10.82.1.2'
Assert-Equal $evidence.checked $false 'private IP skips AS lookup'
Assert-Equal $evidence.error 'private_or_reserved_ip' 'private IP error reason'
Assert-Equal (@($script:httpCalls).Count) $callsBefore 'private IP makes no network call'

Reset-MockState
New-StatusSettings
$evidence = Get-CernetEvidence '2001:db8::1'
Assert-Equal $evidence.checked $false 'IPv6 skips AS lookup'
Assert-Equal $evidence.is_cernet $null 'IPv6 evidence null'

New-StatusSettings
Assert-Equal (Get-CernetLookupTimeout) 8 'default cernet timeout is 8s'
$script:settings.cernet.timeout_seconds = 30
Assert-Equal (Get-CernetLookupTimeout) 8 'cernet timeout capped at 8s'
$script:settings.cernet.timeout_seconds = 5
Assert-Equal (Get-CernetLookupTimeout) 5 'cernet timeout honors smaller config'

Reset-MockState
New-StatusSettings
$script:UseSystemProxyPreference = $true
$script:cernetOk = $true
$script:cernetBody = '{"org":"AS4538 China Education and Research Network Center"}'
$evidence = Get-CernetEvidence '218.197.100.87'
$cernetCalls = @($script:httpCalls | Where-Object { $_.uri -match 'ipinfo|bgpview' })
Assert-Equal $cernetCalls[0].use_proxy $false 'cernet lookup direct even with system proxy preference'
Reset-MockState

# ================= 12. config.example.json 字段 =================
$cfgPath = (Resolve-Path (Join-Path $PSScriptRoot '..\config\config.example.json')).Path
$cfg = Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($cidr in @(Get-BuiltinCampusCidrs)) {
    Assert-Contains @($cfg.campus_cidrs) $cidr "config.example campus_cidrs contains $cidr"
}
Assert-Equal $cfg.campus_cidrs_mode 'append' 'config.example append mode'
Assert-Equal $cfg.proxy_mode 'direct' 'config.example proxy_mode direct'
Assert-Equal $cfg.cernet.timeout_seconds 8 'config.example cernet timeout 8'
Assert-Contains @($cfg.cernet.expected_asns) 4538 'config.example expected ASN 4538'
Assert-Contains @($cfg.campus_private_cidrs) '10.82.0.0/16' 'config.example private 10.82/16'
Assert-Contains @($cfg.campus_private_cidrs) '10.83.0.0/16' 'config.example private 10.83/16'
Assert-Equal $cfg.webvpn.base 'https://webvpn.whut.edu.cn' 'config.example webvpn.base unchanged'
Assert-Equal $cfg.download_dir 'papers' 'config.example download_dir unchanged'
Assert-Equal $cfg.school.domains.Count 3 'config.example school domains unchanged'

Write-Output "PASS: $count offline assertions; no network requests"
