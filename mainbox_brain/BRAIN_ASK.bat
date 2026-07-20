@echo off
:: ============================================================
:: BRAIN_ASK.bat
:: Opens the MaINbox Brain interactive prompt with LLM answering
:: enabled (gemma3:12b via Ollama on tillium-bridge).
:: Run from the mainbox_brain repo root.
:: ============================================================

cd /d "%~dp0"

echo.
echo MaINbox Brain — interactive ask prompt (LLM enabled)
echo   LLM  : gemma3:12b @ http://tillium-bridge:11434
echo   DB   : mainbox.db
echo Type your question at the  request^>  prompt.
echo Type  quit  to exit.
echo.

py -m mainbox_brain.ask --db mainbox.db --llm

echo.
echo Session ended.
pause
