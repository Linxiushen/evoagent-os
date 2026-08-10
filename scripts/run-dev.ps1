param(
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Executable = Join-Path $ProjectRoot ".venv\Scripts\evoagent-os.exe"

if (-not (Test-Path -LiteralPath $Executable)) {
    throw "Development environment is missing. Run scripts/bootstrap.ps1 first."
}

& $Executable --host 127.0.0.1 --port $Port --reload
