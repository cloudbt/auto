@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0open_edge_urls.ps1"

sleep 5
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0click_teams_join.ps1"