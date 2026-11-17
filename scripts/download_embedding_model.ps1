param(
    [string]$Model = "Qwen/Qwen3-Embedding-0.6B",
    [switch]$Official
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $projectRoot "backend"
$pythonExe = Join-Path $backendDir ".venv\Scripts\python.exe"
$modelHome = Join-Path $projectRoot ".data\hf-model"

if (-not (Test-Path $pythonExe)) {
    Write-Host "[xx] Missing backend\.venv. Create the venv and install requirements first." -ForegroundColor Red
    exit 1
}

& $pythonExe -c "import huggingface_hub" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[..] Installing huggingface_hub..." -ForegroundColor Cyan
    & $pythonExe -m pip install huggingface_hub
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

New-Item -ItemType Directory -Force -Path $modelHome | Out-Null

$env:HF_HOME = $modelHome
$env:HF_HUB_DISABLE_SYMLINKS = "1"
if (-not $Official) {
    # huggingface.co is often unreachable from mainland China; hf-mirror.com is a public mirror.
    $env:HF_ENDPOINT = "https://hf-mirror.com"
    Write-Host "[..] Downloading $Model from https://hf-mirror.com ..." -ForegroundColor Cyan
}
else {
    Remove-Item Env:HF_ENDPOINT -ErrorAction SilentlyContinue
    Write-Host "[..] Downloading $Model from https://huggingface.co ..." -ForegroundColor Cyan
}

$script = "from huggingface_hub import snapshot_download; print(snapshot_download('$Model'))"
& $pythonExe -c $script
if ($LASTEXITCODE -ne 0) {
    Write-Host "[xx] Download failed. Check your network and try -Official if the mirror is unavailable." -ForegroundColor Red
    exit $LASTEXITCODE
}

$snapshotRoot = Join-Path $modelHome "hub\models--Qwen--Qwen3-Embedding-0.6B\snapshots"
if (Test-Path $snapshotRoot) {
    $size = (Get-ChildItem $snapshotRoot -Recurse -File | Measure-Object -Property Length -Sum).Sum
    Write-Host ("[ok] Model is ready in {0} ({1:N2} GB)." -f $modelHome, ($size / 1GB)) -ForegroundColor Green
}
else {
    Write-Host "[!!] Download completed but the Qwen snapshot directory was not found." -ForegroundColor Yellow
}
