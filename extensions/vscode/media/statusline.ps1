$ErrorActionPreference = 'Stop'
try {
    $patchportEvent = ($input | Out-String) | ConvertFrom-Json
    $patchportEvent | Add-Member -NotePropertyName patchport_statusline -NotePropertyValue $true -Force
    $patchportBytes = [Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $patchportEvent -Compress -Depth 30))
    $patchportRequest = [Net.HttpWebRequest]::Create($env:PATCHPORT_ENDPOINT)
    $patchportRequest.Method = 'POST'
    $patchportRequest.ContentType = 'application/json'
    $patchportRequest.Headers['Authorization'] = 'Bearer ' + $env:PATCHPORT_TOKEN
    $patchportRequest.Timeout = 2500
    $patchportRequest.ContentLength = $patchportBytes.Length
    $patchportStream = $patchportRequest.GetRequestStream()
    $patchportStream.Write($patchportBytes, 0, $patchportBytes.Length)
    $patchportStream.Close()
    $patchportRequest.GetResponse().Close()
    Write-Output ('PatchPort - ' + $patchportEvent.model.display_name)
} catch { Write-Output 'PatchPort - usage unknown' }
exit 0
