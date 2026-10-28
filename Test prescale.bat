@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Test prescale Real-ESRGAN (nu porni dublarea in acelasi timp)
set PY=python
where python >nul 2>nul || set PY=py
%PY% -c "import requests" 2>nul || %PY% -m pip install --quiet requests
%PY% test_prescale.py %*
echo.
pause
