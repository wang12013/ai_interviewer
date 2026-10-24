@echo off
rem Launcher: looks for a Python environment in several places, first hit wins.
rem   1) project venv  .venv\Scripts
rem   2) local env     %USERPROFILE%\.workbuddy\binaries\python\envs\default
rem   3) pythonw / python on PATH
cd /d "%~dp0"

set "PYW="
set "PY="

if exist "%~dp0.venv\Scripts\pythonw.exe" set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if defined PYW goto run

set "LOCALENV=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts"
if exist "%LOCALENV%\pythonw.exe" set "PYW=%LOCALENV%\pythonw.exe"
if exist "%LOCALENV%\python.exe" set "PY=%LOCALENV%\python.exe"
if defined PYW goto run

where pythonw >nul 2>nul
if %errorlevel%==0 set "PYW=pythonw"
where python >nul 2>nul
if %errorlevel%==0 set "PY=python"
if defined PYW goto run
if defined PY goto run

echo.
echo   [ERROR] Python not found.
echo.
echo   Set up the environment first:
echo     python -m venv .venv
echo     .venv\Scripts\pip install -r requirements.txt
echo.
pause
exit /b 1

:run
if defined PYW (
    start "" "%PYW%" "%~dp0run.py"
) else (
    start "" "%PY%" "%~dp0run.py"
)
exit /b 0
