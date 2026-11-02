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
# 兼容源码包根（skill-src/scripts）与已安装 skill 根（scripts）
$scriptsDir = if (Test-Path (Join-Path $PSScriptRoot 'skill-src\scripts')) {
    Join-Path $PSScriptRoot 'skill-src\scripts'
} elseif (Test-Path (Join-Path $PSScriptRoot 'scripts')) {
    Join-Path $PSScriptRoot 'scripts'
} else {
    throw '找不到 scripts 目录；请在 skill 根目录或源码包根运行。'
}
$nodeExe = $null
$dshRuntime = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies'
$codexRuntime = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies'
foreach ($candidate in @($env:CAMPUS_NODE, (Join-Path $dshRuntime 'node\bin\node.exe'), (Join-Path $codexRuntime 'node\bin\node.exe'), 'node')) {
    if (-not $candidate) { continue }
    $command = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $command) { continue }
    & $command.Source -e 'process.exit(Number.parseInt(process.versions.node)>=22?0:1)'
    if ($LASTEXITCODE -eq 0) { $nodeExe = $command.Source; break }
}
if (-not $nodeExe) { throw '需要 Node 22+；可设置 CAMPUS_NODE 为解释器绝对路径。' }
$reader = Join-Path $scriptsDir 'login_credentials.js'
# Child process consumes the file and returns only readiness flags, not credentials.
& $nodeExe $reader --check --config $ConfigPath
$checkCode = $LASTEXITCODE
if ($CheckOnly -or $checkCode -ne 0) { exit $checkCode }
$browser = Join-Path $scriptsDir 'browser_dl.js'
& $nodeExe $browser --login --credentials $ConfigPath --wait-user $WaitUser --out (Join-Path $PSScriptRoot 'papers')
exit $LASTEXITCODE
