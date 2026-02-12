#!/bin/bash
set -e

LOCATION="swedencentral"
TEMPLATE_FILE="infra/main.bicep"

# Initialize variables
CLIENT_ID=""
CLIENT_SECRET=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        -c|--client-id) CLIENT_ID="$2"; shift ;;
        -s|--client-secret) CLIENT_SECRET="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
    echo "Error: --client-id and --client-secret arguments are required."
    echo "Usage: ./deploy_infra.sh --client-id <ID> --client-secret <SECRET>"
    exit 1
fi

echo "Deploying infrastructure to subscription scope in $LOCATION..."
az deployment sub create \
  --location "$LOCATION" \
  --template-file "$TEMPLATE_FILE" \
  --name "deploy-fs-judgepapers-$(date +%s)" \
  --parameters authClientId="$CLIENT_ID" authClientSecret="$CLIENT_SECRET"

echo "Infrastructure deployment complete."
