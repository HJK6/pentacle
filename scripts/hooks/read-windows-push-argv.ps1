# Git for Windows does not expose its native git.exe parent through MSYS $PPID.
# Emit only a verified git.exe ancestor's argv. Any failed query or malformed
# token leaves stdout empty, so the POSIX hook refuses public pushes closed.
$ErrorActionPreference = 'Stop'

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class PentacleNativeArgv {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr CommandLineToArgvW(string commandLine, out int argc);

    [DllImport("kernel32.dll")]
    public static extern IntPtr LocalFree(IntPtr pointer);

    public static string[] Parse(string commandLine) {
        if (String.IsNullOrEmpty(commandLine)) throw new ArgumentException("empty process command line");
        int count;
        IntPtr pointer = CommandLineToArgvW(commandLine, out count);
        if (pointer == IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
        try {
            if (count < 1) throw new ArgumentException("empty argv");
            var values = new string[count];
            for (int i = 0; i < count; i++) {
                values[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(pointer, i * IntPtr.Size));
            }
            return values;
        } finally {
            LocalFree(pointer);
        }
    }
}
'@

$current = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
$git = $null
for ($depth = 0; $depth -lt 12 -and $null -ne $current; $depth++) {
    if ($current.Name -ieq 'git.exe') {
        $git = $current
        break
    }
    if ($current.Name -notin @('powershell.exe', 'sh.exe', 'bash.exe')) {
        throw "unexpected process in hook ancestry: $($current.Name)"
    }
    $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($current.ParentProcessId)"
}
if ($null -eq $git) { throw 'no native git.exe ancestor' }

$tokens = [PentacleNativeArgv]::Parse($git.CommandLine)
foreach ($token in $tokens) {
    if ($null -eq $token -or $token.Contains("`r") -or $token.Contains("`n")) {
        throw 'invalid git.exe argv token'
    }
}

[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
foreach ($token in $tokens) { [Console]::Write($token + [char]10) }
