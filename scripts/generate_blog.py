#!/usr/bin/env python3
"""
블로그 초안 생성 스크립트

최신 한경 경제 뉴스 + WSJ 클러스터 + 시장 지표 + 문체 가이드/예시글을
하나의 프롬프트로 묶어 Claude API로 초안을 생성하고
data/blog/YYYY-MM-DD.json 에 저장합니다.
조립한 프롬프트는 build/blog_prompt_YYYY-MM-DD.txt 에도 남깁니다.

ANTHROPIC_API_KEY 환경변수가 필요합니다.

실행:
    python scripts/generate_blog.py           # 오늘자 한경이 없거나 오늘 글이 이미 있으면 건너뜀
    python scripts/generate_blog.py --force   # 한경 날짜 검사·중복 방지를 생략하고 생성 (테스트용)

날짜 규칙:
    - 한경 최신 날짜가 오늘(KST)이 아니면 종료 (exit 0)
    - WSJ 는 한경 날짜 이하 중 최신을 쓰되, 3일보다 오래됐으면 빼고 한경만으로 작성
      (source_dates.wsj = null)
"""

import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import anthropic

from number_check import check_post
from usage_log import record

MODEL = "claude-sonnet-4-6"   # generate_comments.py 와 동일
MAX_TOKENS = 8000

BLOG_DIR = "data/blog"
NEWS_DIR = "data/economy_news"
WSJ_DIR = "data/wsj"
MARKET_PATH = "data/market_data.json"
STYLE_DIR = "data/style"
GUIDE_PATH = f"{STYLE_DIR}/guide.md"
SAMPLE_GLOB = f"{STYLE_DIR}/sample-*.txt"
BUILD_DIR = "build"

KST = timezone(timedelta(hours=9))

MARKET_KEYS = ["updated_at", "indices", "exchange_rate", "macro_indicators", "market_diagnosis"]

# WSJ 는 시차·주말 때문에 한경보다 며칠 이를 수 있다. 이 범위를 넘으면 WSJ 를 뺀다.
WSJ_MAX_GAP_DAYS = 3

NO_WSJ_NOTE = """\
## 참고: 이번 글에는 WSJ 자료가 없다
- <wsj> 는 제공되지 않는다. 위 지시 중 <wsj> 에 관한 부분은 무시하고 <hankyung> 과 <market_data> 만으로 쓴다.
- source_dates.wsj 는 null 로 둔다."""

INSTRUCTIONS = """\
## 1. 역할
- 너는 "숲블" 블로그의 글을 대신 쓴다.
- <style_guide> 의 규칙을 반드시 따른다.
- <samples> 는 문체 참고용이다. 내용을 베끼지 마라.

## 2. 카테고리 판정
- 아래 3개 중 하나를 고른다: 거시경제 위성 / 국제경제 레이더 / 리스크 경보실
- 판정 기준
  - 금리, 환율, 통화정책, 물가 → 거시경제 위성
  - 전쟁, 에너지 패권, 지정학, 해외 정세 → 국제경제 레이더
  - 법, 정책, 규제, 제도 변화 → 리스크 경보실
- 여러 개에 걸치면 "글의 결론이 어디로 향하는가"로 정한다.
- <hankyung> 과 <wsj> 양쪽에서 재료가 나오는 소재를 우선한다.

## 3. 글 작성
- <style_guide> 의 골격, 문체, 분량 규칙을 그대로 따른다.
- 모든 수치는 <hankyung>, <wsj>, <market_data> 에 실제로 있는 것만 쓴다.
- 과거 글 상호참조는 하지 마라. (지난 글 목록이 주어지지 않았다.)

## 4. 추천 카드
- <style_guide> 9장 규격을 따른다.
- <hankyung>, <wsj> 에 실제로 등장한 산업·기업만 추천한다.
- 적합한 후보가 없으면 빈 배열을 반환한다.
- 9장 형식은 save_blog_post 도구의 필드에 이렇게 대응한다.
  - industry_candidates: 산업 → industry, 근거 기사 → source, 조사할 것(질문 형태) → research_question
  - company_candidates: 기업 → company, 근거 기사 → source, 왜 볼 만한가 → why,
    확인할 기준 ① 플랫폼 전략 → platform_check, ② 병목 → bottleneck_check
  - source 는 "(한경|WSJ) 기사 제목" 형태로 쓴다.

## 5. 출력 형식
- 결과는 save_blog_post 도구를 한 번 호출해서 제출한다.
- source_dates 에는 <hankyung>, <wsj> 의 date 속성과 <market_data> 의 updated_at 을 그대로 넣는다."""

POST_TOOL = {
    "name": "save_blog_post",
    "description": "숲블 블로그 초안과 추천 카드를 저장한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "category": {"type": "string", "enum": ["거시경제 위성", "국제경제 레이더", "리스크 경보실"]},
            "title": {"type": "string", "description": "글 제목"},
            "body": {"type": "string", "description": "본문 전체 (인사말부터 면책 문구까지)"},
            "source_dates": {
                "type": "object",
                "properties": {
                    "hankyung": {"type": "string"},
                    "wsj": {"type": ["string", "null"]},
                    "market": {"type": "string"},
                },
                "required": ["hankyung", "wsj", "market"],
            },
            "industry_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "industry": {"type": "string"},
                        "source": {"type": "string"},
                        "research_question": {"type": "string"},
                    },
                    "required": ["industry", "source", "research_question"],
                },
            },
            "company_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "company": {"type": "string"},
                        "source": {"type": "string"},
                        "why": {"type": "string"},
                        "platform_check": {"type": "string"},
                        "bottleneck_check": {"type": "string"},
                    },
                    "required": ["company", "source", "why", "platform_check", "bottleneck_check"],
                },
            },
        },
        "required": [
            "date", "category", "title", "body", "source_dates",
            "industry_candidates", "company_candidates",
        ],
    },
}


# ── 데이터 로드 ──────────────────────────────────────────────────────────────────

def read_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_dates(data_dir: str) -> list[str]:
    """index.json 의 dates 를 오름차순으로. 파일이 없거나 비어 있으면 빈 리스트."""
    path = f"{data_dir}/index.json"
    if not os.path.exists(path):
        return []
    try:
        return sorted(read_json(path).get("dates") or [])
    except (json.JSONDecodeError, AttributeError):
        return []


def pick_wsj_date(wsj_dates: list[str], news_date: str) -> tuple[str | None, int | None]:
    """한경 날짜 이전(같은 날 포함) WSJ 중 가장 최신과 그 날짜 차이(일). 없으면 (None, None)."""
    candidates = [d for d in wsj_dates if d <= news_date]
    if not candidates:
        return None, None
    d = candidates[-1]
    gap = (datetime.strptime(news_date, "%Y-%m-%d") - datetime.strptime(d, "%Y-%m-%d")).days
    return d, gap


def load_market() -> dict:
    data = read_json(MARKET_PATH)
    return {k: data.get(k) for k in MARKET_KEYS}


# ── 프롬프트 조립 ────────────────────────────────────────────────────────────────

def format_wsj_sections(sections: list[dict]) -> str:
    return "\n\n".join(
        f"### {s.get('title', '')}\n{s.get('body', '').strip()}" for s in sections
    )


def format_samples(samples: list[tuple[str, str]]) -> str:
    return "\n\n".join(
        f'<sample file="{name}">\n{text.strip()}\n</sample>' for name, text in samples
    )


def tag(name: str, content: str, **attrs: str) -> str:
    attr_str = "".join(f' {k}="{v}"' for k, v in attrs.items())
    return f"<{name}{attr_str}>\n{content.strip()}\n</{name}>"


def build_prompt(
    news_date: str,
    news_content: str,
    wsj_date: str | None,
    wsj_sections: list[dict],
    market: dict,
    guide: str,
    samples: list[tuple[str, str]],
) -> str:
    """wsj_date 가 None 이면 <wsj> 를 빼고 한경만으로 쓰라는 안내를 붙인다."""
    parts = [INSTRUCTIONS]
    if wsj_date is None:
        parts.append(NO_WSJ_NOTE)
    parts += [
        tag("style_guide", guide),
        tag("samples", format_samples(samples)),
        tag("market_data", json.dumps(market, ensure_ascii=False, indent=2),
            updated_at=str(market.get("updated_at"))),
        tag("hankyung", news_content, date=news_date),
    ]
    if wsj_date is not None:
        parts.append(tag("wsj", format_wsj_sections(wsj_sections), date=wsj_date))
    return "\n\n".join(parts) + "\n"


# ── Claude API ───────────────────────────────────────────────────────────────────

def call_claude(prompt: str):
    """save_blog_post 도구 호출을 강제해 구조화된 입력으로 받는다 (JSON 이스케이프 문제 회피)."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        tools=[POST_TOOL],
        tool_choice={"type": "tool", "name": POST_TOOL["name"]},
        messages=[{"role": "user", "content": prompt}],
    )
    record("블로그-초안", resp)
    return resp


def extract_post(resp) -> tuple[dict | None, str]:
    """응답에서 save_blog_post 입력을 꺼낸다. 실패하면 (None, 사유)."""
    if resp.stop_reason == "max_tokens":
        return None, "max_tokens에서 잘림 (도구 입력이 불완전)"
    for block in resp.content:
        if block.type == "tool_use" and block.name == POST_TOOL["name"]:
            post = block.input
            if not isinstance(post, dict):
                return None, "도구 입력이 객체가 아님"
            missing = [k for k in POST_TOOL["input_schema"]["required"] if k not in post]
            if missing:
                return None, f"필수 필드 누락: {missing}"
            return post, ""
    return None, "save_blog_post 도구 호출이 없음"


# ── 저장 ─────────────────────────────────────────────────────────────────────────

def save_post(date: str, post: dict) -> None:
    os.makedirs(BLOG_DIR, exist_ok=True)
    with open(f"{BLOG_DIR}/{date}.json", "w", encoding="utf-8") as f:
        json.dump(post, f, ensure_ascii=False, indent=2)

    index_path = f"{BLOG_DIR}/index.json"
    idx = read_json(index_path) if os.path.exists(index_path) else {"dates": []}
    dates = set(idx.get("dates") or [])
    dates.add(date)
    idx["dates"] = sorted(dates, reverse=True)
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)


# ── 메인 ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    force = "--force" in sys.argv[1:]
    today = datetime.now(KST).strftime("%Y-%m-%d")
    post_path = f"{BLOG_DIR}/{today}.json"

    print("블로그 초안 생성 시작" + (" (--force: 날짜 검사·중복 방지 생략)" if force else ""))

    # 1. 한경 날짜 — 오늘(KST) 신문이 아니면 생성하지 않는다
    news_dates = load_dates(NEWS_DIR)
    if not news_dates:
        print(f"  {NEWS_DIR}/index.json 이 없거나 비어 있습니다. 종료.")
        return
    news_date = news_dates[-1]
    if news_date != today and not force:
        print(f"  오늘({today}) 한경 데이터가 없습니다. (최신: {news_date}) 종료.")
        return

    # 2. 중복 방지
    if os.path.exists(post_path) and not force:
        print(f"  이미 생성됨: {post_path} (다시 생성하려면 --force). 종료.")
        return

    # 3. WSJ 날짜 — 한경보다 0~3일 이전이면 사용, 그보다 오래되면 제외
    wsj_date, gap = pick_wsj_date(load_dates(WSJ_DIR), news_date)
    if wsj_date is None or gap > WSJ_MAX_GAP_DAYS:
        latest = f"최신 {wsj_date}, {gap}일 전" if wsj_date else "사용 가능한 날짜 없음"
        print(f"  WSJ 제외: 한경({news_date}) 기준 {WSJ_MAX_GAP_DAYS}일 이내 WSJ가 없습니다. ({latest})")
        wsj_date = None

    # 4~5. 문체 가이드 / 예시글
    sample_paths = sorted(glob.glob(SAMPLE_GLOB))
    missing = []
    if not os.path.exists(GUIDE_PATH):
        missing.append(GUIDE_PATH)
    if not sample_paths:
        missing.append(SAMPLE_GLOB)
    if missing:
        print("  문체 파일이 없습니다:")
        for m in missing:
            print(f"    - {m}")
        sys.exit(1)

    news_content = read_json(f"{NEWS_DIR}/{news_date}.json").get("content", "")
    wsj_sections = read_json(f"{WSJ_DIR}/{wsj_date}.json").get("sections", []) if wsj_date else []
    market = load_market()
    guide = read_text(GUIDE_PATH)
    samples = [(os.path.basename(p), read_text(p)) for p in sample_paths]

    # 6~7. 조립 및 저장
    prompt = build_prompt(news_date, news_content, wsj_date, wsj_sections, market, guide, samples)

    os.makedirs(BUILD_DIR, exist_ok=True)
    prompt_path = f"{BUILD_DIR}/blog_prompt_{today}.txt"
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)

    print(f"  한경 날짜:          {news_date}")
    if wsj_date:
        print(f"  WSJ 날짜:           {wsj_date} (한경보다 {gap}일 전, 섹션 {len(wsj_sections)}개)")
    else:
        print("  WSJ 날짜:           없음 (WSJ 제외)")
    print(f"  market updated_at:  {market.get('updated_at')}")
    print(f"  예시글 {len(samples)}개:")
    for name, _ in samples:
        print(f"    - {name}")
    print(f"  프롬프트 총 글자 수: {len(prompt):,}")
    print(f"  프롬프트 저장:      {prompt_path}")

    # 8. Claude API 호출
    print(f"\n  Claude API 호출 중... (model={MODEL}, max_tokens={MAX_TOKENS})")
    resp = call_claude(prompt)

    # 9. 도구 입력 추출
    post, reason = extract_post(resp)
    if post is None:
        raw_path = f"{BUILD_DIR}/blog_raw_{today}.txt"
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(resp.model_dump_json(indent=2))
        print(f"  [오류] 응답 처리 실패: {reason}")
        print(f"  원본 응답 저장:     {raw_path}")
        sys.exit(1)

    # 10. 저장 — date 는 모델 출력과 무관하게 파일명(오늘, KST)으로 강제
    if post.get("date") != today:
        print(f"  [보정] date 필드 {post.get('date')!r} → {today!r}")
    post["date"] = today
    if wsj_date is None:
        post.setdefault("source_dates", {})["wsj"] = None
    save_post(today, post)

    print(f"\n  카테고리:           {post.get('category')}")
    print(f"  제목:               {post.get('title')}")
    print(f"  본문 글자 수:       {len(post.get('body') or ''):,}")
    print(f"  산업 후보:          {len(post.get('industry_candidates') or [])}개")
    print(f"  기업 후보:          {len(post.get('company_candidates') or [])}개")
    print(f"  저장 경로:          {post_path}")

    # 11. 수치 대조 (API 미호출) — 원문에 없는 금액·퍼센트를 경고만 하고 저장은 유지
    sources = [news_content] + ([format_wsj_sections(wsj_sections)] if wsj_date else [])
    issues, total = check_post(post, sources, market)
    print(f"\n  수치 대조:          {total}개 중 원문 미확인 {len(issues)}개")
    for i in issues:
        print(f"  [경고] {i['field']}: {i['raw']!r}  … {i['context']} …")


if __name__ == "__main__":
    main()
