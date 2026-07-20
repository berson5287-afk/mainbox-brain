@echo off
rem ---------------------------------------------------------------------------
rem MaINbox Brain auto-sync server (v0.46)
rem Starts the Brain HTTP server with a 15-minute Outlook refresh timer.
rem   - keeps mainbox.db continuously synced from Outlook (COM, local only)
rem   - serves the API the phone client will use later (bind 0.0.0.0 = tailnet)
rem   - logs to %LOCALAPPDATA%\MaINbox\brain_server.log
rem
rem Edit BRAIN_DIR if you move the NEW MBB folder.
rem To auto-start at logon, run ONCE in a normal PowerShell window:
rem   schtasks /Create /TN "MaINbox Brain AutoSync" /SC ONLOGON ^
rem     /TR "'C:\Users\SteveBerson\OneDrive - American Power ESC\Desktop\NEW MBB\mainbox_brain\start_brain_autosync.bat'" /F
rem ---------------------------------------------------------------------------

set "BRAIN_DIR=C:\Users\SteveBerson\OneDrive - American Power ESC\Desktop\NEW MBB\mainbox_brain"
set "LOGDIR=%LOCALAPPDATA%\MaINbox"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

cd /d "%BRAIN_DIR%"

rem pyw = no console window; output still lands in the log via redirection.
start "" /b pyw -m mainbox_brain.server --host 0.0.0.0 --auto-refresh 15 >> "%LOGDIR%\brain_server.log" 2>&1
