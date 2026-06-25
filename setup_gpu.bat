@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title GPU setup (faster-whisper)

if not exist ".venv" (
  echo ERROR: run "run.bat" once first to create .venv, then run this.
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"
echo Installing CUDA runtime (cuBLAS/cuDNN) for GTX 3060 acceleration...
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
echo.
echo Done. Now run "run.bat" - large-v3 will use GPU (float16).
pause
endlocal
