@echo off
REM OPCIONAL: genera Periconet.exe (un solo archivo, como el original).
REM Abre antes Periconet.bat al menos una vez para preparar el entorno.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Abre primero Periconet.bat una vez.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install "pyinstaller>=6"
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --name Periconet ^
    --add-data "frontend;frontend" ^
    --hidden-import app ^
    --hidden-import camera_credentials_win ^
    --collect-submodules uvicorn ^
    --collect-submodules websockets ^
    --collect-submodules pystray ^
    periconet.py
if errorlevel 1 (
    echo Error al crear el .exe
    pause
    exit /b 1
)
if exist config.json copy /y config.json dist\config.json >nul
echo.
echo Listo: dist\Periconet.exe
pause
