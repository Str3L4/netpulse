@echo off
title NETPULSE Dashboard
cd /d "%~dp0"
echo Starting NETPULSE Telemetry Server...
start "" http://127.0.0.1:8888
python server.py
pause
