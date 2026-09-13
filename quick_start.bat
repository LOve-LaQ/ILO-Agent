# ========================================
# ILO-Agent Demo - Quick Start Script
# ========================================

param(
    [string]$Service = "all"  # all, qdrant, api, frontend
)

# 颜色设置
$SuccessColor = "Green"
$InfoColor = "Cyan"
$WarnColor = "Yellow"

function Write-Success {
    param([string]$Message)
    Write-Host "[✅] $Message" -ForegroundColor $SuccessColor
}

function Write-Info {
    param([string]$Message)
    Write-Host "[ℹ️] $Message" -ForegroundColor $InfoColor
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[⚠️] $Message" -ForegroundColor $WarnColor
}

# ========================================
# Step 1: 检查并启动 Qdrant
# ========================================
if ($Service -eq "all" -or $Service -eq "qdrant") {
    Write-Info "=== Checking Qdrant Vector Database ==="
    
    # 检查 Docker 是否运行
    $dockerCheck = docker ps 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Docker Desktop is not running. Please start it first."
        exit 1
    }
    
    # 检查容器是否已存在
    $existingContainer = docker ps -a --filter "name=ilo-qdrant" --format "{{.Names}}"
    
    if ($existingContainer -eq "ilo-qdrant") {
        # 容器存在，检查状态
        $containerStatus = docker inspect ilo-qdrant --format "{{.State.Status}}"
        
        if ($containerStatus -eq "running") {
            Write-Success "Qdrant is already running!"
        } else {
            Write-Info "Starting existing Qdrant container..."
            docker start ilo-qdrant
            Start-Sleep -Seconds 2
            Write-Success "Qdrant started successfully!"
        }
    } else {
        # 首次启动
        Write-Info "Creating Qdrant data directory..."
        if (-not (Test-Path "qdrant_data")) {
            New-Item -ItemType Directory -Path "qdrant_data" -Force | Out-Null
            Write-Success "Created qdrant_data directory"
        }
        
        Write-Info "Pulling latest Qdrant image..."
        docker pull qdrant/qdrant
        
        Write-Info "Starting Qdrant container..."
        docker run -d -p 6333:6333 -p 6334:6334 `
          -v "$PWD\qdrant_data:/qdrant/storage" `
          --name ilo-qdrant `
          qdrant/qdrant
        
        Start-Sleep -Seconds 3
        Write-Success "Qdrant started for the first time!"
    }
    
    # 验证服务
    try {
        $apiResponse = Invoke-RestMethod -Uri "http://localhost:6333/" -Method Get -TimeoutSec 5
        Write-Success "Qdrant API responding! Version: $($apiResponse.version)"
    } catch {
        Write-Warn "Could not connect to Qdrant API. Please check the logs."
    }
}

# ========================================
# Step 2: 启动后端 API
# ========================================
if ($Service -eq "all" -or $Service -eq "api") {
    Write-Info "=== Starting Backend API Server ==="
    
    cd backend
    
    # 检查 Redis 连接
    Write-Info "Checking Redis connection..."
    try {
        $redisCheck = Test-NetAddress "localhost" -Port 6379 -TimeoutSec 2
        if ($redisCheck) {
            Write-Success "Redis is accessible on port 6379"
        } else {
            Write-Warn "Redis is not responding on port 6379"
            Write-Warn "Please make sure Redis is installed and running"
        }
    } catch {
        Write-Warn "Cannot check Redis connectivity"
    }
    
    Write-Info "Starting FastAPI server on http://localhost:8000..."
    Write-Success "Backend will start in a new window..."
    
    # 在新窗口启动 uvicorn
    Start-Process powershell -ArgumentList "-NoExit -Command 'cd ''$PWD''; python -m uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000'"
    
    Write-Success "Backend API starting... Open http://localhost:8000/docs to test"
}

# ========================================
# Step 3: 启动前端
# ========================================
if ($Service -eq "all" -or $Service -eq "frontend") {
    Write-Info "=== Starting Frontend ==="
    
    cd ..
    cd frontend
    
    Write-Info "Starting HTTP server on http://localhost:3000..."
    Write-Success "Frontend will start in a new window..."
    
    # 在新窗口启动 Python HTTP 服务器
    Start-Process powershell -ArgumentList "-NoExit -Command 'cd ''$PWD''; python -m http.server 3000'"
    
    Write-Success "Frontend available at http://localhost:3000"
}

# ========================================
# 完成
# ========================================
Write-Host ""
Write-Success "========================================"
Write-Success "ILO-Agent Demo - All services started!"
Write-Success "========================================"
Write-Host ""
Write-Info "Access URLs:"
Write-Host "  🌐 Frontend:    http://localhost:3000"
Write-Host "  🔧 API Docs:    http://localhost:8000/docs"
Write-Host "  🗄️ Qdrant:      http://localhost:6333/dashboard"
Write-Host ""
Write-Info "Press Ctrl+C in each window to stop the services"
Write-Host ""
