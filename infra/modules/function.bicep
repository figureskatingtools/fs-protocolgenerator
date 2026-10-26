param location string
param functionAppName string
param appServicePlanName string
param appInsightsName string

// Technical logs are kept 90 days (the site's privacy notice says so). A
// classic component's retention follows the Log Analytics workspace it is
// linked to, so both the workspace and the component say 90 explicitly.
@description('Log Analytics workspace behind the App Insights component (log-fs-protocols-<env>).')
param logAnalyticsWorkspaceName string

@description('Days the technical logs are kept (workspace and App Insights). The privacy notice promises 90.')
@minValue(30)
@maxValue(90)
param logRetentionDays int = 90
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

// Kill switch of the nightly orphan step (function_app.py _run_auto_delete):
// true = an orphaned record (no platform competition behind it) older than 60
// days is deleted like a platform "deleted" verdict; false = dry run, the
// timer only logs what it would delete. Bicep-owned, so it survives redeploys.
@description('true = the nightly sweep deletes orphaned records older than 60 days; false = dry-run logging only.')
param orphanPurgeEnabled bool = true

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

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logRetentionDays
  }
}

// Workspace-based: linking it here replaces the default workspace the classic
// component was auto-migrated to. Data already in that old workspace keeps its
// retention until it ages out.
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
    RetentionInDays: logRetentionDays
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
        {
          name: 'ORPHAN_PURGE_ENABLED'
          value: orphanPurgeEnabled ? 'true' : 'false'
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
