@echo off
rem Irodori on Windows: double-click. Needs Python 3.11+ from python.org.
where py >nul 2>nul && (py -3 "%~dp0irodori.py" %*) || (python "%~dp0irodori.py" %*)
if errorlevel 1 pause
