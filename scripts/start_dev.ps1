param(
    [int]$BackendPort = 8001,
    [int]$FrontendPort = 4000,
    [switch]$SkipDocker,
    [switch]$SkipFrontend,
    [switch]$SkipModelCheck
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $projectRoot "backend"
$frontendDir = Join-Path $projectRoot "frontend"
$pythonExe = Join-Path $backendDir ".venv\Scripts\python.exe"
$nextBin = Join-Path $frontendDir "node_modules\next\dist\bin\next"
$modelHome = Join-Path $projectRoot ".data\hf-model"
$logDir = Join-Path $projectRoot ".data\logs"
$runDir = Join-Path $projectRoot ".data\run"

function Write-Step([string]$Message) { Write-Host "[..] $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "[ok] $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "[!!] $Message" -ForegroundColor Yellow }
function Write-Fail([string]$Message) { Write-Host "[xx] $Message" -ForegroundColor Red }

function Test-Port([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(400)) { return $false }
        $client.EndConnect($async)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Test-Http([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
    }
    catch {
        return $false
    }
}

function Wait-Until([scriptblock]$Condition, [int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

New-Item -ItemType Directory -Force -Path $logDir, $runDir | Out-Null

Write-Host ""
Write-Host "Devcooper local startup" -ForegroundColor White
Write-Host "Project: $projectRoot"
Write-Host ""

if (-not (Test-Path (Join-Path $projectRoot ".env"))) {
    Write-Fail "Missing .env. Copy .env.example to .env and fill in LLM_API_KEY / GITHUB_TOKEN first."
    exit 1
}
if (-not (Test-Path $pythonExe)) {
    Write-Fail "Missing backend\.venv. Create it with:"
    Write-Host "       cd backend"
    Write-Host "       python -m venv .venv"
    Write-Host "       .venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}
if (-not (Test-Path $nextBin)) {
    Write-Fail "Missing frontend\node_modules. Run: cd frontend; npm install"
    exit 1
}
if (-not $SkipModelCheck -and -not (Test-Path (Join-Path $modelHome "hub"))) {
    Write-Fail "Missing local embedding model in .data\hf-model."
    Write-Host "       Run: .\scripts\download_embedding_model.ps1"
    exit 1
}

function Test-DockerEngine {
    & cmd /c "docker info >NUL 2>&1"
    return ($LASTEXITCODE -eq 0)
}

# Ensure the Python environment is usable.
$previousPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $pythonExe -c "import fastapi, uvicorn, sentence_transformers" 2>$null
$importExit = $LASTEXITCODE
$ErrorActionPreference = $previousPreference
if ($importExit -ne 0) {
    Write-Fail "Backend Python packages are incomplete. Run:"
    Write-Host "       $pythonExe -m pip install -r `"$backendDir\requirements.txt`""
    Write-Host "       $pythonExe -m pip install -r `"$backendDir\requirements-local-embedding.txt`""
    exit 1
}

if (-not $SkipDocker) {
    $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $dockerCmd) {
        Write-Fail "Docker CLI not found. Install Docker Desktop, or use -SkipDocker with an external PostgreSQL/Milvus."
        exit 1
    }
    if (-not (Test-DockerEngine)) {
        $desktopExe = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"
        if (Test-Path $desktopExe) {
            Write-Step "Docker engine is not running; starting Docker Desktop..."
            Start-Process -FilePath $desktopExe | Out-Null
            $engineReady = Wait-Until { return (Test-DockerEngine) } 120
            if (-not $engineReady) {
                Write-Fail "Docker engine did not become ready within 120s."
                exit 1
            }
        }
        else {
            Write-Fail "Docker engine is not running. Start Docker Desktop first."
            exit 1
        }
    }

    $ErrorActionPreference = "Continue"
    Write-Step "Starting PostgreSQL, etcd, MinIO and Milvus..."
    Push-Location $projectRoot
    try {
        & docker compose up -d postgres etcd minio milvus
        $composeExit = $LASTEXITCODE
    }
    finally {
        Pop-Location
        $ErrorActionPreference = $previousPreference
    }
    if ($composeExit -ne 0) {
        Write-Fail "docker compose failed."
        exit 1
    }

    Write-Step "Waiting for PostgreSQL..."
    $postgresReady = Wait-Until {
        $status = (& docker inspect --format "{{.State.Health.Status}}" devcooper-postgres 2>$null)
        return ($status -eq "healthy")
    } 120
    if (-not $postgresReady) {
        Write-Fail "PostgreSQL did not become healthy within 120s. Check: docker logs devcooper-postgres"
        exit 1
    }
    Write-Step "Waiting for Milvus on port 19530..."
    $milvusReady = Wait-Until { return (Test-Port 19530) } 180
    if (-not $milvusReady) {
        Write-Warn "Milvus is not ready yet. The backend will start, but RAG vector store may report degraded."
    }
    Write-Ok "Infrastructure is ready."
}
else {
    Write-Warn "Skipping Docker checks (-SkipDocker)."
}

$backendPidFile = Join-Path $runDir "backend.pid"
$backendStarted = $false
if (Test-Port $BackendPort) {
    if (Test-Http "http://127.0.0.1:$BackendPort/health") {
        Write-Ok "Backend already running on port $BackendPort."
    }
    else {
        Write-Fail "Port $BackendPort is occupied by another process. Stop it or choose -BackendPort."
        exit 1
    }
}
else {
    Write-Step "Starting backend on http://127.0.0.1:$BackendPort ..."
    $env:HF_HOME = $modelHome
    $env:HF_HUB_OFFLINE = "1"
    $env:TRANSFORMERS_OFFLINE = "1"
    $env:HF_HUB_DISABLE_SYMLINKS = "1"
    $backendOut = Join-Path $logDir "backend.out.log"
    $backendErr = Join-Path $logDir "backend.err.log"
    $backendIn = Join-Path $runDir "backend.stdin"
    New-Item -ItemType File -Force -Path $backendIn | Out-Null
    $backendProcess = Start-Process -FilePath $pythonExe `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$BackendPort") `
        -WorkingDirectory $backendDir `
        -RedirectStandardInput $backendIn `
        -RedirectStandardOutput $backendOut `
        -RedirectStandardError $backendErr `
        -WindowStyle Hidden `
        -PassThru
    $backendProcess.Id | Set-Content -Path $backendPidFile
    $backendStarted = $true
    $backendReady = Wait-Until {
        if ($backendProcess.HasExited) { return $false }
        return (Test-Http "http://127.0.0.1:$BackendPort/health")
    } 90
    if (-not $backendReady) {
        Write-Fail "Backend did not become ready. Last log lines:"
        if (Test-Path $backendErr) { Get-Content $backendErr -Tail 30 }
        exit 1
    }
    Write-Ok "Backend is ready (PID $($backendProcess.Id))."
}

if (-not $SkipFrontend) {
    $frontendPidFile = Join-Path $runDir "frontend.pid"
    if (Test-Port $FrontendPort) {
        if (Test-Http "http://127.0.0.1:$FrontendPort/") {
            Write-Ok "Frontend already running on port $FrontendPort."
        }
        else {
            Write-Fail "Port $FrontendPort is occupied by another process. Stop it or choose -FrontendPort."
            exit 1
        }
    }
    else {
        Write-Step "Starting frontend on http://127.0.0.1:$FrontendPort ..."
        $nodeCmd = Get-Command node -ErrorAction SilentlyContinue
        if (-not $nodeCmd) {
            Write-Fail "Node.js not found in PATH."
            exit 1
        }
        $env:NEXT_PUBLIC_API_BASE_URL = "http://localhost:$BackendPort"
        $frontendOut = Join-Path $logDir "frontend.out.log"
        $frontendErr = Join-Path $logDir "frontend.err.log"
        $frontendIn = Join-Path $runDir "frontend.stdin"
        New-Item -ItemType File -Force -Path $frontendIn | Out-Null
        $frontendProcess = Start-Process -FilePath $nodeCmd.Source `
            -ArgumentList @("`"$nextBin`"", "dev", "-p", "$FrontendPort") `
            -WorkingDirectory $frontendDir `
            -RedirectStandardInput $frontendIn `
            -RedirectStandardOutput $frontendOut `
            -RedirectStandardError $frontendErr `
            -WindowStyle Hidden `
            -PassThru
        $frontendProcess.Id | Set-Content -Path $frontendPidFile
        $frontendReady = Wait-Until {
            if ($frontendProcess.HasExited) { return $false }
            return (Test-Http "http://127.0.0.1:$FrontendPort/")
        } 180
        if (-not $frontendReady) {
            Write-Fail "Frontend did not become ready. Last log lines:"
            if (Test-Path $frontendErr) { Get-Content $frontendErr -Tail 30 }
            exit 1
        }
        Write-Ok "Frontend is ready (PID $($frontendProcess.Id))."
    }
}

Write-Host ""
Write-Host "Devcooper is running:" -ForegroundColor Green
Write-Host "  Frontend: http://localhost:$FrontendPort"
Write-Host "  Backend:  http://localhost:$BackendPort"
Write-Host "  API docs: http://localhost:$BackendPort/docs"
Write-Host "  Logs:     $logDir"
Write-Host "  Stop:     .\scripts\stop_dev.cmd"
Write-Host ""
if ($backendStarted) {
    Write-Host "First RAG request may take ~10s while the local Qwen embedding model loads." -ForegroundColor DarkGray
}
