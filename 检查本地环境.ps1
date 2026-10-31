#requires -Version 5.1
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$env:PYTHONUTF8 = '1'
$bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies'
$pythonExe = $null
foreach ($candidate in @($env:DSH_PYTHON, (Join-Path $bundled 'python\python.exe'), 'python')) {
    if (-not $candidate) { continue }
    $command = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $command) { continue }
    & $command.Source -c 'import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>$null
    if ($LASTEXITCODE -eq 0) { $pythonExe = $command.Source; break }
}
if (-not $pythonExe) { throw '需要 Python 3.10+；可设置 DSH_PYTHON 为解释器绝对路径。' }
$nodeExe = $null
foreach ($candidate in @($env:CAMPUS_NODE, (Join-Path $bundled 'node\bin\node.exe'), 'node')) {
    if (-not $candidate) { continue }
    $command = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $command) { continue }
    & $command.Source -e 'process.exit(Number.parseInt(process.versions.node)>=22?0:1)' 2>$null
    if ($LASTEXITCODE -eq 0) { $nodeExe = $command.Source; break }
}
if (-not $nodeExe) { throw '需要 Node 22+；可设置 CAMPUS_NODE 为绝对路径。' }
& $pythonExe -c 'import pypdf;print(pypdf.__version__)'
if ($LASTEXITCODE -ne 0) { throw "完整离线测试需要 pypdf。请在此环境安装：& '$pythonExe' -m pip install pypdf" }
$skillPath = Join-Path $PSScriptRoot 'skill-src'
Write-Output "Python: $pythonExe"
Write-Output "Node: $nodeExe"
Write-Output '以下测试不启动浏览器，不发校园网或出版社请求。'
& $pythonExe -m unittest discover -s (Join-Path $skillPath 'tests') -p test_offline.py -v
if ($LASTEXITCODE -ne 0) { throw 'Python 回归失败' }
& $nodeExe --test (Join-Path $skillPath 'tests\test_browser.js')
if ($LASTEXITCODE -ne 0) { throw 'Node 回归失败' }
& $nodeExe --test (Join-Path $skillPath 'tests\test_login.js')
if ($LASTEXITCODE -ne 0) { throw '登录配置回归失败' }
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $skillPath 'tests\test_network.ps1')
if ($LASTEXITCODE -ne 0) { throw 'PowerShell 回归失败' }
& $pythonExe (Join-Path $skillPath 'scripts\webvpn_url.py') --selftest
if ($LASTEXITCODE -ne 0) { throw 'WebVPN URL 样本自检失败' }
Write-Output '全部离线检查通过。实际登录、验证码和下载需要现场验收。'
