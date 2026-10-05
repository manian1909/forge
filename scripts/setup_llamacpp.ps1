# Download prebuilt llama.cpp binaries (CUDA 12.4, Windows x64) into tools\llama.cpp
#   powershell -ExecutionPolicy Bypass -File scripts\setup_llamacpp.ps1
param(
    [string]$Cuda = "12.4",
    [string]$Dest = "$PSScriptRoot\..\tools\llama.cpp"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with the progress bar

$releases = Invoke-RestMethod "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10"
$pattern = "llama-b*-bin-win-cuda-$Cuda-x64.zip"
$release = $releases | Where-Object { $_.assets.name -like $pattern } | Select-Object -First 1
if (-not $release) { throw "no release with a $pattern asset found" }

$bin = $release.assets | Where-Object { $_.name -like $pattern } | Select-Object -First 1
$rt = $release.assets | Where-Object { $_.name -eq "cudart-llama-bin-win-cuda-$Cuda-x64.zip" } | Select-Object -First 1

New-Item -ItemType Directory -Force $Dest | Out-Null
foreach ($asset in @($bin, $rt)) {
    if (-not $asset) { continue }
    $zip = Join-Path $env:TEMP $asset.name
    Write-Host "downloading $($asset.name) ($([math]::Round($asset.size / 1MB)) MB)"
    # curl.exe ships with Windows 10+ and is far faster than Invoke-WebRequest
    curl.exe -L --fail --retry 3 -o $zip $asset.browser_download_url
    if ($LASTEXITCODE -ne 0) { throw "download failed: $($asset.name)" }
    Expand-Archive $zip -DestinationPath $Dest -Force
    Remove-Item $zip
}
$server = Get-ChildItem $Dest -Recurse -Filter "llama-server.exe" | Select-Object -First 1
Write-Host "llama.cpp $($release.tag_name) installed: $($server.FullName)"
