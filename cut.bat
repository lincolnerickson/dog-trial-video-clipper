@echo off
REM Convenience wrapper for the batch cutter. Example:
REM   cut.bat --video trial.mp4 --csv clips.csv --out clips
"%~dp0.venv\Scripts\python.exe" "%~dp0cutter.py" %*
