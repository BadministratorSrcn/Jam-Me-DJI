param(
    [string]$Apk = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Apk) {
    $Apk = Join-Path $root "app\app\build\outputs\apk\debug\app-debug.apk"
}
if (-not (Test-Path $Apk)) {
    throw "APK bulunamadi: $Apk (once app klasorunde gradlew assembleDebug calistirin)"
}

$adb = Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"
if (-not (Test-Path $adb)) {
    $adb = (Get-Command adb -ErrorAction SilentlyContinue).Source
}
if (-not $adb) {
    throw "adb bulunamadi. Android SDK platform-tools kurun."
}

$devices = (& $adb devices) | Select-Object -Skip 1 | Where-Object { $_ -match "\sdevice$" }
if (-not $devices) {
    Write-Host "Bagli telefon bulunamadi." -ForegroundColor Yellow
    Write-Host "1) Telefonda Gelistirici Secenekleri > USB hata ayiklama acik olsun."
    Write-Host "2) USB kablosunu takin; telefonda 'Dosya aktarimi' / 'USB hata ayiklama' iznini onaylayin."
    Write-Host "3) Bu komutu tekrar calistirin."
    exit 1
}

Write-Host "Kuruluyor: $Apk" -ForegroundColor Cyan
& $adb install -r $Apk
if ($LASTEXITCODE -ne 0) {
    throw "adb install basarisiz (exit $LASTEXITCODE)"
}
Write-Host "APK kuruldu. RC-N2'yi baglayip Tactical Pilot uygulamasini acin." -ForegroundColor Green
