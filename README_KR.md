# AI Hedge Fund — 한국 모드 (KOSPI·KOSDAQ, 무료 데이터)

이 저장소의 한국 모드는 원래 미국 주식용인 AI 헤지펀드를 **한국 주식**에 쓸 수 있게 한 것입니다.
데이터와 LLM 모두 **무료**로 씁니다.

| 무엇 | 출처 |
|---|---|
| 재무제표·주식수·업종 | [OpenDART](https://opendart.fss.or.kr) (금융감독원, 무료 인증키) |
| 일별 시세·시가총액·KOSPI 지수 | KRX 정보데이터시스템(pykrx, 무료 회원). 실패하면 FinanceDataReader(네이버)로 대체 |
| 투자 판단(LLM) | Google Gemini 무료 등급 |

> **면책:** 교육·연구용입니다. 실제 투자 조언이 아니고, 실제 주문을 내지 않습니다(백테스트와 모의투자만 가능).
> 투자 판단과 그 결과의 책임은 본인에게 있습니다.

계산식과 데이터 출처의 자세한 설명은 [`docs/korea_mode/02_formulas.md`](docs/korea_mode/02_formulas.md)에 있습니다.
미국 모드에 미치는 영향은 [`docs/korea_mode/03_us_mode_impact.md`](docs/korea_mode/03_us_mode_impact.md)에 있습니다.

---

## 목차

1. [Python 설치](#1-python-설치)
2. [Git 설치](#2-git-설치)
3. [Poetry 설치](#3-poetry-설치)
4. [내 fork를 내려받기 (clone)](#4-내-fork를-내려받기-clone)
5. [korea-mode 브랜치로 이동](#5-korea-mode-브랜치로-이동)
6. [설치](#6-설치)
7. [키 4개 발급하고 .env에 입력](#7-키-4개-발급하고-env에-입력)
8. [점검](#8-점검)
9. [실행 예시](#9-실행-예시)
10. [한계 (꼭 읽어 주세요)](#10-한계-꼭-읽어-주세요)
11. [문제 해결](#11-문제-해결)

모든 명령은 **Windows PowerShell** 기준입니다. 시작 메뉴에서 "PowerShell"을 검색해 여세요.
관리자 권한은 필요 없습니다.

---

## 1. Python 설치

**Python 3.12**를 권장합니다(3.11 이상 필요).

```powershell
winget install -e --id Python.Python.3.12
```

`winget`이 없으면 <https://www.python.org/downloads/windows/>에서 설치 파일을 받으세요.
설치 첫 화면에서 **"Add python.exe to PATH"에 체크**해야 합니다.

**PowerShell 창을 닫았다가 새로 연 뒤** 확인합니다.

```powershell
py --version
```

`Python 3.12.x`가 나오면 됩니다.

## 2. Git 설치

```powershell
winget install -e --id Git.Git
```

새 PowerShell 창에서 확인합니다.

```powershell
git --version
```

## 3. Poetry 설치

Poetry는 이 프로젝트가 쓰는 패키지 관리 도구입니다. pipx로 설치하면 깔끔합니다.

```powershell
py -m pip install --user pipx
```

```powershell
py -m pipx ensurepath
```

**PowerShell 창을 닫았다가 새로 연 뒤** 설치합니다.

```powershell
pipx install poetry
```

```powershell
poetry --version
```

## 4. 내 fork를 내려받기 (clone)

1. GitHub에서 이 저장소 오른쪽 위의 **Fork**를 눌러 내 계정으로 복사합니다. 이미 fork가 있으면 건너뜁니다.
2. 내 fork를 내려받습니다. `<내아이디>`를 내 GitHub 아이디로 바꾸세요.

```powershell
cd $HOME
```

```powershell
git clone https://github.com/<내아이디>/ai-hedge-fund.git
```

```powershell
cd ai-hedge-fund
```

## 5. korea-mode 브랜치로 이동

한국 모드가 아직 `main`에 합쳐지지 않았다면 `korea-mode` 브랜치를 씁니다. 합쳐진 뒤라면 이 단계는 건너뛰어도 됩니다.

```powershell
git checkout korea-mode
```

## 6. 설치

Python 3.12로 가상환경을 만들고 필요한 패키지를 설치합니다. 몇 분 걸립니다.

```powershell
poetry env use (py -3.12 -c "import sys; print(sys.executable)")
```

오류가 나면 `poetry env use python`으로 바꿔 보세요(PATH에 있는 Python을 씀).

```powershell
poetry install
```

```powershell
poetry run pytest -q
```

마지막 줄에 `... passed`가 나오고 `failed`가 없으면 설치가 잘 된 것입니다.
`skipped`는 미국 데이터 키가 없어서 건너뛴 테스트라 괜찮습니다.

## 7. 키 4개 발급하고 .env에 입력

| 키 | 발급처 | 비고 |
|---|---|---|
| `DART_API_KEY` | [OpenDART](https://opendart.fss.or.kr) → 회원가입 → 인증키 신청/관리 | 40자리, 무료 |
| `GOOGLE_API_KEY` | [Google AI Studio](https://aistudio.google.com/apikey) → Create API key | 무료 등급 |
| `KRX_ID`, `KRX_PW` | [KRX 정보데이터시스템](https://data.krx.co.kr) 회원가입 | 무료. pykrx가 시세를 받을 때 로그인에 씀 |

예시 파일을 복사해서 엽니다.

```powershell
Copy-Item .env.example .env
```

```powershell
notepad .env
```

파일 맨 아래 "한국 모드" 부분을 찾아, 앞의 `#`을 지우고 값을 넣습니다. 최소한 아래 6줄이 있어야 합니다.

```
DATA_PROVIDER=kr
DART_API_KEY=발급받은-40자리-키
GOOGLE_API_KEY=발급받은-구글-키
KRX_ID=KRX-아이디
KRX_PW=KRX-비밀번호
HEDGE_FUND_LLM_MODEL=gemini-flash-lite-latest
```

- **무료로 쓸 때는 `HEDGE_FUND_LLM_MODEL=gemini-flash-lite-latest`를 권장합니다.** 이 줄이 없을 때 프로그램 기본값은 `gemini-flash-latest`인데, 무료 한도가 하루 20회(2026-11-08 확인)라 백테스트 한 번에 바닥날 수 있습니다. 무료 한도는 모델마다 따로 셉니다.
- 위쪽의 미국용 키(`FINANCIAL_DATASETS_API_KEY`, `ANTHROPIC_API_KEY` 등)는 한국 모드에서는 필요 없습니다. 지우거나 그대로 두세요.
- **`.env`는 절대 GitHub에 올리지 마세요.** `.gitignore`에 들어 있어서 `git add`해도 올라가지 않지만, 화면 공유나 메신저로 보내지도 마세요. 이 프로그램은 키·비밀번호 값을 화면이나 로그에 출력하지 않습니다.
- `DATA_PROVIDER=kr` 줄을 지우면 원래 미국 모드로 돌아갑니다.

## 8. 점검

키와 데이터 경로가 제대로 되는지 한 번에 확인합니다. 삼성전자(005930)로 예를 듭니다.

```powershell
poetry run python scripts/kr_check.py 005930
```

**첫 실행은 DART 재무제표를 내려받느라 종목당 몇 분 걸립니다.** 다음부터는 캐시를 써서 빠릅니다.
출력은 네 부분입니다.

1. 키 있음/없음 (값은 출력하지 않음)
2. 시세 경로별 응답 (pykrx, fdr, fdr-snapshot)
3. 쓸 수 있는 Gemini Flash 모델 목록 (텍스트 생성은 하지 않아 무료 한도를 쓰지 않음)
4. ROE·부채비율·유동비율·PER·PBR·EPS·BPS와 KRX 발표값

검증 결과 (삼성전자 2025 사업보고서):

| 항목 | 이 프로그램 | 비교 대상 |
|---|---|---|
| 부채비율 | 29.94% | 네이버 29.94% |
| ROE | 10.85% | 와이즈리포트 10.78% |
| BPS | 63,997 | KRX·네이버 63,997 |
| EPS | 6,557 | 와이즈리포트 6,564 (KRX 6,605는 공시된 보통주 기본주당이익이라 정의가 다름) |
| 유동비율 | 2.33 | 일치 |

## 9. 실행 예시

처음 한 번 실행하면 `C:\Users\<이름>\.hedge-fund\mandates\example_kr.yaml`(한국용 예시 운용 지침)이 생깁니다.
여기에는 Graham·Buffett·Munger 에이전트, 1억원, 주 1회 리밸런싱, KOSPI 벤치마크가 정해져 있습니다.

### 백테스트 (과거 기간 다시 돌려 보기)

```powershell
poetry run aihf backtest "$HOME\.hedge-fund\mandates\example_kr.yaml" --universe 005930,000660 --start 2026-10-23 --end 2026-11-06
```

- `--universe`: 6자리 종목코드 또는 종목명(`삼성전자,SK하이닉스`).
- `--end`를 빼면 최근 완료된 거래일, `--start`를 빼면 그보다 260일 전입니다.
- 실행 전에 **"예상 LLM 호출 수"**(에이전트 수 × 종목 수 × 평가일 수)가 나옵니다. Gemini 무료 한도를 넘지 않는지 먼저 보세요. 기간을 짧게, 종목을 적게 잡을수록 호출이 적습니다.
- 결과는 수익률 vs KOSPI, 주문 수, 샤프지수, 최대낙폭, 애널리스트별 성적표입니다. 전체 JSON은 `~\.hedge-fund\research\`에 저장됩니다.
- 수수료율·거래세는 mandate의 `costs:`에서 바꿉니다(`commission_rate`, `sell_tax_rate`). 기본값은 0입니다.

에이전트를 줄이려면 mandate 파일을 복사해서 `models:`에서 줄을 지우면 됩니다.

```powershell
Copy-Item "$HOME\.hedge-fund\mandates\example_kr.yaml" "$HOME\.hedge-fund\mandates\my_kr.yaml"
```

```powershell
notepad "$HOME\.hedge-fund\mandates\my_kr.yaml"
```

### 모의투자 (매일 한 번씩 진행)

```powershell
poetry run aihf paper create my-kr-fund --mandate "$HOME\.hedge-fund\mandates\example_kr.yaml" --universe 005930,105560
```

```powershell
poetry run aihf paper tick my-kr-fund
```

```powershell
poetry run aihf paper status my-kr-fund
```

`tick`은 다음 완료된 KRX 거래일 하나를 진행합니다. 주말·휴장일은 건너뜁니다.

### 화면(TUI)으로 쓰기

```powershell
poetry run aihf
```

## 10. 한계 (꼭 읽어 주세요)

| 한계 | 내용 |
|---|---|
| **금융주 판단 한계** | 은행·보험은 예금·보험부채가 부채로 잡혀 부채비율이 10배를 넘는 것이 보통입니다(예: KB금융 12.8배, 하나금융 13.7배). DART 업종코드가 금융업(64·65·66)이면 프롬프트에 이 사실을 설명으로 붙이지만, 에이전트 기준은 제조업용입니다. Graham은 "부채가 적어야 함" 기준으로 금융주를 탈락시키는 경향이 있습니다(설명을 붙이기 전 백테스트에서는 6번 모두 bearish). 은행 전용 지표(BIS 비율, NIM, 대손비용 등)는 제공하지 않습니다. |
| **뉴스 미지원** | 한국 뉴스는 제공하지 않습니다(빈 목록). 뉴스를 읽는 에이전트는 정보 없이 판단합니다. |
| **내부자거래 미지원(제한적)** | DART 임원·주요주주 소유보고로 보유 주식 변동 수량만 받습니다. 거래일·거래가격이 없고, 투자 에이전트 프롬프트에도 들어가지 않습니다. |
| **NXT 종가 차이** | 네이버가 화면에 보여주는 종가는 KRX 공식 종가와 다를 수 있습니다(2026-10-20~11-06 삼성전자, 14일 중 11일 차이, 11-06 네이버 193,500 vs KRX 188,700). 대체거래소(NXT) 시간외 거래까지 합친 통합 시세로 추정됩니다. 이 프로그램은 **KRX 공식 종가(pykrx)** 로 계산하고 체결합니다. KRX 로그인이 실패해 FinanceDataReader(네이버)로 넘어가면 경고를 남기며, 그때는 가격이 네이버 기준이 됩니다. |
| **Gemini 무료 한도** | 무료 등급은 모델마다 분당·하루 호출 수 제한이 있습니다(`gemini-flash-latest` 하루 20회, 2026-11-08 확인). 한도를 넘으면 그 날짜·종목은 에이전트가 기권(판단 없음)합니다. 서버가 60초보다 오래 기다리라고 하면 재시도하지 않고 바로 기권하며, 하루 한도를 다 쓰면 그 사실을 로그에 남깁니다. 현재 사용량은 <https://ai.dev/rate-limit>에서 확인하세요. |
| 실적 서프라이즈 없음 | 무료 컨센서스가 없어 PEAD(실적 서프라이즈) 모델은 항상 중립입니다. |
| 12월 결산만 | 결산월이 12월이 아닌 회사는 재무지표를 비워 둡니다. |
| 일봉만 | 분 단위 펀드는 미국 모드 전용입니다. |
| EPS 근사 | EPS = 지배주주순이익 ÷ 가중평균 발행주식수(보통+우선). 자기주식 소각일을 무료 API로 받을 수 없어 반기·연말 주식수를 직선으로 이은 근사값입니다(삼성전자 2025: 와이즈리포트 대비 −0.11%). |
| 업종 정보 | 과거 시점 값이 아니라 최신 값을 씁니다. |
| 화면 표기 | TUI의 `$` 표기는 그대로입니다(금액은 원화). |
| 실제 주문 없음 | 증권사 주문 연동은 없습니다. |

## 11. 문제 해결

| 증상 | 해결 |
|---|---|
| 한글이 깨져 보임 | PowerShell에서 `chcp 65001`을 실행한 뒤 다시 실행하세요. |
| `KRX 로그인에 실패했습니다` | `KRX_ID`·`KRX_PW`를 확인하세요. 실패해도 FinanceDataReader로 대체되지만 종가가 KRX와 다를 수 있습니다(위 "NXT 종가 차이"). |
| `DART ... status 010/011/020` | DART 키가 틀렸거나(010·011) 하루 호출 한도(020)를 넘었습니다. |
| `하루 무료 한도(…)를 다 썼습니다` | Gemini 하루 한도 소진입니다. 로그에 적힌 시간이 지난 뒤 다시 하거나, `.env`의 `HEDGE_FUND_LLM_MODEL`을 다른 무료 모델로 바꾸세요(`scripts/kr_check.py`의 3번에 목록이 나옵니다). |
| `504 Deadline expired` | Gemini 서버 지연입니다. 자동으로 최대 3번 재시도합니다. 계속되면 `.env`에 `GEMINI_TIMEOUT_SECONDS=300`을 넣으세요. |
| 첫 실행이 너무 오래 걸림 | 종목당 DART 첫 조회는 몇 분 걸립니다. 캐시는 `C:\Users\<이름>\.hedge-fund\cache\kr\`에 저장되어 다음부터 빠릅니다. |
| 계산식을 바꾼 뒤 예전 숫자가 나옴 | 데이터 캐시 `C:\Users\<이름>\.hedge-fund\cache\data\`를 지우면 다시 계산합니다. |
