# ja_reviewer — 딸딸기튜브 신작 해설영상 자동화

일본 신작 AV를 받아 **스토리 구간만 잘라내고, 한국어 대사 자막 + 해설 내레이션**을 자동 생성하는 툴킷.
(3분휴지 스타일 리뷰 + 자막 인터리브 영상용)

## 구성

| 파일 | 실행 위치 | 역할 |
|------|-----------|------|
| `ddalddalgi_studio.py` | **윈도우 PC** (영상 있는 곳) | GUI. 영상→Whisper(일SRT)→메타조회→LLM 스토리분석→ffmpeg 컷 + 대사/내레이션 SRT |
| `meta_api.py` | **우분투** (DB 있는 곳) | LAN 메타 API. `GET /work/<품번>` → 작품 정보 JSON. `python meta_api.py --port 8770` |
| `gen_narration.py` | 우분투 | (CLI) 품번→DB 메타 조회 + 내레이션 생성. studio가 참조하는 메타 로직 |

## 파이프라인

```
[영상 + 품번]
  ① Whisper 일본어 SRT 추출 (faster-whisper, 윈도우 로컬)
  ② LAN 메타 API로 품번 정보 조회 (우분투 DB: 배우·시놉·스리사이즈·레이블·장르·인기)
  ③ LLM(codex/claude CLI)으로 스토리 분석 → 스토리 구간 선정 + 한글 대사 + 내레이션
  [미리보기 JSON 수정] → 확정
  ④ ffmpeg로 스토리 구간만 컷&이어붙이기 + SRT 새 타임라인 재계산
→ <품번>_cut.mp4 + <품번>_대사.srt + <품번>_내레이션.srt
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
