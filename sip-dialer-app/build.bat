@echo off
REM ─────────────────────────────────────────────────────────────────────────
REM  BPO SIP Auto-Dialer — Windows EXE Builder
REM  Run this script once to build the standalone EXE.
REM  Python 3.9-3.12 required.
REM ─────────────────────────────────────────────────────────────────────────

echo [1/5] Checking Python...
python --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo ERROR: Python not found. Download from https://www.python.org/downloads/
    pause & exit /b 1
)

echo [2/5] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install pyvoip sounddevice numpy pyinstaller
IF ERRORLEVEL 1 ( echo Dependency install failed. & pause & exit /b 1 )

echo [3/5] Copying pyvoip source into project folder...
python -c "import pyvoip,shutil,os; dst='pyvoip'; shutil.rmtree(dst,True); shutil.copytree(os.path.dirname(pyvoip.__file__),dst); print('OK')"
IF ERRORLEVEL 1 ( echo Failed to copy pyvoip. & pause & exit /b 1 )

echo [4/5] Building EXE with PyInstaller...
python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name "BPO-SIP-Dialer" ^
    --add-data "pyvoip;pyvoip" ^
    --hidden-import pyvoip ^
    --hidden-import pyvoip.voip ^
    --hidden-import pyvoip.call ^
    --hidden-import pyvoip.rtp ^
    --hidden-import pyvoip.sip ^
    --hidden-import audioop ^
    --hidden-import sounddevice ^
    --collect-all sounddevice ^
    --hidden-import numpy ^
    --hidden-import _cffi_backend ^
    --copy-metadata pyvoip ^
    main.py

echo [5/5] Done!
IF EXIST dist\BPO-SIP-Dialer.exe (
    echo.
    echo =====================================================
    echo  SUCCESS: dist\BPO-SIP-Dialer.exe is ready
    echo  Double-click it to run — no installation needed.
    echo =====================================================
    explorer dist
) ELSE (
    echo Build failed. Check errors above.
)
pause
