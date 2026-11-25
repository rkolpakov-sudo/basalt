# Basalt server control: start / stop / restart / toggle / status.
# One entry point for the desktop launcher (Basalt.bat) and the web UI (POST /api/restart).
param(
    [ValidateSet('start', 'stop', 'restart', 'toggle', 'status')] [string]$Action = 'status',
    [int]$Port = 8080,
    [int]$WaitSeconds = 180,
    [switch]$Open,          # open the web UI once the server answers (the launcher, not a restart)
    [switch]$Quiet          # status: no output, exit code only (0 running, 1 stopped)
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Engine = Join-Path $Root 'bin\basalt.exe'
$Config = Join-Path $Root 'basalt_config.json'
$Url = "http://127.0.0.1:$Port/"

function Write-Line($text) { if (-not $Quiet) { Write-Host $text } }

function Get-Listener {
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
}

function Test-Ready {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/models" -TimeoutSec 3 -UseBasicParsing
        return ($r.Content -match 'basalt')
    } catch { return $false }
}

function Test-Running { return [bool](Get-Listener) }

function Stop-Server {
    $listener = Get-Listener
    $killed = @()
    if ($listener) {
        $owner = $listener.OwningProcess
        $parent = 0
        try { $parent = (Get-CimInstance Win32_Process -Filter "ProcessId=$owner").ParentProcessId } catch { }
        foreach ($procId in @($owner, $parent)) {
            if ($procId -and $procId -gt 0) {
                try { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue; $killed += $procId } catch { }
            }
        }
    }
    # strays: an engine left behind by a killed server, or a second one from an aborted start
    Get-Process -Name basalt -ErrorAction SilentlyContinue | ForEach-Object {
        try { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue; $killed += $_.Id } catch { }
    }
    for ($i = 0; $i -lt 40 -and (Get-Listener); $i++) { Start-Sleep -Milliseconds 500 }
    if ($killed.Count) { Write-Line "[Basalt] Stopped (processes: $($killed -join ', '))." }
    else { Write-Line '[Basalt] The server was not running.' }
    return [bool]$killed.Count
}

function New-StartupInfo([int]$show) {
    # a CIM instance with its properties set at creation: a ClientOnly object has no settable props
    return New-CimInstance -ClassName Win32_ProcessStartup -Namespace root/cimv2 -ClientOnly `
        -Property @{ ShowWindow = [uint16]$show }    # 7 = minimized, shown without taking focus
}

function Invoke-WmiCreate([string]$command, [int]$show) {
    $res = Invoke-CimMethod -ClassName Win32_Process -Namespace root/cimv2 -MethodName Create -Arguments @{
        CommandLine               = $command
        CurrentDirectory          = $Root
        ProcessStartupInformation = (New-StartupInfo $show)
    }
    if ($res.ReturnValue -ne 0) { throw "Win32_Process.Create returned $($res.ReturnValue)" }
    return $res.ProcessId
}

function Start-Server {
    if (Test-Running) {
        Write-Line '[Basalt] The server is already running.'
    } else {
        if (-not (Test-Path $Engine)) { throw "not found: $Engine" }
        Write-Line '[Basalt] Starting the server; no model loads at startup - pick one in the web UI or let the first request load it...'
        # a minimized console, shown without stealing focus from the window you are working in
        $cmd = "cmd /c cd /d `"$Root`" && `"$Engine`" serve --config `"$Config`""
        Invoke-WmiCreate $cmd 7 | Out-Null
    }
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Ready) {
            Write-Line "[Basalt] Ready: $Url"
            if ($Open) { Start-Process $Url }
            return $true
        }
        Start-Sleep -Seconds 2
    }
    Write-Line "[Basalt] No answer after $WaitSeconds s. See the minimized 'Basalt' engine window for the reason."
    return $false
}

switch ($Action) {
    'status' {
        $running = Test-Running
        if ($Quiet) { exit $(if ($running) { 0 } else { 1 }) }
        if ($running) { Write-Host "[Basalt] Running: $Url" } else { Write-Host '[Basalt] Stopped.' }
        exit $(if ($running) { 0 } else { 1 })
    }
    'stop' { Stop-Server | Out-Null; exit 0 }
    'start' { if (Start-Server) { exit 0 } else { exit 1 } }
    'restart' {
        Stop-Server | Out-Null
        Start-Sleep -Seconds 1
        if (Start-Server) { exit 0 } else { exit 1 }
    }
    'toggle' {
        if (Test-Running) { Stop-Server | Out-Null; exit 0 }
        if (Start-Server) { exit 0 } else { exit 1 }
    }
}
