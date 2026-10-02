Option Explicit
' Launcher for Image Compressor. Starts the tkinter GUI with pythonw.exe
' so that no console window appears.
' NOTE: keep this file pure ASCII.

Dim fso, sh, base, exe, target, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
target = base & "\image_compressor.pyw"

If Not fso.FileExists(target) Then
    MsgBox "image_compressor.pyw not found next to this launcher." & vbCrLf & _ 
           "Looked in: " & base, 16, "Image Compressor"
    WScript.Quit 1
End If

exe = "C:\Python314\pythonw.exe"
If Not fso.FileExists(exe) Then
    exe = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe")
End If
If Not fso.FileExists(exe) Then
    exe = "pythonw.exe"
End If

sh.CurrentDirectory = base
cmd = """" & exe & """ """ & target & """"
sh.Run cmd, 0, False
