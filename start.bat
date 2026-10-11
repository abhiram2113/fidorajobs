@echo off
cd /d "%~dp0"
if not exist .env (copy .env.example .env & notepad .env & pause & exit /b)
py app.py
pause
