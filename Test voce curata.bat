@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Test voce curata (pe placa ta; VoiceStudio deschis, nu porni dublarea)
set PY=python
where python >nul 2>nul || set PY=py
%PY% -c "import requests" 2>nul || %PY% -m pip install --quiet requests
%PY% test_voce_curata.py %*
echo.
pause
