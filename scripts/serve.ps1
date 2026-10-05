# Start llama-server with settings tuned for a 4 GB GPU + 16 GB RAM laptop.
#   powershell -ExecutionPolicy Bypass -File scripts\serve.ps1                       # first models\*.gguf
#   powershell -ExecutionPolicy Bypass -File scripts\serve.ps1 -Model models\forge-qwen3-8b-Q4_K_M.gguf
#   ... -Model models\Qwen3-8B-Q4_K_M.gguf -Lora models\forge-lora.gguf             # base + adapter
#   ... -Draft models\Qwen3-0.6B-Q8_0.gguf                                           # speculative decoding
param(
    [string]$Model = "",
    [string]$Lora = "",
    [string]$Draft = "",
    [int]$Ctx = 8192,
    [string]$GpuLayers = "auto",   # "auto" lets llama.cpp fit as many layers as VRAM allows
    [int]$Threads = 8,             # physical cores; hyper-threads don't help token generation
    [int]$Port = 8080
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\.."

$server = Get-ChildItem "$root\tools\llama.cpp" -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $server) { throw "llama-server.exe not found; run scripts\setup_llamacpp.ps1 first" }

if (-not $Model) {
    $found = Get-ChildItem "$root\models" -Filter "*.gguf" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notmatch "lora|0\.6B" } | Select-Object -First 1
    if (-not $found) { throw "no model in $root\models; put your .gguf there or pass -Model" }
    $Model = $found.FullName
}

$serverArgs = @(
    "-m", $Model,
    "--host", "127.0.0.1", "--port", $Port,
    "-c", $Ctx,
    "-t", $Threads,
    "--jinja",                       # use the model's own chat template, enables tool calls
    "--reasoning-budget", "0",       # Qwen3 thinking off: much faster replies
    "-fa", "on",                     # flash attention: less VRAM per token of context
    "-ctk", "q8_0", "-ctv", "q8_0",  # 8-bit KV cache: half the memory of f16, ~no quality loss
    "-np", "1",                      # one chat at a time gets the whole context
    "--cache-reuse", "256"           # reuse the cached prompt across turns
)
if ($GpuLayers -ne "auto") { $serverArgs += @("-ngl", $GpuLayers) }
if ($Lora) { $serverArgs += @("--lora", $Lora) }
if ($Draft) { $serverArgs += @("-md", $Draft, "--draft-max", "16", "--draft-min", "4", "-ngld", "99") }

Write-Host "model: $Model"
& $server.FullName @serverArgs
