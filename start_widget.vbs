' Start AI relay usage widget (no console window)
Set fso = CreateObject("Scripting.FileSystemObject")
Set f = fso.GetFile(WScript.ScriptFullName)
Set d = f.ParentFolder
Set ws = CreateObject("WScript.Shell")
ws.Run """C:\Users\10201\AppData\Local\Programs\Python\Python314\pythonw.exe"" """ & d.Path & "\ai_usage_widget.py""", 0, False
