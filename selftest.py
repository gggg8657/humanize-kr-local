import os
#!/usr/bin/env python3
"""LLM 없이 결정적 파이프라인만 검증한다: shim → 후처리 → 4축 게이트 → diff.
Ollama 호출을 가짜 윤문 함수로 바꿔 끼운다.  python3 selftest.py"""
import json, shutil, sys
import app

SRC = ("AI 기술을 통해 효율을 높일 수 있다. 이에 있어서 중요한 점은 데이터에 의해 생성된 결과를 검증하는 것이다. "
       "2024년 기준 국내 도입률은 37%에 달했으며, 삼성전자와 네이버가 선도하고 있다. 결론적으로, 이는 시사하는 바가 크다. "
       "또한 이러한 접근은 매우 혁신적인 것으로 판단되어진다.")
GOOD = ("AI 기술로 효율을 높일 수 있다. 여기서 중요한 건 데이터가 만든 결과를 검증하는 것이다. "
        "2024년 기준 국내 도입률은 37%에 달했으며 삼성전자와 네이버가 선도하고 있다. "
        "또한 이러한 접근은 혁신적으로 판단된다.\n\n<!-- HUMANIZE-SUMMARY\ngrade: A\nedits: 4\n-->")
BAD = "완전히 다른 글입니다. 여기에는 원문과 전혀 무관한 내용이 들어 있습니다. 숫자 99%도 새로 넣었습니다."

calls = []
def fake(system, user, model, on_token=None):
    calls.append(system[:20])
    out = FAKE_OUT[min(len(calls) - 1, len(FAKE_OUT) - 1)]
    if on_token: on_token(out)
    return out

app.ollama = fake

# 1) standard 경로: 진단 1 + 윤문 1 = 2콜, 게이트 OK, 수치·고유명사 보존, SUMMARY 분리
FAKE_OUT = ["# 진단\n- D-1, A-8", GOOD]
r = app.humanize(SRC, "essay", "fake", "standard")
assert r["calls"] == 2 and r["gate_code"] == 0, (r["calls"], r["gate"], r["log"])
assert "37%" in r["output"] and "삼성전자" in r["output"] and "HUMANIZE-SUMMARY" not in r["output"]
assert "grade: A" in r["summary"] and "<del>" in r["diff_html"] and "<ins>" in r["diff_html"]
assert 0 < r["change_rate"] < 0.3

# 2) ABORT → 롤백 재실행 → 다시 ABORT → 원문 반환
calls.clear(); FAKE_OUT = ["진단", BAD, BAD]
r = app.humanize(SRC, "essay", "fake", "standard")
assert r["rolled_back"] and r["finalized"] == "abort" and r["output"] == SRC, r["log"]

# 3) heavy: 진단 + 윤문 + finalizer = 3콜
calls.clear(); FAKE_OUT = ["진단", GOOD, GOOD]
r = app.humanize(SRC, "essay", "fake", "heavy")
assert r["calls"] == 3 and r["finalized"] == "corrected" and r["gate_code"] == 0

# 4) light: 1콜, 진단 없음
calls.clear(); FAKE_OUT = [GOOD]
r = app.humanize(SRC, "essay", "fake", "light")
assert r["calls"] == 1 and r["diagnosis"] == ""

# 브라우저가 떠나도(첫 진행 이벤트 뒤 쓰기가 BrokenPipeError) 작업은 끝까지 — 결과는 실행 기록에 남고, 서버 오류·트레이스백 없음
import io, threading, urllib.request
from http.server import ThreadingHTTPServer


class Cut:
    def __init__(s, f, n):
        s.f, s.n = f, n

    def write(s, b):
        s.n -= 1
        if s.n < 0:
            raise BrokenPipeError(32, "Broken pipe")
        return s.f.write(b)

    def flush(s):
        pass

    def __getattr__(s, k):
        return getattr(s.f, k)


class CutH(app.H):
    def setup(s):
        super().setup()
        s.wfile = Cut(s.wfile, 2)  # 응답 머리 + 첫 이벤트까지만 닿음


ERRS = []


class Srv(ThreadingHTTPServer):
    def handle_error(s, req, addr):
        ERRS.append(sys.exc_info()[1])


def cut_post(path, body, user="dc-test"):
    cut = Srv(("127.0.0.1", 0), CutH)
    threading.Thread(target=cut.serve_forever, daemon=True).start()
    err0, sys.stderr = sys.stderr, io.StringIO()
    try:
        r = urllib.request.Request(f"http://127.0.0.1:{cut.server_address[1]}{path}", json.dumps(body).encode(),
                                   {"Content-Type": "application/json", "X-Agent-User": user})
        got, log = urllib.request.urlopen(r, timeout=60).read().decode(), sys.stderr.getvalue()
    finally:
        sys.stderr = err0
        cut.shutdown()
        cut.server_close()
    assert got.count("data: ") == 1 and not ERRS and "Traceback" not in log and log.count("연결 끊김") == 1, (got, ERRS, log)
    return os.path.join(app.WS, "u", user)
calls.clear(); FAKE_OUT = [GOOD]
d = cut_post("/api/humanize", {"text": SRC, "route": "light", "model": "fake"})
try:
    runs = app.list_runs(d)
    assert len(runs) == 1 and len(calls) == 1, (runs, calls)
finally:
    shutil.rmtree(d, ignore_errors=True)

# 저작권 표기: ui.html 에서 지워도 서버가 다시 붙인다 (LICENSE·NOTICE)
import base64 as _b
_h = app.signed(app.HTML.replace("data-sig", "").replace('name="author"', ""))
assert "data-sig" in _h and 'name="author"' in _h and _b.b64decode("ZG9uZ2p1a2ltLmRldkBnbWFpbC5jb20=").decode() in _h, "저작권 표기 누락"

print("selftest OK — runs:", [x["run_id"] for x in app.list_runs()[:4]])
