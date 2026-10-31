#requires -Version 5.1
<#
.SYNOPSIS
  把本 skill 的独立 Edge/Chrome 窗口置前/置顶（人机验证、登录窗口被挡住时使用）。

.DESCRIPTION
  只按窗口标题匹配浏览器窗口（msedge/chrome），不会操作其他应用。用于 browser_dl.js
  进入 NEED_USER（Cloudflare 人机验证、统一认证等）但用户找不到窗口的场景。

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\raise_browser.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\raise_browser.ps1 -TopMost
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\raise_browser.ps1 -KeepTopMost
#>
[CmdletBinding()]
param(
    [string]$TitlePattern = '请稍候|Are you a robot|ScienceDirect|WebVPN|统一身份认证|验证|中国知网|万方|维普|登录',
    [switch]$TopMost,
    [switch]$KeepTopMost,
    [switch]$ListOnly
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class CampusWin32 {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern bool FlashWindowEx(ref FLASHWINFO fwi);
    [StructLayout(LayoutKind.Sequential)] public struct FLASHWINFO { public uint cbSize; public IntPtr hwnd; public uint dwFlags; public uint uCount; public uint dwTimeout; }
}
"@

$procs = @(Get-Process msedge, chrome -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle })
$matches = @($procs | Where-Object { $_.MainWindowTitle -match $TitlePattern })
if (-not $matches) {
    Write-Output (([ordered]@{
        status = 'not_found'
        pattern = $TitlePattern
        windows = @($procs | ForEach-Object { $_.MainWindowTitle })
        hint = '没找到匹配的浏览器窗口；可能尚未打开或标题已变化，可在 -TitlePattern 里换关键词'
    }) | ConvertTo-Json -Depth 4)
    exit 2
}
$results = @()
foreach ($p in $matches) {
    $h = $p.MainWindowHandle
    [CampusWin32]::ShowWindowAsync($h, 9) | Out-Null   # SW_RESTORE
    [CampusWin32]::SetForegroundWindow($h) | Out-Null
    if ($TopMost -or $KeepTopMost) {
        [CampusWin32]::SetWindowPos($h, [IntPtr](-1), 0, 0, 0, 0, 0x0001 -bor 0x0002 -bor 0x0040) | Out-Null
        if (-not $KeepTopMost) {
            Start-Sleep -Milliseconds 1200
            [CampusWin32]::SetWindowPos($h, [IntPtr](-2), 0, 0, 0, 0, 0x0001 -bor 0x0002 -bor 0x0040) | Out-Null
        }
    }
    $flash = New-Object CampusWin32+FLASHWINFO
    $flash.cbSize = [uint32][Runtime.InteropServices.Marshal]::SizeOf($flash)
    $flash.hwnd = $h
    $flash.dwFlags = 3      # FLASHW_ALL
    $flash.uCount = 5
    $flash.dwTimeout = 0
    [CampusWin32]::FlashWindowEx([ref]$flash) | Out-Null
    $results += [ordered]@{ process = $p.ProcessName; pid = $p.Id; title = $p.MainWindowTitle }
}
if (-not $ListOnly) {
    Write-Output (([ordered]@{ status = 'raised'; keep_topmost = [bool]$KeepTopMost; windows = $results }) | ConvertTo-Json -Depth 4)
}
