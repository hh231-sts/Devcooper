@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_dev.ps1" %*
exit /b %ERRORLEVEL%
