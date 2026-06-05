targetScope = 'subscription'

param location string = 'swedencentral'
param resourceGroupName string = ''
param authClientId string = ''
param tenantId string = ''

// Custom domain for the web app (e.g. 'judgepapers.figureskatingtools.com').
// Empty = skip DNS + domain binding. The DNS zone itself is deployed by the
// root frontend site (figureskatingtools.com landing page); this deployment
// only manages its own record sets in that zone.
param customDomain string = ''
param dnsZoneName string = 'figureskatingtools.com'
param dnsZoneResourceGroup string = 'rg-fs-dns'

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: resourceGroupName
  location: location
}

module authManagedIdentity 'modules/auth-identity.bicep' = {
  scope: rg
  name: 'authIdentityDeployment'
  params: {
    location: location
    managedIdentityName: 'mi-fs-judgepapers-auth-${uniqueString(rg.id)}'
  }
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
    authManagedIdentityClientId: authManagedIdentity.outputs.clientId
    authManagedIdentityResourceId: authManagedIdentity.outputs.resourceId
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
    authManagedIdentityClientId: authManagedIdentity.outputs.clientId
    authManagedIdentityResourceId: authManagedIdentity.outputs.resourceId
    tenantId: !empty(tenantId) ? tenantId : subscription().tenantId
  }
}

// DNS records (CNAME + asuid TXT) in the shared figureskatingtools.com zone
module dns 'modules/dns.bicep' = if (!empty(customDomain)) {
  scope: resourceGroup(dnsZoneResourceGroup)
  name: 'dnsDeployment'
  params: {
    dnsZoneName: dnsZoneName
    recordName: replace(customDomain, '.${dnsZoneName}', '')
    targetHostname: webApp.outputs.webAppDefaultHostName
    domainVerificationId: webApp.outputs.customDomainVerificationId
  }
}

// Hostname binding + managed certificate (requires DNS records above)
module webAppCustomDomain 'modules/webapp-customdomain.bicep' = if (!empty(customDomain)) {
  scope: rg
  name: 'customDomainDeployment'
  params: {
    webAppName: webApp.outputs.webAppName
    customDomain: customDomain
    appServicePlanId: webApp.outputs.appServicePlanId
    location: location
  }
  dependsOn: [
    dns
  ]
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
output authManagedIdentityClientId string = authManagedIdentity.outputs.clientId
output authManagedIdentityObjectId string = authManagedIdentity.outputs.principalId
output customDomain string = customDomain
