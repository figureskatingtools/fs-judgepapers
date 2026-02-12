targetScope = 'subscription'

param location string = 'swedencentral'
param resourceGroupName string = ''
param authClientId string = ''
@secure()
param authClientSecret string = ''
param tenantId string = ''

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

module webApp 'modules/webapp.bicep' = {
  scope: rg
  name: 'webAppDeployment'
  params: {
    location: location
    webAppName: 'app-fs-judgepapers-${uniqueString(rg.id)}'
    appServicePlanName: 'asp-fs-judgepapers-web'
    authClientId: authClientId
    authClientSecret: authClientSecret
    tenantId: !empty(tenantId) ? tenantId : subscription().tenantId
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
    allowedOrigins: [
      'https://${webApp.outputs.webAppDefaultHostName}'
    ]
    authClientId: authClientId
    authClientSecret: authClientSecret
    tenantId: !empty(tenantId) ? tenantId : subscription().tenantId
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
output webAppName string = webApp.outputs.webAppName
output webAppDefaultHostName string = webApp.outputs.webAppDefaultHostName
