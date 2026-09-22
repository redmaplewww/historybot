$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$apiKey = $env:HISTORYBOT_API_KEY
if ([string]::IsNullOrWhiteSpace($apiKey)) {
    throw "HISTORYBOT_API_KEY must be set in the current environment before starting the repair"
}
$env:HISTORYBOT_LLM_PROVIDER = "openai_compatible"
$env:HISTORYBOT_BASE_URL = "https://gpt.sweeneyhotel.top:8096"
$env:HISTORYBOT_API_KEY = $apiKey
$env:HISTORYBOT_MODEL = "gpt-5.6-terra"
$env:HISTORYBOT_REASONING_EFFORT = "none"
$env:HISTORYBOT_HTTP_TIMEOUT = "300"
uv run python -m historybot.bulk_extract --db data/three_kingdoms_rebuilt_v2.db --batch-size 100 --concurrency 2 --retries 3 --people-only *> data/people_identity_repair.log
