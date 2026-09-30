@echo off
rem Starts the Red Commander bridge. Double-click it, or run it at logon (see README).
rem It waits for a DCS mission with Olympus, spawns the defence, and keeps running across mission restarts.
rem Change SCENARIO to the scenario you want. Add --plan logs\plan-....json to reuse a saved plan.

set SCENARIO=scenarios\defend-kutaisi.yaml

cd /d "%~dp0"
python -m bridge run %SCENARIO% %*
pause
