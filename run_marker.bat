@echo off
REM Launch the marking tool. Optionally drag a video file onto this .bat to open it.
"%~dp0.venv\Scripts\pythonw.exe" "%~dp0marker.py" %*
