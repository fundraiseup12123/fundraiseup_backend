# Sets live production env vars on Railway from backend/.env
# Usage: railway login   (once)
#        cd backend
#        .\scripts\set-live-railway-env.ps1 -BackendService <backend-service-name> -FrontendService <frontend-service-name>

param(
    [Parameter(Mandatory = $true)]
    [string]$BackendService,
    [Parameter(Mandatory = $true)]
    [string]$FrontendService
)

$ErrorActionPreference = "Stop"
$envFile = Join-Path $PSScriptRoot "..\.env"
if (-not (Test-Path $envFile)) {
    throw "Missing $envFile"
}

$vars = @{}
Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }
    $key = $line.Substring(0, $idx).Trim()
    $value = $line.Substring($idx + 1).Trim()
    if ($key) { $vars[$key] = $value }
}

$backendKeys = @(
    "STRIPE_SECRET_KEY",
    "STRIPE_PUBLISHABLE_KEY",
    "STRIPE_CONNECT_CLIENT_ID",
    "STRIPE_WEBHOOK_SECRET",
    "FRONTEND_URL",
    "CUSTOM_DOMAIN_CNAME_TARGET",
    "PLATFORM_ROOT_DOMAIN",
    "SUPABASE_URL",
    "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_SECRET_KEY",
    "SUPABASE_JWKS_URL",
    "PAYPAL_CLIENT_ID",
    "PAYPAL_CLIENT_SECRET",
    "PAYPAL_ENV",
    "PAYPAL_CURRENCY"
)

Write-Host "Setting backend service variables on Railway ($BackendService)..."
foreach ($key in $backendKeys) {
    if (-not $vars.ContainsKey($key)) { continue }
    npx @railway/cli variable set "$key=$($vars[$key])" --service $BackendService
}

$frontendMap = @{
    "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY" = $vars["STRIPE_PUBLISHABLE_KEY"]
    "NEXT_PUBLIC_PAYPAL_CLIENT_ID"       = $vars["PAYPAL_CLIENT_ID"]
    "NEXT_PUBLIC_PAYPAL_CURRENCY"        = $vars["PAYPAL_CURRENCY"]
    "NEXT_PUBLIC_STRIPE_MERCHANT_COUNTRY" = "US"
}

Write-Host "Setting frontend service variables on Railway ($FrontendService)..."
foreach ($entry in $frontendMap.GetEnumerator()) {
    if (-not $entry.Value) { continue }
    npx @railway/cli variable set "$($entry.Key)=$($entry.Value)" --service $FrontendService
}

Write-Host "Done. Redeploy both services from the Railway dashboard if needed."
