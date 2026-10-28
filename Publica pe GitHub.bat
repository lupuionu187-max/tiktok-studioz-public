@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Publica pe GitHub - repo PUBLIC curat (doar cod)

set PY=python
where python >nul 2>nul || set PY=py

echo ============================================================
echo  Urc codul (DOAR cod, fara chei/date) intr-un repo PUBLIC.
echo  Repo-ul public trebuie sa existe deja ^(l-ai creat pe github.com^).
echo ============================================================
echo.
set /p URL=Lipeste adresa repo-ului public si apasa Enter:
if "%URL%"=="" ( echo Nu ai pus adresa. Ies. & pause & exit /b 1 )

set "DEST=%~dp0..\tiktok-studioz-public"
echo.
echo Construiesc folderul curat in:
echo   "%DEST%"
%PY% "%~dp0pregateste_public.py" "%DEST%"
if errorlevel 1 (
    echo.
    echo S-a gasit o problema ^(vezi mai sus^) sau un posibil secret. NU urc nimic.
    pause
    exit /b 1
)

cd /d "%DEST%"
if not exist ".git" (
    git init
    git branch -M main
    git remote add origin "%URL%"
) else (
    git remote set-url origin "%URL%" 2>nul || git remote add origin "%URL%"
)
git add -A
git commit -m "Actualizare cod public"
git branch -M main
rem --force: suprascrie repo-ul public cu codul curat (e doar o oglinda a codului, fara date)
git push -u origin main --force

echo.
echo ============================================================
echo  Gata. Daca vezi erori mai sus, trimite-mi o poza.
echo  Urmatorul pas: porneste GitHub Pages (vezi GHID_LANSARE.md).
echo ============================================================
pause
