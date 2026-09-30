# humanize-kr-local — Humanize KR 폐쇄망 로컬 실행 패키지

> **한 줄 요약** — 오픈소스 Humanize KR([im-not-ai](https://github.com/epoko77-ai/im-not-ai), MIT)의 AI 한글 티 제거 파이프라인을
> Claude Code 없이 폐쇄망에서 돌아가게 만든 패키지입니다. 파이썬 표준 라이브러리만 써서 설치 없이 폴더 복사로 배포되고,
> 서버에 있는 Ollama나 vLLM에 환경변수 하나로 붙습니다. 원본의 정량 점수·게이트 스크립트는 그대로 쓰고 LLM 호출 부분만 로컬 서버로 바꿨습니다.
> 로컬 8B 모델로 PoC를 돌려 동작을 확인했고, GPU 서버에서 큰 모델을 붙이면 품질이 올라갑니다.
>
> - **의존성**: 없음. Python 3.9 이상.
> - **의미 훼손 안전장치**: 코드로 판정하는 게이트가 변경률 50% 초과·수치·고유명사 소실을 잡으면 결과를 버리고 원문을 반환.
> - **모델**: 한국어 되는 30B급 이상 아무거나. 모델을 바꿔도 코드 수정 없음.

[epoko77-ai/im-not-ai](https://github.com/epoko77-ai/im-not-ai) (Humanize KR, MIT)의 AI 한글 티 제거 파이프라인을
**Claude Code 없이** 로컬 LLM 서버(Ollama · vLLM 등 OpenAI 호환)로 돌리는 웹 UI / CLI 패키지입니다.

- 외부 의존성 0 — Python 3.9+ 표준 라이브러리만. `pip install` 없음.
- 폐쇄망 배포: 이 폴더를 통째로 복사하면 끝. 모델 서빙은 기존 서버(Ollama, vLLM, LM Studio…)를 그대로 씁니다.
- 원본 스킬의 light / standard / heavy 3경로, 결정적 게이트(변경률·구조·수치·서법), 롤백·finalize 규칙을 그대로 재현합니다.

## 실행

```bash
# Ollama (기본)
python3 app.py                                  # http://localhost:8765
LLM_MODEL=gpt-oss:20b python3 app.py

# OpenAI 호환 서버 (vLLM, LM Studio, llama.cpp server, TGI …)
LLM_API=openai LLM_BASE_URL=http://gpu-server:8000/v1 LLM_MODEL=Qwen3-32B python3 app.py

# CLI (stdin → JSON stdout, exit 2 = 게이트 ABORT)
python3 app.py --cli column heavy < input.txt

# LLM 없이 결정적 파이프라인만 자가 검증
python3 selftest.py
```

| 환경변수 | 기본 | 설명 |
|---|---|---|
| `LLM_API` | `ollama` | `ollama` 또는 `openai` |
| `LLM_BASE_URL` | `http://localhost:11434` / `http://localhost:8000/v1` | 서버 주소 |
| `LLM_MODEL` | `qwen3:8b` | 기본 모델 (UI에서 변경 가능) |
| `LLM_API_KEY` | (없음) | OpenAI 호환 서버에 키가 필요할 때 |
| `NUM_CTX` | `16384` | Ollama 컨텍스트 창 (룰북 + 원문이 잘리지 않게) |
| `TEMPERATURE` | `0.2` | |
| `PORT` | `8765` | |

## 파이프라인

```
입력
 ├ scripts/prepare_monolith_input.py   위생(NFC·제로폭 제거) + 정량 점수 + route_hint     [LLM 0]
 ├ [standard·heavy] 진단 콜             roles/diagnostician.md + diagnosis-rules.md          [LLM 1]
 ├ 윤문 콜                              goal-prompt.md + quick-rules.md (토큰 스트리밍)      [LLM 1]
 ├ scripts/restore_modality.py         서법(당위·추측) 소실 문장을 원문으로 복원            [LLM 0]
 ├ scripts/strip_injected_commas.py    윤문이 새로 넣은 연결어미 쉼표 제거                  [LLM 0]
 ├ scripts/verify_gates.py             변경률 30%/50% · S1 목표달성 · 수치주입 · 서법 4축   [LLM 0]
 ├ [ABORT ≥50%]  보수 강도 롤백 재실행 1회 → 다시 ABORT면 원문 반환
 └ [heavy 또는 WARN]  finalizer 콜: 원문↔윤문본 대조 국소 보정 → 게이트 재실행              [LLM 1]
```

경로별 LLM 콜 수: light 1 · standard 2 · heavy 3 (롤백·finalize 승급 시 +1).

`goal-prompt.md`는 원본 SKILL.md와 `roles/monolith.md`에서 LLM이 지켜야 할 목표·불변 규칙·출력 계약만 추린 시스템 프롬프트이며,
실행 시 뒤에 `quick-rules.md`(85패턴 룰북)가 자동으로 붙습니다.

## 웹 UI

모델·장르·경로 선택 → 윤문. 진행 단계와 윤문 토큰이 실시간으로 표시되고, 결과는 **변경 비교(diff)** · 진단 · 게이트 리포트 · SUMMARY · 로그 탭으로 봅니다.
모든 실행은 `_workspace/{날짜-태그}/`에 원본·진단·중간본·최종본·`result.json`으로 남고, UI 하단 "이전 실행 기록"에서 다시 불러올 수 있습니다.

## 폴더 구조 (원본에서 동작에 필요한 것만)

```
app.py            서버 + 파이프라인 (단일 파일)
ui.html           웹 UI
goal-prompt.md    윤문 시스템 프롬프트 (이 패키지에서 작성)
selftest.py       LLM 없는 자가 검증
scripts/          원본 결정적 스크립트 그대로 (prepare_monolith_input · verify_gates · checks · restore_modality · strip_injected_commas · sanitize_text · console · verify_change_rate)
skills/humanize-korean/references/
  quick-rules.md        윤문 룰북 (85패턴 압축)
  diagnosis-rules.md    진단 전용 인덱스
  roles/*.md            진단·윤문·finalize 역할 정의
  metrics.py, metrics_v2.py, baseline*.json   정량 지표
```

원본 스크립트는 `skills/humanize-korean/references/` 경로를 상대로 참조하므로 레이아웃을 바꾸지 않았습니다.

## 실측 (M-시리즈 Mac, PoC)

| 모델 | 경로 | 콜 | 소요 | 변경률 | 게이트 |
|---|---|---|---|---|---|
| qwen3:8b | standard | 2 | 약 90초 | 13% | OK |
| qwen3:8b | heavy | 3 | 약 3분 | 15% | WARN(구조축) → finalize |
| gpt-oss:20b | standard | 2 | 약 2분 | 낮음 | OK |

8B~20B급 로컬 모델은 이중피동·결론 접속사·기계적 열거는 잡지만 "~에 있어서", "~에 의해", "시사하는 바가 크다" 같은 번역투를 자주 남깁니다.
룰북이 아니라 모델 크기 문제이므로 GPU 서버에서는 30B 이상급 모델을 권합니다. 게이트가 의미 훼손은 막아 주므로 모델을 바꿔도 안전합니다.

## 한계 / 미구현

- 1만 5천 자 초과 청킹(`--chunk` + `reassemble_chunks.py`)은 넣지 않았습니다. 원본 실측상 단일 콜이 더 싸고 품질이 같습니다. 장문이 필요하면 추가.
- 진단·finalize 프롬프트는 원본 역할 정의를 그대로 씁니다. 로컬 모델용 few-shot 튜닝은 하지 않았습니다.

## 출처 / 라이선스

이 패키지는 **[epoko77-ai/im-not-ai](https://github.com/epoko77-ai/im-not-ai)** (Humanize KR v2.3.2)의 파생물입니다.
`scripts/`, `skills/humanize-korean/references/` 아래 파일은 원본 저장소의 것을 수정 없이 가져왔고, `app.py` · `ui.html` · `goal-prompt.md` · `selftest.py`는 이 패키지에서 새로 작성했습니다.
원본 저작권은 원저자(epoko77-ai)에게 있으며, 원본과 동일하게 [MIT License](LICENSE)로 배포합니다. `NOTICE` 참조.
