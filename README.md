# ja_reviewer — 딸딸기튜브 신작 해설영상 자동화

일본 신작 AV mp4 하나를 받아 **노출 장면을 3중 필터로 제거하고, 스토리 구간만 컷 + 한국어 대사 자막 + 해설 내레이션(TTS) + 인포배너까지 번인한 완성본**을 뽑는 파이프라인.
목표는 "영상 던지면 1분 완성본" — 풀오토 기준 171분 원본 → 완성본까지 실측 **11분**.

## 전체 흐름

```
원본 mp4
 ⓪ 3중 노출필터(클린) : 2️⃣STT(대사 버블) → 3️⃣CLIP(장면 의미) → 1️⃣NN(화면 노출) 순차 스캔→컷
 ① 전사(2-pass)      : small 스캔(136배속) → AI 선정 후 keep만 large-v3 정밀 재전사
 ② AI 처리           : LLM(codex/claude CLI)이 스토리 keep 선정 + 내레이션 생성 (map-reduce)
 ③ 자막              : keep 재타이밍, 대사(여/남 색분리) + 내레이션(대사 바로 위)
 ④ 인포배너          : gen_infocard — 품번→메타 → 프레임/인포카드/워터마크 PNG
 ⑤ TTS               : voicebox(로컬 17493) 내레이션 합성, seed 고정, 더킹 믹스
 ⑥ 굽기              : 하드섭 + 배너 + 효과음 1패스 인코딩 → self-eval 자체검사
 → 전수 노출검사 통과 시 {out_dir}/_완성/, 검출 시 _검수필요/ 격리
```

## 구성

| 경로 | 실행 위치 | 역할 |
|------|-----------|------|
| `server/app.py` + `web/` | 윈도우 PC | FastAPI + 웹 GUI(자동/수동 모드), 잡 큐 + SSE 로그 |
| `server/pipeline.py` | 윈도우 PC | 파사드 — 실구현은 `server/core/` (기존 import 호환용) |
| `server/core/` | 윈도우 PC | 실제 로직 17모듈 (아래 참조) |
| `server/watcher.py` | 윈도우 PC | 감시폴더 → 품번 추정 → 풀오토 큐 자동 투입 |
| `meta_api.py` | 우분투(또는 로컬) | 품번→`jav_2026.db` 메타 JSON. `python meta_api.py --port 8770` |
| `gen_infocard.py` | 윈도우 PC | 배너 4레이어 PNG(playwright 렌더) + 미리보기/데모 |
| `tools/` | 윈도우 PC | 서버 없이 쓰는 배치 CLI (설정은 `studio_config.json` 공유) |

### server/core/ 모듈

| 모듈 | 역할 |
|------|------|
| `transcribe.py` | 2-pass 전사 — `transcribe_scan`(small·배치) / `transcribe_ranges`(keep만 large-v3) |
| `llm.py` / `prompts.py` | codex/claude CLI 호출(stdin), 딸감별사 톤 + 인간톤 v2(`_human_tone`) + 안전룰 |
| `cutter.py` | smart-cut(경계 GOP만 재인코딩) → copy → 재인코딩 3단 폴백, 컷 경계 30ms 오디오 페이드 |
| `moan.py` | 2️⃣ STT 필터 — 대사 버블 방식(내용 대사 ±pad 보호, 무대사 구간 삭제) |
| `intimacy.py` | 3️⃣ CLIP 필터 — ONNX 양자화, 스킨십 vs 일상 margin + 지속 14s |
| `nsfw.py` | 1️⃣ NudeNet 필터 + 완성본 전수검사 (모자이크 장면은 사각 → 눈검사 필요) |
| `subs.py` | ASS 하드섭, 배너/짤 오버레이, 무도식 강조·정보 연출(기본 off) |
| `tts.py` | voicebox 폴링(600s), `build_narration_wav`, 더킹 믹스 |
| `bgm.py` | demucs BGM 제거(시스템 파이썬 외부 호출 — venv엔 torch 없음) |
| `selfeval.py` | 번인 후 자체검사 — 팝/정지/무음/자막 커버리지 → `{품번}_검사.json` |
| `regen.py` | 내레이션 재생성 / 구간 재선정 (GUI ③ 버튼 + tools 래퍼) |
| `assets.py` / `sfx.py` | 상황별 짤 오버레이(`_assets/`), ffmpeg lavfi 합성 효과음 |

### tools/ — 배치 CLI (서버 없이, ja12 모음집 11편 검증)

`batch_clean`(3중 필터) → `batch_review`(전사+AI) → `batch_produce`(TTS+번인, voicebox 자가복구) 순.
보조: `batch_rework`(재컷+BGM제거+무음제거 재작업), `batch_regen_nar`/`regen_narration`(내레이션 재생성, 모음집 서수 인트로), `dump_keep_transcript`(keep 정밀 전사 → 대사 전량 자막화), `trim_final_flags`(최종본 노출 재컷), `batch_final_check`(전수검사), `burn_only`, `replan`, `run_single`, `transcribe_hq`.

## 세 가지 사용법

- **풀오토**: GUI '🔮 폴더 감시' 토글 → 감시폴더에 mp4 투하 → 자동으로 ⓪~⑥ 완주. 대사 있는 작품(다큐형/인터뷰형) 전용 — 신음 위주 본편형은 keep 부족으로 자동 중단(`min_keep_ratio` 가드).
- **수동(GUI)**: 1️⃣NN/2️⃣STT/3️⃣CLIP 개별 스캔·컷(⚡ 순차 자동 클린 = 2→3→1 최적 순서), 단계별 실행, 프롬프트 복붙 우회 지원.
- **배치(tools/)**: 모음집 등 다건 일괄. 감시폴더와 충돌하는 경우 서버 없이 이걸로.

## 실행

```bat
run.bat                     :: venv 자동 생성/활성화 → http://127.0.0.1:8000
setup_gpu.bat               :: (최초 1회) cuBLAS/cuDNN — 없으면 faster-whisper GPU 실패
```

전제: ffmpeg PATH, voicebox(로컬 17493), meta_api(우분투 8770 또는 로컬), codex/claude CLI 로그인.
반드시 venv로 기동할 것 — 시스템 파이썬이면 cublas DLL을 못 찾는다.

## 함정 모음 (실전에서 얻은 것)

- **voicebox 타임아웃** = 십중팔구 voicebox 프로세스 사망. 재기동 → 17493 확인 → 큐 resume(완료 단계 자동 스킵).
- **LLM 프롬프트는 stdin으로** — argv로 넘기면 잘려서 "자막을 보내달라"는 헛응답(거부 아님).
- **BatchedInferencePipeline은 세그먼트를 뭉갬**(42→3세그) — 정밀 전사엔 사용 금지, 기본 off.
- **NudeNet 사각**: 모자이크 행위 장면(검출 0), 어두운 조명+옷 입은 애무씬 → 3️⃣CLIP과 최종 눈검사(몽타주)로 보완.
- **ffmpeg fps 샘플링 그리드**: 0.25s와 0.5s 그리드는 서로 다른 프레임을 봄 — 노출 재검은 두 그리드 합집합으로.
- **배너 PNG는 1920×1080 캔버스** — 굽기에서 영상 해상도로 스케일(`_prep_banner_layers(vid_wh=)`), 프레임 테두리는 22px 핑크 단일 계열(노랑 섞으면 끊겨 보임).
- **재전사가 항상 낫지 않다**: 속삭임은 정밀 전사도 놓침 — 대사 개수가 원본보다 줄면 원본 유지.

## 문서

- `docs/3중_노출필터.md` — 필터 3종 원리·실측·최적 순차 순서
- `docs/AI파이프라인_비용구조.md` — LLM 호출 3종과 선컷 절감
- `docs/videouse_반영.md` — 컷 경계 페이드/스냅/self-eval 실측 근거
- `벤치마킹/` — 골채널 4곳 대본 분석(내레이션 인간톤 v2 기준서)

## 이력 요약

- **2026-07-06** 윈도우 WIP + 우분투 원격 커밋 병합(딸감별사 톤 채택)
- **2026-07-11** core 분할 + smart-cut + 풀오토 v1(감시폴더)
- **2026-07-12** 2-pass 전사(136배속 스캔) + NudeNet 가드 + 자동/수동 UI 분리 + ⓪클린 스테이지
- **2026-07-13** STT 대사버블 필터 + CLIP 의미 스캔(3중 필터 완성) + 무도식 연출 + BGM 제거 + 짤 에셋
- **2026-07-16~18** ja12 모음집 11편 배치 완주(720p→1080p 리프레임 올인원 재번인 `_reburn_1080.py`) + 내레이션 인간톤 v2
- **2026-07-19** 배너 프레임 개선 — 핑크 단일 계열 + 22px (노랑 그라데이션이 테두리 끊김으로 보이던 문제)
