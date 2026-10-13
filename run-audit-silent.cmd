@echo off
rem Cloud audit: if the GitHub schedule missed today's run, this machine sends the report itself.
rem Silent version (no pause) - used by Windows Task Scheduler. Keep this file pure ASCII.
cd /d D:\SDUWatch
set PY="C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"
if not exist %PY% set PY=python
%PY% cloud-audit.py >> "D:\SDUWatch\audit.log" 2>&1
exit /b 0
