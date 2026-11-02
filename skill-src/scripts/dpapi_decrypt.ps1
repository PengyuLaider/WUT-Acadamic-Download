#requires -Version 5.1
param([Parameter(Mandatory = $true)][string]$Path)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
Add-Type -AssemblyName System.Security
$resolved = (Resolve-Path -LiteralPath $Path).Path
$blob = [IO.File]::ReadAllBytes($resolved)
$plain = [Security.Cryptography.ProtectedData]::Unprotect(
    $blob, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
# 只把解密结果写给调用进程（stdout），不写日志、不落盘
[Console]::Out.Write([Text.Encoding]::UTF8.GetString($plain))
