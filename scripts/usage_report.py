#!/usr/bin/env python3
"""
data/usage_log.jsonl 을 집계해 오늘 실행분 요약을 출력하고,
GitHub Actions 실행 요약(Summary) 탭에 표로 남긴다.

워크플로 마지막 단계에서 실행:
    python scripts/usage_report.py
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

LOG_PATH = "data/usage_log.jsonl"
KST = timezone(timedelta(hours=9))


def load_rows() -> list[dict]:
    if not os.path.exists(LOG_PATH):
        return []
    rows = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def main() -> None:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    rows = load_rows()
    today_rows = [r for r in rows if r.get("date") == today]

    if not today_rows:
        print("오늘 기록된 API 호출이 없습니다.")
        return

    # 단계별 집계
    agg = defaultdict(lambda: {"calls": 0, "in": 0, "out": 0, "cost": 0.0, "truncated": 0})
    for r in today_rows:
        a = agg[r.get("label", "미분류")]
        a["calls"] += 1
        a["in"]  += r.get("input_tokens", 0) + r.get("cache_read_input_tokens", 0) \
                                             + r.get("cache_creation_input_tokens", 0)
        a["out"] += r.get("output_tokens", 0)
        a["cost"] += r.get("cost_usd", 0.0)
        if r.get("stop_reason") == "max_tokens":
            a["truncated"] += 1

    total_cost = sum(a["cost"] for a in agg.values())
    total_calls = sum(a["calls"] for a in agg.values())

    # 월 추정 (30일 기준)
    monthly = total_cost * 30

    lines = [
        f"## 📊 API 사용량 — {today}",
        "",
        "| 단계 | 호출 | 입력 토큰 | 출력 토큰 | 비용(USD) | 비중 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label, a in sorted(agg.items(), key=lambda kv: -kv[1]["cost"]):
        share = (a["cost"] / total_cost * 100) if total_cost else 0
        warn = f" ⚠️{a['truncated']}회 잘림" if a["truncated"] else ""
        lines.append(
            f"| {label}{warn} | {a['calls']} | {a['in']:,} | {a['out']:,} "
            f"| ${a['cost']:.4f} | {share:.1f}% |"
        )
    lines += [
        f"| **합계** | **{total_calls}** | | | **${total_cost:.4f}** | |",
        "",
        f"**오늘 총 ${total_cost:.4f}** (약 {total_cost * 1400:,.0f}원) "
        f"→ 매일 이 수준이면 월 약 **${monthly:.2f}**",
    ]

    # 누적 이력 (최근 7일)
    by_date = defaultdict(float)
    for r in rows:
        by_date[r.get("date", "?")] += r.get("cost_usd", 0.0)
    recent = sorted(by_date.items(), reverse=True)[:7]
    if len(recent) > 1:
        lines += ["", "### 최근 7일 추이", "", "| 날짜 | 비용(USD) |", "|---|---:|"]
        lines += [f"| {d} | ${c:.4f} |" for d, c in recent]

    out = "\n".join(lines)
    print(out)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(out + "\n")


if __name__ == "__main__":
    main()
