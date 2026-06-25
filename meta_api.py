#!/usr/bin/env python3
"""
LAN 메타 API — 윈도우 GUI(딸딸기 스튜디오)가 품번으로 작품 정보를 가져가는 서버.

같은 네트워크의 윈도우 PC에서:
    GET http://<우분투IP>:8765/work/<품번>   → 작품 메타 JSON
    GET http://<우분투IP>:8765/health         → {"ok":true}

DB(jav_2026.db)는 이 우분투에만 있고, 윈도우는 이 API로만 조회(파일 복사 X).
바인드 0.0.0.0 = 같은 네트워크에서 접근 가능. 사용: python meta_api.py --port 8765
"""
import json
import argparse
import sqlite3
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

import gen_narration as g

DB = Path(__file__).parent / "jav_2026.db"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        b = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path in ("/health", "/"):
            return self._json({"ok": True, "service": "ddalddalgi meta-api"})
        if path.startswith("/work/"):
            code = path[len("/work/"):].strip("/")
            if not code:
                return self._json({"error": "code required"}, 400)
            try:
                con = sqlite3.connect(str(DB))
                m = g.fetch_meta(con, code)
                con.close()
            except Exception as e:
                return self._json({"error": str(e)}, 500)
            if not m or (len(m) == 1 and "code" in m):
                return self._json({"error": f"{code} not found"}, 404)
            m["meas"] = g.meas_str(m)
            return self._json(m)
        self.send_error(404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0")  # LAN 접근
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"meta-api on http://{args.host}:{args.port}  (DB={DB})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
