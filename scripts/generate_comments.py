#!/usr/bin/env python3
"""
Claude API로 시장 데이터 기반 AI 브리핑 생성
ANTHROPIC_API_KEY 환경변수가 필요합니다.
"""

import json
import os

import anthropic

DATA_PATH = "data/market_data.json"

SYSTEM_PROMPT = """당신은 경제·금융 전문 분석가입니다. 매일 아침 투자자들을 위한 간결하고 통찰력 있는 시장 브리핑을 작성합니다.

작성 원칙:
- 300~450자 분량으로 핵심만 서술
- 숫자 나열보다 흐름과 맥락 중심
- 전일 주요 변동의 배경·영향 분석
- 오늘 주목할 리스크와 기회 포인트 제시
- 전문적이지만 읽기 쉬운 한국어"""


def load_data() -> dict:
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict) -> None:
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def pct(val) -> str:
    if val is None:
        return "N/A"
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.2f}%"


def price_line(label: str, value, change_pct, unit: str = "") -> str:
    if value is None:
        return f"- {label}: 데이터 없음"
    return f"- {label}: {value:,.2f}{unit} ({pct(change_pct)})"


def build_prompt(d: dict) -> str:
    ex  = d.get("exchange_rate", {})
    idx = d.get("indices", {})
    com = d.get("commodities", {})
    fg  = d.get("fear_greed", {})
    vix = d.get("vix", {})
    pf  = d.get("portfolio", [])

    lines = [
        f"기준 시각: {d.get('updated_at', 'N/A')}",
        "",
        "## 환율",
        price_line("원/달러", ex.get("usd_krw"), ex.get("change_pct"), "원"),
        "",
        "## 미국 지수",
        price_line("S&P 500", idx.get("sp500", {}).get("value"), idx.get("sp500", {}).get("change_pct")),
        price_line("NASDAQ",  idx.get("nasdaq", {}).get("value"), idx.get("nasdaq", {}).get("change_pct")),
        price_line("다우존스", idx.get("dow", {}).get("value"),  idx.get("dow", {}).get("change_pct")),
        "",
        "## 한국 지수",
        price_line("KOSPI",  idx.get("kospi", {}).get("value"),  idx.get("kospi", {}).get("change_pct")),
        price_line("KOSDAQ", idx.get("kosdaq", {}).get("value"), idx.get("kosdaq", {}).get("change_pct")),
        "",
        "## 원자재 / 귀금속",
        price_line("금",     com.get("gold", {}).get("price"),        com.get("gold", {}).get("change_pct"),        "$"),
        price_line("은",     com.get("silver", {}).get("price"),      com.get("silver", {}).get("change_pct"),      "$"),
        price_line("WTI 원유", com.get("crude_oil", {}).get("price"), com.get("crude_oil", {}).get("change_pct"),   "$"),
        price_line("천연가스", com.get("natural_gas", {}).get("price"), com.get("natural_gas", {}).get("change_pct"), "$"),
        "",
        "## 시장 심리",
        f"- VIX: {vix.get('value', 'N/A')}",
        f"- 공포탐욕 지수: {fg.get('value', 'N/A')} ({fg.get('label', 'N/A')})",
    ]

    holdings = [s for s in pf if s.get("shares", 0) > 0 and s.get("price")]
    if holdings:
        lines += ["", "## 포트폴리오"]
        for s in holdings:
            pl_str = f" | 평가손익: {'+' if s['pl'] >= 0 else ''}{s['pl']:,.0f}원 ({pct(s['pl_pct'])})" \
                     if s.get("avg_price", 0) > 0 else ""
            lines.append(
                f"- {s['name']}: {s['price']:,.0f}원 ({pct(s['change_pct'])}){pl_str}"
            )

    return "\n".join(lines)


def generate_briefing(data: dict) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": f"아래 시장 데이터를 바탕으로 오늘의 아침 경제 브리핑을 작성해주세요.\n\n[시장 데이터]\n{build_prompt(data)}",
            }
        ],
    )
    return response.content[0].text


def main() -> None:
    print("AI 브리핑 생성 중...")
    data = load_data()
    briefing = generate_briefing(data)
    data["briefing"] = briefing
    save_data(data)
    print("완료: 브리핑 저장")
    print(f"\n--- 브리핑 미리보기 ---\n{briefing[:300]}...")


if __name__ == "__main__":
    main()
