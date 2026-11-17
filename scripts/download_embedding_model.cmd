@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0download_embedding_model.ps1" %*
exit /b %ERRORLEVEL%
