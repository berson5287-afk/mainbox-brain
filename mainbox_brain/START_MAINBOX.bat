@echo off
:: ============================================================
:: START_MAINBOX.bat
:: Starts the MaINbox Brain API server, then the Voice server.
:: Run this from the mainbox_brain repo root, e.g.:
::   C:\Users\SteveBerson\OneDrive - American Power ESC\Desktop\NEW MBB\mainbox_brain\
:: ============================================================

cd /d "%~dp0"

echo.
echo Starting MaINbox Brain server (port 8585)...
start "MaINbox Brain" cmd /k "py -m mainbox_brain.server --db mainbox.db --port 8585"

:: Give the Brain a moment to initialize before the voice server connects
timeout /t 3 /nobreak >nul

echo Starting MaINbox Voice server (port 8770)...
start "MaINbox Voice" cmd /k "python mainbox_voice\brain_voice_server.py"

echo.
echo Both servers are starting in separate windows.
echo   Brain : http://127.0.0.1:8585/health
echo   Voice : http://100.89.98.118:8770  (check the Voice window for your token)
echo.
echo Close this window or press any key to exit.
pause >nul
