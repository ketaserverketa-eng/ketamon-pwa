param(
    [int]$Port = 5001,
    [int]$BackendPort = 5000,
    [string]$PublicUrl = ""
)

$ErrorActionPreference = "Stop"

$scriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$dataDir     = Join-Path $scriptDir "data"
$watchdog    = Join-Path $scriptDir "watchdog.ps1"
$lockFile    = Join-Path $dataDir "watchdog.lock"
$windowsRoot = $env:WINDIR
if (-not $windowsRoot) {
    $windowsRoot = $env:SystemRoot
}
$powershell  = $null
if ($windowsRoot) {
    $powershell = Join-Path $windowsRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
}

if (-not (Test-Path -LiteralPath $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
}

if (-not $powershell -or -not (Test-Path -LiteralPath $powershell)) {
    $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
}

function Read-WatchdogPid {
    if (-not (Test-Path -LiteralPath $lockFile)) {
        return $null
    }

    try {
        $raw = Get-Content -LiteralPath $lockFile -Raw -ErrorAction Stop
        $digits = ($raw -replace "[^0-9]", "").Trim()
        if ($digits) {
            return [int]$digits
        }
    } catch {
    }

    return $null
}

$existingPid = Read-WatchdogPid
if ($existingPid) {
    try {
        Stop-Process -Id $existingPid -Force -ErrorAction Stop
        Start-Sleep -Seconds 2
    } catch {
    }
}

$watchdogArgs = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $watchdog,
    "-Port", $Port,
    "-BackendPort", $BackendPort
)
if ($PublicUrl) {
    $watchdogArgs += @("-PublicUrl", $PublicUrl)
}

$proc = Start-Process -FilePath $powershell `
    -ArgumentList $watchdogArgs `
    -WorkingDirectory $scriptDir `
    -WindowStyle Hidden `
    -PassThru

Write-Output ("KetaMon watchdog started without ngrok (PID {0})." -f $proc.Id)
Write-Output ("Watchdog log: {0}" -f (Join-Path $dataDir "watchdog.log"))
