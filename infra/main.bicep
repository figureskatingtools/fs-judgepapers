targetScope = 'subscription'

// Backend-only infrastructure. The frontend (and its Easy Auth / custom domain /
// DNS) now lives in the figureskatingtools-site repo, which hosts every tool on
// https://figureskatingtools.com/<tool>/ and proxies /<tool>/api/* to the
// Function App deployed here. This template therefore only creates storage, the
// Function App (+ App Insights) and the storage role assignments.
param location string = 'swedencentral'
param resourceGroupName string = ''

// Shared secret between the site router proxy and the Function App (see
// function.bicep / function_app.py:_proxy_secret_ok and PROXY-CONTRACT.md).
@secure()
param proxySharedSecret string = ''

// Site repo's platform storage account (stfsplat*) whose competition-data
// container holds the shared competition file pool. Empty = pool import off.
param platformStorageAccountName string = ''
param platformDataContainerName string = 'competition-data'

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: resourceGroupName
  location: location
}

module storage 'modules/storage.bicep' = {
  scope: rg
  name: 'storageDeployment'
  params: {
    location: location
    storageAccountName: 'stfsjudge${uniqueString(rg.id)}'
    containerName: 'fs-judgepapers'
  }
}

module function 'modules/function.bicep' = {
  scope: rg
  name: 'functionDeployment'
  params: {
    location: location
    functionAppName: 'func-fs-judgepapers-${uniqueString(rg.id)}'
    appServicePlanName: 'asp-fs-judgepapers'
    appInsightsName: 'ai-fs-judgepapers'
    storageAccountName: storage.outputs.storageAccountName
    deploymentContainerUrl: 'https://${storage.outputs.storageAccountName}.blob.${environment().suffixes.storage}/app-package'
    proxySharedSecret: proxySharedSecret
    platformStorageAccountName: platformStorageAccountName
    platformDataContainerName: platformDataContainerName
  }
}

module roleAssignment 'modules/roleassignment.bicep' = {
  scope: rg
  name: 'roleAssignmentDeployment'
  params: {
    storageAccountName: storage.outputs.storageAccountName
    functionPrincipalId: function.outputs.functionPrincipalId
  }
}

output resourceGroupName string = rg.name
output storageAccountName string = storage.outputs.storageAccountName
output functionAppName string = function.outputs.functionAppName

// Consumed by the site repo (TOOL_PRINCIPAL_ID_JUDGEPAPERS) so the shared
// competition-data container can grant this Function App read access.
output functionPrincipalId string = function.outputs.functionPrincipalId
