#!/bin/bash
set -e

LOCATION="swedencentral"
TEMPLATE_FILE="infra/main.bicep"

# Initialize variables
PROXY_SECRET=""
MAILGUN_API_KEY=""
EXAMPLE_VIDEO_URL=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        -s|--proxy-secret) PROXY_SECRET="$2"; shift ;;
        -m|--mailgun-api-key) MAILGUN_API_KEY="$2"; shift ;;
        -v|--example-video-url) EXAMPLE_VIDEO_URL="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; echo "Usage: ./deploy_infra.sh [--proxy-secret <SECRET>] [--mailgun-api-key <KEY>] [--example-video-url <URL>]"; exit 1 ;;
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
    echo "Warning: --mailgun-api-key not provided; the send e-mail feature only logs messages (dry run) until the next CI deploy."
fi

echo "Deploying infrastructure to subscription scope in $LOCATION..."
az deployment sub create \
  --location "$LOCATION" \
  --template-file "$TEMPLATE_FILE" \
  --name "deploy-fs-judgepapers-$(date +%s)" \
  --parameters proxySharedSecret="$PROXY_SECRET" \
    mailgunApiKey="$MAILGUN_API_KEY" \
    exampleVideoUrl="$EXAMPLE_VIDEO_URL"

echo "Infrastructure deployment complete."
