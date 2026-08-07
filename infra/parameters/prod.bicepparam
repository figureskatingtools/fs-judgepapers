using '../main.bicep'

param resourceGroupName = 'rg-fs-judgepapers-prod'
param location = 'swedencentral'
// proxySharedSecret is injected from GitHub Environment secrets at deploy time
