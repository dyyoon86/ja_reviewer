#!/usr/bin/env bash
# Ubuntu/맥용 실행 — venv 자동 생성 후 서버 기동
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "[1/3] 가상환경(.venv) 생성..."
  python3 -m venv .venv
  echo "[2/3] 패키지 설치..."
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r server/requirements.txt
fi
echo "[3/3] 서버 시작 → http://127.0.0.1:8000"
exec .venv/bin/python -m server.app
