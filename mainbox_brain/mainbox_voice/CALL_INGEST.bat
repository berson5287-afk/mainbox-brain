@echo off
:: ============================================================
:: CALL_INGEST.bat — watch the synced call-recordings folder,
:: transcribe locally, forward into MaINbox Voice.
:: Put this next to call_ingest.py in the mainbox_voice folder.
:: One-time: pip install faster-whisper
:: ============================================================
cd /d "%~dp0"
py call_ingest.py --dir "C:\CallRecordings" --server http://127.0.0.1:8770
pause
