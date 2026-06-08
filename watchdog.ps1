param(
    [int]$Port = 5001,
    [int]$BackendPort = 5000,
    [int]$CheckIntervalSeconds = 20,
    [string]$PublicUrl = ""
)

$ErrorActionPreference = "Stop"

$scriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$dataDir     = Join-Path $scriptDir "data"
$logFile     = Join-Path $dataDir "watchdog.log"
$lockFile    = Join-Path $dataDir "watchdog.lock"
$backendPidFile = Join-Path $dataDir "ketamon_backend.pid"
$flaskPidFile = Join-Path $dataDir "ketamon_web.pid"
$ngrokPidFile = Join-Path $dataDir "ngrok.pid"
$backendOut  = Join-Path $scriptDir "backend\uvicorn.out.log"
$backendErr  = Join-Path $scriptDir "backend\uvicorn.err.log"
$flaskOut    = Join-Path $scriptDir "server.log"
$flaskErr    = Join-Path $scriptDir "server_err.log"

if (-not (Test-Path -LiteralPath $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
}

function Write-Log {
    param([string]$Message)

    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $logFile -Value $line -Encoding utf8

    try {
        $lines = Get-Content -LiteralPath $logFile -ErrorAction SilentlyContinue
        if (@($lines).Count -gt 500) {
            $lines | Select-Object -Last 500 | Set-Content -LiteralPath $logFile -Encoding utf8
        }
    } catch {
    }
}

function Resolve-PythonPath {
    $candidates = @(
        $env:KETAMON_PYTHON,
        "C:\Python314\python.exe",
        (Join-Path $scriptDir "backend\.venv\Scripts\python.exe")
    ) | Where-Object { $_ }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    throw "Python executable not found."
}

function Resolve-BackendPythonPath {
    $candidates = @(
        $env:KETAMON_BACKEND_PYTHON,
        (Join-Path $scriptDir "backend\.venv\Scripts\python.exe"),
        (Resolve-PythonPath)
    ) | Where-Object { $_ }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    throw "Backend Python executable not found."
}

function Read-PidFile {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    try {
        $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop
        $digits = ($raw -replace "[^0-9]", "").Trim()
        if ($digits) {
            return [int]$digits
        }
    } catch {
    }

    return $null
}

function Write-PidFile {
    param(
        [string]$Path,
        [int]$Value
    )

    Set-Content -LiteralPath $Path -Value ([string]$Value) -Encoding ascii
}

function Remove-PidFileSafe {
    param([string]$Path)

    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    }
}

function Stop-ProcessSafe {
    param(
        [int]$ProcessId,
        [string]$Reason
    )

    if (-not $ProcessId -or $ProcessId -eq $PID) {
        return
    }

    try {
        $proc = Get-Process -Id $ProcessId -ErrorAction Stop
        Stop-Process -Id $proc.Id -Force -ErrorAction Stop
        Write-Log ("Stopped PID {0} ({1})." -f $proc.Id, $Reason)
    } catch {
    }
}

function Get-ListeningPids {
    param([int]$ListenPort)

    try {
        $rows = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction Stop
        return @($rows | Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        $pids = @()
        $lines = netstat -ano 2>$null | Select-String (":{0}\s+.*LISTENING" -f $ListenPort)
        foreach ($line in $lines) {
            $parts = ($line.ToString() -split "\s+") | Where-Object { $_ }
            if ($parts.Count -ge 5 -and $parts[-1] -match "^\d+$") {
                $pids += [int]$parts[-1]
            }
        }
        return @($pids | Select-Object -Unique)
    }
}

function Stop-ProcessesOnPort {
    param(
        [int]$ListenPort,
        [string]$Reason
    )

    foreach ($listenPid in Get-ListeningPids -ListenPort $ListenPort) {
        Stop-ProcessSafe -ProcessId $listenPid -Reason $Reason
    }
}

function Test-LocalHealth {
    try {
        $response = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/health" -f $Port) -TimeoutSec 5
        return ($response.ok -eq $true)
    } catch {
        return $false
    }
}

function Test-BackendHealth {
    try {
        $response = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/health" -f $BackendPort) -TimeoutSec 5
        return ($response.ok -eq $true) -and ([string]$response.service -eq "ketamon-saas-api")
    } catch {
        return $false
    }
}

function Start-Flask {
    param([string]$PythonPath)

    Stop-ProcessSafe -ProcessId (Read-PidFile -Path $flaskPidFile) -Reason "stale Flask PID"
    Stop-ProcessesOnPort -ListenPort $Port -Reason ("free port {0} before Flask restart" -f $Port)

    Write-Log ("Starting Flask with {0} on port {1}." -f $PythonPath, $Port)

    $env:PORT = [string]$Port
    $resolvedUrl = if ($PublicUrl) { $PublicUrl.TrimEnd("/") } else { "https://ketamon-pwa-cloud.onrender.com" }
    $env:KETAMON_PUBLIC_URL = $resolvedUrl
    $proc = Start-Process -FilePath $PythonPath `
        -ArgumentList @("app.py") `
        -WorkingDirectory $scriptDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $flaskOut `
        -RedirectStandardError $flaskErr `
        -PassThru

    Write-PidFile -Path $flaskPidFile -Value $proc.Id

    for ($attempt = 1; $attempt -le 15; $attempt++) {
        Start-Sleep -Seconds 2
        if (Test-LocalHealth) {
            Write-Log ("Flask healthy on port {0} (PID {1})." -f $Port, $proc.Id)
            return $true
        }
    }

    Write-Log ("Flask failed local /health checks after restart (PID {0})." -f $proc.Id)
    return $false
}

function Start-Backend {
    param([string]$PythonPath)

    Stop-ProcessSafe -ProcessId (Read-PidFile -Path $backendPidFile) -Reason "stale backend PID"
    Stop-ProcessesOnPort -ListenPort $BackendPort -Reason ("free port {0} before backend restart" -f $BackendPort)

    Write-Log ("Starting backend FastAPI with {0} on port {1}." -f $PythonPath, $BackendPort)

    $proc = Start-Process -FilePath $PythonPath `
        -ArgumentList @(
            "-m",
            "uvicorn",
            "backend.app:app",
            "--host", "0.0.0.0",
            "--port", [string]$BackendPort
        ) `
        -WorkingDirectory $scriptDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $backendOut `
        -RedirectStandardError $backendErr `
        -PassThru

    Write-PidFile -Path $backendPidFile -Value $proc.Id

    for ($attempt = 1; $attempt -le 20; $attempt++) {
        Start-Sleep -Seconds 2
        if (Test-BackendHealth) {
            Write-Log ("Backend healthy on port {0} (PID {1})." -f $BackendPort, $proc.Id)
            return $true
        }
    }

    Write-Log ("Backend failed /health checks after restart (PID {0})." -f $proc.Id)
    return $false
}

function Stop-LegacyNgrok {
    Stop-ProcessSafe -ProcessId (Read-PidFile -Path $ngrokPidFile) -Reason "legacy ngrok disabled"

    foreach ($proc in @(Get-Process -Name "ngrok" -ErrorAction SilentlyContinue)) {
        Stop-ProcessSafe -ProcessId $proc.Id -Reason "legacy ngrok cleanup"
    }

    Remove-PidFileSafe -Path $ngrokPidFile
}

function Acquire-Lock {
    $existingPid = Read-PidFile -Path $lockFile
    if ($existingPid) {
        try {
            $existing = Get-Process -Id $existingPid -ErrorAction Stop
            if ($existing.Id -ne $PID) {
                Write-Log ("Another watchdog is already running with PID {0}. Exiting." -f $existing.Id)
                exit 0
            }
        } catch {
        }
    }

    Write-PidFile -Path $lockFile -Value $PID
}

$pythonPath = Resolve-PythonPath
$backendPythonPath = Resolve-BackendPythonPath

Acquire-Lock
Write-Log ("Watchdog started without ngrok. FrontendPort={0} BackendPort={1} PID={2}" -f $Port, $BackendPort, $PID)

$backendFailures = 0
$localFailures = 0

try {
    Stop-LegacyNgrok

    if (-not (Test-BackendHealth)) {
        if (-not (Start-Backend -PythonPath $backendPythonPath)) {
            Write-Log "Initial backend start did not pass /health."
        }
    } else {
        Write-Log ("Backend /health already OK on port {0}." -f $BackendPort)
    }

    if (-not (Test-LocalHealth)) {
        if (-not (Start-Flask -PythonPath $pythonPath)) {
            Write-Log "Initial Flask start did not pass /health."
        }
    } else {
        Write-Log ("Local /health already OK on port {0}." -f $Port)
    }

    while ($true) {
        Start-Sleep -Seconds $CheckIntervalSeconds

        if (Test-BackendHealth) {
            $backendFailures = 0
        } else {
            $backendFailures += 1
            Write-Log ("Backend /health failed ({0}/2)." -f $backendFailures)
            if ($backendFailures -ge 2) {
                if (Start-Backend -PythonPath $backendPythonPath) {
                    $backendFailures = 0
                }
            }
        }

        if (Test-LocalHealth) {
            $localFailures = 0
        } else {
            $localFailures += 1
            Write-Log ("Local /health failed ({0}/2)." -f $localFailures)
            if ($localFailures -ge 2) {
                if (Start-Flask -PythonPath $pythonPath) {
                    $localFailures = 0
                }
                continue
            }
        }
    }
} finally {
    Remove-PidFileSafe -Path $lockFile
}
