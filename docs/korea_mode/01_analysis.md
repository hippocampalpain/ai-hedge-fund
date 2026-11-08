# 한국 모드 1단계: 구조 분석

이 문서는 코드를 고치기 전에 데이터 계층을 조사한 결과입니다. (버전 2.7.1 기준)

## 1. 데이터 계층 구성

| 구성 요소 | 파일 | 역할 |
|---|---|---|
| 인터페이스 | `hedge_fund/data/protocol.py` (`DataClient`) | 모든 데이터 제공자가 지켜야 하는 메서드 9개. 상속 없이 메서드만 같으면 됨 |
| 미국 구현 | `hedge_fund/data/client.py` (`FDClient`) | Financial Datasets API 호출. 실패는 예외, "데이터 없음"은 빈 목록/None |
| 디스크 캐시 | `hedge_fund/data/cached.py` (`CachedDataClient`) | 어떤 `DataClient`든 감싸서 `~/.hedge-fund/cache/data/`에 JSON 저장. 신선도 판단은 뉴욕 날짜 기준 |
| 데이터 모델 | `hedge_fund/data/models.py` | pydantic 모델 (`Price`, `FinancialMetrics`, `CompanyFacts` …) |
| 거래일 | `hedge_fund/data/sessions.py` | 벤치마크의 실제 일봉으로 거래일 그리드를 만듦. "완료된 날짜"를 뉴욕 시간으로 계산 |
| 생성 위치 | `run.py`, `tui/backtest.py`, `tui/paper.py`, `tui/paper_view.py` | `FDClient()`를 직접 생성 (공장 함수 없음) |

## 2. 데이터 함수 목록

| 함수 | 입력 | 반환 | 실제 사용처 |
|---|---|---|---|
| `get_prices` | ticker, start_date, end_date, interval="day" | `list[Price]` (open, close, high, low, volume:int, time) | 거래일 그리드(벤치마크), 평가·체결 가격, 유동성(ADV·변동성), 팩터 베타 |
| `get_financial_metrics` | ticker, end_date, period="ttm", limit | `list[FinancialMetrics]` 최신순, **filing_date ≤ end_date만** | 모든 LLM 에이전트 (FundamentalsSnapshot) |
| `get_company_facts` | ticker | `CompanyFacts` (name, sector, industry, exchange …) | 스냅샷의 섹터, 리스크의 섹터 한도 |
| `get_earnings_history` | ticker, limit | `list[EarningsRecord]` (filing_date, source_type, quarterly.eps_surprise) | PEAD 모델, 분 단위 펀드 |
| `get_snapshot` | ticker | `PriceSnapshot` (price, time) | 분 단위 펀드·페이퍼 화면만 |
| `get_news` | ticker, end_date, start_date, limit | `list[CompanyNews]` | 분 단위 펀드만 |
| `get_insider_trades` | ticker, end_date, start_date, limit | `list[InsiderTrade]` | **현재 사용하는 에이전트 없음** |
| `get_earnings` | ticker | `Earnings` | 사용처 없음 |
| `get_market_cap` | ticker, end_date | float | 사용처 없음 (스냅샷은 미래 누설 때문에 일부러 안 씀) |

## 3. 어떤 에이전트가 어떤 필드를 쓰나

| 에이전트 | 데이터 경로 | 쓰는 필드 |
|---|---|---|
| buffett, munger, graham, lynch, druckenmiller (LLM) | `features/snapshot.py` → `get_financial_metrics(period="ttm", limit=20)` + `get_company_facts` | report_period, filing_date, market_cap, price_to_earnings_ratio, return_on_equity, gross_margin, operating_margin, net_margin, debt_to_equity, current_ratio, revenue_growth, earnings_per_share, book_value_per_share, free_cash_flow_per_share, sector, industry. **TTM 행이 최소 4개** 있어야 판단(없으면 기권) |
| pead (퀀트) | `get_earnings_history` | quarterly.eps_surprise(BEAT/MISS), source_type="8-K", filing_date |
| 엔진(리스크·체결) | `get_prices`, `get_company_facts` | close, volume, time, sector |

## 4. 한국 데이터로 채우는 방법

출처: **DART** = OpenDART 전체 재무제표 / 주식총수 / 임원·주요주주 소유보고, **가격** = pykrx (→ FinanceDataReader 대체), **계산** = 앞의 두 출처로 직접 계산, **불가** = 무료로 구할 수 없음 → `None`

| 필드 | 출처 | 계산식 / 비고 |
|---|---|---|
| report_period, filing_date | DART | 분기말 날짜 / 그 분기를 완성시킨 보고서의 접수일(`rcept_no` 앞 8자리 = `rcept_dt`) |
| currency | — | `"KRW"` |
| market_cap | 가격 | 공시일 기준 종가 × 상장주식수 (pykrx 시가총액) |
| price_to_earnings_ratio | 계산 | 시가총액 / TTM 지배주주순이익 (pykrx PER과 비교용 출력) |
| price_to_book_ratio | 계산 | 시가총액 / 지배주주지분 (pykrx PBR과 비교) |
| price_to_sales_ratio | 계산 | 시가총액 / TTM 매출 |
| enterprise_value | 계산 | 시가총액 + 차입금 − 현금성자산 (차입금 계정이 없으면 None) |
| EV/EBITDA, EV/Revenue | 계산 | EBITDA = 영업이익 + 감가상각비(현금흐름표) |
| free_cash_flow_yield, peg_ratio | 계산 | FCF/시가총액, PER/(EPS성장률×100) |
| gross/operating/net margin | DART+계산 | 매출총이익·영업이익·당기순이익 ÷ 매출 (금융업은 매출총이익이 없어 None) |
| return_on_equity | 계산 | TTM 지배주주순이익 / 평균 지배주주지분 (네이버 금융과 같은 방식) |
| return_on_assets | 계산 | TTM 당기순이익 / 평균 자산총계 |
| return_on_invested_capital | 계산 | 영업이익×(1−유효세율) / (자본 + 차입금 − 현금) |
| 회전율·DSO·운전자본회전율 | 계산 | 매출(또는 매출원가) / 평균 재고·매출채권·운전자본 |
| current/quick/cash ratio, OCF ratio | DART+계산 | 유동자산·(유동자산−재고)·현금·영업CF ÷ 유동부채 |
| debt_to_equity | 계산 | **부채총계 / 자본총계** (네이버 "부채비율"과 같은 정의, 단 %가 아니라 배수) |
| debt_to_assets | 계산 | 부채총계 / 자산총계 |
| interest_coverage | 일부 | 영업이익 / 이자비용 — 이자비용이 본문에 따로 있을 때만, 없으면 None |
| *_growth | 계산 | 이번 TTM ÷ 4분기 전 TTM − 1 (전년 동기 대비) |
| payout_ratio | 계산 | 배당금 지급액(현금흐름표) / 지배주주순이익 |
| EPS, BPS, FCF/share | 계산 | ÷ 유통주식수(DART 주식총수 현황, 보통주) |
| CompanyFacts.name/sector | KRX 종목목록 | 종목명, 업종 |
| InsiderTrade | DART 임원·주요주주 소유보고 | 보고자·직위·등기여부·증감 주식수·보유 주식수. **거래가격·거래일은 불가 → None** |
| CompanyNews | 불가(1차) | 빈 목록 + 로그 |
| EarningsRecord.eps_surprise | 불가 | 무료 컨센서스가 없음 → 빈 목록 → PEAD는 한국에서 항상 중립 |
| PriceSnapshot | 가격 | 가장 최근 종가로 대체 (실시간 아님) |

## 5. 미국(USD)을 가정한 코드

1. `features/breakpoints.py` — 시가총액 분위를 **미국 주식 달러 기준**(Ken French 표)으로 계산. 원화 시총을 넣으면 삼성전자가 미국 최상위로 잘못 분류됨 → 한국 모드에서는 사용하지 않음
2. `brokers/costs.py` — 수수료가 "주당 달러"(`commission_per_share`). 한국은 거래대금 대비 % + 매도 시 거래세 → 설정값 추가 필요
3. `data/sessions.py`, `pipeline/clock.py` — "완료된 거래일"을 뉴욕 날짜로 계산 → 한국 모드는 서울 날짜
4. `fund/spec.py` — 기본 벤치마크 `SPY`, 기본 자본금 100,000. 삼성전자 1주가 약 19만원이라 원화로 10만원이면 한 주도 못 삼 → 한국용 예시 mandate는 1억원
5. `features/snapshot.py` — 블라인드 프롬프트 문구 "US stocks", 금액 표기가 B/M 단위 (블라인드 백테스트에서는 금액이 숨겨지므로 영향 적음)
6. TUI/CLI 화면의 `$` 표기 — 표시만의 문제로 1차 버전에서는 그대로 둠
7. 분 단위 펀드(미국 장 시간 04:00–20:00 ET) — 한국 모드는 일 단위 이상만 지원
