@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Test dublaj - A vs B (nu atinge dublarea care merge)
set PY=python
where python >nul 2>nul || set PY=py
%PY% test_dublaj.py %*
echo.
pause
