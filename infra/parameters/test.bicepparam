using '../main.bicep'

param resourceGroupName = 'rg-fs-judgepapers-test'
param location = 'swedencentral'
// authClientId is injected from GitHub Environment secrets at deploy time
param customDomain = 'test.judgepapers.figureskatingtools.com'
