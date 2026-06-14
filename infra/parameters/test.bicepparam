using '../main.bicep'

param resourceGroupName = 'rg-fs-protocols-test'
param location = 'swedencentral'
// authClientId is injected from GitHub Environment secrets at deploy time
param customDomain = 'test.protocols.figureskatingtools.com'
