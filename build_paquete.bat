@echo off
setlocal enabledelayedexpansion
REM ==================================================================
REM  BOTCorrecciones - build del paquete distribuible (PyInstaller onedir)
REM  Uso: doble click DESDE LA CARPETA DEL PROYECTO (donde esta main.py)
REM  Resultado: dist\BOTCorrecciones -> carpeta lista para zipear y pasar
REM ==================================================================
cd /d "%~dp0"

REM --- Verificaciones previas ---------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] No se encontro Python en el PATH.
    pause
    exit /b 1
)
if not exist main.py (
    echo [ERROR] No se encontro main.py. Ejecutar este bat desde la carpeta del proyecto.
    pause
    exit /b 1
)
if not exist credentials.json (
    echo [ERROR] Falta credentials.json en la carpeta del proyecto.
    pause
    exit /b 1
)

echo [1/6] Entorno de build (.venv_build)...
if not exist .venv_build (
    python -m venv .venv_build
    if errorlevel 1 (
        echo [ERROR] No se pudo crear el venv.
        pause
        exit /b 1
    )
)
call .venv_build\Scripts\activate.bat

echo [2/6] Instalando dependencias...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo [ERROR] Fallo la instalacion de dependencias.
    pause
    exit /b 1
)

echo [3/6] Instalando Firefox de Playwright (si ya esta, no re-descarga)...
python -m playwright install firefox
if errorlevel 1 (
    echo [ERROR] Fallo la descarga del Firefox de Playwright.
    pause
    exit /b 1
)

echo [4/6] Compilando con PyInstaller (onedir)...
pyinstaller --noconfirm --clean --onedir --console ^
    --name BOTCorrecciones ^
    --collect-all playwright ^
    main.py
if errorlevel 1 (
    echo [ERROR] Fallo PyInstaller.
    pause
    exit /b 1
)

set "DEST=dist\BOTCorrecciones"

echo [5/6] Copiando el Firefox de Playwright al paquete...
if not exist "%DEST%\ms-playwright" mkdir "%DEST%\ms-playwright"
set COPIADO=0
for /d %%D in ("%LOCALAPPDATA%\ms-playwright\firefox-*") do (
    robocopy "%%D" "%DEST%\ms-playwright\%%~nxD" /e /njh /njs /ndl /nc /ns /nfl >nul
    if !errorlevel! geq 8 (
        echo [ERROR] Fallo la copia de %%~nxD
        pause
        exit /b 1
    )
    set COPIADO=1
)
if "!COPIADO!"=="0" (
    echo [ERROR] No se encontro firefox-* en %LOCALAPPDATA%\ms-playwright
    pause
    exit /b 1
)

echo [6/6] Credenciales, launcher y LEEME...
copy /y credentials.json "%DEST%\credentials.json" >nul

> "%DEST%\Ejecutar BOT.bat" (
    echo @echo off
    echo cd /d "%%~dp0"
    echo echo Recorda: la VPN tiene que estar conectada.
    echo BOTCorrecciones.exe
    echo pause
)

> "%DEST%\LEEME.txt" (
    echo BOT de Correcciones - paquete autocontenido
    echo =============================================
    echo.
    echo Requisitos en esta PC:
    echo   - VPN conectada al portal de Gasnor
    echo   - Nada mas: no hace falta Python ni instalar nada
    echo.
    echo Uso:
    echo   1. Cargar los casos pendientes en el Google Sheet
    echo   2. Doble click en "Ejecutar BOT.bat"
    echo   3. El avance se ve fila por fila en el Sheet
    echo      El detalle queda en logs\bot.log
    echo.
    echo NO borrar, mover ni renombrar:
    echo   credentials.json  ms-playwright\  _internal\
)

echo.
echo ==================================================================
echo  LISTO: "%DEST%" quedo armada.
echo  Comprimila en un zip y pasasela a tus companeros.
echo ==================================================================
pause
