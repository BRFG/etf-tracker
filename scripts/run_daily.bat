@echo off
rem Daily incremental collection for the two ETF sites.
rem Called by Windows Task Scheduler at 08:45 on trading days, or run manually.
rem Keep this file ASCII-only: cmd.exe parses .bat with the OEM codepage (936),
rem so any non-ASCII comment would break line parsing.
rem
rem Task Scheduler settings:
rem   Program : C:\Windows\System32\cmd.exe
rem   Arguments: /c "D:\...\scripts\run_daily.bat"
rem   Start in : the project root (same folder that contains scripts\)
cd /d "%~dp0.."
set "PY=C:\Users\14457\.conda\envs\excel\python.exe"
"%PY%" scripts\collect.py
exit /b %ERRORLEVEL%
