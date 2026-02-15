#!/bin/bash

# Usage: ./create_auth_app.sh <AppName> <SWA_Hostname>
# Example: ./create_auth_app.sh "JudgePapersApp" "polite-cliff-0.azurestaticapps.net"

# Exit on error
set -e

APP_NAME=$1
SWA_HOSTNAME=$2

# Validate inputs
if [ -z "$APP_NAME" ] || [ -z "$SWA_HOSTNAME" ]; then
    echo "Error: Missing arguments."
    echo "Usage: $0 <AppName> <SWA_Hostname>"
    echo "Example: $0 \"my-app\" \"polite-cliff-01234.azurestaticapps.net\""
    exit 1
fi

# Ensure https protocol is valid in the hostname
if [[ "$SWA_HOSTNAME" != http* ]]; then
    REDIRECT_URI="https://$SWA_HOSTNAME/.auth/login/aad/callback"
else
    REDIRECT_URI="$SWA_HOSTNAME/.auth/login/aad/callback"
fi

echo "----------------------------------------------------------------"
echo "Creating Azure App Registration for Static Web App Custom Auth"
echo "App Name     : $APP_NAME"
echo "Redirect URI : $REDIRECT_URI"
echo "----------------------------------------------------------------"

# 1. Create App Registration
# - Enables ID Tokens (--enable-id-token-issuance true) for Implicit/Hybrid flow
# - Sets Redirect URI
# - Sets Sign-in Audience to allow Guests and Personal Accounts (AzureADandPersonalMicrosoftAccount)
echo "Creating application..."
APP_ID=$(az ad app create \
    --display-name "$APP_NAME" \
    --web-redirect-uris "$REDIRECT_URI" \
    --enable-id-token-issuance true \
    --sign-in-audience AzureADandPersonalMicrosoftAccount \
    --query appId -o tsv)

echo "✅ App created with Client ID: $APP_ID"

# Wait for propagation to avoid "App does not exist" errors
echo "Waiting 30 seconds for AzureAD propagation..."
sleep 30

# 2. Set Access Token Version to 2
# This fixes the issue where SWA expects v2 tokens but AAD issues v1 by default
echo "Configuring requestedAccessTokenVersion to 2..."
# We need the Object ID for the graph call
# Add retry logic for fetching Object ID
for i in {1..5}; do
    OBJECT_ID=$(az ad app show --id "$APP_ID" --query id -o tsv 2>/dev/null) && break
    echo "Retry $i: Waiting for App to populate..."
    sleep 5
done

if [ -z "$OBJECT_ID" ]; then
    echo "Error: Could not retrieve Object ID for App $APP_ID. Azure propagation timeout."
    exit 1
fi

az rest --method PATCH \
    --uri "https://graph.microsoft.com/v1.0/applications/$OBJECT_ID" \
    --headers 'Content-Type=application/json' \
    --body '{"api":{"requestedAccessTokenVersion":2}}'
echo "✅ Access Token Version updated"

# 3. Add User.Read Permission
echo "Adding Microsoft Graph User.Read permission..."
az ad app update --id "$APP_ID" --required-resource-accesses '[{
    "resourceAppId": "00000003-0000-0000-c000-000000000000",
    "resourceAccess": [
        {
            "id": "e1fe6dd8-ba31-4d61-89e7-88639da4683d",
            "type": "Scope"
        }
    ]
}]'
echo "✅ User.Read permission added"

# 4. Create Service Principal (Enterprise Application)
echo "Creating Service Principal (Enterprise Application)..."
# Check if SP already exists to avoid error
SP_ID=$(az ad sp show --id "$APP_ID" --query id -o tsv 2>/dev/null || echo "")
if [ -z "$SP_ID" ]; then
    az ad sp create --id "$APP_ID"
    echo "Service Principal created"
else
    echo "ℹ️ Service Principal already exists"
fi

# 5. Output info for federated credential setup
# The federated identity credential linking this app registration to the
# user-assigned managed identity is created automatically by the CI/CD pipeline
# after the managed identity is provisioned via Bicep.
# No client secret is needed — Easy Auth uses federated credentials instead.

# 6. Get Tenant ID
TENANT_ID=$(az account show --query tenantId -o tsv)

echo ""
echo "====================================================="
echo "SETUP COMPLETE"
echo "====================================================="
echo "App Registration has been configured."
echo ""
echo "Client ID:  $APP_ID"
echo "Object ID:  $OBJECT_ID"
echo "Tenant ID:  $TENANT_ID"
echo ""
echo "IMPORTANT: No client secret was generated."
echo "Easy Auth uses federated identity credentials (FIC) with a"
echo "user-assigned managed identity instead of a client secret."
echo ""
echo "NEXT STEPS:"
echo "1. Deploy infrastructure with Bicep (creates the managed identity)"
echo "2. The CI/CD pipeline will create the federated identity credential"
echo "   linking this app registration to the managed identity"
echo "3. Set AUTH_CLIENT_ID=$APP_ID and AUTH_APP_OBJECT_ID=$OBJECT_ID"
echo "   in your GitHub Environment secrets"
echo "4. Go to Azure Portal > Enterprise Applications > $APP_NAME > Permissions"
echo "5. Click 'Grant admin consent for <TenantName>'."
echo "6. (Optional) Go to 'Properties' and set 'Assignment required' to 'Yes'"
echo "====================================================="
