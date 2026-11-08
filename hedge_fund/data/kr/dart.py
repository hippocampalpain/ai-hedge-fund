"""OpenDART REST client — only the four endpoints Korean mode needs.

Verified against the OpenDART developer guide (opendart.fss.or.kr/guide):

    corpCode.xml          ZIP → CORPCODE.xml: corp_code, corp_name, stock_code
    fnlttSinglAcntAll     단일회사 전체 재무제표 (corp_code, bsns_year, reprt_code, fs_div)
    stockTotqySttus       주식의 총수 현황 (istc_totqy, tesstk_co, distb_stock_co, se)
    elestock              임원·주요주주 소유보고 (rcept_dt, repror, sp_stock_lmp_cnt …)

Status codes: 000 정상, 013 조회된 데이터 없음 (→ empty, a data fact),
020 요청 제한 초과, 010/011/012/901 키·IP 문제 (→ KRDataError, fail loud).

The API key is sent as the ``crtfc_key`` query parameter, so request URLs
contain it: every error message is scrubbed before it leaves this module,
and nothing here logs a URL.
"""

from __future__ import annotations

import io
import logging
import os
import xml.etree.ElementTree as ET
import zipfile
from datetime import date

import requests

from hedge_fund.data.kr.store import DiskCache, KRDataError, scrub, Throttle, with_retry
from hedge_fund.data.provider import DART_KEY_ENV

logger = logging.getLogger(__name__)

BASE_URL = "https://opendart.fss.or.kr/api"

# 보고서 코드 → 회계연도 안에서 몇 번째 분기를 끝내는 보고서인지
REPORT_CODES = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}
QUARTER_TO_REPORT = {q: code for code, q in REPORT_CODES.items()}

NO_DATA = "013"
CORP_CODES_MAX_AGE = 30 * 86400
SHORT_TTL = 86400  # a report that may still be filed: ask again after a day


class _Retryable(Exception):
    """A transient HTTP failure worth another attempt."""


class DartClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        cache: DiskCache | None = None,
        min_interval: float | None = None,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        self._key = api_key if api_key is not None else os.environ.get(DART_KEY_ENV, "")
        self._cache = cache or DiskCache("dart")
        self._throttle = Throttle(min_interval if min_interval is not None
                                  else float(os.environ.get("DART_MIN_INTERVAL", "0.3")))
        self._timeout = timeout
        self._session = session or requests.Session()

    def close(self) -> None:
        self._session.close()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    def corp_codes(self) -> list[dict]:
        """Every listed company: {corp_code, corp_name, stock_code}. The ZIP
        is ~30 MB of XML once a month; the parsed list is cached as JSON."""
        key = self._cache.key("corpCode")
        hit = self._cache.get(key, max_age=CORP_CODES_MAX_AGE)
        if hit is not None:
            return hit
        raw = self._get_bytes("corpCode.xml", {})
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                xml_bytes = archive.read(archive.namelist()[0])
        except zipfile.BadZipFile:
            # An error comes back as a JSON/XML status body instead of a ZIP.
            raise KRDataError(f"DART corpCode.xml did not return a ZIP: {scrub(raw[:200].decode('utf-8', 'replace'), [self._key])}")
        rows = []
        for node in ET.fromstring(xml_bytes).iter("list"):
            stock = (node.findtext("stock_code") or "").strip()
            if stock:  # listed companies only
                rows.append({
                    "corp_code": (node.findtext("corp_code") or "").strip(),
                    "corp_name": (node.findtext("corp_name") or "").strip(),
                    "stock_code": stock,
                })
        self._cache.put(key, rows)
        return rows

    def financial_statements(self, corp_code: str, year: int, reprt_code: str, fs_div: str) -> list[dict]:
        """All accounts of one report ([] when DART has none)."""
        return self._list("fnlttSinglAcntAll.json", {
            "corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt_code, "fs_div": fs_div,
        }, final_if_empty=_report_is_final(year))

    def share_counts(self, corp_code: str, year: int, reprt_code: str) -> list[dict]:
        """주식의 총수 현황 rows for one report ([] when none)."""
        return self._list("stockTotqySttus.json", {
            "corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt_code,
        }, final_if_empty=_report_is_final(year))

    def company(self, corp_code: str) -> dict | None:
        """기업개황 (company.json): corp_name, stock_name, induty_code, acc_mt
        (결산월 MM) … — fields at the top level, not under ``list``."""
        key = self._cache.key("company.json", corp_code)
        hit = self._cache.get(key, max_age=CORP_CODES_MAX_AGE)
        if hit is not None:
            return hit or None
        body = self._get_json("company.json", {"corp_code": corp_code})
        status = str(body.get("status", ""))
        if status == NO_DATA:
            info: dict = {}
        elif status == "000":
            info = {k: v for k, v in body.items() if k not in ("status", "message")}
        else:
            raise KRDataError(f"DART company.json status {status}: {scrub(str(body.get('message', ''))[:200], [self._key])}")
        self._cache.put(key, info)
        return info or None

    def insider_reports(self, corp_code: str) -> list[dict]:
        """임원·주요주주 소유보고, all dates. New filings arrive daily."""
        return self._list("elestock.json", {"corp_code": corp_code}, ttl=SHORT_TTL)

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _list(self, endpoint: str, params: dict, *, final_if_empty: bool = False,
              ttl: float | None = None) -> list[dict]:
        key = self._cache.key(endpoint, params)
        hit = self._cache.get(key)
        if hit is not None:
            # A filed report is final. "No data yet" for a recent year, or a
            # feed that grows (elestock), is asked again after a day.
            if ttl is None and (hit or final_if_empty):
                return hit
            if self._cache.get(key, max_age=ttl or SHORT_TTL) is not None:
                return hit
        body = self._get_json(endpoint, params)
        status = str(body.get("status", ""))
        if status == NO_DATA:
            rows: list[dict] = []
        elif status == "000":
            rows = body.get("list") or []
        else:
            raise KRDataError(
                f"DART {endpoint} status {status}: {scrub(str(body.get('message', ''))[:200], [self._key])}"
            )
        self._cache.put(key, rows)
        return rows

    def _require_key(self) -> None:
        if not self._key:
            raise KRDataError(f"{DART_KEY_ENV} is not set — add it to .env (https://opendart.fss.or.kr 에서 무료 발급)")

    def _get(self, endpoint: str, params: dict) -> requests.Response:
        self._require_key()

        def call() -> requests.Response:
            self._throttle.wait()
            resp = self._session.get(f"{BASE_URL}/{endpoint}", params={"crtfc_key": self._key, **params},
                                     timeout=self._timeout)
            if resp.status_code >= 500 or resp.status_code == 429:
                raise _Retryable(f"HTTP {resp.status_code}")
            if resp.status_code >= 400:
                raise KRDataError(f"DART {endpoint} returned HTTP {resp.status_code}")
            return resp

        return with_retry(call, what=f"DART {endpoint}", retry_on=(requests.RequestException, _Retryable),
                          secrets=[self._key])

    def _get_json(self, endpoint: str, params: dict) -> dict:
        resp = self._get(endpoint, params)
        try:
            body = resp.json()
        except ValueError:
            raise KRDataError(f"DART {endpoint} returned non-JSON") from None
        status = str(body.get("status", ""))
        if status == "020":
            raise KRDataError("DART 요청 제한 초과(020): 하루 20,000건 한도. 내일 다시 실행하거나 캐시를 활용하세요.")
        return body

    def _get_bytes(self, endpoint: str, params: dict) -> bytes:
        return self._get(endpoint, params).content


def _report_is_final(year: int, today: date | None = None) -> bool:
    """Every report for fiscal *year* is filed by the end of the next year's
    first quarter, so "no data" two years later is a settled fact."""
    return year <= (today or date.today()).year - 2


def receipt_date(row: dict) -> str | None:
    """The disclosure date: the first 8 digits of rcept_no (YYYYMMDD), the
    day DART accepted the filing. This is the point-in-time key."""
    no = str(row.get("rcept_no") or "")
    if len(no) >= 8 and no[:8].isdigit():
        return f"{no[:4]}-{no[4:6]}-{no[6:8]}"
    return None
