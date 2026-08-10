$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Development environment is missing. Run scripts/bootstrap.ps1 first."
}

Push-Location $ProjectRoot
try {
    & $Python -m ruff check apps packages sdk tests
    & $Python -m pytest -q tests
    & $Python -m pytest -q packages/contracts/tests
    & $Python -m pytest -q sdk/python/tests
    & $Python -m pytest -q services/runtime/tests
    & $Python -m pytest -q services/fleet/tests
    & $Python -m pytest -q services/forge/tests
    & $Python -m pytest -q services/observability/tests
    & $Python -m pytest -q services/realtime/tests
    $Node = Get-Command node -ErrorAction SilentlyContinue
    if ($Node) {
        Get-ChildItem services/realtime/tests/js/*.mjs | ForEach-Object {
            & $Node.Source --test $_.FullName
        }
    }
    $Pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
    if ($Pnpm) {
        Push-Location sdk/typescript
        try {
            & $Pnpm.Source install --frozen-lockfile
            & $Pnpm.Source test
        }
        finally {
            Pop-Location
        }
    }
}
finally {
    Pop-Location
}
