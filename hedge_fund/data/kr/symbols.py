"""종목 식별: 6자리 종목코드 · 종목명 · DART corp_code · 지수.

- 종목 목록(이름, 시장, 업종, 결산월)은 FinanceDataReader의 ``KRX-DESC``
  목록에서 받아 하루 동안 파일 캐시한다.
- 종목코드 → corp_code 변환표는 DART corpCode.xml에서 받아 30일 캐시한다
  (dart.DartClient.corp_codes).
"""

from __future__ import annotations

import contextlib
import io
import logging
import re

from hedge_fund.data.kr.store import DiskCache, KRDataError

logger = logging.getLogger(__name__)

# 벤치마크/지수: 이름 → (pykrx 지수코드, FinanceDataReader 심볼)
INDICES = {
    "KOSPI": ("1001", "KS11"),
    "KS11": ("1001", "KS11"),
    "KOSDAQ": ("2001", "KQ11"),
    "KQ11": ("2001", "KQ11"),
    "KOSPI200": ("1028", "KS200"),
    "KS200": ("1028", "KS200"),
}

# 2024년부터 영문이 섞인 코드도 있다 (예: 0001A0)
CODE_PATTERN = re.compile(r"^\d{4}[0-9A-Z]{2}$")
LISTING_MAX_AGE = 86400


def is_index(symbol: str) -> bool:
    return symbol.upper() in INDICES


def is_code(text: str) -> bool:
    return bool(CODE_PATTERN.match(text.strip().upper()))


class SymbolBook:
    """Listing lookups, cached on disk. *fetch_listing* is injectable for tests."""

    def __init__(self, cache: DiskCache | None = None, fetch_listing=None) -> None:
        self._cache = cache or DiskCache("symbols")
        self._fetch_listing = fetch_listing or _fdr_listing
        self._rows: list[dict] | None = None

    def listing(self) -> list[dict]:
        """[{code, name, market, industry, settle_month}] for every KRX stock."""
        if self._rows is not None:
            return self._rows
        key = self._cache.key("krx-desc")
        rows = self._cache.get(key, max_age=LISTING_MAX_AGE)
        if rows is None:
            try:
                rows = self._fetch_listing()
                self._cache.put(key, rows)
            except Exception as exc:  # stale beats nothing for slow-moving metadata
                rows = self._cache.get(key)
                if rows is None:
                    raise KRDataError(f"KRX 종목 목록을 받지 못했습니다: {type(exc).__name__}: {str(exc)[:200]}") from None
                logger.warning("KRX listing refresh failed; using the cached copy")
        self._rows = rows
        return rows

    def lookup(self, code: str) -> dict | None:
        code = code.upper()
        return next((r for r in self.listing() if r["code"] == code), None)

    def resolve(self, text: str) -> str:
        """'005930' → '005930', '삼성전자' → '005930', 'KOSPI' → 'KOSPI'."""
        raw = text.strip()
        if is_index(raw):
            return raw.upper()
        if is_code(raw):
            return raw.upper()
        wanted = re.sub(r"\s+", "", raw).lower()
        matches = [r for r in self.listing() if re.sub(r"\s+", "", r["name"]).lower() == wanted]
        if len(matches) == 1:
            return matches[0]["code"]
        if len(matches) > 1:
            raise ValueError(f"종목명 {raw!r}이(가) 여러 종목과 일치합니다: {[m['code'] for m in matches]}")
        raise ValueError(f"종목 {raw!r}을(를) 찾지 못했습니다. 6자리 종목코드(예: 005930)로 입력해 보세요.")


def _fdr_listing() -> list[dict]:
    with contextlib.redirect_stdout(io.StringIO()):
        import FinanceDataReader as fdr

        df = fdr.StockListing("KRX-DESC")
    rows = []
    for rec in df.to_dict("records"):
        code = str(rec.get("Code") or "").strip()
        if not code:
            continue
        rows.append({
            "code": code.upper(),
            "name": str(rec.get("Name") or "").strip(),
            "market": _text(rec.get("Market")),
            "industry": _text(rec.get("Industry")),
            "sector": _text(rec.get("Sector")),
            "settle_month": _settle_month(rec.get("SettleMonth")),
        })
    return rows


def _text(value) -> str | None:
    if value is None or (isinstance(value, float) and value != value):
        return None
    text = str(value).strip()
    return text or None


def _settle_month(value) -> int | None:
    """'12월' → 12."""
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else None
