#requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [string]$ConfigPath = '',
    [ValidateRange(1,1800)][int]$WaitUser = 240
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
if (-not $ConfigPath) { $ConfigPath = Join-Path $PSScriptRoot '登录配置.private.json' }
$nodeExe = $null
$bundledNode = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
foreach ($candidate in @($env:CAMPUS_NODE,$bundledNode,'node')) {
    if (-not $candidate) { continue }
    $command = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $command) { continue }
    & $command.Source -e 'process.exit(Number.parseInt(process.versions.node)>=22?0:1)'
    if ($LASTEXITCODE -eq 0) { $nodeExe = $command.Source; break }
}
if (-not $nodeExe) { throw '需要 Node 22+；可设置 CAMPUS_NODE 为解释器绝对路径。' }
$reader = Join-Path $PSScriptRoot 'skill-src\scripts\login_credentials.js'
# Child process consumes the file and returns only readiness flags, not credentials.
& $nodeExe $reader --check --config $ConfigPath
$checkCode = $LASTEXITCODE
if ($CheckOnly -or $checkCode -ne 0) { exit $checkCode }
$browser = Join-Path $PSScriptRoot 'skill-src\scripts\browser_dl.js'
& $nodeExe $browser --login --credentials $ConfigPath --wait-user $WaitUser --out (Join-Path $PSScriptRoot 'papers')
exit $LASTEXITCODE
