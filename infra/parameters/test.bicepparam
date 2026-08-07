using '../main.bicep'

param resourceGroupName = 'rg-fs-protocols-test'
param location = 'swedencentral'
// proxySharedSecret is injected from GitHub Environment secrets at deploy time
