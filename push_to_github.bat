@echo off
title NETPULSE - Push to GitHub
cd /d "%~dp0"
echo ========================================================
echo   NETPULSE: Отправка в GitHub (Str3L4/netpulse)
echo ========================================================
echo.
git push -u origin main
echo.
pause
