@echo off
chcp 65001 >nul
rem Double-click entry for the realtime-subtitle bootstrap installer.
rem Runs the sibling bootstrap.ps1 with the user's own arguments, then keeps
rem the window open so any error message can be read.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1" %*
echo.
pause
