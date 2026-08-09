param location string
param functionAppName string
param appServicePlanName string
param appInsightsName string
param storageAccountName string
param deploymentContainerUrl string

// Shared secret the site router proxy sends as X-Proxy-Secret. Empty = the
// function doesn't enforce it (local/dev). See function_app.py:_proxy_secret_ok
// and PROXY-CONTRACT.md.
@secure()
param proxySharedSecret string = ''

// Platform (site repo) storage account holding the shared competition file pool
// this app reads with its managed identity. Empty = pool import stays off (see
// function_app.py:get_platform_container_client).
param platformStorageAccountName string = ''
param platformDataContainerName string = 'competition-data'

resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: appServicePlan.id
    siteConfig: {
      // No CORS origins: the browser never talks to this Function App directly.
      // All traffic arrives server-side from the site router proxy
      // (figureskatingtools.com/<tool>/api/*), so cross-origin access is never
      // needed. An empty array also clears origins left by a prior deploy.
      cors: {
        allowedOrigins: []
      }
      // Explicitly no inbound IP restrictions. The endpoint must stay reachable
      // by the site router proxy AND by the CI deploy's sync-triggers/health-check
      // (a Deny lock 403s the GitHub runner and hangs the pipeline). An empty
      // array also clears any restriction left over from a prior deploy.
      ipSecurityRestrictions: []
      ipSecurityRestrictionsDefaultAction: 'Allow'
      appSettings: [
        {
          name: 'AzureWebJobsStorage__accountName'
          value: storageAccountName
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          name: 'PROXY_SHARED_SECRET'
          value: proxySharedSecret
        }
        {
          name: 'PLATFORM_STORAGE_ACCOUNT'
          value: platformStorageAccountName
        }
        {
          name: 'PLATFORM_DATA_CONTAINER'
          value: platformDataContainerName
        }
      ]
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: deploymentContainerUrl
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      runtime: {
        name: 'python'
        version: '3.13'
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 100
        instanceMemoryMB: 2048
      }
    }
  }
}

resource authSettings 'Microsoft.Web/sites/config@2022-03-01' = {
  parent: functionApp
  name: 'authsettingsV2'
  properties: {
    // The site router (figureskatingtools.com) handles the real Entra login and
    // proxies requests here, forwarding the user's email plus the shared
    // secret. The Function App must allow anonymous so those proxied requests
    // reach the app, which authorizes via the forwarded header
    // (get_user_email_from_header — see PROXY-CONTRACT.md). No identity
    // provider is registered here: this app has no login surface of its own.
    globalValidation: {
      requireAuthentication: false
      unauthenticatedClientAction: 'AllowAnonymous'
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
  }
}

output functionAppName string = functionApp.name
output functionAppId string = functionApp.id
output functionPrincipalId string = functionApp.identity.principalId
