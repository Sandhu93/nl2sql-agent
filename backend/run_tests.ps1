# run_tests.ps1 — Run pytest inside Docker and save output to a timestamped log.
#
# Tests always run inside the backend Docker container so they have access to
# the real PostgreSQL, Redis, and ChromaDB — matching the production environment.
#
# Usage:
#   .\run_tests.ps1                              # all tests
#   .\run_tests.ps1 -m unit                      # unit tests only (fast, no Docker deps)
#   .\run_tests.ps1 -m integration               # integration tests (needs stack running)
#   .\run_tests.ps1 tests/unit/test_embedding_versioning.py   # single file
#   .\run_tests.ps1 -k "content_hash"            # by keyword
#
# Prerequisites:
#   docker compose up -d   (stack must be running for integration tests)
#
# Output is shown in the terminal AND saved to tests/results/<timestamp>.log

param(
    [Parameter(ValueFromRemainingArguments)]
    [string[]]$PytestArgs
)

$ProjectRoot = Split-Path $PSScriptRoot -Parent
$ResultsDir  = Join-Path $PSScriptRoot "tests\results"
New-Item -ItemType Directory -Force -Path $ResultsDir | Out-Null

$Timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$LogFile   = Join-Path $ResultsDir "$Timestamp.log"

Write-Host ""
Write-Host "Running pytest inside Docker container..." -ForegroundColor Cyan
Write-Host "Output -> $LogFile" -ForegroundColor Cyan
Write-Host ""

# Run pytest in the backend container, stream to terminal and capture to file.
$Output = docker compose -f "$ProjectRoot\docker-compose.yml" exec backend `
    pytest @PytestArgs 2>&1 | Tee-Object -FilePath $LogFile

$ExitCode = $LASTEXITCODE

Write-Host ""
Write-Host "Results saved to: $LogFile" -ForegroundColor Cyan

if ($ExitCode -eq 0) {
    Write-Host "All tests passed." -ForegroundColor Green
} else {
    Write-Host "Tests failed (exit code $ExitCode)." -ForegroundColor Red
}

exit $ExitCode
