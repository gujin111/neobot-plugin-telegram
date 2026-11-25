<#
.SYNOPSIS
  Windows 宿主侧自检：找出容器/虚拟机能用的代理地址，必要时放开防火墙端口。

.DESCRIPTION
  在「跑 Docker Desktop / WSL 的那台 Windows」上执行（普通权限即可查看；
  加 -OpenFirewall 需要管理员）。它会：

  1. 列出宿主局域网 IPv4；
  2. 找出代理类进程（Clash / mihomo / v2ray / sing-box / Xray …）及其监听端口；
  3. 判断监听地址：只有 127.0.0.1 时**容器连不上**，会明确提示改成 0.0.0.0 / 开「允许局域网连接」；
  4. 打印容器里应该填的 proxy 地址；
  5. -OpenFirewall <端口> 时创建入站放行规则（管理员）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File extras\windows-host-proxy.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File extras\windows-host-proxy.ps1 -OpenFirewall 7890
#>
[CmdletBinding()]
param(
    [int]$OpenFirewall = 0,
    [int[]]$Ports = @(7890, 7891, 7897, 7898, 1080, 10808, 10809, 20171, 3128, 8889)
)

$ErrorActionPreference = 'Continue'

function Write-Section($text) { Write-Host ""; Write-Host "== $text ==" -ForegroundColor Cyan }
function Write-Ok($text) { Write-Host "  [OK] $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "  [!]  $text" -ForegroundColor Yellow }

Write-Section '宿主局域网 IPv4'
$addresses = @()
try {
    $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
        Select-Object @{ n = 'IPAddress'; e = { $_.IPAddress } },
                      @{ n = 'InterfaceAlias'; e = { $_.InterfaceAlias } },
                      @{ n = 'PrefixOrigin'; e = { $_.PrefixOrigin } })
} catch {
    $addresses = @()
}
if (-not $addresses) {
    # 精简系统 / 受限策略下没有 NetTCPIP 模块，退回 CIM 查询
    foreach ($ip in @(Get-CimInstance Win32_NetworkAdapterConfiguration -ErrorAction SilentlyContinue |
            Where-Object { $_.IPEnabled } | ForEach-Object { $_.IPAddress })) {
        if ($ip -match '^\d{1,3}(\.\d{1,3}){3}$' -and $ip -notlike '127.*' -and $ip -notlike '169.254.*') {
            $addresses += [pscustomobject]@{ IPAddress = $ip; InterfaceAlias = 'CIM'; PrefixOrigin = 'cim' }
        }
    }
}
if (-not $addresses) {
    # 再退回 ipconfig 文本解析（不需要 NetTCPIP / CIM）
    foreach ($line in @(& ipconfig 2>$null)) {
        if ($line -notmatch 'IPv4') { continue }
        $match = [regex]::Match($line, '(\d{1,3}(?:\.\d{1,3}){3})')
        if ($match.Success) {
            $ip = $match.Groups[1].Value
            if ($ip -notlike '127.*' -and $ip -notlike '169.254.*') {
                $addresses += [pscustomobject]@{ IPAddress = $ip; InterfaceAlias = 'ipconfig'; PrefixOrigin = 'ipconfig' }
            }
        }
    }
}
if (-not $addresses) {
    # 最后退回 .NET DNS 解析主机名
    try {
        foreach ($item in [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName())) {
            $ip = $item.IPAddressToString
            if ($ip -match '^\d{1,3}(\.\d{1,3}){3}$' -and $ip -notlike '127.*') {
                $addresses += [pscustomobject]@{ IPAddress = $ip; InterfaceAlias = 'DNS'; PrefixOrigin = 'dns' }
            }
        }
    } catch {
    }
}
if (-not $addresses) {
    Write-Warn '没取到局域网地址'
}
foreach ($item in $addresses) {
    Write-Host ("  {0,-16} {1}  ({2})" -f $item.IPAddress, $item.InterfaceAlias, $item.PrefixOrigin)
}

Write-Section '代理进程'
$proxyPattern = 'clash|verge|mihomo|v2ray|xray|sing-box|nekoray|shadowsocks|trojan'
$proxyProcesses = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match $proxyPattern })
if (-not $proxyProcesses) {
    Write-Warn '没有发现代理进程（Clash / v2rayN / sing-box 都没在跑）'
} else {
    foreach ($process in $proxyProcesses) {
        Write-Host ("  {0} (PID {1})  {2}" -f $process.ProcessName, $process.Id, $process.Path)
    }
}

Write-Section '监听端口（代理相关）'
$listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue)
if (-not $listeners) {
    # 没有 NetTCPIP 时解析 netstat
    foreach ($line in @(& netstat -ano 2>$null)) {
        $parts = @(($line -split '\s+') | Where-Object { $_ })
        if ($parts.Count -ge 5 -and $parts[0] -eq 'TCP' -and $parts[3] -eq 'LISTENING') {
            $local = $parts[1]
            $index = $local.LastIndexOf(':')
            if ($index -gt 0) {
                $listeners += [pscustomobject]@{
                    LocalAddress  = $local.Substring(0, $index).Trim('[', ']')
                    LocalPort     = [int]$local.Substring($index + 1)
                    OwningProcess = [int]$parts[4]
                }
            }
        }
    }
}
$proxyPids = @($proxyProcesses | ForEach-Object { $_.Id })
$hits = @($listeners | Where-Object {
        ($Ports -contains $_.LocalPort) -or ($proxyPids -contains $_.OwningProcess)
    })

$usable = @()
if (-not $hits) {
    Write-Warn '没有代理端口在监听'
} else {
    foreach ($hit in ($hits | Sort-Object LocalPort -Unique)) {
        $owner = (Get-Process -Id $hit.OwningProcess -ErrorAction SilentlyContinue).ProcessName
        $local = $hit.LocalAddress
        $loopbackOnly = ($local -eq '127.0.0.1') -or ($local -eq '::1')
        $hostHint = if ($loopbackOnly) { '只监听本机 → 容器连不到' } else { '对外监听 → 容器可用' }
        Write-Host ("  {0}:{1}  进程={2}  {3}" -f $local, $hit.LocalPort, $owner, $hostHint)
        if (-not $loopbackOnly) {
            $firstIp = ($addresses | Select-Object -First 1).IPAddress
            $usable += [pscustomobject]@{ Port = $hit.LocalPort; Address = $firstIp }
        }
    }
}

Write-Section '容器里应该填'
if ($usable) {
    foreach ($entry in ($usable | Sort-Object Port -Unique)) {
        Write-Ok ("proxy = ""http://host.docker.internal:{0}""" -f $entry.Port)
        Write-Host ("         （Docker Desktop 用它；Linux/WSL 容器改成 http://{0}:{1}）" -f $entry.Address, $entry.Port)
    }
    Write-Host ''
    Write-Host '  在容器里验证：' -NoNewline
    Write-Host ("  curl -sS -m 10 -x http://host.docker.internal:{0} https://api.telegram.org/bot<TOKEN>/getMe" -f ($usable[0].Port))
} else {
    Write-Warn '当前没有可给容器使用的代理地址。'
    Write-Host '     有代理软件 → 打开「允许局域网连接」(allow-lan: true / bind-address: "*") 后重跑本脚本；'
    Write-Host '     没有代理   → 用 extras/cloudflare-worker.js 部署一个反代，把插件 endpoint 指过去。'
}

Write-Section 'WSL（如果你把 NeoBot 跑在 WSL 而不是 Docker）'
$wsl = Get-Command wsl -ErrorAction SilentlyContinue
if ($wsl) {
    $distros = @(& wsl -l -q 2>$null | Where-Object { $_ -and $_ -notmatch '^\s*$' })
    if ($distros) {
        Write-Host ("  发行版: " + ($distros -join '、'))
        Write-Host '  在 WSL 里执行 ip route show default，取 default via 后面的地址，就是 Windows 宿主：'
        Write-Host '    ip route show default'
        Write-Host '  然后插件里填 proxy = "http://<那个地址>:7890"'
    } else {
        Write-Warn 'WSL 未安装发行版（或 wsl 不可用）'
    }
}

if ($OpenFirewall -gt 0) {
    Write-Section ("放行防火墙入站 TCP " + $OpenFirewall)
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Warn '需要管理员权限：请用「以管理员身份运行」重开 PowerShell 再执行本命令'
    } else {
        $name = "NeoBot proxy TCP $OpenFirewall"
        $existing = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
        if ($existing) {
            Write-Ok "规则已存在：$name"
        } else {
            New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP `
                -LocalPort $OpenFirewall -Action Allow -Profile Private, Domain | Out-Null
            Write-Ok "已创建入站规则：$name（Private/Domain）"
        }
    }
}

Write-Host ''
