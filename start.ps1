<#
  SMSocket kernel launcher - PowerShell entry point.
  Foreground by default; -Background detaches (pid -> runtime\SMSocket.pid,
  stdout -> runtime\SMSocket.log).
  .\start.ps1                      # config.yaml, port from config
  .\start.ps1 -Background -Port 8000
  .\start.ps1 -Config config.yaml -Reload -NoKey
#>
[CmdletBinding()]
param(
    [string]$Config = "",
    [string]$ListenHost = "",
    [int]$Port = 0,
    [switch]$Reload,
    [switch]$NoKey,
    [switch]$RotateKey,
    [switch]$Background
)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root
$Runtime = Join-Path $Root "runtime"
if (-not (Test-Path $Runtime)) { New-Item -ItemType Directory -Path $Runtime | Out-Null }
$PidFile = Join-Path $Runtime "SMSocket.pid"
$LogFile = Join-Path $Runtime "SMSocket.log"
$ErrFile = Join-Path $Runtime "SMSocket.err.log"

function Find-Python {
    if ($env:SMSSOCKET_PYTHON -and (Test-Path $env:SMSSOCKET_PYTHON)) { return $env:SMSSOCKET_PYTHON }
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return $venv }
    foreach ($c in @("python", "py", "python3")) {
        $p = Get-Command $c -ErrorAction SilentlyContinue
        if ($p) { return $p.Source }
    }
    throw "no python found - set SMSSOCKET_PYTHON or create .venv"
}

function Show-Info([int]$ShownPort) {
    $keyFile = Join-Path $Root "smsocket.key"
    $key = if (Test-Path $keyFile) { (Get-Content $keyFile -First 1).Trim() } else { "-" }
    Write-Host ("  web  http://127.0.0.1:{0}/" -f $ShownPort)
    Write-Host ("  api  http://127.0.0.1:{0}/v1" -f $ShownPort)
    Write-Host ("  key  {0}" -f $key)
    Write-Host ("  log  {0}" -f $LogFile)
}

$Py = Find-Python
$argList = @("run.py")
if ($Config) { $argList += @("--config", $Config) }
if ($ListenHost) { $argList += @("--host", $ListenHost) }
if ($Port) { $argList += @("--port", "$Port") }
if ($Reload) { $argList += "--reload" }
if ($NoKey) { $argList += "--no-key" }
if ($RotateKey) { $argList += "--rotate-key" }

$cfgPath = if ($Config) { $Config } else { "config.yaml" }
if (Test-Path $cfgPath) {
    $cfgText = Get-Content $cfgPath -Raw
    $vars = [regex]::Matches($cfgText, '\$\{([A-Za-z_][A-Za-z0-9_]*)\}') | ForEach-Object { $_.Groups[1].Value } | Where-Object { $_ -like '*_KEY*' } | Select-Object -Unique
    $missing = @($vars | Where-Object { -not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($_, "User")) -eq $false -and -not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($_, "Machine")) -eq $false -and -not (Test-Path ("env:" + $_)) })
    if ($missing.Count -gt 0) {
        Write-Host "[warn] upstream key env var(s) not set in the launching environment: $($missing -join ', ') - the gateway will start with 0 usable keys and answer 503 on /v1/ until they are injected and smsocket is restarted"
    }
}

$ShownPort = if ($Port) { $Port } else { 8000 }
$cfgPath = if ($Config) { $Config } else { "config.yaml" }
if (-not (Test-Path $cfgPath)) { Write-Host "[start] missing $cfgPath - copy config.example.yaml"; exit 1 }
$m = Select-String -Path $cfgPath -Pattern 'listen:\s*\S+?:(\d+)' | Select-Object -First 1
if ($m) { $ShownPort = [int]$m.Matches[0].Groups[1].Value }

if (Test-Path $PidFile) {
    $old = (Get-Content $PidFile -First 1).Trim()
    if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
        Write-Host "[start] already running (pid $old) - run .\stop.ps1 first"
        Show-Info $ShownPort
        exit 0
    }
    Remove-Item $PidFile -Force
}

if (-not $Background) {
    Write-Host "[start] $Py $($argList -join ' ')"
    & $Py $argList
    exit $LASTEXITCODE
}

Write-Host "[start] detaching: $Py $($argList -join ' ')"
$p = Start-Process -FilePath $Py -ArgumentList $argList -WorkingDirectory $Root `
    -WindowStyle Hidden -RedirectStandardOutput $LogFile -RedirectStandardError $ErrFile -PassThru
Set-Content -Path $PidFile -Value $p.Id
Start-Sleep -Seconds 3
if (-not (Get-Process -Id $p.Id -ErrorAction SilentlyContinue)) {
    Write-Host "[start] kernel died - last stderr lines:"
    if (Test-Path $ErrFile) { Get-Content $ErrFile -Tail 20 }
    Remove-Item $PidFile -Force
    exit 1
}
Write-Host "[start] kernel up (pid $($p.Id))"
Show-Info $ShownPort
