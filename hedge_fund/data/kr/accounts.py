"""Pull named items out of one DART fnlttSinglAcntAll response.

Which column holds the number (verified in the OpenDART guide):

- thstrm_amount      당기금액. 분/반기 보고서의 (포괄)손익계산서에서는 **3개월** 금액.
- thstrm_add_amount  당기누적금액 (분/반기 손익계산서의 누적).
- 현금흐름표는 분/반기에도 누적으로만 공시되므로 thstrm_amount가 누적이다.
- 사업보고서(11011)의 thstrm_amount는 연간(=4분기 누적) 금액이다.

So for a *flow* item this module always returns the **year-to-date
cumulative** amount; fundamentals.py turns cumulative into single quarters
by subtraction (Q4 = 연간 − 3분기 누적). A *stock* item is the period-end
balance.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

ACCOUNTS_PATH = Path(__file__).resolve().parent / "accounts.yaml"
ANNUAL_REPORT = "11011"

_NUMBERING = re.compile(r"^(?:[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\.?|\d+\.|\(\d+\)|[가-하]\.)")


@lru_cache(maxsize=1)
def account_map() -> dict[str, dict]:
    return yaml.safe_load(ACCOUNTS_PATH.read_text(encoding="utf-8"))


def normalize_name(name: str) -> str:
    """'Ⅰ. 매출액 ' → '매출액': drop whitespace and leading numbering."""
    text = re.sub(r"\s+", "", name or "")
    return _NUMBERING.sub("", text)


def parse_amount(value) -> float | None:
    """'1,234' → 1234.0, '-1,234' → -1234.0, '' / '-' / None → None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    if text in ("", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _value(row: dict, kind: str, reprt_code: str) -> float | None:
    if kind == "stock":
        return parse_amount(row.get("thstrm_amount"))
    if row.get("sj_div") in ("IS", "CIS") and reprt_code != ANNUAL_REPORT:
        cumulative = parse_amount(row.get("thstrm_add_amount"))
        return cumulative if cumulative is not None else parse_amount(row.get("thstrm_amount"))
    amount = parse_amount(row.get("thstrm_amount"))
    return amount if amount is not None else parse_amount(row.get("thstrm_add_amount"))


def _ord(row: dict) -> int:
    try:
        return int(row.get("ord") or 0)
    except (TypeError, ValueError):
        return 0


def find_row(rows: list[dict], spec: dict) -> dict | None:
    """The row for one item: account_id first, then account_nm candidates,
    statement by statement in spec order; the earliest ``ord`` wins."""
    names = {normalize_name(n) for n in spec.get("names", [])}
    ids = set(spec.get("ids", []))
    for sj in spec["sj"]:
        candidates = sorted((r for r in rows if r.get("sj_div") == sj), key=_ord)
        by_id = [r for r in candidates if r.get("account_id") in ids]
        if by_id:
            return by_id[0]
        by_name = [r for r in candidates if normalize_name(r.get("account_nm", "")) in names]
        if by_name:
            return by_name[0]
    return None


def extract_items(rows: list[dict], reprt_code: str, mapping: dict[str, dict] | None = None) -> dict[str, float | None]:
    """{item: value} for every item in the mapping (None when not found).
    Flow items are year-to-date cumulative; see the module docstring."""
    mapping = mapping or account_map()
    items: dict[str, float | None] = {}
    for item, spec in mapping.items():
        row = find_row(rows, spec)
        items[item] = _value(row, spec["kind"], reprt_code) if row is not None else None
    return items


def flow_items(mapping: dict[str, dict] | None = None) -> list[str]:
    mapping = mapping or account_map()
    return [k for k, v in mapping.items() if v["kind"] == "flow"]


def stock_items(mapping: dict[str, dict] | None = None) -> list[str]:
    mapping = mapping or account_map()
    return [k for k, v in mapping.items() if v["kind"] == "stock"]
