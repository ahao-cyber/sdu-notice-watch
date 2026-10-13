@echo off
rem Local supplement run: covers the two campus-IP-only sources.
rem Silent version (no pause) - used by Windows Task Scheduler.
cd /d D:\SDUWatch
set PY="C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"
if not exist %PY% set PY=python
%PY% watch.py --local --send --skip-empty-send --state state-local.json >> "D:\SDUWatch\local-run.log" 2>&1
exit /b 0
