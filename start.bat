@echo off
title Mein Assistent
cd /d "%~dp0"

rem ngrok-Domain aus der .env lesen (Zeile: NGROK_DOMAIN=deine-domain.ngrok-free.dev)
set NGROK_DOMAIN=
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
  if "%%a"=="NGROK_DOMAIN" set NGROK_DOMAIN=%%b
)
if "%NGROK_DOMAIN%"=="" (
  echo NGROK_DOMAIN fehlt in der .env
  pause
  exit /b 1
)

echo Starte Flask...
start "Flask" python app.py

timeout /t 3 /nobreak >nul

echo Starte ngrok...
start "ngrok" ngrok http 5000 --domain=%NGROK_DOMAIN%

echo.
echo Dashboard: http://localhost:5000
echo Extern:    https://%NGROK_DOMAIN%
echo.
pause
