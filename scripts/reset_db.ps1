# scripts/reset_db.ps1
# Windows twin of scripts/reset_db.sh (decision D-38): drop and rebuild the development database
# inside the docker compose service db, so no psql is needed on the host. Never run it during a
# measured run. Then it creates the DATA_DIR folders, copies seed_data\problems into them and writes
# the capacity profile with scripts\capacity.py, which connects with DATABASE_URL (database orch by
# default, so DB_NAME must name the same database).
# Usage, from any folder: powershell -ExecutionPolicy Bypass -File scripts\reset_db.ps1
$ErrorActionPreference = 'Stop'
$OutputEncoding = New-Object System.Text.UTF8Encoding $false  # pipe the SQL to psql as UTF-8
$db = if ($env:DB_NAME) { $env:DB_NAME } else { 'orch' }
$data = if ($env:DATA_DIR) { $env:DATA_DIR } else { '.\data' }
$capacityProfile = if ($env:CAPACITY_PROFILE) { $env:CAPACITY_PROFILE } else { 's0' }

function Invoke-Psql {
    param([string]$Database, [string]$File, [string[]]$Commands = @())
    $arguments = @('compose', 'exec', '-T', '-e', 'PGOPTIONS=-c client_min_messages=warning', 'db',
                   'psql', '-U', 'postgres', '-d', $Database, '-v', 'ON_ERROR_STOP=1', '-q')
    foreach ($command in $Commands) { $arguments += @('-c', $command) }
    if ($File) { Get-Content -Raw -Encoding UTF8 $File | docker @arguments }
    else { docker @arguments }
    if ($LASTEXITCODE -ne 0) { throw "psql failed on database $Database ($File $Commands), exit code $LASTEXITCODE" }
}

function Wait-Database {
    # A new volume runs initdb first, and the entrypoint's temporary server listens on a socket
    # only, so pg_isready over TCP waits for the real server.
    $ErrorActionPreference = 'Continue'  # docker writes to stderr while the service starts
    $deadline = (Get-Date).AddSeconds(60)
    $said = $false
    while ($true) {
        docker compose exec -T db pg_isready -q -h 127.0.0.1 -U postgres 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { return }
        if ((Get-Date) -gt $deadline) {
            throw 'PostgreSQL is not ready after 60 s: run "docker compose up -d db", then check "docker compose logs db".'
        }
        if (-not $said) { Write-Output 'Waiting for PostgreSQL to accept connections...'; $said = $true }
        Start-Sleep -Milliseconds 500
    }
}

function Invoke-Capacity {
    # capacity.py writes a refusal to stderr. Under Stop, PowerShell 5.1 turns a redirected stderr
    # line into a terminating error, so the exit code decides instead.
    $ErrorActionPreference = 'Continue'
    $python = if (Test-Path .venv\Scripts\python.exe) { '.venv\Scripts\python.exe' } else { 'python' }
    & $python -m scripts.capacity --profile $capacityProfile
    if ($LASTEXITCODE -ne 0) { throw "scripts\capacity.py failed, exit code $LASTEXITCODE" }
}

Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    Wait-Database
    Invoke-Psql -Database postgres -File db\roles.sql
    Invoke-Psql -Database postgres -Commands "DROP DATABASE IF EXISTS $db WITH (FORCE)", "CREATE DATABASE $db"
    Invoke-Psql -Database $db -File db\schema.sql
    Invoke-Psql -Database $db -File db\seed.sql
    Write-Output "Database $db rebuilt from db\roles.sql, db\schema.sql and db\seed.sql."
    foreach ($folder in 'problems', 'payloads', 'runs', 'logs') {
        New-Item -ItemType Directory -Force -Path (Join-Path $data $folder) | Out-Null
    }
    Copy-Item -Path seed_data\problems\* -Destination (Join-Path $data 'problems') -Recurse -Force
    Write-Output "Data folders ready under $data, with the problems of seed_data\problems."
    Invoke-Capacity
}
finally {
    Pop-Location
}
