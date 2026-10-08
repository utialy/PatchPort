$ErrorActionPreference = 'Stop'
try {
    if (-not $env:PATCHPORT_ENDPOINT -or -not $env:PATCHPORT_TOKEN -or $args.Count -ne 1) { exit 0 }
    $patchportEvent = [string]$args[0] | ConvertFrom-Json
    $patchportCodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex' }
    $patchportEvent | Add-Member -NotePropertyName patchport_codex_home -NotePropertyValue $patchportCodexHome -Force
    $patchportPayload = ConvertTo-Json -InputObject $patchportEvent -Compress -Depth 30
    if ($patchportPayload.Length -gt 131072) { exit 0 }
    $patchportBytes = [System.Text.Encoding]::UTF8.GetBytes($patchportPayload)
    $patchportRequest = [System.Net.HttpWebRequest]::Create($env:PATCHPORT_ENDPOINT)
    $patchportRequest.Method = 'POST'
    $patchportRequest.ContentType = 'application/json'
    $patchportRequest.Headers['Authorization'] = 'Bearer ' + $env:PATCHPORT_TOKEN
    $patchportRequest.Timeout = 3000
    $patchportRequest.ContentLength = $patchportBytes.Length
    $patchportStream = $patchportRequest.GetRequestStream()
    $patchportStream.Write($patchportBytes, 0, $patchportBytes.Length)
    $patchportStream.Close()
    $patchportResponse = $patchportRequest.GetResponse()
    $patchportResponse.Close()
} catch {
    # Notifications never change the provider's approval or retry behavior.
}
exit 0
