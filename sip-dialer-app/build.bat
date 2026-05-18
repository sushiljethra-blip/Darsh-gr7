@echo off
REM ─────────────────────────────────────────────────────────────────────────
REM  BPO SIP Auto-Dialer — Windows EXE Builder
REM  Run this script once to build the standalone EXE.
REM  Python 3.9+ required.
REM ─────────────────────────────────────────────────────────────────────────

echo [1/4] Checking Python...
python --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo ERROR: Python not found. Download from https://www.python.org/downloads/
    pause & exit /b 1
)

echo [2/4] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install pyvoip sounddevice numpy pyinstaller

echo [3/4] Building EXE with PyInstaller...
python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name "BPO-SIP-Dialer" ^
    --collect-all pyvoip ^
    --collect-all sounddevice ^
    --copy-metadata pyvoip ^
    --hidden-import pyvoip ^
    --hidden-import pyvoip.voip ^
    --hidden-import pyvoip.call ^
    --hidden-import pyvoip.rtp ^
    --hidden-import pyvoip.sip ^
    --hidden-import audioop ^
    --hidden-import sounddevice ^
    --hidden-import numpy ^
    --hidden-import _cffi_backend ^
    main.py

echo [4/4] Done!
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
