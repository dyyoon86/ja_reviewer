# -*- coding: utf-8 -*-
"""사람 호출 — 배치가 멈추고 확인을 기다릴 때. 전부 best-effort(실패해도 파이프라인은 안 죽는다).
  ① 비프 3회(콘솔 PC 앞에 있을 때)
  ② Windows 토스트
  ③ 카카오톡 '나에게 보내기' — news-kakao-bot 의 .env/tokens.json 재사용(config notify_kakao_dir)
"""
import json
import os
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KAKAO_DIR_DEFAULT = r"E:\vscode\workspace\news-kakao-bot"


def _cfg():
    try:
        return json.loads((ROOT / "studio_config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def beep():
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1000, 250)
    except Exception:
        pass


def toast(title, body):
    """Windows 토스트(알람 소리 포함). XmlDocument.LoadXml 방식 — GetTemplateContent+AppendChild 는
    PS 5.1 에서 InvokeMethodOnNull 이 났다."""
    if os.name != "nt":
        return
    ps = r"""
$ErrorActionPreference = "Stop"
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$esc = [System.Security.SecurityElement]::Escape
$xml = "<toast><visual><binding template='ToastText02'><text id='1'>" + $esc.Invoke($env:JA_TOAST_TITLE) + "</text><text id='2'>" + $esc.Invoke($env:JA_TOAST_BODY) + "</text></binding></visual><audio src='ms-winsoundevent:Notification.Looping.Alarm2' loop='false'/></toast>"
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$n = New-Object Windows.UI.Notifications.ToastNotification $doc
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell1.0\powershell.exe').Show($n)
"""
    try:
        env = dict(os.environ, JA_TOAST_TITLE=title[:80], JA_TOAST_BODY=body[:200])
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-Command", ps], env=env, capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def kakao(text):
    """news-kakao-bot 과 같은 방식: refresh_token 으로 access 갱신 → talk/memo/default/send."""
    c = _cfg()
    if c.get("notify_kakao", True) is False:
        return False
    d = Path(c.get("notify_kakao_dir") or KAKAO_DIR_DEFAULT)
    env, tok = {}, {}
    try:
        for line in (d / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
        tok = json.loads((d / "tokens.json").read_text(encoding="utf-8"))
        params = {"grant_type": "refresh_token", "client_id": env["KAKAO_REST_KEY"],
                  "refresh_token": tok["refresh_token"]}
        if env.get("KAKAO_CLIENT_SECRET"):
            params["client_secret"] = env["KAKAO_CLIENT_SECRET"]
        req = urllib.request.Request("https://kauth.kakao.com/oauth/token",
                                     data=urllib.parse.urlencode(params).encode())
        resp = json.load(urllib.request.urlopen(req, timeout=15))
        tok["access_token"] = resp["access_token"]
        if resp.get("refresh_token"):
            tok["refresh_token"] = resp["refresh_token"]
            (d / "tokens.json").write_text(json.dumps(tok, ensure_ascii=False, indent=2), encoding="utf-8")
        template = {"object_type": "text", "text": text[:1000],
                    "link": {"web_url": "https://github.com/dyyoon86/ja_reviewer"}}
        req = urllib.request.Request(
            "https://kapi.kakao.com/v2/api/talk/memo/default/send",
            data=urllib.parse.urlencode({"template_object": json.dumps(template, ensure_ascii=False)}).encode(),
            headers={"Authorization": "Bearer " + tok["access_token"],
                     "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
        r = json.load(urllib.request.urlopen(req, timeout=15))
        return r.get("result_code") == 0
    except Exception:
        return False


def notify(title, body, log=print):
    """세 경로 전부 시도. 반환: 카카오 성공 여부."""
    beep()
    toast(title, body)
    ok = kakao(f"[{title}]\n{body}")
    log(f"  알림: 비프·토스트 발송, 카카오 {'OK' if ok else '실패/꺼짐'}")
    return ok
