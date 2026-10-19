param location string
param functionAppName string
param appServicePlanName string
param appInsightsName string
param storageAccountName string
param deploymentContainerUrl string

// Shared secret the site router proxy sends as X-Proxy-Secret. The gate fails
// closed: empty = every request but /api/health is a 401. See
// storage_helpers._proxy_secret_ok and PROXY-CONTRACT.md.
@secure()
param proxySharedSecret string = ''

// The platform's storage account (`stfsplat*`, owned by the figureskatingtools-site
// repo): the shared per-competition file pool this app imports from (Blob Data
// Reader), the retention clock and the competition access tables
// (`useridentities`, `competitionaccess` — Table Data Reader, granted by the site
// repo). Empty = the import feature is off and every non-admin request naming a
// competition is a 503 (access cannot be checked).
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
      cors: {
        // No browser ever calls this Function App directly — every request comes
        // from the site router proxy (server-to-server), so no origin is allowed.
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
    // proxies requests here, forwarding the user's tenant/object id plus the
    // shared secret. The Function App must allow anonymous so those proxied
    // requests reach the app, which authorizes via the forwarded headers
    // (storage_helpers.get_caller) and the platform's access tables
    // (platform_access.py). No identity provider is registered here — this app
    // is never browsed to directly.
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
