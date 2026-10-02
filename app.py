#!/usr/bin/env python3
"""Humanize KR — 로컬 Ollama 웹 UI / CLI. 외부 의존성 없음 (stdlib만).

  python3 app.py                         # http://localhost:8765
  OLLAMA_MODEL=gpt-oss:20b python3 app.py
  python3 app.py --cli [genre] [route] < input.txt   # JSON 결과를 stdout에

파이프라인 (원본 im-not-ai 스킬의 light/standard/heavy 3경로를 그대로 재현):
  shim(위생·정량점수·route_hint)
  → [standard/heavy] 진단 콜
  → 윤문 콜 (토큰 스트리밍)
  → restore_modality · strip_injected_commas (결정적 복원, LLM 0콜)
  → verify_gates (변경률·구조·수치·서법 4축 게이트)
  → [ABORT] 보수 강도로 롤백 재실행 1회
  → [heavy 또는 WARN] finalizer 콜 (원문 대조 국소 보정) → 게이트 재실행
"""
import datetime
import difflib
import html
import json
import os
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
REFS = os.path.join(ROOT, "skills", "humanize-korean", "references")
SCRIPTS = os.path.join(ROOT, "scripts")
WS = os.environ.get("WORKSPACE") or os.path.join(ROOT, "_workspace")  # 포털이 AGENT_DATA/<도구> 로 모아 줌
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("LLM_MODEL", os.environ.get("OLLAMA_MODEL", "qwen3:8b"))
PORT = int(os.environ.get("PORT", "8765"))
NUM_CTX = int(os.environ.get("NUM_CTX", "16384"))
GENRES = ("essay", "column", "report", "blog", "abstract")


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def write(p, s):
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


def ref(*parts):
    return read(os.path.join(REFS, *parts))


def run(script, *args):
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args],
                       capture_output=True, text=True, cwd=ROOT)
    return r.returncode, (r.stdout + r.stderr).strip()


# ── LLM 백엔드 (Ollama 또는 OpenAI 호환 서버: vLLM·LM Studio·llama.cpp 등) ─────
LLM_API = os.environ.get("LLM_API", "ollama")  # ollama | openai
BASE = os.environ.get("LLM_BASE_URL", OLLAMA if LLM_API == "ollama" else "http://localhost:8000/v1").rstrip("/")
API_KEY = os.environ.get("LLM_API_KEY", "")
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.2"))


def _clean(out):
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
    out = re.sub(r"^```\w*\s*\n", "", out)
    out = re.sub(r"\n?```\s*$", "", out)
    out = re.sub(r"\n?```\s*(<!--\s*HUMANIZE-SUMMARY)", r"\n\n\1", out)  # 본문만 펜스로 감싸고 요약을 뒤에 붙이는 모델(gemma4)
    return out.strip()


def _post_stream(url, body):
    hdr = {"Content-Type": "application/json"}
    if API_KEY:
        hdr["Authorization"] = "Bearer " + API_KEY
    req = urllib.request.Request(url, json.dumps(body).encode(), hdr)
    with urllib.request.urlopen(req, timeout=3600) as r:
        for line in r:
            line = line.strip()
            if line.startswith(b"data:"):  # SSE (openai)
                line = line[5:].strip()
            if line and line != b"[DONE]":
                yield json.loads(line)


def ollama(system, user, model, on_token=None):
    """스트리밍 chat. 백엔드는 LLM_API 환경변수로 선택. think:false 미지원 모델이면 자동 재시도."""
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if LLM_API == "openai":
        body = {"model": model, "stream": True, "temperature": TEMPERATURE, "messages": msgs}
        url = BASE + "/chat/completions"
    else:
        body = {"model": model, "stream": True, "think": False, "messages": msgs,
                "options": {"temperature": TEMPERATURE, "num_ctx": NUM_CTX}}
        url = BASE + "/api/chat"
    for attempt in (0, 1):
        try:
            buf = []
            for j in _post_stream(url, body):
                if "error" in j:
                    raise RuntimeError(str(j["error"]))
                if LLM_API == "openai":
                    tok = (j.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
                else:
                    tok = j.get("message", {}).get("content", "")
                if tok:
                    buf.append(tok)
                    if on_token:
                        on_token(tok)
                if j.get("done"):
                    break
            return _clean("".join(buf))
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            if attempt == 0 and "think" in body and "think" in msg:  # 모델이 think 파라미터 미지원
                body.pop("think")
                continue
            raise RuntimeError(f"LLM HTTP {e.code}: {msg[:300]}")


def models():
    hdr = {"Authorization": "Bearer " + API_KEY} if API_KEY else {}
    if LLM_API == "openai":
        with urllib.request.urlopen(urllib.request.Request(BASE + "/models", headers=hdr), timeout=10) as r:
            return [m["id"] for m in json.load(r)["data"]]
    with urllib.request.urlopen(BASE + "/api/tags", timeout=10) as r:
        return [m["name"] for m in json.load(r)["models"]]


# ── 결정적 후처리 + 게이트 ───────────────────────────────────────────────
def postprocess(d, genre, log):
    for s in ("restore_modality.py", "strip_injected_commas.py"):
        _, out = run(s, "--before", f"{d}/01_input.txt", "--after", f"{d}/final.md", "--out", f"{d}/final.md")
        log.append(f"[{s}] {out or '변경 없음'}")
    code, out = run("verify_gates.py", "--before", f"{d}/01_input.txt", "--after", f"{d}/final.md",
                    "--genre", genre, "--json")
    i = out.rfind("\n{")
    try:
        report = json.loads(out[i + 1:]) if i >= 0 else {}
    except json.JSONDecodeError:
        report = {}
    log.append("[verify_gates]\n" + (out[:i] if i >= 0 else out))
    return code, report


def strip_summary(text):
    m = re.search(r"<!--\s*HUMANIZE-SUMMARY(.*?)-->", text, re.S)
    body = re.sub(r"<!--\s*HUMANIZE-SUMMARY.*", "", text, flags=re.S).strip()
    return body, (m.group(1).strip() if m else "")


def diff_html(a, b):
    tok = lambda s: re.findall(r"\s+|[가-힣]+|\w+|[^\s\w]", s)
    ta, tb = tok(a), tok(b)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(html.escape("".join(ta[i1:i2])))
        else:
            if i2 > i1:
                out.append("<del>" + html.escape("".join(ta[i1:i2])) + "</del>")
            if j2 > j1:
                out.append("<ins>" + html.escape("".join(tb[j1:j2])) + "</ins>")
    return "".join(out).replace("\n", "<br>")


GATE_LABEL = ["OK", "WARN (30%+)", "ABORT (50%+)", "ERROR"]


# ── 파이프라인 ──────────────────────────────────────────────────────────
def humanize(text, genre="essay", model=MODEL, route=None, emit=lambda ev: None):
    if genre not in GENRES:
        genre = "essay"
    run_id = f"{datetime.date.today()}-{secrets.token_hex(2)}"
    d = os.path.join(WS, run_id)
    os.makedirs(d)
    write(f"{d}/01_input.txt", text)
    log, calls = [], 0

    emit({"stage": "shim", "msg": "정량 사전 점수 계산"})
    _, out = run("prepare_monolith_input.py", "--run-dir", d, "--genre", genre)
    log.append("[shim] " + out)
    try:
        metrics = json.load(open(f"{d}/00_metrics.json", encoding="utf-8"))
    except Exception:
        metrics = {}
    forced = route in ("light", "standard", "heavy")
    route = route if forced else (metrics.get("route_hint") or "standard")
    emit({"stage": "route", "msg": f"경로 {route} ({'사용자 지정' if forced else 'route_hint'})"})

    diag = ""
    if route != "light":
        emit({"stage": "diagnose", "msg": "진단 콜 (지배 패턴 3~6개)"})
        diag_sys = (ref("roles", "diagnostician.md")
                    + "\n\n출력은 02_diagnosis.md 본문(마크다운)만 쓴다. 원문을 고쳐 쓰지 않는다.\n\n---\n\n"
                    + ref("diagnosis-rules.md"))
        diag = ollama(diag_sys, read(f"{d}/01_input_with_metrics.txt"), model)
        calls += 1
        write(f"{d}/02_diagnosis.md", diag)
        _, out = run("prepare_monolith_input.py", "--run-dir", d, "--genre", genre,
                     "--diagnosis", f"{d}/02_diagnosis.md")
        log.append("[shim+diagnosis] " + out)

    system = read(os.path.join(ROOT, "goal-prompt.md")) + "\n" + ref("quick-rules.md")
    conservative = "\n\n**강도: 보수.** 내용 앵커 원형 보존, 원문에 없던 표현 삽입 금지, 확신 없는 구간은 그대로 둔다."
    if route == "light":
        system += conservative

    def rewrite(sys_prompt, label):
        nonlocal calls
        emit({"stage": "rewrite", "msg": label, "reset": True})
        out = ollama(sys_prompt, read(f"{d}/01_input_with_metrics.txt"), model,
                     on_token=lambda t: emit({"token": t}))
        calls += 1
        write(f"{d}/final.md", out)
        emit({"stage": "gate", "msg": "결정적 복원 + 4축 게이트"})
        return postprocess(d, genre, log)

    gate_code, report = rewrite(system, "윤문 콜")
    rolled_back = False
    if gate_code == 2:  # 과윤문 사고 → 보수 강도 롤백 재실행 1회
        os.replace(f"{d}/final.md", f"{d}/final_aborted.md")
        gate_code, report = rewrite(system + conservative + "\n직전 시도가 변경률 50%를 넘겨 폐기됐다. 절반 이하로만 손댄다.",
                                    "롤백 재실행 (보수 강도)")
        rolled_back = True

    finalized = None
    if gate_code == 2:
        write(f"{d}/final.md", text)  # 두 번 연속 ABORT → 원문 반환 (게이트는 고치지 않는다)
        finalized = "abort"
    elif route == "heavy" or gate_code == 1:  # finalize 승급 규칙
        emit({"stage": "finalize", "msg": "finalizer 콜 (원문 대조 국소 보정)"})
        os.replace(f"{d}/final.md", f"{d}/final_pre_finalize.md")
        fin_sys = (ref("roles", "finalizer.md")
                   + "\n\n출력 계약: 보정된 윤문 본문 전체만 쓴다(설명·머리말 금지). 본문 끝에 `<!-- HUMANIZE-SUMMARY ... -->` 블록 하나를 둔다. "
                   "국소 보정으로 의미를 복구할 수 없으면 본문 대신 `HOLD_AND_REPORT: <사유>` 한 줄만 쓴다.")
        fin_user = (f"[원문]\n{text}\n\n[윤문본]\n{read(f'{d}/final_pre_finalize.md')}\n\n"
                    + (f"[진단]\n{diag}" if diag else "[진단] 없음 (light 승급)"))
        fin = ollama(fin_sys, fin_user, model, on_token=None)
        calls += 1
        if fin.startswith("HOLD_AND_REPORT"):
            os.replace(f"{d}/final_pre_finalize.md", f"{d}/final.md")
            finalized = fin
        else:
            write(f"{d}/final.md", fin)
            gate_code, report = postprocess(d, genre, log)
            finalized = "corrected"
        write(f"{d}/09_finalize.json", json.dumps({"verdict": finalized, "gate": gate_code}, ensure_ascii=False))

    body, summary = strip_summary(read(f"{d}/final.md"))
    result = {
        "run_id": run_id, "route": route, "model": model, "genre": genre, "calls": calls,
        "input": text, "output": body, "summary": summary, "diagnosis": diag,
        "gate_code": gate_code, "gate": GATE_LABEL[min(gate_code, 3)], "gate_report": report,
        "rolled_back": rolled_back, "finalized": finalized,
        "change_rate": (report.get("change_rate") or {}).get("rate"),
        "metrics": metrics.get("metrics", {}), "diff_html": diff_html(text, body),
        "log": "\n".join(log), "ts": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    write(f"{d}/result.json", json.dumps(result, ensure_ascii=False, indent=1))
    return result


def list_runs():
    out = []
    if not os.path.isdir(WS):
        return out
    for name in sorted(os.listdir(WS), reverse=True)[:50]:
        p = os.path.join(WS, name, "result.json")
        if os.path.exists(p):
            try:
                j = json.load(open(p, encoding="utf-8"))
                out.append({k: j.get(k) for k in ("run_id", "route", "model", "gate", "gate_code", "ts")}
                           | {"head": j.get("input", "")[:40]})
            except Exception:
                pass
    return out


# ── HTTP ───────────────────────────────────────────────────────────────
HTML = read(os.path.join(ROOT, "ui.html")) if os.path.exists(os.path.join(ROOT, "ui.html")) else "ui.html 없음"

# ── 저작권 표기 (LICENSE·NOTICE 참고) ─────────────────────────────────────
_SIG = __import__("base64").b64decode("wqkgMjAyNiDquYDrj5nso7wgwrcgZG9uZ2p1a2ltLmRldkBnbWFpbC5jb20=").decode()
_SIG_A = __import__("base64").b64decode("RG9uZ0p1IEtpbSA8ZG9uZ2p1a2ltLmRldkBnbWFpbC5jb20+").decode()


def signed(html):
    """화면에 저작권 표기를 붙인다. ui.html 에서 지워져도 서버가 내보낼 때 다시 붙는다."""
    name, mail = _SIG.split(" · ")
    if 'name="author"' not in html:
        meta = f'<meta name="author" content="{name[7:]} <{mail}>">'
        html = html.replace("<head>", "<head>" + meta, 1) if "<head>" in html else meta + html
    if "data-sig" not in html:
        tag = (f'<!-- {_SIG} --><div data-sig title="{mail}" style="text-align:center;font-size:11px;color:#9aa0a6;'
               f'opacity:.55;margin:28px 0 8px">{name}</div>')
        html = html.replace("</body>", tag + "</body>", 1) if "</body>" in html else html + tag
    return html


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        if "/api/humanize" in (a[0] if a else ""):
            super().log_message(fmt, *a)

    def _send(self, body, ctype="application/json", code=200):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("X-Author", _SIG_A)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        try:
            if self.path == "/api/models":
                return self._send(models())
            if self.path == "/api/runs":
                return self._send(list_runs())
            m = re.fullmatch(r"/api/runs/(\d{4}-\d{2}-\d{2}-[0-9a-f]{4})", self.path)
            if m:
                return self._send(read(os.path.join(WS, m.group(1), "result.json")).encode())
            self._send(signed(HTML.replace("%MODEL%", json.dumps(MODEL))).encode(), "text/html; charset=utf-8")
        except Exception as e:
            self._send({"error": f"{type(e).__name__}: {e}"}, code=500)

    def do_POST(self):
        if self.path != "/api/humanize":
            return self._send({"error": "not found"}, code=404)
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = (req.get("text") or "").strip()
        if not text:
            return self._send({"error": "빈 입력"}, code=400)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(ev):
            self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        try:
            emit({"done": humanize(text, req.get("genre") or "essay", req.get("model") or MODEL,
                                   req.get("route"), emit)})
        except Exception as e:
            emit({"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        genre = sys.argv[2] if len(sys.argv) > 2 else "essay"
        route = sys.argv[3] if len(sys.argv) > 3 else None
        r = humanize(sys.stdin.read(), genre, MODEL, route,
                     emit=lambda ev: print(f"[{ev['stage']}] {ev['msg']}", file=sys.stderr) if "stage" in ev else None)
        r.pop("diff_html")
        print(json.dumps(r, ensure_ascii=False, indent=1))
        sys.exit(0 if r["gate_code"] < 2 else 2)
    print(f"Humanize KR local → http://localhost:{PORT}  (api={LLM_API} base={BASE} model={MODEL})  {_SIG}")
    ThreadingHTTPServer(("", PORT), H).serve_forever()
