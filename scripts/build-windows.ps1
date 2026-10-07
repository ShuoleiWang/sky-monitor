<#
.SYNOPSIS
    Builds the Sky Monitor product for Windows: a folder with sky-monitor.exe and
    sky-monitor-tray.exe (PyInstaller), an ffmpeg executable, and an installer
    (Inno Setup, when ISCC.exe is on PATH) or a zip otherwise.

.PARAMETER FfmpegUrl
    A zip of an ffmpeg build whose bin\ffmpeg.exe is shipped with the product.  The
    default is BtbN's LGPL build, so that the product keeps its own licence terms.
.PARAMETER FfmpegSha256
    When given, the download must have this SHA-256; the script prints the hash of
    what it downloaded so that it can be pinned next time.
.PARAMETER SkipFfmpeg
    Reuse packaging\ffmpeg\ffmpeg.exe from an earlier run.

Run from the repository root (any directory works; paths are taken from the script).
#>
param(
    [string]$FfmpegUrl = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n7.1-latest-win64-lgpl-7.1.zip",
    [string]$FfmpegSha256 = "",
    [switch]$SkipFfmpeg
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pack = Join-Path $root "packaging"
$build = Join-Path $root "build"
$venv = Join-Path $build "sky-monitor-venv"
$ffmpegDir = Join-Path $pack "ffmpeg"
New-Item -ItemType Directory -Force -Path $build, $ffmpegDir | Out-Null

Write-Host "== Python environment"
if (-not (Test-Path "$venv\Scripts\python.exe")) { & py -3.12 -m venv $venv }
& "$venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$venv\Scripts\python.exe" -m pip install --quiet -e "$root[tray]" "pyinstaller>=6.10"

if (-not $SkipFfmpeg) {
    Write-Host "== ffmpeg: $FfmpegUrl"
    $zip = Join-Path $build "ffmpeg-download.zip"
    Invoke-WebRequest -Uri $FfmpegUrl -OutFile $zip
    $hash = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
    Write-Host "   sha256 $hash"
    if ($FfmpegSha256 -and $hash -ne $FfmpegSha256.ToLower()) { throw "ffmpeg download does not match the given SHA-256" }
    $extract = Join-Path $build "ffmpeg-extract"
    if (Test-Path $extract) { Remove-Item -Recurse -Force $extract }
    Expand-Archive -Path $zip -DestinationPath $extract
    $exe = Get-ChildItem -Path $extract -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
    if (-not $exe) { throw "no ffmpeg.exe in the download" }
    Copy-Item $exe.FullName (Join-Path $ffmpegDir "ffmpeg.exe") -Force
    Get-ChildItem -Path $exe.Directory.Parent.FullName -Filter "LICENSE*" | ForEach-Object { Copy-Item $_.FullName (Join-Path $ffmpegDir $_.Name) -Force }
}
if (-not (Test-Path (Join-Path $ffmpegDir "ffmpeg.exe"))) { throw "packaging\ffmpeg\ffmpeg.exe is missing" }

Write-Host "== PyInstaller"
& "$venv\Scripts\pyinstaller.exe" --noconfirm --clean `
    --distpath (Join-Path $build "sky-monitor-dist") --workpath (Join-Path $build "sky-monitor-work") `
    (Join-Path $pack "sky-monitor.spec")
$dist = Join-Path $build "sky-monitor-dist\sky-monitor"
& (Join-Path $dist "sky-monitor.exe") --version

$iscc = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
if ($iscc) {
    Write-Host "== Inno Setup"
    & $iscc.Source (Join-Path $pack "installer.iss")
    Get-ChildItem -Path $build -Filter "SkyMonitor-Setup-*.exe" | ForEach-Object { Write-Host "installer: $($_.FullName)" }
} else {
    Write-Host "== Inno Setup (ISCC.exe) not found: zipping the folder instead"
    $zipOut = Join-Path $build "SkyMonitor-windows-x64.zip"
    if (Test-Path $zipOut) { Remove-Item $zipOut }
    Compress-Archive -Path "$dist\*" -DestinationPath $zipOut
    Write-Host "zip: $zipOut"
}
Write-Host "RESULT: build finished"
