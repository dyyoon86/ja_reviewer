# ja_reviewer 아키텍처 & 로드맵 — 웹 프론트 전환

## 배경 / 결정

Tkinter + VLC(또는 OpenCV) 조합은 **영상 미리보기·마킹**에서 계속 막혔다
(VLC 임베드 타이밍, 비트수 불일치, OpenCV는 소리 없음·수동 프레임 렌더). 마킹 도구의 핵심은 영상 재생인데,
**HTML5 `<video>`** 는 소리·프레임단위 시킹·인/아웃 마킹을 공짜로 준다.

→ **GUI를 웹 프론트로 전환한다.** 단, 한 번에 Tauri까지 가지 않고 2단계로:

- **Phase 1 (지금): FastAPI + 로컬 웹 UI** — 브라우저에서 연다. Rust·사이드카 패키징 불필요.
- **Phase 2 (나중): Tauri 래핑** — 같은 웹 프론트를 그대로 감싸 더블클릭 네이티브 앱 + 설치본.

웹 프론트(`index.html`+JS)는 두 단계가 **공유**하므로 Phase 1 작업이 Phase 2에서 버려지지 않는다.

## 재생 비교 (전환 근거)

- **OpenCV + Pillow (Tkinter)**: 프레임 직접 디코드→리사이즈→PhotoImage 교체 루프. 소리 없음, 시킹 수동,
  끊김/동기 이슈, 코드 많고 취약.
- **HTML5 `<video>` (웹/Tauri)**: 브라우저 내장 플레이어. 소리 O, 프레임단위 시킹, 부드러움,
  인/아웃 마킹 JS 몇 줄. 타임라인 UI 자연스러움. → 마킹 도구로는 압도적.

## 그대로 재사용하는 파이썬 파이프라인

기존 `ddalddalgi_studio.py` 의 순수 로직 함수들은 **UI와 무관**하므로 그대로 가져와 FastAPI로 감싼다:

- `transcribe(video, model)` — faster-whisper (GTX3060, large-v3, float16)
- `fetch_meta(api, code)` — LAN 메타 API(`meta_api.py`, 우분투 8770) 조회
- `prompt_auto / prompt_manual(meta, segs, target_sec)` — LLM 프롬프트(자동 선정 / 수동 압축)
- `call_llm(prompt, which)` — codex / claude CLI
- `keep_from_exclude(total, excludes)` — 삭제구간 → 남길구간(여집합)
- `cut_video(video, keep, out)` — ffmpeg trim+concat
- `retime(entries, keep, snap)` — 컷 타임라인으로 자막 재계산(내레이션 snap)
- `write_srt`, `ranges_from_text`, `video_duration`, `s2srt`

이 함수들은 검증 완료(여집합·재타이밍·2단컷·LLM·ffmpeg 합성영상 테스트 통과). 로직 재작성 거의 없음.

## Phase 1 설계 — FastAPI + 로컬 웹 UI

### 파일 구조 (예정)
```
ja_reviewer/
├ server/
│  ├ app.py            # FastAPI 엔트리(+ 정적 index.html 서빙, 브라우저 자동 오픈)
│  ├ pipeline.py       # 위 순수 함수들(ddalddalgi_studio.py에서 분리)
│  └ jobs.py           # 백그라운드 작업 + 진행상황 SSE
├ web/
│  ├ index.html        # 단일 페이지 (video + 타임라인 + 컨트롤)
│  ├ app.js            # 마킹/단축키/SSE/요청
│  └ style.css         # 편집기 톤(다크)
├ ddalddalgi_studio.py # (백업) Tkinter 버전 — 당분간 유지
├ meta_api.py          # 우분투 LAN 메타 API (변경 없음)
└ gen_narration.py     # 메타 로직 (변경 없음)
```

### 엔드포인트 (안)
- `GET  /`                      → index.html
- `POST /open`                  → 로컬 파일 경로 받기(또는 서버측 파일 다이얼로그). body: `{path}`
- `GET  /video/stream?path=`    → **HTTP Range 스트리밍** (→ `<video>` 시킹 지원, 큰 파일도 OK)
- `GET  /meta/{code}`           → 메타 조회 (LAN API 프록시)
- `POST /transcribe`            → `{path}` → 일본어 세그먼트 (job)
- `POST /analyze`               → 자동: `{path, code, target_sec, llm}` → keep/dialogue/narration JSON
- `POST /cut`                   → 수동: `{path, code, excludes[], target_sec, llm}` → 2단컷 + SRT
- `POST /tts`                   → 내레이션 SRT → voicebox MCP → 내레이션 WAV (Phase 1.5)
- `GET  /events/{job}`          → **SSE** 진행 로그/상태 스트리밍

### 프론트 (안)
- `<video>` + 커스텀 타임라인(현재시간·삭제구간 마커 표시)
- 단축키: **Space**(재생/정지), **`[`/`]` 또는 I/O**(구간 인/아웃), **←/→**(5초)
- 모드 토글: **직접 컷 편집** / **자동 분석**
- 삭제구간 리스트(추가/삭제), 리뷰 길이 드롭다운(1/2/5/10분, 기본 1분)
- 진행 로그 패널(SSE), 자동 모드는 결과 JSON 미리보기·수정 → 확정
- 결과: `_final.mp4` + `_대사.srt` + `_내레이션.srt` (+ `_내레이션.wav`)

### 실행 (안)
```
pip install fastapi uvicorn faster-whisper
python -m server.app      # → http://127.0.0.1:8000 자동 오픈
```
- 영상은 업로드가 아니라 **로컬 경로**로 다룬다(대용량 업로드 회피). 브라우저엔 Range 스트리밍.

## Phase 2 설계 — Tauri 래핑 (나중)

- 같은 `web/` 프론트를 Tauri 윈도우에 로드.
- FastAPI를 **사이드카 바이너리**로 번들(PyInstaller로 묶어 Tauri `externalBin`).
- 네이티브 파일 다이얼로그(`@tauri-apps/api/dialog`)로 `/open` 대체.
- 산출물: 윈도우 설치본(.msi/.exe) 더블클릭 실행.
- 추가비용: Rust 툴체인 + 사이드카 패키징. 프론트/백엔드 로직은 Phase 1 그대로 재사용.

## TTS (voicebox MCP) — 별도 진행

- 한국어 **내레이션만** TTS. 일본 배우 대사는 TTS 안 함.
- 음성이 자막 슬롯보다 길면 **A안(자연 길이 재생, 페이싱이 음성 주도)**.
- voicebox = 사용자 PC 로컬 TTS MCP. 붙이려면 인터페이스 필요:
  실행방식(HTTP/SSE 포트 or stdio command), tool 이름·파라미터(text/speaker/output), 한국어 화자.
- `/tts` 엔드포인트에서 내레이션 SRT 줄별 합성 → 타임코드 정렬 → `_내레이션.wav` 생성(엔진 플러그인식).

## 진행 순서

1. `pipeline.py` 분리(기존 함수 이동, UI 무관 로직만) — 동작 동일 검증
2. FastAPI 골격 + `/video/stream`(Range) + `/` 정적 서빙
3. `web/` 단일 페이지: video + 마킹 + 단축키
4. `/transcribe` `/analyze` `/cut` 연결(SSE 진행)
5. voicebox 정보 확보 후 `/tts`
6. (나중) Tauri 래핑 → 설치본

## 메모

- 기존 Tkinter 버전(`ddalddalgi_studio.py`)은 백업으로 유지(웹 버전 안정화까지).
- 메타 DB는 우분투에만 있고 윈도우는 `meta_api`로만 조회(파일 복사 X) — 변경 없음.
- LLM은 codex/claude CLI 선택. codex 토큰 revoke 시 claude 권장.
