#!/usr/bin/env python3
"""
Claude API 토큰 사용량 기록 모듈.

각 스크립트에서 API 응답을 받은 직후 record()를 호출하면
data/usage_log.jsonl 에 한 줄씩 누적된다. (스레드 안전)

사용법:
    from usage_log import record
    resp = client.messages.create(...)
    record("한경-기사추출", resp, detail=filename)
"""

import json
import os
import threading
from datetime import datetime, timedelta, timezone

LOG_PATH = "data/usage_log.jsonl"
KST = timezone(timedelta(hours=9))
_lock = threading.Lock()

# 100만 토큰당 USD (입력, 출력)
PRICING = {
    "claude-sonnet-4-6":            (3.00, 15.00),
    "claude-haiku-4-5-20251001":    (1.00,  5.00),
    "claude-opus-4-8":              (5.00, 25.00),
}
_DEFAULT_PRICE = (3.00, 15.00)


def _price_for(model: str) -> tuple[float, float]:
    if model in PRICING:
        return PRICING[model]
    # 버전 접미사가 붙은 모델명 대응 (예: claude-sonnet-4-6-20260101)
    for key, val in PRICING.items():
        if model.startswith(key):
            return val
    return _DEFAULT_PRICE


def estimate_cost(model: str, usage: dict) -> float:
    """캐시 쓰기(1.25x) / 캐시 읽기(0.10x)를 반영한 USD 추정 비용."""
    in_rate, out_rate = _price_for(model)

    plain_in  = usage.get("input_tokens", 0)
    cache_w   = usage.get("cache_creation_input_tokens", 0)
    cache_r   = usage.get("cache_read_input_tokens", 0)
    out       = usage.get("output_tokens", 0)

    cost = (
        plain_in * in_rate
        + cache_w * in_rate * 1.25
        + cache_r * in_rate * 0.10
        + out * out_rate
    ) / 1_000_000
    return round(cost, 6)


def record(label: str, resp, detail: str = "") -> None:
    """API 응답 객체를 받아 사용량을 파일에 append 한다. 실패해도 본 작업을 막지 않는다."""
    try:
        u = resp.usage
        usage = {
            "input_tokens":  getattr(u, "input_tokens", 0) or 0,
            "output_tokens": getattr(u, "output_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens":     getattr(u, "cache_read_input_tokens", 0) or 0,
        }
        model = getattr(resp, "model", "unknown")

        row = {
            "ts": datetime.now(KST).isoformat(timespec="seconds"),
            "date": datetime.now(KST).strftime("%Y-%m-%d"),
            "label": label,
            "detail": detail,
            "model": model,
            "stop_reason": getattr(resp, "stop_reason", None),
            **usage,
            "cost_usd": estimate_cost(model, usage),
        }

        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with _lock:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

        flag = "  ⚠️ max_tokens에서 잘림" if row["stop_reason"] == "max_tokens" else ""
        print(
            f"    [usage] {label}"
            f" in={usage['input_tokens']:,}"
            f" out={usage['output_tokens']:,}"
            f" ${row['cost_usd']:.4f}{flag}"
        )
    except Exception as e:  # 로깅 실패가 파이프라인을 죽이지 않도록
        print(f"    [usage] 기록 실패({label}): {e}")
