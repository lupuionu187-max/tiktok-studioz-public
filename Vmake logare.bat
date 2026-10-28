@echo off
rem Deschide Vmake intr-un Chrome normal, cu profilul separat al aplicatiei (vmake_profil).
rem Intra in contul tau Vmake, apoi INCHIDE fereastra: aplicatia foloseste logarea salvata.
echo Se deschide Vmake. Logheaza-te in contul tau, apoi inchide fereastra Chrome.
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir="%~dp0vmake_profil" --no-first-run https://vmake.ai/video-enhancer/upload
