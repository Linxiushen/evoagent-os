$ErrorActionPreference = "Stop"

if (-not $env:EVOAGENT_OS_TOKEN) {
    throw "Set EVOAGENT_OS_TOKEN to the value used by the local control plane."
}
$controlPlaneUrl = if ($env:EVOAGENT_OS_URL) {
    $env:EVOAGENT_OS_URL.TrimEnd("/")
} else {
    "http://127.0.0.1:8800"
}

$headers = @{
    Authorization = "Bearer $($env:EVOAGENT_OS_TOKEN)"
    "Idempotency-Key" = "market-research-demo-v1"
}

$response = Invoke-RestMethod `
    -Method Post `
    -Uri "$controlPlaneUrl/api/v1/demo/launch" `
    -Headers $headers

$response | ConvertTo-Json -Depth 8
