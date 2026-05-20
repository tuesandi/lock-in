@echo off
title Mein Assistent
cd /d "%~dp0"

echo Starte Flask...
start "Flask" python app.py

timeout /t 3 /nobreak >nul

echo Starte ngrok...
start "ngrok" ngrok http 5000 --domain=unmatched-starter-abstract.ngrok-free.dev

echo.
echo Dashboard: http://localhost:5000
echo Extern:    https://unmatched-starter-abstract.ngrok-free.dev
echo.
pause
