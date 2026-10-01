# ViralClip AI - start everything with one double-click (Windows). Called by start-windows.bat.
# First run: creates .env with random secrets, builds the app (5-10 min), opens the dashboard.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$port = if ($env:WEB_PORT) { $env:WEB_PORT } else { "3000" }
$url = "http://localhost:$port"
function Say($msg) { Write-Host "`n> $msg" -ForegroundColor Magenta }
function Fail($msg) { Write-Host "`nX $msg" -ForegroundColor Red; exit 1 }

# 1. Docker installed and running?
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker is niet geinstalleerd. Installeer Docker Desktop via https://www.docker.com/products/docker-desktop/ en start dit opnieuw."
}
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Say "Docker Desktop wordt gestart..."
    $dd = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dd) { Start-Process $dd }
    Write-Host -NoNewline "Wachten tot Docker draait"
    for ($i = 0; $i -lt 90; $i++) {
        docker info *> $null
        if ($LASTEXITCODE -eq 0) { break }
        Write-Host -NoNewline "."; Start-Sleep -Seconds 2
    }
    Write-Host ""
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { Fail "Docker draait niet. Open Docker Desktop, wacht tot er 'Engine running' staat en start dit opnieuw." }
}

# 2. Settings file with random secrets (only the first time)
function New-Secret { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 40 | ForEach-Object { [char]$_ }) }
if (-not (Test-Path ".env")) {
    Say "Instellingenbestand .env aanmaken (met willekeurige geheime sleutels)"
    $content = Get-Content ".env.example" -Raw -Encoding UTF8
    $content = $content -replace "(?m)^APP_SECRET_KEY=.*$", "APP_SECRET_KEY=$(New-Secret)"
    $content = $content -replace "(?m)^API_AUTH_TOKEN=.*$", "API_AUTH_TOKEN=$(New-Secret)"
    $content = $content -replace "(?m)^POSTGRES_PASSWORD=.*$", "POSTGRES_PASSWORD=$(New-Secret)"
    [System.IO.File]::WriteAllText((Join-Path (Get-Location) ".env"), $content)
}
New-Item -ItemType Directory -Force -Path "data\inbox" | Out-Null

# 3. Build + start
Say "ViralClip AI starten (de eerste keer duurt dit 5-10 minuten)..."
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Fail "Starten mislukt. Bekijk de melding hierboven of voer uit: docker compose logs --tail 50" }

# 4. Wait until the dashboard answers, then open it
Write-Host -NoNewline "Wachten tot het dashboard klaar is"
for ($i = 0; $i -lt 150; $i++) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
        $code = $r.StatusCode
    } catch {
        $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
    }
    if ($code -eq 200 -or $code -eq 401) {
        Write-Host ""
        Say "Klaar! Het dashboard opent nu: $url"
        Start-Process $url
        Write-Host "Stoppen: dubbelklik stop-windows.bat (of in Docker Desktop: Containers -> viralclip -> Stop)."
        exit 0
    }
    Write-Host -NoNewline "."; Start-Sleep -Seconds 2
}
Write-Host ""
Fail "Het dashboard reageert nog niet. Bekijk wat er gebeurt met: docker compose logs --tail 50"
