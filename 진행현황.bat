@echo off
chcp 65001 >nul
cd /d "%~dp0"
.venv\Scripts\python.exe tools\_progress.py --watch --src "C:\Users\yoon\Desktop\04. 영상작업\2026-04-23_JA_Review\26" %*
