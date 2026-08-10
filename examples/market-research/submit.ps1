$ErrorActionPreference = "Stop"

$fleetUrl = if ($env:EVOAGENT_FLEET_URL) {
    $env:EVOAGENT_FLEET_URL.TrimEnd("/")
} else {
    "http://127.0.0.1:8833"
}
$workflowPath = Join-Path $PSScriptRoot "workflow.json"

$response = Invoke-RestMethod `
    -Method Post `
    -Uri "$fleetUrl/v1/workflows" `
    -ContentType "application/json" `
    -InFile $workflowPath

$response | ConvertTo-Json -Depth 5
