@echo off
REM Same as run_marker.bat but keeps a console window open so any error is visible.
"%~dp0.venv\Scripts\python.exe" "%~dp0marker.py" %*
echo.
pause
