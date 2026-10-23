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
RESOURCE_GROUP=""
ORPHAN_PURGE=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        -s|--proxy-secret) PROXY_SECRET="$2"; shift ;;
        -g|--resource-group) RESOURCE_GROUP="$2"; shift ;;
        -v|--example-video-url) EXAMPLE_VIDEO_URL="$2"; shift ;;
        --orphan-purge)
            ORPHAN_PURGE="$2"
            if [ "$ORPHAN_PURGE" != "true" ] && [ "$ORPHAN_PURGE" != "false" ]; then
                echo "Error: --orphan-purge must be 'true' or 'false' (got '$ORPHAN_PURGE')."
                exit 1
            fi
            shift ;;
        *) echo "Unknown parameter passed: $1"; echo "Usage: [MAILGUN_API_KEY=...] ./deploy_infra.sh --resource-group <NAME> [--proxy-secret <SECRET>] [--example-video-url <URL>] [--orphan-purge true|false]"; exit 1 ;;
    esac
    shift
done

if [ -z "$RESOURCE_GROUP" ]; then
    echo "Error: --resource-group argument is required."
    echo "Usage: [MAILGUN_API_KEY=...] ./deploy_infra.sh --resource-group <NAME> [--proxy-secret <SECRET>] [--example-video-url <URL>] [--orphan-purge true|false]"
    exit 1
fi

# NB: the Bicep appSettings array is authoritative, so omitting --proxy-secret
# sets the Function App's PROXY_SHARED_SECRET to empty. The gate fails closed,
# so the backend then refuses every request (401) until the next CI deploy
# (which injects it from the GitHub environment secret).
if [ -z "$PROXY_SECRET" ]; then
    echo "Warning: --proxy-secret not provided; the Function App will refuse every request (401) until the next CI deploy."
fi
if [ -z "$MAILGUN_API_KEY" ]; then
    echo "Warning: MAILGUN_API_KEY not set; the send e-mail feature only logs messages (dry run) until the next CI deploy."
fi

# Orphan purge (Bicep orphanPurgeEnabled -> ORPHAN_PURGE_ENABLED). The Bicep
# default is true, so a manual run must never fall back to it: without
# --orphan-purge, keep what the Function App has now (the workflow deploys the
# GitHub variable ORPHAN_PURGE_ENABLED), and if that can't be read (no app yet,
# no permission) deploy false (dry run) rather than silently enabling deletion.
if [ -z "$ORPHAN_PURGE" ]; then
    CURRENT_PURGE=""
    FUNCTION_APP=$(az functionapp list --resource-group "$RESOURCE_GROUP" \
        --query "[?starts_with(name, 'func-fs-judgepapers-')].name" -o tsv 2>/dev/null || true)
    if [ -n "$FUNCTION_APP" ] && [ "$(printf '%s\n' "$FUNCTION_APP" | wc -l)" -eq 1 ]; then
        CURRENT_PURGE=$(az functionapp config appsettings list --resource-group "$RESOURCE_GROUP" \
            --name "$FUNCTION_APP" --query "[?name=='ORPHAN_PURGE_ENABLED'].value | [0]" -o tsv 2>/dev/null \
            | tr -d '[:space:]' || true)
    fi
    if [ "$CURRENT_PURGE" = "true" ] || [ "$CURRENT_PURGE" = "false" ]; then
        ORPHAN_PURGE="$CURRENT_PURGE"
        echo "Keeping the deployed ORPHAN_PURGE_ENABLED=$ORPHAN_PURGE of $FUNCTION_APP (override with --orphan-purge true|false)."
    else
        ORPHAN_PURGE="false"
        echo "Notice: could not read the deployed ORPHAN_PURGE_ENABLED in $RESOURCE_GROUP; deploying it as false (dry run: orphans are only logged). Pass --orphan-purge true to enable deletion."
    fi
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
  --parameters resourceGroupName="$RESOURCE_GROUP" exampleVideoUrl="$EXAMPLE_VIDEO_URL" \
    orphanPurgeEnabled="$ORPHAN_PURGE"

echo "Infrastructure deployment complete."
