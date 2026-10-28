@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Urc aplicatia pe GitHub - nu inchide fereastra

rem Folderul de postare ramane unde e. Aici doar il copiez langa aplicatie, ca sa ajunga si el pe GitHub.
rem Fara video, arhive, modele si medii Python: GitHub nu le primeste oricum.
set "POSTARE=%USERPROFILE%\.openclaw\workspace\tiktok_bot"
if exist "%POSTARE%" (
    echo Copiez aplicatia de postare de la %POSTARE% ...
    robocopy "%POSTARE%" "%~dp0tiktok_bot" /E /XO /XD .git *venv* __pycache__ *_profil /XF *.mp4 *.mov *.mkv *.avi *.mp3 *.wav *.m4a *.flac *.aac *.ogg *.zip *.7z *.rar *.apk *.apks *.xapk *.aab *.obb *.pt *.pth *.ckpt *.safetensors *.onnx *.bin /R:1 /W:1 /NFL /NDL /NP /NJH /NJS
) else (
    echo Nu gasesc aplicatia de postare la %POSTARE% - urc doar restul.
)

echo.
echo Salvez schimbarile...
git add -A
git commit -q -m "Actualizare %date% %time%"

echo.
echo Iau ce e nou pe GitHub...
git pull --no-edit

echo.
echo Urc pe GitHub...
git push

echo.
echo Gata. Daca vezi o eroare mai sus, trimite-mi o poza.
pause
