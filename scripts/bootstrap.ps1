param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPath = Join-Path $ProjectRoot ".venv"

& $Python -m venv $VenvPath
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e "${ProjectRoot}[dev]"
& $VenvPython -m pip install -e "$ProjectRoot\packages\contracts"
& $VenvPython -m pip install -e "$ProjectRoot\sdk\python"
& $VenvPython -m pip install -e "$ProjectRoot\services\runtime"
& $VenvPython -m pip install -e "$ProjectRoot\services\fleet"
& $VenvPython -m pip install -e "$ProjectRoot\services\forge"
& $VenvPython -m pip install -e "$ProjectRoot\services\observability"
& $VenvPython -m pip install -e "$ProjectRoot\services\realtime[dev]"

Write-Host "Environment ready: $VenvPath"
Write-Host "Run: .\.venv\Scripts\evoagent-os.exe --reload"
