using '../main.bicep'

param resourceGroupName = 'rg-fs-protocols-prod'
param location = 'swedencentral'
// authClientId is injected from GitHub Environment secrets at deploy time
param customDomain = 'protocols.figureskatingtools.com'
