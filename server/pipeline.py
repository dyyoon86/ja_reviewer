#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ja_reviewer 파이프라인 — UI 무관 순수 로직 (파사드).

실제 구현은 server/core/ 서브모듈로 분할됐다:
  common     시간/SRT 유틸, keep/retime, ffprobe 조회
  transcribe ① Whisper 전사(환청 억제·CUDA DLL) + Claude 검증
  llm        ②③ 메타 조회 + LLM CLI 호출(stdin 필수)
  prompts    프롬프트 빌더(딸감별사 톤·예산·시간규칙)
  cutter     ④ ffmpeg 컷(NVENC/무손실 카피)
  tts        ⑤ voicebox TTS·내레이션 합성·더킹·먹싱
  subs       ⑥ ASS 하드섭 + 인포배너

이 파일은 기존 import 경로(`from server import pipeline as P`) 호환을 위해
모든 공개/내부 심볼을 재수출한다. 새 코드는 core 모듈을 직접 import해도 된다.

log 콜백은 진행상황 출력용(기본 print). 서버에선 SSE 큐로 연결.
"""
# 기존 pipeline 네임스페이스에 있던 stdlib도 유지(P.Path 등으로 쓰던 코드 호환)
import os
import re
import json
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from server.core.common import (
    FFPROBE_TIMEOUT, FFMPEG_TIMEOUT, _part_path, _finalize,
    TARGETS, sec2label, s2srt, hhmmss, parse_time, ranges_from_text,
    _BREAK_SUFFIX, _BREAK_PUNCT, _good_break, _wrap_chunks, split_entries,
    sanitize_segments, clamp_durations, write_srt, video_duration,
    parse_keep, clamp_stars, parse_lines, keep_from_exclude,
    _fit_tail, retime, srt_parse, video_wh,
)
from server.core.transcribe import (
    HALLUCINATION_JA, _looks_hallucinated, _ensure_cuda_dll_path,
    transcribe, build_initial_prompt, verify_transcript, write_verify_report,
)
from server.core.llm import fetch_meta, _cli_path, call_llm, llm_ping
from server.core.prompts import (
    _meta_block, _translate, NARRATION_STYLES, _style_cinema, _style,
    _must_have, _style_3min, _hint_block, _timeline_rule,
    _TTS_CHARS_PER_SEC, _BREATH, _SEC_PER_SENT, _CINEMA_SPEECH_RATIO,
    narration_budget, _roundup_block, prompt_auto, prompt_highlight, prompt_manual,
)
from server.core.cutter import has_nvenc, _vcodec_args, cut_video, _kf_after, cut_video_copy
from server.core.tts import (
    tts_profiles, tts_generate, audio_duration, MIN_GAP, MAX_TEMPO,
    build_narration_wav, merge_spans, _duck_expr, ORIG_AUDIO_MODES, mux_narration,
)
from server.core.subs import (
    _ass_color, _ass_time, _ALIGN, STYLE_DEFAULT, STYLE_TAGNAME, SPEAKER_TAGNAME,
    _style_line, _ass_anim, build_ass, BANNER_ANIM,
    _prep_banner_layers, _banner_filter, burn_subs,
)
