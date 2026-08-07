using '../main.bicep'

param resourceGroupName = 'rg-fs-judgepapers-test'
param location = 'swedencentral'
// proxySharedSecret is injected from GitHub Environment secrets at deploy time
