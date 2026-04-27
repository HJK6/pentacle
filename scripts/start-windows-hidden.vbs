Option Explicit

Dim shell, repo, electron
Set shell = CreateObject("Shell.Application")

repo = "C:\Users\vamsh\repos\pentacle"
electron = repo & "\node_modules\electron\dist\electron.exe"

shell.ShellExecute electron, ".", repo, "runas", 0
