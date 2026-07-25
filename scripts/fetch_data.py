#!/usr/bin/env python3
"""
경제 대시보드 시장 데이터 수집 스크립트

포트폴리오 설정: 아래 PORTFOLIO 딕셔너리에서
보유 수량(shares)과 평단가(avg_price)를 본인 값으로 수정하세요.
"""

import json
import os
from datetime import datetime

import pytz
import requests
import yfinance as yf

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
KR_RATE_PATH = "data/kr_rate.json"

# ============================================================
# 포트폴리오 설정 — shares와 avg_price를 실제 값으로 수정
# ============================================================
PORTFOLIO = {
    "010140.KS": {"name": "삼성중공업",            "shares": 165, "avg_price": 30156,  "currency": "KRW"},
    "189300.KS": {"name": "인텔리안테크",           "shares":  25, "avg_price": 168500, "currency": "KRW"},
    "107640.KS": {"name": "한중엔시에스",           "shares":  68, "avg_price": 58800,  "currency": "KRW"},
    "171090.KS": {"name": "선익시스템",             "shares":  45, "avg_price": 91000,  "currency": "KRW"},
    "000720.KS": {"name": "현대건설",               "shares":  25, "avg_price": 160800, "currency": "KRW"},
    "0004G0.KS": {"name": "1Q 미국배당TOP30",       "shares":  63, "avg_price": 9385,   "currency": "KRW"},
    "458730.KS": {"name": "TIGER 미국배당다우존스", "shares":  52, "avg_price": 15198,  "currency": "KRW"},
    "SPYM":      {"name": "SPDR Portfolio S&P 500", "shares":  17, "avg_price": 82.66,  "currency": "USD"},
    "QBTS":      {"name": "디웨이브퀀텀",           "shares":  65, "avg_price": 29.90,  "currency": "USD"},
}


def get_price_data(ticker_str: str) -> dict | None:
    """yfinance로 현재가 / 전일비 / 등락률 조회"""
    try:
        hist = yf.Ticker(ticker_str).history(period="5d")
        if len(hist) < 2:
            return None
        current = float(hist["Close"].iloc[-1])
        prev    = float(hist["Close"].iloc[-2])
        change  = current - prev
        change_pct = (change / prev * 100) if prev else 0.0
        return {
            "price":      round(current,    4),
            "change":     round(change,     4),
            "change_pct": round(change_pct, 4),
        }
    except Exception as e:
        print(f"  [WARN] {ticker_str}: {e}")
        return None


def vix_to_fear_greed(vix: float) -> int:
    """VIX → 0-100 공포탐욕 점수 (역상관 선형 변환)
    VIX 10 → 100(극도의 탐욕), VIX 45 → 0(극도의 공포)
    """
    score = 100 - (vix - 10) * (100 / 35)
    return max(0, min(100, int(score)))


def fg_label(value: int) -> str:
    if value <= 24: return "극도의 공포"
    if value <= 44: return "공포"
    if value <= 55: return "중립"
    if value <= 75: return "탐욕"
    return "극도의 탐욕"


def entry(d: dict | None, key: str = "price") -> dict:
    """None-safe 데이터 딕셔너리 생성"""
    if d:
        return {"price": d["price"], "change": d["change"], "change_pct": d["change_pct"]}
    return {"price": None, "change": None, "change_pct": None}


def index_entry(d: dict | None) -> dict:
    if d:
        return {"value": d["price"], "change": d["change"], "change_pct": d["change_pct"]}
    return {"value": None, "change": None, "change_pct": None}


# ============================================================
# 거시 지표 (FRED API)
# ============================================================

def fred_observations(series_id: str, limit: int = 5) -> list[dict] | None:
    """FRED에서 최신순 관측치 목록을 가져온다. 결측치(".")는 제외. 실패 시 None."""
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        print(f"  [WARN] FRED_API_KEY 미설정 — {series_id} 스킵")
        return None
    try:
        resp = requests.get(
            FRED_OBSERVATIONS_URL,
            params={
                "series_id": series_id,
                "api_key": api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": limit,
            },
            timeout=10,
        )
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        return [o for o in obs if o.get("value") not in (None, ".")]
    except Exception as e:
        print(f"  [WARN] FRED {series_id} 호출 실패: {e}")
        return None


def fred_latest_change(series_id: str) -> tuple[float | None, float | None]:
    """최근 2개 관측치의 (현재값, 직전값)을 반환. 실패 시 (None, None)."""
    obs = fred_observations(series_id, limit=5)
    if not obs or len(obs) < 2:
        return None, None
    return round(float(obs[0]["value"]), 4), round(float(obs[1]["value"]), 4)


def fred_cpi_yoy() -> tuple[float | None, float | None]:
    """CPIAUCSL(월간 지수)로 전년동월비 %를 직접 계산. (현재 YoY, 직전월 YoY) 반환."""
    obs = fred_observations("CPIAUCSL", limit=20)
    if not obs or len(obs) < 14:
        return None, None
    values = [float(o["value"]) for o in obs]
    current_yoy = (values[0] - values[12]) / values[12] * 100
    prev_yoy    = (values[1] - values[13]) / values[13] * 100
    return round(current_yoy, 2), round(prev_yoy, 2)


def load_kr_rate() -> dict | None:
    """data/kr_rate.json에서 한국은행 기준금리 수동 값을 읽는다."""
    try:
        with open(KR_RATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  [WARN] {KR_RATE_PATH} 읽기 실패: {e}")
        return None


def interpret_us_rate(v: float) -> str:
    if v >= 4.5: return "고금리 기조"
    if v >= 3.5: return "중립 수준"
    return "완화적"


def interpret_us_10y(v: float) -> str:
    if v >= 4.5: return "장기 금리 부담 구간"
    if v >= 3.5: return "보통 수준"
    return "낮은 수준"


def interpret_cpi(v: float) -> str:
    if v >= 3: return "목표(2%) 상회, 인플레 압력 존재"
    if v >= 2: return "목표 근접"
    return "저물가 구간"


def interpret_unemployment(v: float) -> str:
    if v >= 4.5: return "고용 둔화 신호"
    if v >= 3.5: return "안정적"
    return "완전고용 근접"


def interpret_kr_rate(kr_rate: float, us_rate: float) -> str:
    diff = us_rate - kr_rate
    status = "역전" if diff > 0 else "정상"
    return f"한미 금리차 {abs(diff):.2f}%p, {status} 구간"


def macro_entry(value, prev, interpretation: str | None) -> dict:
    return {"value": value, "prev": prev, "interpretation": interpretation}


def collect_macro_indicators() -> dict:
    print("  거시 지표 수집 중 (FRED)...")

    us_rate, us_rate_prev = fred_latest_change("DFEDTARU")
    us_10y, us_10y_prev = fred_latest_change("DGS10")
    us_cpi_yoy, us_cpi_yoy_prev = fred_cpi_yoy()
    us_unemployment, us_unemployment_prev = fred_latest_change("UNRATE")

    kr_rate_data = load_kr_rate()
    kr_rate = kr_rate_data.get("rate") if kr_rate_data else None

    return {
        "us_rate": macro_entry(
            us_rate, us_rate_prev,
            interpret_us_rate(us_rate) if us_rate is not None else None,
        ),
        "us_10y": macro_entry(
            us_10y, us_10y_prev,
            interpret_us_10y(us_10y) if us_10y is not None else None,
        ),
        "us_cpi_yoy": macro_entry(
            us_cpi_yoy, us_cpi_yoy_prev,
            interpret_cpi(us_cpi_yoy) if us_cpi_yoy is not None else None,
        ),
        "us_unemployment": macro_entry(
            us_unemployment, us_unemployment_prev,
            interpret_unemployment(us_unemployment) if us_unemployment is not None else None,
        ),
        "kr_rate": {
            "value": kr_rate,
            "effective_date": kr_rate_data.get("effective_date") if kr_rate_data else None,
            "interpretation": (
                interpret_kr_rate(kr_rate, us_rate)
                if kr_rate is not None and us_rate is not None else None
            ),
        },
    }


# ============================================================
# 종합 시장 진단 (규칙 기반, Claude API 미사용)
# ============================================================

_TIGHTENING_PHRASES = {
    "고금리 기조",
    "장기 금리 부담 구간",
    "목표(2%) 상회, 인플레 압력 존재",
    "완전고용 근접",
}
_EASING_PHRASES = {
    "완화적",
    "낮은 수준",
    "저물가 구간",
    "고용 둔화 신호",
}


def classify_signal(interpretation: str) -> str:
    """지표 해석 문구 → 긴축 신호 / 완화 신호 / 중립"""
    if interpretation in _TIGHTENING_PHRASES:
        return "긴축 신호"
    if interpretation in _EASING_PHRASES:
        return "완화 신호"
    return "중립"


def diagnose_market(macro: dict) -> dict:
    """macro_indicators를 규칙 기반으로 집계해 국면 태그 + 요약 문장 생성."""
    if all(ind.get("value") is None for ind in macro.values()):
        return {"phase": "데이터 없음", "summary": "지표 수집 실패"}

    signals = [
        classify_signal(ind["interpretation"])
        for ind in macro.values()
        if ind.get("value") is not None and ind.get("interpretation")
    ]
    tighten = signals.count("긴축 신호")
    ease = signals.count("완화 신호")

    if tighten > ease:
        phase = "긴축 국면"
    elif ease > tighten:
        phase = "완화 기대"
    else:
        phase = "관망세"

    def fmt(v: float | None) -> str:
        return f"{v:.2f}" if v is not None else "N/A"

    us_rate    = macro.get("us_rate", {}).get("value")
    kr_rate    = macro.get("kr_rate", {}).get("value")
    cpi        = macro.get("us_cpi_yoy", {}).get("value")
    cpi_interp = macro.get("us_cpi_yoy", {}).get("interpretation")
    gap = round(us_rate - kr_rate, 2) if us_rate is not None and kr_rate is not None else None

    summary = (
        f"현재는 {phase} 구간입니다. "
        f"미국 기준금리 {fmt(us_rate)}%, 한국 기준금리 {fmt(kr_rate)}%로 "
        f"금리차 {fmt(gap)}%p이며, "
        f"CPI는 {fmt(cpi)}%로 {cpi_interp or '데이터 없음'} 상태입니다."
    )

    return {"phase": phase, "summary": summary}


def main() -> None:
    kst = pytz.timezone("Asia/Seoul")
    updated_at = datetime.now(kst).strftime("%Y-%m-%d %H:%M KST")
    print(f"[{updated_at}] 데이터 수집 시작")

    print("  환율 수집 중...")
    ex_raw = get_price_data("USDKRW=X")
    exchange_rate = {
        "usd_krw":    ex_raw["price"]      if ex_raw else None,
        "change":     ex_raw["change"]     if ex_raw else None,
        "change_pct": ex_raw["change_pct"] if ex_raw else None,
    }

    print("  미국 지수 수집 중...")
    indices = {
        "sp500":  index_entry(get_price_data("^GSPC")),
        "nasdaq": index_entry(get_price_data("^IXIC")),
        "dow":    index_entry(get_price_data("^DJI")),
    }

    print("  한국 지수 수집 중...")
    indices["kospi"]  = index_entry(get_price_data("^KS11"))
    indices["kosdaq"] = index_entry(get_price_data("^KQ11"))

    print("  원자재 수집 중...")
    commodities = {
        "gold":        entry(get_price_data("GC=F")),
        "silver":      entry(get_price_data("SI=F")),
        "crude_oil":   entry(get_price_data("CL=F")),
        "natural_gas": entry(get_price_data("NG=F")),
    }

    print("  VIX 수집 중...")
    vix_raw = get_price_data("^VIX")
    vix = index_entry(vix_raw)
    if vix_raw:
        fg_val = vix_to_fear_greed(vix_raw["price"])
        fear_greed = {"value": fg_val, "label": fg_label(fg_val)}
    else:
        fear_greed = {"value": None, "label": None}

    print("  포트폴리오 수집 중...")
    portfolio = []
    for ticker, cfg in PORTFOLIO.items():
        d = get_price_data(ticker)
        price    = d["price"] if d else None
        shares   = cfg["shares"]
        avg      = cfg["avg_price"]
        currency = cfg.get("currency", "KRW")
        decimals = 2 if currency == "USD" else 0
        value  = round(price * shares, decimals) if price and shares > 0 else 0
        pl     = round((price - avg) * shares, decimals) if price and shares > 0 and avg > 0 else 0
        pl_pct = round((price - avg) / avg * 100, 4) if price and avg > 0 else 0
        portfolio.append({
            "ticker":     ticker,
            "name":       cfg["name"],
            "currency":   currency,
            "price":      round(price, 2) if price else None,
            "change":     round(d["change"], 2) if d else None,
            "change_pct": round(d["change_pct"], 4) if d else None,
            "shares":     shares,
            "avg_price":  avg,
            "value":      value,
            "pl":         pl,
            "pl_pct":     pl_pct,
        })

    macro_indicators = collect_macro_indicators()
    market_diagnosis = diagnose_market(macro_indicators)

    data = {
        "updated_at":        updated_at,
        "exchange_rate":     exchange_rate,
        "indices":           indices,
        "commodities":       commodities,
        "vix":               vix,
        "fear_greed":        fear_greed,
        "portfolio":         portfolio,
        "macro_indicators":  macro_indicators,
        "market_diagnosis":  market_diagnosis,
        "briefing":          "",   # generate_comments.py가 채움
    }

    os.makedirs("data", exist_ok=True)
    with open("data/market_data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"완료: data/market_data.json 저장 ({updated_at})")


if __name__ == "__main__":
    main()
