Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
desktopDir = fso.GetParentFolderName(WScript.ScriptFullName) & "\desktop"
electronExe = desktopDir & "\node_modules\electron\dist\electron.exe"
shell.CurrentDirectory = desktopDir
shell.Run """" & electronExe & """ .", 1, False
