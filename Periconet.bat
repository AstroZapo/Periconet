@echo off
setlocal
title Periconet
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto check_deps

echo Preparando Periconet por primera vez (1-2 minutos)...
set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD (
    where python >nul 2>nul && set "PYCMD=python"
)
if not defined PYCMD goto no_python
%PYCMD% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 goto no_python
%PYCMD% -m venv .venv
if errorlevel 1 goto venv_error

:check_deps
fc /b requirements.txt ".venv\requirements-ok.txt" >nul 2>nul
if not errorlevel 1 goto run
echo Instalando componentes (solo la primera vez, necesita Internet)...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto pip_error
copy /y requirements.txt ".venv\requirements-ok.txt" >nul

:run
start "" ".venv\Scripts\pythonw.exe" periconet.py
exit /b 0

:no_python
echo.
echo No encuentro Python 3.11 o superior.
echo Instala Python 3.13 desde https://www.python.org/downloads/windows/
echo (marca la casilla "Add python.exe to PATH" al instalar) y vuelve a abrir Periconet.bat
start "" "https://www.python.org/downloads/windows/"
pause
exit /b 1

:venv_error
echo No se pudo preparar el entorno de Python.
pause
exit /b 1

:pip_error
echo No se pudieron instalar los componentes. Revisa la conexion a Internet.
pause
exit /b 1
