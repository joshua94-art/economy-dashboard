#!/usr/bin/env python3
"""
블로그 초안의 수치(금액·퍼센트)가 원문에 실제로 있는지 대조하는 모듈.

Claude API를 호출하지 않습니다 — 정규식으로 수치를 뽑아 단위를 정규화한 뒤 비교합니다.
  - "$28 Billion" = "280억 달러" = 2.8e10 USD 로 같은 값 취급
  - 통화가 다르면 불일치 ("4兆弗" ≠ "4조원")
  - 표기 자릿수만큼 반올림 허용 ("5.80%" ≈ 5.8031, "1,394원" ≈ 1394.02)

금액·퍼센트만 검사합니다. 연도, 날짜, 지수 포인트, 사람 수 같은 맨 숫자는 건너뜁니다.

사용법:
    from number_check import check_post
    issues, total = check_post(post, sources=[news_content, wsj_text], market=market)
"""

import json
import re

NUM = r"\d[\d,]*(?:\.\d+)?"
KUNIT = r"[십백천]?[만억조兆]|천"

_KR_DIGIT = {"십": 10, "백": 100, "천": 1_000}
_KR_BASE = {"만": 10**4, "억": 10**8, "조": 10**12, "兆": 10**12}
_EN_UNIT = {"thousand": 10**3, "million": 10**6, "m": 10**6,
            "billion": 10**9, "bn": 10**9, "trillion": 10**12, "tn": 10**12}
_CURRENCY = {"$": "USD", "달러": "USD", "弗": "USD", "dollar": "USD", "dollars": "USD",
             "원": "KRW", "엔": "JPY", "유로": "EUR", "위안": "CNY"}
_PCT = {"%": "%", "퍼센트": "%", "%p": "%p", "%포인트": "%p", "퍼센트포인트": "%p", "bp": "bp"}

# 순서가 중요: 앞 패턴이 차지한 구간은 뒤 패턴이 다시 잡지 않는다.
_PATTERNS = [
    ("pct", re.compile(rf"({NUM})\s*(퍼센트포인트|%포인트|%p|%|퍼센트|bp)")),
    ("usd", re.compile(rf"\$\s?({NUM})\s*(trillion|billion|million|thousand|bn|tn|조|억|만)?", re.I)),
    ("en",  re.compile(rf"({NUM})\s*(trillion|billion|million)\s*(dollars?)?", re.I)),
    ("kr",  re.compile(rf"({NUM})\s*({KUNIT})(?:\s*({NUM})\s*({KUNIT}))?\s*(원|달러|弗|엔|유로|위안)?")),
    ("cur", re.compile(rf"({NUM})\s*(원|달러|弗|엔|유로|위안)")),
]


def _num(s: str) -> tuple[float, int]:
    """'1,394.02' → (1394.02, 2). 두 번째 값은 소수 자릿수."""
    s = s.replace(",", "")
    return float(s), len(s.split(".")[1]) if "." in s else 0


def _kr_mult(unit: str) -> int:
    if unit in ("천",):
        return 1_000
    digit = _KR_DIGIT.get(unit[0], 1) if len(unit) == 2 else 1
    return digit * _KR_BASE[unit[-1]]


def extract(text: str) -> list[dict]:
    """텍스트에서 금액·퍼센트를 뽑아 {raw, kind, value, unit, tol, start, end} 목록으로 반환."""
    found, taken = [], []
    for kind, pat in _PATTERNS:
        for m in pat.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            q = _parse(kind, m)
            if q is None:
                continue
            q.update(raw=m.group(0).strip(), start=m.start(), end=m.end())
            found.append(q)
            taken.append((m.start(), m.end()))
    return sorted(found, key=lambda q: q["start"])


def _parse(kind: str, m: re.Match) -> dict | None:
    g = m.groups()
    if kind == "pct":
        v, d = _num(g[0])
        return {"kind": "pct", "unit": _PCT[g[1]], "value": v, "tol": 0.5 * 10**-d}
    if kind == "usd":
        v, d = _num(g[0])
        u = (g[1] or "").lower()
        mult = _EN_UNIT.get(u) or (_KR_BASE.get(u) if u else 1)
        return {"kind": "money", "unit": "USD", "value": v * mult, "tol": 0.5 * 10**-d * mult}
    if kind == "en":
        v, d = _num(g[0])
        mult = _EN_UNIT[g[1].lower()]
        cur = "USD" if g[2] else None
        return {"kind": "money", "unit": cur, "value": v * mult, "tol": 0.5 * 10**-d * mult}
    if kind == "kr":
        v1, d1 = _num(g[0])
        m1 = _kr_mult(g[1])
        value, tol = v1 * m1, 0.5 * 10**-d1 * m1
        if g[2]:
            v2, d2 = _num(g[2])
            m2 = _kr_mult(g[3])
            value += v2 * m2
            tol = 0.5 * 10**-d2 * m2
        cur = _CURRENCY.get(g[4]) if g[4] else None
        # 통화 없는 '3천', '1000만' 은 사람 수·건수일 가능성이 커서 제외
        if cur is None and g[1][-1] not in "억조兆":
            return None
        return {"kind": "money", "unit": cur, "value": value, "tol": tol}
    if kind == "cur":
        v, d = _num(g[0])
        return {"kind": "money", "unit": _CURRENCY[g[1]], "value": v, "tol": 0.5 * 10**-d}
    return None


def _bare_numbers(obj) -> list[float]:
    """market_data 의 숫자 값(단위 없음)을 절댓값으로 모은다."""
    out = []
    if isinstance(obj, dict):
        for v in obj.values():
            out += _bare_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _bare_numbers(v)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append(abs(float(obj)))
    return out


def _matches(q: dict, src: list[dict], bare: list[float]) -> bool:
    eps = 1e-9 * max(1.0, q["value"])
    for s in src:
        if s["kind"] != q["kind"]:
            continue
        if q["kind"] == "pct" and s["unit"] != q["unit"]:
            continue
        if q["kind"] == "money" and q["unit"] and s["unit"] and q["unit"] != s["unit"]:
            continue
        if abs(q["value"] - s["value"]) <= max(q["tol"], s["tol"]) + eps:
            return True
    # market_data 는 숫자에 단위가 없으므로, 배수 없는 금액과 퍼센트만 값으로 비교
    if q["kind"] == "pct" or q["value"] < 10**4:
        return any(abs(q["value"] - b) <= q["tol"] + eps for b in bare)
    return False


def _fields(post: dict):
    """(필드 경로, 문자열) 을 순회. posts 배열(하루 여러 편)과 예전 단일 글 형식 모두 지원."""
    for key in ("title", "body"):
        if isinstance(post.get(key), str):
            yield key, post[key]
    for i, p in enumerate(post.get("posts") or []):
        for key in ("title", "body"):
            if isinstance((p or {}).get(key), str):
                yield f"posts[{i}].{key}", p[key]
    for key in ("industry_candidates", "company_candidates"):
        for i, c in enumerate(post.get(key) or []):
            for k, v in (c or {}).items():
                if isinstance(v, str):
                    yield f"{key}[{i}].{k}", v


def check_post(post: dict, sources: list[str], market: dict) -> tuple[list[dict], int]:
    """초안의 수치 중 원문에서 확인되지 않은 것을 반환. (불일치 목록, 검사한 수치 총수)"""
    src_text = "\n".join(sources) + "\n" + json.dumps(market, ensure_ascii=False)
    src = extract(src_text)
    bare = _bare_numbers(market)

    issues, total = [], 0
    for field, text in _fields(post):
        for q in extract(text):
            total += 1
            if not _matches(q, src, bare):
                ctx = text[max(0, q["start"] - 25):q["end"] + 15].replace("\n", " ")
                issues.append({"field": field, "raw": q["raw"], "context": ctx})
    return issues, total
