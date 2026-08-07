#!/bin/bash
set -e

LOCATION="swedencentral"
TEMPLATE_FILE="infra/main.bicep"

# Backend-only infrastructure (storage + Function App). The frontend/Web App,
# Easy Auth app registration and DNS moved to the figureskatingtools-site repo.

# Initialize variables
PROXY_SECRET=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        -s|--proxy-secret) PROXY_SECRET="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

# NB: the Bicep appSettings array is authoritative, so omitting --proxy-secret
# sets the Function App's PROXY_SHARED_SECRET to empty, disabling the proxy gate
# until the next CI deploy (which injects it from the GitHub environment secret).
if [ -z "$PROXY_SECRET" ]; then
    echo "Warning: --proxy-secret not provided; the router -> Function proxy gate will be disabled until the next CI deploy."
fi

echo "Deploying infrastructure to subscription scope in $LOCATION..."
az deployment sub create \
  --location "$LOCATION" \
  --template-file "$TEMPLATE_FILE" \
  --name "deploy-fs-protocols-$(date +%s)" \
  --parameters proxySharedSecret="$PROXY_SECRET"

echo "Infrastructure deployment complete."
