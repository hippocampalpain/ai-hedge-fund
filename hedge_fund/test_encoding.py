"""Every text file the program (and its tests) reads or writes names its
encoding. Without it Python uses the locale's code page — cp949 on a Korean
Windows PC — and a UTF-8 file with Korean in it (example_kr.yaml, the DART
cache, a ledger) fails with "'cp949' codec can't decode byte …" unless
PYTHONUTF8=1 is set. This scan keeps it that way on every platform."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_METHODS = {"read_text", "write_text"}


def _mode(call: ast.Call, position: int) -> str | None:
    if len(call.args) > position and isinstance(call.args[position], ast.Constant):
        return call.args[position].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def _missing_encoding(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or "encoding" in {k.arg for k in node.keywords}:
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr in TEXT_METHODS:
            found.append(f"{path.relative_to(ROOT)}:{node.lineno} .{fn.attr}()")
        elif isinstance(fn, ast.Attribute) and fn.attr == "open" and "b" not in (_mode(node, 0) or ""):
            found.append(f"{path.relative_to(ROOT)}:{node.lineno} .open()")
        elif isinstance(fn, ast.Name) and fn.id == "open" and "b" not in (_mode(node, 1) or ""):
            found.append(f"{path.relative_to(ROOT)}:{node.lineno} open()")
    return found


def test_every_text_read_and_write_names_utf8():
    files = sorted((ROOT / "hedge_fund").rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py"))
    missing = [hit for f in files for hit in _missing_encoding(f)]
    assert not missing, "add encoding=\"utf-8\":\n" + "\n".join(missing)

