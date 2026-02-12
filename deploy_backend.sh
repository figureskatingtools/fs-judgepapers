#!/bin/bash
set -e

# Parse arguments first
RESOURCE_GROUP=""
while [[ "$#" -gt 0 ]]; do
    case $1 in
        -g|--resource-group) RESOURCE_GROUP="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [ -z "$RESOURCE_GROUP" ]; then
    echo "Error: --resource-group (-g) is required."
    echo "Usage: ./deploy_backend.sh --resource-group <resource-group-name>"
    exit 1
fi

# 1. Setup Environment
# Ensure we are in the right directory
cd infra/functions

# 2. Prepare Deployment Artifact
echo "Creating backend deployment artifact..."

# Create build directory
BUILD_DIR="../../backend_build"
echo "Preparing build in $BUILD_DIR..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
cp -r . "$BUILD_DIR"
cd "$BUILD_DIR"

# Cleanup - remove dev/test files and artifacts
rm -rf .venv __pycache__ .git .vscode *.pyc local.settings.json
rm -f test_*.py

# Install dependencies specifically for Flex Consumption
echo "Installing dependencies to .python_packages..."
mkdir -p .python_packages/lib/site-packages
pip install -r requirements.txt --target .python_packages/lib/site-packages

# Create zip file
echo "Creating backend.zip..."
zip -r ../infra/backend.zip . 

# Go back to the root directory
cd ..

# 3. Get Function App Name
echo "Fetching Function App info..."
echo "Using Resource Group: $RESOURCE_GROUP"
FUNC_INFO=$(az functionapp list --resource-group "$RESOURCE_GROUP" --query "[?contains(name, 'func-fs-judgepapers')].{name:name, rg:resourceGroup}" -o tsv | head -n 1)

if [ -z "$FUNC_INFO" ]; then
    echo "Error: Could not find Function App matching 'func-fs-judgepapers'. Try specifying --resource-group."
    exit 1
fi

read -r FUNC_APP_NAME RESOURCE_GROUP <<< "$FUNC_INFO"

echo "Deploying to Function App: $FUNC_APP_NAME (RG: $RESOURCE_GROUP)"

# 4. Deploy
echo "Publishing function code..."
az functionapp deployment source config-zip -g "$RESOURCE_GROUP" -n "$FUNC_APP_NAME" --src infra/backend.zip

echo "Backend deployment complete."
