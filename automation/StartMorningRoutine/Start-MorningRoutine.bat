@echo off
powershell.exe -ExecutionPolicy Bypass -File "./Start-MorningRoutine.ps1" -IgnoreTimeWindow  -SkipBrowser -SkipExcel
pause