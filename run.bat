@echo off
chcp 65001 >nul
cd /d %~dp0
title 리뷰 편집 스튜디오

if not exist .venv (
  echo [1/3] 가상환경(.venv) 생성...
  python -m venv .venv
  if errorlevel 1 (
    echo  ! python 이 없습니다. python.org 에서 64bit Python 설치 후 다시 실행하세요.
    pause & exit /b 1
  )
  call .venv\Scripts\activate.bat
  echo [2/3] 패키지 설치... (처음 한 번만, 몇 분 걸립니다)
  python -m pip install --upgrade pip
  pip install -r server\requirements.txt
) else (
  call .venv\Scripts\activate.bat
)

echo [3/3] 서버 시작 - 브라우저가 자동으로 열립니다. (종료: 이 창에서 Ctrl+C)
python -m server.app
pause
