#!/bin/bash
set -e

LOCATION="swedencentral"
TEMPLATE_FILE="infra/main.bicep"

# Initialize variables. The Mailgun key is read from the environment, never a
# flag, so it stays out of shell history and process listings:
#   read -rs MAILGUN_API_KEY && export MAILGUN_API_KEY && ./deploy_infra.sh ...
PROXY_SECRET=""
MAILGUN_API_KEY="${MAILGUN_API_KEY:-}"
EXAMPLE_VIDEO_URL=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        -s|--proxy-secret) PROXY_SECRET="$2"; shift ;;
        -v|--example-video-url) EXAMPLE_VIDEO_URL="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; echo "Usage: [MAILGUN_API_KEY=...] ./deploy_infra.sh [--proxy-secret <SECRET>] [--example-video-url <URL>]"; exit 1 ;;
    esac
    shift
done

# NB: the Bicep appSettings array is authoritative, so omitting --proxy-secret
# sets the Function App's PROXY_SHARED_SECRET to empty, disabling the proxy gate
# until the next CI deploy (which injects it from the GitHub environment secret).
if [ -z "$PROXY_SECRET" ]; then
    echo "Warning: --proxy-secret not provided; the Web App -> Function proxy gate will be disabled until the next CI deploy."
fi
if [ -z "$MAILGUN_API_KEY" ]; then
    echo "Warning: MAILGUN_API_KEY not set; the send e-mail feature only logs messages (dry run) until the next CI deploy."
fi

# Secrets go to az through a private parameters file, not its argv.
PARAMS_FILE="$(mktemp)"
chmod 600 "$PARAMS_FILE"
trap 'rm -f "$PARAMS_FILE"' EXIT
PROXY_SECRET="$PROXY_SECRET" MAILGUN_API_KEY="$MAILGUN_API_KEY" python3 -c '
import json, os, sys
json.dump({
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {
        "proxySharedSecret": {"value": os.environ["PROXY_SECRET"]},
        "mailgunApiKey": {"value": os.environ["MAILGUN_API_KEY"]},
    },
}, sys.stdout)' > "$PARAMS_FILE"

echo "Deploying infrastructure to subscription scope in $LOCATION..."
az deployment sub create \
  --location "$LOCATION" \
  --template-file "$TEMPLATE_FILE" \
  --name "deploy-fs-judgepapers-$(date +%s)" \
  --parameters "@$PARAMS_FILE" \
  --parameters exampleVideoUrl="$EXAMPLE_VIDEO_URL"

echo "Infrastructure deployment complete."
