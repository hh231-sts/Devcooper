param(
    [switch]$BackendOnly,
    [switch]$FrontendOnly
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runDir = Join-Path $projectRoot ".data\run"

function Stop-ServiceByPidFile([string]$Name) {
    $pidFile = Join-Path $runDir "$Name.pid"
    if (-not (Test-Path $pidFile)) {
        Write-Host "[--] No PID file for $Name." -ForegroundColor DarkGray
        return
    }
    $processId = (Get-Content $pidFile -Raw).Trim()
    if ($processId -match '^\d+$') {
        & taskkill /PID $processId /T /F *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[ok] Stopped $Name (PID $processId)." -ForegroundColor Green
        }
        else {
            Write-Host "[!!] $Name (PID $processId) was not running." -ForegroundColor Yellow
        }
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

if (-not $FrontendOnly) { Stop-ServiceByPidFile "backend" }
if (-not $BackendOnly) { Stop-ServiceByPidFile "frontend" }
