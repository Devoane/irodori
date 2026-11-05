@echo off
rem Kaicho on Windows: double-click. Needs Python 3.11+ from python.org.
where py >nul 2>nul && (py -3 "%~dp0kaicho.py" %*) || (python "%~dp0kaicho.py" %*)
if errorlevel 1 pause
