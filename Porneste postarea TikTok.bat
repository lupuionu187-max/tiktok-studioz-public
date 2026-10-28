@echo off
chcp 65001 >nul
title Postare TikTok - lasa fereastra deschisa cat lucrezi
cd /d "%USERPROFILE%\.openclaw\workspace\tiktok_bot\studio"
echo Opresc copiile vechi ale aplicatiei de postare...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*web.py*' -and $_.CommandLine -notlike '*dublare*' } | ForEach-Object { Write-Host ('  oprit PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
for /f "tokens=5" %%p in ('netstat -ano -p tcp ^| findstr /R /C:":5055 .*LISTENING"') do taskkill /PID %%p /F >nul 2>&1
timeout /t 2 /nobreak >nul
echo.
echo Pornesc aplicatia de postare... (se deschide singura in browser)
set PYTHONIOENCODING=utf-8
python web.py
echo.
echo Aplicatia de postare s-a oprit. Daca vezi o eroare mai sus, trimite-mi-o.
pause
