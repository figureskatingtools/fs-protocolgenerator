targetScope = 'subscription'

// Backend-only deployment.
//
// The frontend, Easy Auth and the custom domain used to live here (an App
// Service Web App proxying to this Function App). They now live in the
// figureskatingtools-site repo, which serves the Protocol Generator UI at
// https://figureskatingtools.com/protocolgenerator/ and proxies
// /protocolgenerator/api/* to this Function App. This template therefore
// deploys only storage + the Function App + its role assignments.

param location string = 'swedencentral'
param resourceGroupName string = ''

// Shared secret between the site router proxy and the Function App; required —
// the gate fails closed when it is empty
// (see function.bicep / storage_helpers._proxy_secret_ok and PROXY-CONTRACT.md).
@secure()
param proxySharedSecret string = ''

// The platform's storage account name (`stfsplat*`, from the figureskatingtools-site
// deployment) whose `competition-data` container holds the shared competition file
// pool and whose tables hold the retention clock and competition access.
// Required in practice: empty = import_platform_file stays off, bound records are
// never auto-deleted and every non-admin competition request is a 503.
param platformStorageAccountName string = ''
param platformDataContainerName string = 'competition-data'

// Nightly orphan step: true = delete orphaned records older than 60 days,
// false = dry run (logs only) — the kill switch (GitHub variable
// ORPHAN_PURGE_ENABLED=false). See modules/function.bicep.
param orphanPurgeEnabled bool = true

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: resourceGroupName
  location: location
}

module storage 'modules/storage.bicep' = {
  scope: rg
  name: 'storageDeployment'
  params: {
    location: location
    storageAccountName: 'stfsprot${uniqueString(rg.id)}'
    containerName: 'fs-protocolgenerator'
  }
}

module function 'modules/function.bicep' = {
  scope: rg
  name: 'functionDeployment'
  params: {
    location: location
    functionAppName: 'func-fs-protocols-${uniqueString(rg.id)}'
    appServicePlanName: 'asp-fs-protocols'
    appInsightsName: 'ai-fs-protocols'
    // rg-fs-protocols-<env> -> log-fs-protocols-<env>
    logAnalyticsWorkspaceName: 'log-fs-protocols-${last(split(resourceGroupName, '-'))}'
    storageAccountName: storage.outputs.storageAccountName
    deploymentContainerUrl: 'https://${storage.outputs.storageAccountName}.blob.${environment().suffixes.storage}/app-package'
    proxySharedSecret: proxySharedSecret
    platformStorageAccountName: platformStorageAccountName
    platformDataContainerName: platformDataContainerName
    orphanPurgeEnabled: orphanPurgeEnabled
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
output orphanPurgeEnabled bool = orphanPurgeEnabled
output functionAppName string = function.outputs.functionAppName
// Consumed by the site repo (TOOL_PRINCIPAL_ID_PROTOCOLGENERATOR) to grant this
// Function App read access to the shared competition-data container.
output functionPrincipalId string = function.outputs.functionPrincipalId
