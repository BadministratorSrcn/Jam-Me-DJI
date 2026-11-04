param(
    [Parameter(Mandatory = $true)][string]$TilesDir,
    [string]$PcIp = "",
    [int]$Port = 8080,
    [double]$Vmax = 1.2,
    [double]$StopRadius = 2.5,
    [string]$Source = "rtmp://127.0.0.1:1935/live"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$tiles = (Resolve-Path $TilesDir).Path

if (-not (Test-Path (Join-Path $tiles "tile_config.json"))) {
    throw "tile_config.json bulunamadi: $tiles"
}

# Refresh PATH so a freshly installed ffmpeg is visible.
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path", "User")

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw "ffmpeg bulunamadi. 'winget install Gyan.FFmpeg' ile kurun."
}

if (-not $PcIp) {
    $PcIp = (Get-NetIPAddress -AddressFamily IPv4 |
             Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
             Sort-Object -Property InterfaceAlias |
             Select-Object -First 1).IPAddress
}
if (-not $PcIp) { throw "PC IP belirlenemedi; -PcIp ile verin." }

$mtx = (Get-Command mediamtx -ErrorAction SilentlyContinue).Source
if (-not $mtx) {
    $candidate = Join-Path $env:LOCALAPPDATA "JamMe\mediamtx\mediamtx.exe"
    if (Test-Path $candidate) { $mtx = $candidate }
}
if (-not $mtx) { throw "mediamtx bulunamadi (PATH veya %LOCALAPPDATA%\JamMe\mediamtx)." }

function Start-Console([string]$title, [string]$command) {
    $cmd = "`$host.UI.RawUI.WindowTitle='$title'; $command"
    Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoExit", "-Command", $cmd) | Out-Null
}

Start-Console "JAM-Me RTMP" "& '$mtx'"
Start-Sleep -Seconds 2

$vmaxStr = $Vmax.ToString([System.Globalization.CultureInfo]::InvariantCulture)
$stopStr = $StopRadius.ToString([System.Globalization.CultureInfo]::InvariantCulture)
$bridgeCmd = "cd '$root'; `$env:Path='$($env:Path)'; python pc_bridge\bridge_server.py --source '$Source' --tiles-dir '$tiles' --port $Port --vmax $vmaxStr --stop-radius $stopStr"
Start-Console "JAM-Me Bridge" $bridgeCmd

Start-Sleep -Seconds 2
$guiCmd = "cd '$root'; `$env:Path='$($env:Path)'; python pc_bridge\control_gui.py --tiles-dir '$tiles' --port $Port"
Start-Console "JAM-Me GUI" $guiCmd

Write-Host ""
Write-Host "JAM-Me gercek ucus yigini baslatildi." -ForegroundColor Green
Write-Host "  RTMP     : rtmp://$($PcIp):1935/live"
Write-Host "  Bridge   : http://$($PcIp):$Port  (vmax=$vmaxStr m/s, stop=$stopStr m)"
Write-Host "  Karolar  : $tiles"
Write-Host ""
Write-Host "Telefon uygulamasi:"
Write-Host "  PC IP    : $PcIp"
Write-Host "  Port     : $Port"
Write-Host "  RTMP     : rtmp://$($PcIp):1935/live"
Write-Host ""
Write-Host "Windows firewall 8080/1935 kapaliysa yonetici PowerShell'de acin:"
Write-Host "  netsh advfirewall firewall add rule name='JamMe 8080' dir=in action=allow protocol=TCP localport=8080"
Write-Host "  netsh advfirewall firewall add rule name='JamMe 1935' dir=in action=allow protocol=TCP localport=1935"
