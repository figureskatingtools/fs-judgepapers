param location string
param functionAppName string
param appServicePlanName string
param appInsightsName string
param storageAccountName string
param deploymentContainerUrl string
param allowedOrigins array = []
param authClientId string = ''
param authManagedIdentityClientId string = ''
param authManagedIdentityResourceId string = ''
param tenantId string = ''

// Comma-separated list of the Web App's outbound IPs. When set, the Function
// App only accepts inbound traffic from these IPs (i.e. only via the Web App
// proxy). Empty = no IP restriction.
param allowedOutboundIps string = ''

var outboundIpList = empty(allowedOutboundIps) ? [] : split(allowedOutboundIps, ',')
var ipSecurityRestrictions = [
  for (ip, i) in outboundIpList: {
    ipAddress: '${ip}/32'
    action: 'Allow'
    priority: 100 + i
    name: 'webapp-outbound-${i}'
  }
]

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
    type: 'SystemAssigned,UserAssigned'
    userAssignedIdentities: {
      '${authManagedIdentityResourceId}': {}
    }
  }
  properties: {
    serverFarmId: appServicePlan.id
    siteConfig: {
      cors: {
        allowedOrigins: allowedOrigins
      }
      // Lock inbound traffic to the Web App's outbound IPs (proxy only).
      // Leaves the SCM/deployment endpoint unrestricted so CI can still deploy.
      ipSecurityRestrictions: empty(allowedOutboundIps) ? null : ipSecurityRestrictions
      ipSecurityRestrictionsDefaultAction: empty(allowedOutboundIps) ? null : 'Deny'
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
          name: 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'
          value: authManagedIdentityClientId
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
        version: '3.11'
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
    // The Web App handles the real Entra login and proxies requests here,
    // forwarding the user's email. The Function App must allow anonymous so
    // those proxied requests reach the app, which authorizes via the forwarded
    // header (get_user_email_from_header). Inbound access is locked to the Web
    // App's IPs (ipSecurityRestrictions above). The AAD provider stays enabled
    // so a bearer token is still validated when one is present.
    globalValidation: {
      requireAuthentication: false
      unauthenticatedClientAction: 'AllowAnonymous'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: !empty(authClientId)
        registration: {
          clientId: authClientId
          clientSecretSettingName: 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            authClientId
            'api://${authClientId}'
          ]
        }
      }
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
