@echo off
rem Opens the Red Commander window: edit the scenario and settings, start and stop the bridge, watch its log.
rem Keep this console window open; the commander runs in it while the window is up.

cd /d "%~dp0"
python -m bridge.gui
if errorlevel 1 pause
