#!/bin/bash

# Usage: ./create_auth_app.sh <AppName> <Hostname>
# Example: ./create_auth_app.sh "ProtocolGeneratorApp" "protocols.figureskatingtools.com"

# Exit on error
set -e

APP_NAME=$1
SWA_HOSTNAME=$2

# Validate inputs
if [ -z "$APP_NAME" ] || [ -z "$SWA_HOSTNAME" ]; then
    echo "Error: Missing arguments."
    echo "Usage: $0 <AppName> <Hostname>"
    echo "Example: $0 \"ProtocolGeneratorApp\" \"protocols.figureskatingtools.com\""
    exit 1
fi

# Ensure https protocol is valid in the hostname
if [[ "$SWA_HOSTNAME" != http* ]]; then
    REDIRECT_URI="https://$SWA_HOSTNAME/.auth/login/aad/callback"
else
    REDIRECT_URI="$SWA_HOSTNAME/.auth/login/aad/callback"
fi

echo "----------------------------------------------------------------"
echo "Creating Azure App Registration for Easy Auth"
echo "App Name     : $APP_NAME"
echo "Redirect URI : $REDIRECT_URI"
echo "----------------------------------------------------------------"

# 1. Create App Registration
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
echo "Configuring requestedAccessTokenVersion to 2..."
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
SP_ID=$(az ad sp show --id "$APP_ID" --query id -o tsv 2>/dev/null || echo "")
if [ -z "$SP_ID" ]; then
    az ad sp create --id "$APP_ID"
    echo "Service Principal created"
else
    echo "ℹ️ Service Principal already exists"
fi

# 5. Federated identity credential is created automatically by the CI/CD pipeline
#    after the managed identity is provisioned via Bicep. No client secret needed.

# 6. Get Tenant ID
TENANT_ID=$(az account show --query tenantId -o tsv)

echo ""
echo "====================================================="
echo "SETUP COMPLETE"
echo "====================================================="
echo "Client ID:  $APP_ID"
echo "Object ID:  $OBJECT_ID"
echo "Tenant ID:  $TENANT_ID"
echo ""
echo "NEXT STEPS:"
echo "1. Deploy infrastructure with Bicep (creates the managed identity)"
echo "2. The CI/CD pipeline will create the federated identity credential"
echo "   linking this app registration to the managed identity"
echo "3. Set AUTH_CLIENT_ID=$APP_ID and AUTH_APP_OBJECT_ID=$OBJECT_ID"
echo "   in your GitHub Environment secrets"
echo "4. Go to Azure Portal > Enterprise Applications > $APP_NAME > Permissions"
echo "5. Click 'Grant admin consent for <TenantName>'."
echo "====================================================="
