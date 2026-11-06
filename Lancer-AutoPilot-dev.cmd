@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
if exist "C:\Program Files\WSJTX-AutoPilot\_internal\wsjtx-ap1\bin\wsjtx.exe" set "WSJTX_AUTOPILOT_APP_DIR=C:\Program Files\WSJTX-AutoPilot"
py -m wsjtx_autopilot.gui.app
