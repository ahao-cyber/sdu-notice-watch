@echo off
cd /d D:\SDUWatch
where python >nul 2>nul
if %errorlevel%==0 (
  python watch.py --local --send --state state-local.json
) else (
  "C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe" watch.py --local --send --state state-local.json
)
echo.
echo done. press any key to close.
pause >nul
