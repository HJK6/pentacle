Option Explicit

Dim shell, repo, ps, args
Set shell = CreateObject("Shell.Application")

repo = "C:\Users\vamsh\repos\pentacle"
ps = "$repo = 'C:\Users\vamsh\repos\pentacle'; " & _
  "$procs = Get-CimInstance Win32_Process | Where-Object { " & _
  "($_.Name -like 'electron*' -and $_.CommandLine -like ('*' + $repo + '*')) -or " & _
  "($_.Name -eq 'node.exe' -and $_.CommandLine -like '*electron\cli.js*' -and $_.CommandLine -like ('*' + $repo + '*')) " & _
  "}; " & _
  "$procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; " & _
  "Start-Sleep -Milliseconds 500; " & _
  "Start-Process -FilePath (Join-Path $repo 'node_modules\electron\dist\electron.exe') -ArgumentList '.' -WorkingDirectory $repo"

args = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command " & Chr(34) & ps & Chr(34)
shell.ShellExecute "powershell.exe", args, "", "runas", 0
