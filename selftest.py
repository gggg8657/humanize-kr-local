#!/usr/bin/env python3
"""LLM 없이 결정적 파이프라인만 검증한다: shim → 후처리 → 4축 게이트 → diff.
Ollama 호출을 가짜 윤문 함수로 바꿔 끼운다.  python3 selftest.py"""
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

print("selftest OK — runs:", [x["run_id"] for x in app.list_runs()[:4]])
