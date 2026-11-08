"""한국 모드 점검 스크립트 — 키·접속·지표를 한 번에 확인한다.

    poetry run python scripts/kr_check.py              # 005930 기준
    poetry run python scripts/kr_check.py 000660 2026-11-06

출력:
  1. 환경 변수 있음/없음 (값은 절대 출력하지 않음)
  2. KRX 로그인 성공 여부, 시세 경로별 응답
  3. 사용 가능한 Gemini Flash 계열 모델 이름 (텍스트 생성은 하지 않음 → 무료 사용량 소모 없음)
  4. 최근 TTM 지표: ROE, 부채비율, PER … 그리고 KRX(pykrx)가 발표한 PER/PBR/EPS/BPS
     → 네이버 금융 값과 직접 비교해 보세요.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path.cwd() / ".env", override=False)
load_dotenv(Path.home() / ".hedge-fund" / ".env", override=False)
os.environ["DATA_PROVIDER"] = "kr"

import requests  # noqa: E402

from hedge_fund.data.kr.client import KRDataClient  # noqa: E402
from hedge_fund.data.kr.prices import FdrSource, kr_completed_through, PykrxSource, SnapshotSource  # noqa: E402


def pct(v):
    return "-" if v is None else f"{v * 100:.2f}%"


def num(v, digits=2):
    return "-" if v is None else f"{v:,.{digits}f}"


def main() -> None:
    ticker = sys.argv[1] if len(sys.argv) > 1 else "005930"
    as_of = sys.argv[2] if len(sys.argv) > 2 else kr_completed_through()

    print("== 1. 환경 변수 (값은 출력하지 않음)")
    for var in ("DATA_PROVIDER", "DART_API_KEY", "GOOGLE_API_KEY", "KRX_ID", "KRX_PW", "HEDGE_FUND_LLM_MODEL"):
        print(f"  {var:22s} {'있음' if os.environ.get(var) else '없음'}")

    print("\n== 2. 시세 경로")
    start = (date.fromisoformat(as_of) - timedelta(days=10)).isoformat()
    for source in (PykrxSource(), FdrSource(), SnapshotSource()):
        if not source.available():
            print(f"  {source.name:13s} 사용 안 함 (KRX_ID/KRX_PW 없음 또는 로그인 실패)")
            continue
        for sym in (ticker, "KOSPI"):
            try:
                bars = source.bars(sym, start, as_of)
                last = bars[-1] if bars else None
                print(f"  {source.name:13s} {sym:7s} {len(bars):2d}개" + (f"  마지막 {last['date']} 종가 {last['close']:,.2f}" if last else ""))
            except Exception as exc:
                print(f"  {source.name:13s} {sym:7s} 실패: {type(exc).__name__}: {str(exc)[:100]}")

    print("\n== 3. Gemini 모델 (목록만 조회)")
    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        print("  GOOGLE_API_KEY 없음")
    else:
        try:
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", params={"pageSize": 200},
                             headers={"x-goog-api-key": key}, timeout=30)
            if r.ok:
                names = [m["name"].removeprefix("models/") for m in r.json().get("models", [])
                         if "flash" in m["name"] and "generateContent" in m.get("supportedGenerationMethods", [])
                         and not any(x in m["name"] for x in ("tts", "image", "omni"))]
                print("  " + ", ".join(names))
                print("  기본값: gemini-flash-latest  (바꾸려면 .env에 HEDGE_FUND_LLM_MODEL=gemini-flash-lite-latest)")
            else:
                print(f"  HTTP {r.status_code}: {r.json().get('error', {}).get('status')}")
        except Exception as exc:
            print(f"  실패: {type(exc).__name__}")

    print(f"\n== 4. {ticker} 재무지표 (기준일 {as_of}, 그날까지 공시된 보고서만 사용)")
    with KRDataClient() as client:
        facts = client.get_company_facts(ticker)
        print(f"  {facts.name} · {facts.exchange} · {facts.industry}")
        rows = client.get_financial_metrics(ticker, as_of, limit=4)
        if not rows:
            print("  지표 없음 (DART 키, 결산월, 종목코드를 확인하세요)")
        print("  분기말        공시일       ROE      부채비율    유동비율   PER     PBR    EPS          BPS")
        for m in rows:
            print(f"  {m.report_period}  {m.filing_date}  {pct(m.return_on_equity):>8s}  {pct(m.debt_to_equity):>9s}"
                  f"  {num(m.current_ratio):>7s}  {num(m.price_to_earnings_ratio):>6s}  {num(m.price_to_book_ratio):>5s}"
                  f"  {num(m.earnings_per_share, 0):>10s}  {num(m.book_value_per_share, 0):>10s}")
        ref = client.krx_reference(ticker, as_of)
        if ref:
            print(f"\n  KRX 발표값 ({ref['date']}): PER {num(ref['PER'])}  PBR {num(ref['PBR'])}  "
                  f"EPS {num(ref['EPS'], 0)}  BPS {num(ref['BPS'], 0)}")
        print("\n  비교 방법")
        print("  - 위 표의 PER/PBR은 '그 보고서 공시일의 종가' 기준, ROE·EPS는 최근 4개 분기 합(TTM)입니다.")
        print("  - KRX/네이버 PER은 '오늘 종가 ÷ 직전 사업연도 EPS'라서 값이 다를 수 있습니다.")
        print("    사업보고서 행(12-31)의 EPS가 KRX EPS와 가까우면 계산이 맞는 것입니다.")
        print("  - 부채비율은 네이버와 같은 정의(부채총계 ÷ 자본총계)로 % 표시했습니다.")


if __name__ == "__main__":
    main()
