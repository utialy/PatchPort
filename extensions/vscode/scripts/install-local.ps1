param([string]$EvidenceDirectory = '')
$ErrorActionPreference = 'Stop'
$patchportRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../../..')).Path
$patchportVersion = (Get-Content -LiteralPath (Join-Path $PSScriptRoot '../package.json') -Raw | ConvertFrom-Json).version
$patchportEvidence = if ($EvidenceDirectory) { [System.IO.Path]::GetFullPath($EvidenceDirectory) } else { Join-Path $patchportRoot '.bridge/artifacts' }
$patchportVsix = Join-Path $patchportEvidence ('patchport-' + $patchportVersion + '-win32-x64.vsix')
$patchportCli = (Get-Command code.cmd -ErrorAction Stop).Source
& $patchportCli --install-extension $patchportVsix --force
if ($LASTEXITCODE -ne 0) { throw 'VSIX installation failed.' }
$patchportInstalled = & $patchportCli --list-extensions --show-versions
if ($LASTEXITCODE -ne 0 -or $patchportInstalled -notcontains ('utialy.patchport@' + $patchportVersion)) { throw 'The installed extension version was not confirmed.' }
$patchportReceipt = @{ extension = 'utialy.patchport'; version = $patchportVersion; installed = $true; cli = $patchportCli; installedAt = [DateTime]::UtcNow.ToString('o'); vsixSha256 = (Get-FileHash -LiteralPath $patchportVsix -Algorithm SHA256).Hash }
$patchportReceipt | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $patchportEvidence 'local-install.json') -Encoding UTF8
Write-Output ('Confirmed utialy.patchport@' + $patchportVersion + ' in the normal VS Code extension profile.')
