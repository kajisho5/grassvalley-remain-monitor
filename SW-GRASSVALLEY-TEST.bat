@echo off
title SW GRASSVALLEY - 接続テスト
cd /d "%‾dp0"
set SCRIPT=%‾dp0sw_grassvalley_monitor.py

echo T2/T3 の IP とVDCPポートを入れてください。
echo 例: 192.168.0.50:8000
echo.
set /p TARGET=IP:ポート = 

where py >nul 2>&1 && goto PY
where python >nul 2>&1 && goto PYTHON
echo Python が見つかりません。
pause
goto END

:PY
py "%SCRIPT%" --try %TARGET%
goto END

:PYTHON
python "%SCRIPT%" --try %TARGET%

:END
echo.
pause
