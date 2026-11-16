# Private host adapter for the existing interactive microphone task. No caller arguments.
$ErrorActionPreference = 'Stop'
$micRoot = Join-Path $env:USERPROFILE '.config\pentacle-mic'
$taskName = 'Pentacle Local Microphone'
function Read-MicStatus {
    try { return Invoke-RestMethod 'http://127.0.0.1:7780/status' -TimeoutSec 2 } catch { return $null }
}
function Is-Ready($s) {
    if (!$s) { return $false }
    # An existing Off service is usable; Start must not turn it on without consent.
    if ($s.mode -eq 'off') { return $true }
    return ($s.mode -in @('on','clipboard','meeting') -and $s.asr.loaded -and $s.audio.health_state -eq 'ok' -and $s.audio_buffer.ready)
}
try {
    $task = Get-ScheduledTask -TaskName $taskName
    if ($task.Principal.UserId -notmatch '(^|\\)hjk6$' -or $task.Principal.LogonType.ToString() -notin @('Interactive','InteractiveToken')) { throw 'Unexpected microphone task identity.' }
    $expectedExe = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $expectedArgs = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File ' + (Join-Path $micRoot 'start.ps1')
    if (@($task.Actions).Count -ne 1 -or $task.Actions[0].Execute -ne $expectedExe -or $task.Actions[0].Arguments -ne $expectedArgs) { throw 'Unexpected microphone task action.' }
    $manifest = Get-Content (Join-Path $micRoot 'source-manifest.json') -Raw | ConvertFrom-Json
    if ($manifest.source -notmatch '^[0-9a-f]{40}$') { throw 'Invalid microphone source manifest.' }
    $runtime = Join-Path $micRoot ('runtime-actions-' + $manifest.source.Substring(0,8))
    $startup = Get-Content (Join-Path $micRoot 'start.ps1') -Raw
    if (!$startup.Contains("`$env:MIC_RUNTIME_SOURCE = '$($manifest.source)'")) { throw 'Microphone source identity changed.' }
    foreach ($f in $manifest.files.PSObject.Properties) {
        if ($f.Name -notmatch '^[a-zA-Z0-9_]+\.py$') { throw 'Invalid runtime filename.' }
        if ((Get-FileHash (Join-Path $runtime $f.Name) -Algorithm SHA256).Hash.ToLower() -ne $f.Value) { throw 'Microphone runtime bytes changed.' }
    }
    $status = Read-MicStatus
    if (Is-Ready $status) { Write-Output '{"ok":true,"ready":true}'; exit 0 }
    $submitted = $false
    # Never restart a running/loading/busy task, including an unhealthy one.
    if ($task.State.ToString() -ne 'Running' -and !$status) {
        if ($task.State.ToString() -ne 'Ready') { throw 'Microphone task is disabled or unavailable.' }
        $listener = Get-NetTCPConnection -LocalPort 7780 -State Listen -ErrorAction SilentlyContinue
        if ($listener) { throw 'Microphone port is already occupied.' }
        $interactive = Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" | Where-Object {
            $owner = Invoke-CimMethod -InputObject $_ -MethodName GetOwner
            $_.SessionId -gt 0 -and $owner.User -eq 'hjk6'
        }
        if (!$interactive) { throw 'Sign into the Windows desktop before starting the microphone.' }
        Start-ScheduledTask -TaskName $taskName
        $submitted = $true
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        Start-Sleep -Milliseconds 500
        $status = Read-MicStatus
        if ((Is-Ready $status) -and (!$submitted -or ($status.mode -eq 'on' -and $status.on_listener_state -eq 'LISTENING'))) {
            Write-Output '{"ok":true,"ready":true}'; exit 0
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Microphone did not become ready before the deadline.'
} catch {
    # Parent exposes a short fixed error, keeping paths/log output host-local.
    Write-Error $_.Exception.Message
    exit 1
}
