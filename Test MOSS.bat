@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Test MOSS-TTS (pod separat, se sterge singur la final)
set PY=python
where python >nul 2>nul || set PY=py
%PY% test_moss.py %*
echo.
pause
