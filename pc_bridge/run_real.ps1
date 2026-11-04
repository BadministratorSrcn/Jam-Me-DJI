param(
    [Parameter(Mandatory = $true)][string]$TilesDir,
    [string]$PcIp = "",
    [int]$Port = 8080,
    [double]$Vmax = 1.2,
    [double]$StopRadius = 2.5,
    [string]$Source = "rtmp://127.0.0.1:1935/live",
    [string]$MapProvider = "local"
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

$mtx = $null
$isRtsp = $Source -like "rtsp://*"
if (-not $isRtsp) {
    $mtx = (Get-Command mediamtx -ErrorAction SilentlyContinue).Source
    if (-not $mtx) {
        $candidate = Join-Path $env:LOCALAPPDATA "JamMe\mediamtx\mediamtx.exe"
        if (Test-Path $candidate) { $mtx = $candidate }
    }
    if (-not $mtx) { throw "mediamtx bulunamadi (PATH veya %LOCALAPPDATA%\JamMe\mediamtx)." }
}

function Start-Console([string]$title, [string]$command) {
    $cmd = "`$host.UI.RawUI.WindowTitle='$title'; $command"
    Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoExit", "-Command", $cmd) | Out-Null
}

if ($isRtsp) {
    Start-Console "JAM-Me Bridge (RTSP)" "Write-Host 'RTSP source: $Source'"
    Start-Sleep -Seconds 1
} else {
    Start-Console "JAM-Me RTMP" "& '$mtx'"
    Start-Sleep -Seconds 2
}

$vmaxStr = $Vmax.ToString([System.Globalization.CultureInfo]::InvariantCulture)
$stopStr = $StopRadius.ToString([System.Globalization.CultureInfo]::InvariantCulture)
$bridgeCmd = "cd '$root'; `$env:Path='$($env:Path)'; python pc_bridge\bridge_server.py --source '$Source' --tiles-dir '$tiles' --port $Port --vmax $vmaxStr --stop-radius $stopStr"
Start-Console "JAM-Me Bridge" $bridgeCmd

Start-Sleep -Seconds 2
$guiMapProvider = $MapProvider
if ($MapProvider -eq "google") { $guiMapProvider = "esri" }
$guiExtra = ""
if ($isRtsp) { $guiExtra += " --rtsp-url 'http://127.0.0.1:$Port/stream.mjpg'" }
if ($guiMapProvider -ne "local") { $guiExtra += " --map-provider $guiMapProvider" }
$guiCmd = "cd '$root'; `$env:Path='$($env:Path)'; python pc_bridge\control_gui.py --tiles-dir '$tiles' --port $Port$guiExtra"
Start-Console "JAM-Me GUI" $guiCmd

if ($MapProvider -eq "google") {
    if (-not $env:GOOGLE_MAPS_API_KEY) {
        Write-Host "UYARI: GOOGLE_MAPS_API_KEY tanimli degil; Google harita anahtarsiz acilir." -ForegroundColor Yellow
        Write-Host "  Once: `$env:GOOGLE_MAPS_API_KEY='...'" -ForegroundColor Yellow
    }
    Start-Sleep -Seconds 1
    $pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if (-not $pyw) { $pyw = (Get-Command python).Source -replace 'python\.exe$','pythonw.exe' }
    Start-Process -FilePath $pyw -ArgumentList @(
        "pc_bridge\google_map.py", "--port", "$Port",
        "--width", "1000", "--height", "700", "--x", "80", "--y", "80"
    ) -WorkingDirectory $root
}

Write-Host ""
Write-Host "JAM-Me gercek ucus yigini baslatildi." -ForegroundColor Green
if ($isRtsp) {
    Write-Host "  RTSP     : $Source"
} else {
    Write-Host "  RTMP     : rtmp://$($PcIp):1935/live"
}
Write-Host "  Bridge   : http://$($PcIp):$Port  (vmax=$vmaxStr m/s, stop=$stopStr m)"
Write-Host "  Karolar  : $tiles"
if ($MapProvider -eq "google") {
    Write-Host "  Harita   : Google Maps (WebView2) + yerel ESRI yedek"
} else {
    Write-Host "  Harita   : $MapProvider (dinamik: kaydirdikca karo indirir)"
}
Write-Host ""
if ($isRtsp) {
    Write-Host "Telefon uygulamasi: RTSP STREAM (PHONE SERVER) secin."
    Write-Host "Gercek RTSP URL'sini uygulama logunda 'Live555 gen url' satirinda bulun."
} else {
    Write-Host "Telefon uygulamasi:"
    Write-Host "  PC IP    : $PcIp"
    Write-Host "  Port     : $Port"
    Write-Host "  RTMP     : rtmp://$($PcIp):1935/live"
}
Write-Host ""
if (-not $isRtsp) {
    Write-Host "Windows firewall 8080/1935 kapaliysa yonetici PowerShell'de acin:"
    Write-Host "  netsh advfirewall firewall add rule name='JamMe 8080' dir=in action=allow protocol=TCP localport=8080"
    Write-Host "  netsh advfirewall firewall add rule name='JamMe 1935' dir=in action=allow protocol=TCP localport=1935"
}
