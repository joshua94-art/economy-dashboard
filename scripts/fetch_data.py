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
import yfinance as yf

# ============================================================
# 포트폴리오 설정 — shares와 avg_price를 실제 값으로 수정
# ============================================================
PORTFOLIO = {
    "010140.KS": {"name": "삼성중공업",   "shares": 165, "avg_price": 30156},
    "189300.KS": {"name": "인텔리안테크", "shares":  25, "avg_price": 168500},
    "329180.KS": {"name": "한중엔시에스", "shares":  68, "avg_price": 58800},
    "036710.KS": {"name": "선익시스템",   "shares":  45, "avg_price": 91000},
    "000720.KS": {"name": "현대건설",     "shares":  25, "avg_price": 160800},
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
        price  = d["price"] if d else None
        shares = cfg["shares"]
        avg    = cfg["avg_price"]
        value  = round(price * shares, 0) if price and shares > 0 else 0
        pl     = round((price - avg) * shares, 0) if price and shares > 0 and avg > 0 else 0
        pl_pct = round((price - avg) / avg * 100, 4) if price and avg > 0 else 0
        portfolio.append({
            "ticker":     ticker,
            "name":       cfg["name"],
            "price":      round(price, 2) if price else None,
            "change":     round(d["change"], 2) if d else None,
            "change_pct": round(d["change_pct"], 4) if d else None,
            "shares":     shares,
            "avg_price":  avg,
            "value":      value,
            "pl":         pl,
            "pl_pct":     pl_pct,
        })

    data = {
        "updated_at":    updated_at,
        "exchange_rate": exchange_rate,
        "indices":       indices,
        "commodities":   commodities,
        "vix":           vix,
        "fear_greed":    fear_greed,
        "portfolio":     portfolio,
        "briefing":      "",   # generate_comments.py가 채움
    }

    os.makedirs("data", exist_ok=True)
    with open("data/market_data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"완료: data/market_data.json 저장 ({updated_at})")


if __name__ == "__main__":
    main()
