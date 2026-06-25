# ja_reviewer — 딸딸기튜브 신작 해설영상 자동화

일본 신작 AV를 받아 **스토리 구간만 잘라내고, 한국어 대사 자막 + 해설 내레이션**을 자동 생성하는 툴킷.
(3분휴지 스타일 리뷰 + 자막 인터리브 영상용)

## 구성

| 파일 | 실행 위치 | 역할 |
|------|-----------|------|
| `ddalddalgi_studio.py` | **윈도우 PC** (영상 있는 곳) | GUI. 영상→Whisper(일SRT)→메타조회→LLM 스토리분석→ffmpeg 컷 + 대사/내레이션 SRT |
| `meta_api.py` | **우분투** (DB 있는 곳) | LAN 메타 API. `GET /work/<품번>` → 작품 정보 JSON. `python meta_api.py --port 8770` |
| `gen_narration.py` | 우분투 | (CLI) 품번→DB 메타 조회 + 내레이션 생성. studio가 참조하는 메타 로직 |

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

## 윈도우 실행 준비

```
pip install faster-whisper
# ffmpeg 설치 후 PATH 등록
# LLM: codex 또는 claude CLI 로그인
python ddalddalgi_studio.py
```
- 메타API 주소 기본값 `http://172.30.1.40:8770` (같은 LAN의 우분투)

## 메모

- DB(`jav_2026.db`)는 우분투(jarank 프로젝트)에만 있음 → 윈도우는 meta_api로만 조회(파일 복사 X).
- meta_api는 우분투에서 `@reboot` cron으로 자동 기동.
- codex 토큰 revoke 시 윈도우에서 `claude` CLI 선택 권장.
