#!/usr/bin/env python3
"""
Claude API로 시장 데이터 기반 섹션 코멘트 + 전체 브리핑 생성
ANTHROPIC_API_KEY 환경변수가 필요합니다.
"""

import json
import os

import anthropic

DATA_PATH = "data/market_data.json"

SYSTEM_PROMPT = """당신은 경제·금융 전문 분석가입니다. 매일 아침 투자자들을 위한 간결하고 통찰력 있는 시장 분석을 제공합니다.

분석 원칙:
- 숫자 나열보다 흐름과 맥락 중심으로 서술
- 전일 주요 변동의 배경과 영향 분석
- 오늘 주목할 리스크와 기회 포인트 제시
- 전문적이지만 읽기 쉬운 한국어 사용"""


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
        price_line("S&P 500", idx.get("sp500",  {}).get("value"), idx.get("sp500",  {}).get("change_pct")),
        price_line("NASDAQ",  idx.get("nasdaq", {}).get("value"), idx.get("nasdaq", {}).get("change_pct")),
        price_line("다우존스", idx.get("dow",   {}).get("value"), idx.get("dow",    {}).get("change_pct")),
        "",
        "## 한국 지수",
        price_line("KOSPI",  idx.get("kospi",  {}).get("value"), idx.get("kospi",  {}).get("change_pct")),
        price_line("KOSDAQ", idx.get("kosdaq", {}).get("value"), idx.get("kosdaq", {}).get("change_pct")),
        "",
        "## 원자재 / 귀금속",
        price_line("금",       com.get("gold",        {}).get("price"), com.get("gold",        {}).get("change_pct"), "$"),
        price_line("은",       com.get("silver",      {}).get("price"), com.get("silver",      {}).get("change_pct"), "$"),
        price_line("WTI 원유", com.get("crude_oil",   {}).get("price"), com.get("crude_oil",   {}).get("change_pct"), "$"),
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
            pl_str = (
                f" | 평가손익: {'+' if s['pl'] >= 0 else ''}{s['pl']:,.0f}원 ({pct(s['pl_pct'])})"
                if s.get("avg_price", 0) > 0 else ""
            )
            lines.append(f"- {s['name']}: {s['price']:,.0f}원 ({pct(s['change_pct'])}){pl_str}")

    return "\n".join(lines)


def generate_all_comments(data: dict) -> dict:
    """섹션 코멘트(한국·미국 지수)와 전체 브리핑을 한 번의 API 호출로 생성"""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    user_msg = f"""아래 시장 데이터를 분석하여 반드시 JSON 형식으로만 응답하세요. 마크다운·설명 없이 JSON 객체만 출력하세요.

{{
  "kr_comment": "KOSPI/KOSDAQ 섹션 바로 아래 표시할 2~3문장 분석. 흐름과 특징적 움직임 중심.",
  "us_comment": "S&P500/NASDAQ/DOW 섹션 바로 아래 표시할 2~3문장 분석. 전일 미국 시장 주요 재료와 방향성 중심.",
  "briefing": "전체 시장 아침 브리핑 300~450자. 환율·원자재·공포탐욕 지수 흐름 포함."
}}

[시장 데이터]
{build_prompt(data)}"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        system=[
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = response.content[0].text.strip()
    # 마크다운 코드블록이 포함된 경우 제거
    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"):
            raw = raw[4:]

    return json.loads(raw.strip())


def main() -> None:
    print("AI 코멘트 생성 중...")
    data = load_data()

    result = generate_all_comments(data)

    data["briefing"] = result.get("briefing", "")
    data["section_comments"] = {
        "kr_indices": result.get("kr_comment", ""),
        "us_indices": result.get("us_comment", ""),
    }

    save_data(data)
    print("완료: 섹션 코멘트 및 브리핑 저장")
    print(f"\n[KR 코멘트]\n{result.get('kr_comment', '')}")
    print(f"\n[US 코멘트]\n{result.get('us_comment', '')}")
    print(f"\n[브리핑 미리보기]\n{result.get('briefing', '')[:250]}...")


if __name__ == "__main__":
    main()
