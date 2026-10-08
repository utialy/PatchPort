param([int]$Port = 9347)
$ErrorActionPreference = 'Stop'
$patchportRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../../..')).Path
$patchportEvidence = Join-Path $patchportRoot '.bridge/vscode-qa'
$patchportCli = (Get-Command code.cmd -ErrorAction Stop).Source
$patchportCode = Join-Path (Split-Path (Split-Path $patchportCli -Parent) -Parent) 'Code.exe'
if (-not (Test-Path -LiteralPath $patchportCode)) { throw 'Locate the VS Code executable beside its bin directory.' }
$patchportPortableBefore = $env:VSCODE_PORTABLE
$env:VSCODE_PORTABLE = Join-Path $patchportEvidence 'vscode-portable'
$patchportArgs = @(
    '--new-window', '--skip-welcome', '--skip-release-notes',
    ('--remote-debugging-port=' + $Port),
    (Join-Path $patchportEvidence 'ui-project')
)
$patchportQuoted = $patchportArgs | ForEach-Object { '"' + $_.Replace('"', '\"') + '"' }
$patchportNodeMode = $env:ELECTRON_RUN_AS_NODE
Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
$patchportProcess = Start-Process -FilePath $patchportCode -ArgumentList $patchportQuoted -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $patchportEvidence 'vscode-stdout.log') -RedirectStandardError (Join-Path $patchportEvidence 'vscode-stderr.log')
if ($null -ne $patchportNodeMode) { $env:ELECTRON_RUN_AS_NODE = $patchportNodeMode }
if ($null -ne $patchportPortableBefore) { $env:VSCODE_PORTABLE = $patchportPortableBefore } else { Remove-Item Env:VSCODE_PORTABLE -ErrorAction SilentlyContinue }
@{ pid = $patchportProcess.Id; port = $Port; project = (Join-Path $patchportEvidence 'ui-project') } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $patchportEvidence 'qa-window.json') -Encoding UTF8
Write-Output ('Started isolated VS Code, PID ' + $patchportProcess.Id)
Start-Sleep -Seconds 2
Write-Output ('Exited: ' + $patchportProcess.HasExited + '; exit code: ' + $patchportProcess.ExitCode)
