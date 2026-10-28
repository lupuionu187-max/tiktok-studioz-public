@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Pornire automata a motorului la Windows

rem --- folderul aplicatiei, fara backslash final ---
set "APP=%~dp0"
if "%APP:~-1%"=="\" set "APP=%APP:~0,-1%"

rem --- alege pythonw (ascuns) daca exista, altfel python ---
set "PYEXE=pythonw"
where pythonw >nul 2>nul || set "PYEXE=python"

rem --- creeaza un mic script in folderul Startup, care porneste motorul ASCUNS la fiecare boot ---
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS=%STARTUP%\TikTok Studioz motor.vbs"

> "%VBS%" echo Set sh = CreateObject("WScript.Shell")
>> "%VBS%" echo sh.CurrentDirectory = "%APP%"
>> "%VBS%" echo sh.Run "%PYEXE% ""%APP%\dublare_ui.py"" --no-browser", 0, False

echo ============================================================
echo  GATA. Motorul va porni singur, ascuns, la fiecare pornire
echo  a Windows-ului. Cand intri pe site, se conecteaza singur.
echo ============================================================
echo.
echo  Il pornesc si ACUM, ca sa nu astepti un restart...
start "" "%PYEXE%" "%APP%\dublare_ui.py" --no-browser
echo  Pornit.
echo.
echo  Ca sa anulezi mai tarziu pornirea automata, sterge fisierul:
echo    %VBS%
echo.
pause
