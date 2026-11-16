@echo off
rem NemoWhisper: hold Ctrl+Win to dictate (cleaned-up text pasted at the cursor); add Shift for a voice command.
rem Runs as a tray icon with no console; Quit from the icon's menu. A second launch says it's already running.
rem Needs Bolt on http://127.0.0.1:8080 (scripts\run-bolt.ps1). Data (log, history) goes to %USERPROFILE%\.local\dictate.
start "" uvw run --gui-script "%~dp0nemowhisper.py"
