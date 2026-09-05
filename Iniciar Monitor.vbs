Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

pythonExe = scriptDir & "\.venv\Scripts\pythonw.exe"
scriptFile = scriptDir & "\telegram_monitor.pyw"

If Not fso.FileExists(pythonExe) Then
    MsgBox "Nao encontrei " & pythonExe & vbCrLf & vbCrLf & _
        "Crie o ambiente virtual primeiro (num terminal, dentro desta pasta):" & vbCrLf & _
        "python -m venv .venv" & vbCrLf & _
        ".venv\Scripts\pip install -r requirements.txt", vbCritical, "Telegram Ofertas Monitor"
    WScript.Quit 1
End If

Set objShell = CreateObject("WScript.Shell")
objShell.CurrentDirectory = scriptDir
objShell.Run """" & pythonExe & """ """ & scriptFile & """", 0, False
