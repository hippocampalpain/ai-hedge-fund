# 미국 모드 영향

한국 모드 작업(통합 테스트 후속 수정) 중 **미국 모드(DATA_PROVIDER 미설정)의 동작도 바꾸는** 변경을 모았다.
나머지 변경(평가일 종가 밸류에이션, EPS 정의, FDR 경고, 모의투자 거래일 기준)은 한국 모드 전용이다.

## 요약

| 변경 | 미국 모드 영향 | 결과가 달라지나 | 되돌리는 방법 |
|---|---|---|---|
| 백테스트 평가일 = 거래일만 | **있음** (백테스트만) | 일부 백테스트에서 LLM 호출 수·판단이 달라질 수 있음 | — (설정 없음) |
| 스냅샷에 P/B 열 추가 | **있음** (백테스트·모의투자 모두) | 프롬프트 문구가 바뀌어 LLM 응답이 달라질 수 있음. 첫 실행은 LLM 캐시 미스 | — |
| 데이터 캐시 버전(`cache_version`) | 없음 | 아니오. 미국 `FDClient`에는 속성이 없어 캐시 키 그대로 | — |
| 일시적 오류 재시도·Gemini 제한 시간 | 미국 기본 모델(Anthropic)은 없음. **Gemini를 쓸 때만** 있음 | 아니오 (실패하던 호출이 재시도로 성공할 수 있음) | `LLM_TRANSIENT_RETRIES=0`, `GEMINI_TIMEOUT_SECONDS=60` |
| 긴 대기(>60초) 429는 바로 기권, 하루 한도 소진 시 빠른 기권 | **Gemini를 쓸 때만** 있음 | 6번 재시도해도 실패할 호출이 바로 기권 (결과는 같고 시간만 줄어듦) | `LLM_MAX_RETRY_HINT_SECONDS=100000` |
| `CompanyFacts.industry_code` 필드, 금융업 부채비율 설명 | 없음 | 아니오. 미국 API는 이 필드를 보내지 않아 `None`이고, 설명이 없으면 스냅샷 해시에서도 빠진다 | — |
| 모의투자(paper) 거래일 기준 | 없음 | 아니오. `is_kr_mode()`일 때만 적용 | — |
| 모의투자 runner의 Windows 대응 (실행 중 확인·중지) | **있음** (모든 모드, 운영체제별) | Windows: 이제 정상 동작(이전에는 Ctrl+C·강제 종료). Linux·macOS: 동작 같음, 중지할 때 stop 파일을 하나 더 씀 | — |

## 1. 백테스트 평가일 = 거래일만

- **무엇이 바뀌었나:** 백테스트(`aihf backtest`, TUI 백테스트)에서 판단을 체결하기 직전의 "새로 고침" 기준일이 *체결일의 달력상 전날*에서 *직전 거래일*로 바뀌었다(`DailyClock(data, sessions)`, `ReplaySchedule.assessment_dates`). 거래일 목록은 벤치마크(미국은 보통 SPY)의 실제 거래일이다.
- **미국 백테스트에서 달라지는 경우:**
  - **월요일이 휴장일인 주** (예: MLK·Presidents·Memorial·Labor Day): 금요일 판단을 화요일에 체결할 때, 이전에는 휴장일인 월요일을 기준으로 애널리스트를 한 번 더 불렀다. 이제는 기준일이 금요일(= 판단일)이라 다시 부르지 않는다. **LLM 호출이 줄어든다.**
  - **휴장일에 공시된 자료** (예: Good Friday처럼 EDGAR는 열고 시장은 쉬는 날): 이전에는 그 체결에 반영됐을 수 있다. 이제는 다음 리밸런싱 때 반영된다. 최대 한 리밸런싱 주기만큼 늦어진다.
  - **일반적인 주** (월요일 판단, 화요일 체결): 이전에도 기준일이 월요일이라 차이가 없다.
- **바뀌지 않은 것:** 미국 모의투자(paper)는 여전히 달력상 전날을 기준으로 쓴다(`paper/tick.py` `previous_session`, `clock_for`). 기존 미국 테스트는 수정 없이 모두 통과한다.

## 2. 스냅샷에 P/B 열 추가

- **무엇이 바뀌었나:** LLM 에이전트에게 주는 `FundamentalsSnapshot`에 `price_to_book_ratio`(P/B) 열이 생겼다. 블라인드(백테스트)와 일반(모의투자·라이브) 프롬프트 모두에 해당한다.
- **이유:** 이전에는 P/B가 없어서 에이전트가 `P/E × ROE`로 P/B를 짐작했다. ROE가 평균 자본 기준이라 이 곱은 실제 P/B보다 크다(한국 모드 삼성전자 사례: 짐작 ≈3.7, 실제 2.50).
- **미국 모드 영향:**
  - 미국 P/B 값은 Financial Datasets API의 `price_to_book_ratio`를 그대로 쓴다(이 저장소에서 계산하지 않음).
  - 프롬프트 문자열과 `content_hash`가 바뀌므로 **기존 LLM 프롬프트 캐시(`~/.hedge-fund/cache/llm/`)는 맞지 않는다.** 미국 백테스트를 다시 돌리면 첫 실행에서 LLM을 새로 부르고, 비용과 시간이 든다.
  - 같은 기간이라도 에이전트 판단이 이전 결과와 달라질 수 있다(입력이 하나 늘었으므로).
- 열 위치가 바뀌어 `hedge_fund/features/test_snapshot.py`의 열 번호를 한 칸씩 고쳤다.

## 3. 데이터 캐시 버전 (`cache_version`)

- `CachedDataClient`는 감싼 클라이언트에 `cache_version` 속성이 있으면 캐시 키에 넣는다. `KRDataClient.cache_version = "kr-2"`.
- 미국 `FDClient`에는 이 속성이 없으므로 **캐시 키 문자열이 이전과 같다**(`test_clients_without_a_cache_version_keep_their_keys`). 미국 데이터 캐시(`~/.hedge-fund/cache/data/`)는 그대로 재사용된다.

## 4. 일시적 오류 재시도와 Gemini 제한 시간

- 재시도 로직(`RateLimitPolicy`)은 **Google(Gemini) 클라이언트에만** 붙어 있다. 미국 기본 모델(`claude-opus-5-5`, Anthropic)과 OpenAI·DeepSeek·xAI·TypeSafe 경로는 바뀌지 않았다.
- 미국 모드에서 `--model gemini-...`를 쓰면 한국 모드와 같이 적용된다.
  - 429는 최대 6회 재시도한다(이전과 같음).
  - 500/502/503/504, Deadline expired, UNAVAILABLE, 타임아웃은 최대 3회 재시도한다(새로 생김).
  - 요청 제한 시간은 180초다(이전 60초).
- 끄려면 `.env`에 다음을 넣는다.

```
LLM_TRANSIENT_RETRIES=0
GEMINI_TIMEOUT_SECONDS=60
```

## 5. 금융업 부채비율 설명

- `CompanyFacts`에 `industry_code`(DART 업종코드) 필드가 생겼다. 미국 Financial Datasets 응답에는 없으므로 항상 `None`이다.
- 설명은 `location == "KR"`이고 업종코드가 64·65·66으로 시작할 때만 붙는다. 미국 은행(예: JPM)에는 붙지 않는다.
- `FundamentalsSnapshot.leverage_note`가 `None`이면 `content_hash` 계산에서 빠지므로, 미국 스냅샷 해시는 이 변경 전과 같다(`test_us_snapshots_have_no_leverage_note_and_hash_as_before`).

## 6. 모의투자 runner의 Windows 대응

`hedge_fund/paper/runner.py`는 미국·한국 모드가 함께 쓴다.

- **이전 문제 (Windows):** `is_running`이 `os.kill(pid, 0)`으로 runner가 살아 있는지 확인했다. 그런데 Windows에서 `0`은 `signal.CTRL_C_EVENT`라서, 확인하는 대신 콘솔에 **Ctrl+C를 보냈다.**
  - `aihf paper list`·`status`·`stop`, TUI 모의투자 화면(주기적으로 확인)에서 일어났다.
  - `pytest`에서는 `test_paper_minute_fund_ticks_and_runs_like_a_daily_one`이 PID 파일에 자기 PID를 쓴 뒤 `paper list`를 불러, 테스트가 끝난 직후 `KeyboardInterrupt`가 났다.
  - `stop_runner`의 `os.kill(pid, SIGTERM)`은 Windows에서 `TerminateProcess`(강제 종료)라서, 세션 도중에 runner가 죽고 PID 파일이 남았다.
- **지금:**
  - Windows에서는 `OpenProcess`·`GetExitCodeProcess`로 **조회만** 한다.
  - 중지는 모든 OS에서 `runner.stop` 파일로 요청하고, runner가 1초 안에 보고 진행 중인 세션을 마친 뒤 멈춘다. Linux·macOS에서는 이전처럼 SIGTERM도 보낸다.
  - TUI에서 띄우는 runner는 Windows에서 별도 프로세스 그룹·콘솔 없음(`CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`)으로 실행해, 앱 콘솔의 Ctrl+C가 닿지 않는다.
  - `serve`는 끝날 때(예외 포함) 신호 핸들러, 감시 스레드, PID·stop 파일을 모두 원상복구한다.
- **테스트 안전장치:** `hedge_fund/conftest.py`가 모든 테스트 뒤에 SIGINT·SIGTERM 핸들러가 원래대로인지, `aihf-` 스레드가 남지 않았는지 검사한다.

## 7. 한국 모드 전용 (미국 모드 영향 없음)

| 변경 | 적용 조건 |
|---|---|
| 최신 행 PER·PBR·PSR을 평가일 종가로 재계산 | `KRDataClient` 안에서만 |
| EPS = 지배주주순이익 ÷ 가중평균 발행주식수 | `KRDataClient` 안에서만 |
| FDR(네이버) 종가로 대체될 때 경고 | 한국 시세 서비스 안에서만 |
| 모의투자 새로 고침 기준일 = KRX 직전 거래일 | `is_kr_mode()` |
| 실행 전 "예상 LLM 호출 수" 출력 | `aihf backtest`에서 `is_kr_mode()`일 때만 |
