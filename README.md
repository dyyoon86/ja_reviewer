# ja_reviewer — 딸딸기튜브 신작 해설영상 자동화

> **GUI = 웹 프론트**(FastAPI + 로컬 웹 UI). Phase 2에서 Tauri 래핑 예정. 상세: [docs/architecture.md](docs/architecture.md).

일본 신작 AV를 받아 **스토리 구간만 잘라내고, 한국어 대사 자막 + 해설 내레이션**을 자동 생성하는 툴킷.
(3분휴지 스타일 리뷰 + 자막 인터리브 영상용)

## 구성

| 파일 | 실행 위치 | 역할 |
|------|-----------|------|
| `server/app.py` + `web/` | **윈도우 PC** (영상 있는 곳) | 웹 GUI/백엔드. 영상→Whisper→메타조회→LLM→ffmpeg 컷 + 대사/내레이션 SRT |
| `server/pipeline.py` | 윈도우 PC | UI 무관 파이프라인 로직(전사·메타·LLM 프롬프트·컷·재타이밍) |
| `meta_api.py` | **우분투** (DB 있는 곳) | LAN 메타 API. `GET /work/<품번>` → 작품 정보 JSON. `python meta_api.py --port 8770` |
| `gen_narration.py` | 우분투 | 품번→DB 메타 조회 로직(`meta_api`가 사용) |
| `tools/` | 윈도우 PC | 단독 실행 CLI 툴 모음(아래 참조). 설정은 `studio_config.json` 공유 |

### tools/ — 단독 실행 CLI

GUI 없이 출력 폴더를 직접 손볼 때 쓰는 툴. 모두 `python tools/<이름>.py`로 실행하며
`meta_api`·`out_dir`·`llm` 등은 `studio_config.json`에서 읽는다(`tools/_common.py`).

| 툴 | 역할 |
|----|------|
| `run_single.py <품번>` | trim.mp4가 있는 폴더 전체 파이프라인(전사→메타→LLM→SRT) |
| `replan.py <폴더>` | plan.json의 keep 구간을 LLM으로 재선정 + final.mp4 재컷 |
| `regen_narration.py <폴더>` | 내레이션만 6슬롯 규칙으로 재생성(SRT/JSON 갱신) |
| `transcribe_hq.py --video <mp4>` | 고품질 전사 + Claude 검증 리포트(품질 판단용) |
| `batch_fix_tts.py` | (원오프) 2026-07-10 배치 keep 수정 + TTS 일괄 — 패턴 참고용 |

## 두 가지 모드

GUI 탭으로 선택.

### ● 수동 (정사장면 직접 제외) — 토큰 절약·추천
```
[영상] → 내장 플레이어로 정사장면 구간 체크(또는 텍스트 12:30-18:00 입력)
  ① ffmpeg로 그 구간 빼고 스토리만 이어붙임 (먼저 컷)
  ② 잘린(짧은) 영상만 Whisper 전사
  ③ LAN 메타 API 조회
  ④ LLM(codex/claude)은 번역 + 내레이션만 (구간선정 X → 프롬프트 작음 = 토큰↓)
→ <품번>_cut.mp4 + 대사.srt + 내레이션.srt   (컷영상 기준 전사라 재타이밍 불필요)
```
마킹은 **내장 VLC 플레이어 시각 체크** + **텍스트 구간 입력** 둘 다 지원(VLC 없으면 텍스트만).

### ● 자동 (LLM이 알아서)
```
[영상] → ① Whisper 풀 전사 → ② 메타 → ③ LLM 스토리 구간 선정+번역+내레이션
  → [미리보기 JSON 수정] → 확정 → ④ ffmpeg 컷 + SRT 재타이밍(내레이션은 컷 경계로 스냅)
→ <품번>_cut.mp4 + 대사.srt + 내레이션.srt
```

## 규칙 (skills/ 참조)

- **풀 SRT에서 선정적(신음·탄성·반복) 제외, 스토리 대사만** 추출 → 전체 전개 커버.
- 번역: `skills/jav-subtitle-translate` (Eddy_Wind 프롬프트 기반, 신음은 맥락 감정표현).
- 내레이션: `skills/jav-narration` (3분휴지 4단: 전환→시놉→평가→총평). 평가는 영상 못보니 창작.
- 내레이션 음성(TTS)은 사용자가 별도 처리. 본 툴은 자막까지.

## 웹 버전 (Phase 1, 권장) 실행 — venv 자동

**윈도우**: `run.bat` 더블클릭. (첫 실행 때 `.venv` 만들고 패키지 설치 → 이후엔 바로 서버 시작)
- GPU(GTX 3060) 가속 원하면 첫 실행 후 한 번 `setup_gpu.bat` 실행 (cuBLAS/cuDNN 설치).

**우분투/맥**: `./run.sh`

전제: ffmpeg PATH 등록, LLM은 codex 또는 claude CLI 로그인.
수동 실행하려면:
```
python -m venv .venv && .venv\Scripts\activate     # (win)  /  source .venv/bin/activate (linux)
pip install -r server/requirements.txt             # fastapi uvicorn faster-whisper
python -m server.app                               # → http://127.0.0.1:8000 자동 오픈
```
- `영상 선택`(네이티브 다이얼로그) 또는 경로 붙여넣기 → `<video>`로 재생(소리 O)
- 단축키: `Space` 재생/정지, `[`/`I` 구간시작, `]`/`O` 구간끝, `←`/`→` 5초
- **직접 컷 편집**: 구간 마킹 → 편집 실행(선택 구간 삭제→전사→AI 압축→자막/내레이션)
- **자동 (AI 분석)**: 자동 분석 → 결과 확인/수정 → 확정 렌더
- 진행상황은 로그창에 실시간(SSE). 출력: `<품번>_final.mp4` + `_대사.srt` + `_내레이션.srt`
- mkv/avi는 브라우저 미리보기 제약 → mp4 권장. 메타는 LAN `meta_api`로 조회.

## 메모

- DB(`jav_2026.db`)는 우분투(jarank 프로젝트)에만 있음 → 윈도우는 meta_api로만 조회(파일 복사 X).
- meta_api는 우분투에서 `@reboot` cron으로 자동 기동.
- codex 토큰 revoke 시 윈도우에서 `claude` CLI 선택 권장.
