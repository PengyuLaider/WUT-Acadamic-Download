#requires -Version 5.1
$ErrorActionPreference = 'Stop'
$scriptFile = Join-Path $PSScriptRoot '..\scripts\campus_net.ps1'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $scriptFile), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
# Load function definitions only: never execute CLI entrypoint or make HTTP requests.
$ast.FindAll({param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $false) | ForEach-Object {
    Invoke-Expression $_.Extent.Text
}
$count = 0
function Assert-Equal($actual, $expected, $name) {
    if ($actual -ne $expected) { throw "FAIL $name : actual=$actual expected=$expected" }
    $script:count++
}
$script:fake = @{ok=$true;status=200;final_url='https://lib.example.org';redirected=$false;body_head='University Library';body_full='University Library';error=$null}
function Test-Url($Url) { return $script:fake }
$public = [pscustomobject]@{name='public';url='https://lib.example.org';must_contain=@('University');must_not_contain=@('Denied');login_markers=@()}
Assert-Equal (Test-CampusProbe $public).authenticated $null 'public page is not campus evidence'
$restricted = [pscustomobject]@{name='restricted';url='https://lib.example.org';must_contain=@('University');must_not_contain=@('Denied');login_markers=@();proves_campus_access=$true}
Assert-Equal (Test-CampusProbe $restricted).authenticated $true 'explicit restricted probe positive marker'
$script:fake.status=403; $script:fake.ok=$false
Assert-Equal (Test-CampusProbe $restricted).authenticated $null '403 is not success'
$script:fake.status=200; $script:fake.ok=$true; $script:fake.body_full='Denied'; $script:fake.body_head='Denied'
Assert-Equal (Test-CampusProbe $restricted).authenticated $false 'explicit denial'
Assert-Equal (Test-CidrContains '10.1.2.3' '10.0.0.0/9') $true 'partial-byte CIDR positive'
Assert-Equal (Test-CidrContains '10.200.2.3' '10.0.0.0/9') $false 'partial-byte CIDR negative'
Assert-Equal (Test-CidrContains '10.1.2.3' '10.0.0.0/33') $false 'invalid mask'
Assert-Equal (Test-CidrContains '10.1.2.3' '0.0.0.0/0') $true 'zero-bit mask'
# The browser session must remain unknown even when the independent HTTP probe is unauthenticated.
$settings = [ordered]@{school=@{name='test'};_config_path='test';campus_cidrs=@();on_campus_probes=@($public);webvpn=@{enabled=$true;base='https://vpn.example.org';cookie_env='CAMPUS_TEST_NOT_SET';login_markers=@('login')};carsi=@{enabled=$false;portal='';idp_entity_id=''}}
$script:fake.body_full='University Library';$script:fake.body_head='University Library'
function Get-LocalIPv4 { return @() }
function Get-PublicIP { return @{ok=$true;ip='192.0.2.1'} }
function Get-CaptivePortalState { return @{detected=$false;probes=@()} }
function Get-ProxyState { return @{} }
function Invoke-WebVpnProbe { return @{logged_in=$false} }
$status = Get-NetworkStatus
Assert-Equal $status.on_campus $null 'public homepage leaves campus unknown'
Assert-Equal $status.vpn_connected $null 'independent HTTP does not invalidate browser session'
Assert-Equal $status.webvpn.http_probe_logged_in $false 'HTTP probe result remains available'
Assert-Equal $status.browser_session 'not_observed' 'scope explicit'
Write-Output "PASS: $count offline assertions; no network requests"
