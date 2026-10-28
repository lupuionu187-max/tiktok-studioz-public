@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Test voce (nu atinge dublarea care merge)
set PY=python
where python >nul 2>nul || set PY=py
%PY% test_voce.py %*
echo.
pause
