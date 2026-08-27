@echo off
title SW GRASSVALLEY MONITOR (DEMO)
cd /d "%‾dp0"
set SCRIPT=%‾dp0sw_grassvalley_monitor.py

where py >nul 2>&1 && goto PY
where python >nul 2>&1 && goto PYTHON
echo Python が見つかりません。
pause
goto END

:PY
py "%SCRIPT%" --demo
goto END

:PYTHON
python "%SCRIPT%" --demo

:END
pause
