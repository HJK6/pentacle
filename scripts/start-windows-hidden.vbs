Option Explicit

Dim shell, repo, electron
Set shell = CreateObject("WScript.Shell")

repo = "C:\Users\vamsh\repos\pentacle"
electron = repo & "\node_modules\electron\dist\electron.exe"

shell.CurrentDirectory = repo
shell.Run """" & electron & """ .", 1, False
