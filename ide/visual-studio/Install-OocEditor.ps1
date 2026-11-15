[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ExtensionsRoot = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.vs/Extensions')
)

$ErrorActionPreference = 'Stop'

$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$grammar = Join-Path $PSScriptRoot 'Syntaxes/OOC.json'
$syntaxDirectory = Join-Path $ExtensionsRoot 'OOC/Syntaxes'

if ($PSCmdlet.ShouldProcess($syntaxDirectory, 'Install the Visual Studio OOC syntax grammar')) {
    New-Item -ItemType Directory -Force -Path $syntaxDirectory | Out-Null
    Copy-Item -LiteralPath $grammar -Destination (Join-Path $syntaxDirectory 'OOC.json') -Force
}

$python = Join-Path $repo '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Warning 'Set up the OOC Python environment from the repository Quick start before adding the tools.'
}

Write-Output 'Reopen any .ckt files in Visual Studio to load the grammar.'
Write-Output 'In Tools > External Tools, add the following entry. Select Use Output window.'
Write-Output 'Title: OOC Solve'
Write-Output "Command: $python"
Write-Output 'Arguments: -m circuitlab.cli solve "$(ItemPath)"'
Write-Output "Initial directory: $repo"
Write-Output 'For a second tool titled OOC Check, use -m circuitlab.cli check "$(ItemPath)".'
