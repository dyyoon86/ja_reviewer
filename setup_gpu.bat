@echo off
chcp 65001 >nul
cd /d %~dp0
title GPU 가속 설치 (faster-whisper)

if not exist .venv (
  echo  ! 먼저 run.bat 을 한 번 실행해 .venv 를 만든 뒤 이걸 실행하세요.
  pause & exit /b 1
)
call .venv\Scripts\activate.bat
echo GTX 3060 가속용 CUDA 런타임(cuBLAS/cuDNN) 설치...
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
echo.
echo 완료! 이제 run.bat 로 실행하면 large-v3 가 GPU(float16)로 빠르게 돕니다.
pause
