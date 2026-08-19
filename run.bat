@echo off
cd /d "%~dp0"
python glow_avatar_gui.py
if errorlevel 1 pause
