Option Explicit

Dim shell, fso, scriptDir, projectDir, logDir
Dim psExe, runEcn, logFile, cmd

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
projectDir = fso.GetParentFolderName(scriptDir)
logDir = fso.BuildPath(projectDir, "logs")

If Not fso.FolderExists(logDir) Then
    fso.CreateFolder(logDir)
End If

psExe = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
runEcn = fso.BuildPath(scriptDir, "run_ecn_research.ps1")
logFile = fso.BuildPath(logDir, "ecn_live.log")

cmd = "cmd.exe /d /c " & Quote(Quote(psExe) & _
      " -NoProfile -ExecutionPolicy Bypass -File " & Quote(runEcn) & _
      " >> " & Quote(logFile) & " 2>&1")

shell.Run cmd, 0, False

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function
