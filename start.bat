@echo off
chcp 65001 >nul
title xImJxstinn Stream Clipper
cd /d "%~dp0"

set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)

if not defined PY (
  echo Python fehlt - wird jetzt einmalig installiert ...
  winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
  if errorlevel 1 (
    echo.
    echo Automatische Installation ging nicht.
    echo Bitte Python von https://www.python.org/downloads/ installieren
    echo ^(Haken bei "Add python.exe to PATH" setzen^) und start.bat neu starten.
    pause
    exit /b 1
  )
  set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)

if "%PY%"=="py -3" (
  start "" pyw -3 "%~dp0StreamClipper.pyw"
  exit /b 0
)
if "%PY%"=="python" (
  where pythonw >nul 2>&1 && start "" pythonw "%~dp0StreamClipper.pyw" && exit /b 0
  python "%~dp0StreamClipper.pyw"
  exit /b 0
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" (
  start "" "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" "%~dp0StreamClipper.pyw"
) else (
  "%PY%" "%~dp0StreamClipper.pyw"
)
exit /b 0
