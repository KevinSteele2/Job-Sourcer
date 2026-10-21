@echo off
rem Double-click to start Job Sourcer. It opens in your browser.
rem Close this window to stop the app.
title Job Sourcer
cd /d "%~dp0"
python app.py
rem Keep the window open if the app crashed, so the error stays readable.
pause
