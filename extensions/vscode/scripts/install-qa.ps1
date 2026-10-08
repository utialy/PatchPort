$ErrorActionPreference = 'Stop'
$patchportRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../../..')).Path
$patchportEvidence = Join-Path $patchportRoot '.bridge/vscode-qa'
$patchportArtifacts = Join-Path $patchportRoot '.bridge/artifacts'
$patchportCli = (Get-Command code.cmd -ErrorAction Stop).Source
$patchportVersion = (Get-Content -LiteralPath (Join-Path $PSScriptRoot '../package.json') -Raw | ConvertFrom-Json).version
$patchportPortableBefore = $env:VSCODE_PORTABLE
try {
    $env:VSCODE_PORTABLE = Join-Path $patchportEvidence 'vscode-portable'
    & $patchportCli --install-extension (Join-Path $patchportArtifacts ('patchport-' + $patchportVersion + '-win32-x64.vsix')) --force
    if ($LASTEXITCODE -ne 0) { throw 'VSIX installation failed.' }
} finally {
    if ($null -ne $patchportPortableBefore) { $env:VSCODE_PORTABLE = $patchportPortableBefore } else { Remove-Item Env:VSCODE_PORTABLE -ErrorAction SilentlyContinue }
}
