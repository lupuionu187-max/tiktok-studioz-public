@echo off
chcp 65001 >nul
echo Opresc aplicatia de postare TikTok (portul 5055)...
for /f "tokens=5" %%p in ('netstat -ano -p tcp ^| findstr /R /C:":5055 .*LISTENING"') do taskkill /PID %%p /F
echo.
echo Gata. Run-ul in curs s-a oprit. Profilele Dolphin raman deschise.
echo Acum apasa F5 in aplicatia de dublare, apoi Postare TikTok - porneste versiunea noua.
pause
