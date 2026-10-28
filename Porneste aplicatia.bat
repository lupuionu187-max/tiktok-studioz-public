@echo off
chcp 65001 >nul
cd /d "%~dp0"
title TikTok Studioz - nu inchide fereastra asta cat lucrezi

set PY=python
where python >nul 2>nul || set PY=py

rem Git: necesar pentru actualizarea automata a aplicatiei
where git >nul 2>nul || (
    echo Instalez Git ^(pentru actualizari automate^), dureaza 1-2 minute...
    winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements
)

rem ffmpeg: pune video-ul final impreuna (voce + subtitrari + watermark) la MP4
where ffmpeg >nul 2>nul || if not exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" (
    echo Instalez ffmpeg ^(pentru subtitrari si watermark la MP4^), dureaza 1-2 minute...
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
)

rem Ollama: model de traducere local, gratuit
where ollama >nul 2>nul || if not exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
    echo Instalez Ollama ^(traducere gratuita pe calculatorul tau^), dureaza cateva minute...
    winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements
)

rem Lansatorul: verifica actualizarile, apoi porneste aplicatia
%PY% launcher.py
echo.
pause
