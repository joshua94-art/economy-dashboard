#!/usr/bin/env python3
"""
블로그 초안 생성 스크립트

최신 한경 경제 뉴스 + WSJ 클러스터 + 시장 지표 + 문체 가이드/예시글을
하나의 프롬프트로 묶어 Claude API로 초안을 생성하고
data/blog/YYYY-MM-DD.json 에 저장합니다.
조립한 프롬프트는 build/blog_prompt_YYYY-MM-DD.txt 에도 남깁니다.

ANTHROPIC_API_KEY 환경변수가 필요합니다.

실행:
    python scripts/generate_blog.py           # 쓸 자료가 없거나 오늘 글이 이미 있으면 건너뜀
    python scripts/generate_blog.py --force   # 날짜 검사·중복 방지를 생략하고 최신 한경으로 생성 (테스트용)

날짜 규칙:
    - 오늘(KST) 한경이 있으면 → 한경 중심으로 작성
        WSJ 는 한경 날짜 이하 중 최신을 쓰되, 3일보다 오래됐으면 빼고 한경만으로 작성
        (source_dates.wsj = null)
    - 오늘 한경이 없으면(일요일·휴간일) → WSJ 만으로 작성 (source_dates.hankyung = null)
        WSJ 는 오늘 또는 어제 날짜(시차)여야 한다. 그보다 오래됐으면 종료 (exit 0)
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
MAX_TOKENS = 16000   # 하루 최대 3편 (한 편 ≈ 3~4k 토큰)

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

# 한경이 없는 날 WSJ 만으로 쓸 때, WSJ 가 오늘(KST) 기준 이 일수 이내여야 한다 (시차로 하루 늦은 날짜).
WSJ_ONLY_MAX_AGE_DAYS = 1

NO_WSJ_NOTE = """\
## 참고: 이번 글에는 WSJ 자료가 없다
- <wsj> 는 제공되지 않는다. 위 지시 중 <wsj> 에 관한 부분은 무시하고 <hankyung> 과 <market_data> 만으로 쓴다.
- source_dates.wsj 는 null 로 둔다."""

NO_HANKYUNG_NOTE = """\
## 참고: 이번 글에는 한경 자료가 없다
- 오늘은 한국경제신문이 없는 날이다. <hankyung> 은 제공되지 않는다.
- 위 지시 중 <hankyung> 에 관한 부분은 무시하고 <wsj> 와 <market_data> 만으로 쓴다.
  ("한경과 WSJ 양쪽에서 재료가 나오는 소재를 우선한다"는 적용하지 않는다.)
- 추천 카드의 source 는 "WSJ 기사 제목" 형태로 쓴다.
- source_dates.hankyung 은 null 로 둔다."""

# 하루 글 편수 상한 (basis → 최대 편수). 재료가 부족하면 모델이 더 적게 쓸 수 있다.
POSTS_BOTH = {"한경": 2, "WSJ": 1}   # 한경 + WSJ 가 모두 있는 날
POSTS_SINGLE = 2                      # 둘 중 하나만 있는 날

CATEGORIES = ["거시경제 위성", "국제경제 레이더", "리스크 경보실"]

INSTRUCTIONS = """\
## 1. 역할
- 너는 "숲블" 블로그의 글을 대신 쓴다.
- <style_guide> 의 규칙을 반드시 따른다.
- <samples> 는 문체 참고용이다. 내용을 베끼지 마라.

## 2. 오늘 쓸 글
{post_plan}
- 각 글의 basis 는 그 글의 주재료가 된 신문이다 ("한경" 또는 "WSJ").
  주재료가 아닌 쪽 신문과 <market_data> 는 보조 재료로 함께 써도 된다.
- 같은 날 글끼리 소재가 겹치지 않게 한다.
- 위 편수는 상한이다. 쓸 만한 소재가 부족하면 억지로 채우지 말고 편수를 줄인다. (최소 1편)
- 한경 기반 글을 앞 칸(post1 부터)에, WSJ 기반 글을 뒤 칸에 둔다.

## 3. 카테고리 판정 (글마다)
- 아래 3개 중 하나를 고른다: 거시경제 위성 / 국제경제 레이더 / 리스크 경보실
- 판정 기준
  - 금리, 환율, 통화정책, 물가 → 거시경제 위성
  - 전쟁, 에너지 패권, 지정학, 해외 정세 → 국제경제 레이더
  - 법, 정책, 규제, 제도 변화 → 리스크 경보실
- 여러 개에 걸치면 "글의 결론이 어디로 향하는가"로 정한다.
- 소재를 고를 때는 <hankyung> 과 <wsj> 양쪽에 재료가 함께 있는 것을 우선한다.

## 4. 글 작성 (글마다)
- <style_guide> 의 골격, 문체, 분량 규칙을 글 한 편마다 그대로 따른다. (분량은 한 편 기준)
- 모든 수치는 <hankyung>, <wsj>, <market_data> 에 실제로 있는 것만 쓴다.
- 과거 글 상호참조는 하지 마라. (지난 글 목록이 주어지지 않았다.)

## 5. 추천 카드 (하루 한 세트)
- 글마다가 아니라 그날 전체로 한 번만 만든다.
- <style_guide> 9장 규격을 따른다.
- <hankyung>, <wsj> 에 실제로 등장한 산업·기업만 추천한다.
- 적합한 후보가 없으면 빈 배열을 반환한다.
- 9장 형식은 save_blog_post 도구의 필드에 이렇게 대응한다.
  - industry_candidates: 산업 → industry, 근거 기사 → source, 조사할 것(질문 형태) → research_question
  - company_candidates: 기업 → company, 근거 기사 → source, 왜 볼 만한가 → why,
    확인할 기준 ① 플랫폼 전략 → platform_check, ② 병목 → bottleneck_check
  - source 는 "(한경|WSJ) 기사 제목" 형태로 쓴다.

## 6. 출력 형식
- 결과는 save_blog_post 도구를 한 번 호출해서 제출한다.
- 글 1편은 post1_basis / post1_category / post1_title / post1_body 칸에, 2편은 post2_*, 3편은 post3_* 칸에 담는다.
  쓰지 않는 칸은 생략한다.
- source_dates 에는 <hankyung>, <wsj> 의 date 속성과 <market_data> 의 updated_at 을 그대로 넣는다."""

# 글 목록을 배열(posts)로 받으면, 긴 본문이 든 배열을 모델이 JSON 문자열로 감싸 보내다
# 따옴표 이스케이프가 깨지는 일이 반복됐다. 그래서 글마다 최상위 필드(post1_* ~ post3_*)로 받고,
# 저장할 때 posts 배열로 모은다. 저장 형식과 화면은 그대로.
MAX_POST_SLOTS = 3
POST_FIELDS = ("basis", "category", "title", "body")
POST_SLOT_PROPS: dict = {}
for _i in range(1, MAX_POST_SLOTS + 1):
    POST_SLOT_PROPS.update({
        f"post{_i}_basis": {"type": "string", "enum": ["한경", "WSJ"], "description": f"{_i}번째 글의 주재료 신문"},
        f"post{_i}_category": {"type": "string", "enum": CATEGORIES},
        f"post{_i}_title": {"type": "string", "description": f"{_i}번째 글 제목"},
        f"post{_i}_body": {"type": "string", "description": f"{_i}번째 글 본문 전체 (인사말부터 면책 문구까지)"},
    })
POST_SLOT_REQUIRED = [f"post1_{f}" for f in POST_FIELDS]   # 최소 1편

POST_TOOL = {
    "name": "save_blog_post",
    "description": "숲블 블로그의 그날 글(여러 편)과 추천 카드를 저장한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            **POST_SLOT_PROPS,
            "source_dates": {
                "type": "object",
                "properties": {
                    "hankyung": {"type": ["string", "null"]},
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
        "required": ["date", *POST_SLOT_REQUIRED, "source_dates", "industry_candidates", "company_candidates"],
    },
}


def post_limits(has_hankyung: bool, has_wsj: bool) -> dict[str, int]:
    """그날 자료에 따른 basis 별 최대 편수."""
    if has_hankyung and has_wsj:
        return dict(POSTS_BOTH)
    return {"한경": POSTS_SINGLE} if has_hankyung else {"WSJ": POSTS_SINGLE}


def post_plan_text(limits: dict[str, int]) -> str:
    total = sum(limits.values())
    parts = " + ".join(f"{b} 기반 최대 {n}편" for b, n in limits.items())
    return f"- 오늘은 {parts} (총 최대 {total}편)을 쓴다."


def enforce_limits(posts: list[dict], limits: dict[str, int]) -> tuple[list[dict], list[str]]:
    """basis 별 상한을 넘거나 허용되지 않은 basis 의 글은 잘라낸다. (남긴 글, 버린 사유 목록)"""
    kept, dropped, count = [], [], {b: 0 for b in limits}
    for p in posts:
        b = p.get("basis")
        if b not in limits:
            dropped.append(f"basis {b!r} 는 오늘 쓸 수 없음: {p.get('title')!r}")
        elif count[b] >= limits[b]:
            dropped.append(f"{b} 기반 상한 {limits[b]}편 초과: {p.get('title')!r}")
        else:
            count[b] += 1
            kept.append(p)
    # 한경 기반 글을 먼저
    kept.sort(key=lambda p: 0 if p.get("basis") == "한경" else 1)
    return kept, dropped


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
    news_date: str | None,
    news_content: str,
    wsj_date: str | None,
    wsj_sections: list[dict],
    market: dict,
    guide: str,
    samples: list[tuple[str, str]],
) -> str:
    """news_date / wsj_date 가 None 이면 해당 태그를 빼고, 그 자료 없이 쓰라는 안내를 붙인다."""
    limits = post_limits(news_date is not None, wsj_date is not None)
    parts = [INSTRUCTIONS.format(post_plan=post_plan_text(limits))]
    if news_date is None:
        parts.append(NO_HANKYUNG_NOTE)
    if wsj_date is None:
        parts.append(NO_WSJ_NOTE)
    parts += [
        tag("style_guide", guide),
        tag("samples", format_samples(samples)),
        tag("market_data", json.dumps(market, ensure_ascii=False, indent=2),
            updated_at=str(market.get("updated_at"))),
    ]
    if news_date is not None:
        parts.append(tag("hankyung", news_content, date=news_date))
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
            # post1_* ~ post3_* 칸을 posts 배열로 모은다 (제목·본문이 빈 칸은 쓰지 않은 칸)
            posts = []
            for i in range(1, MAX_POST_SLOTS + 1):
                slot = {f: post.pop(f"post{i}_{f}", None) for f in POST_FIELDS}
                if isinstance(slot["title"], str) and slot["title"].strip() \
                        and isinstance(slot["body"], str) and slot["body"].strip():
                    slot["basis"] = _normalize_basis(slot["basis"])
                    posts.append(slot)
            if not posts:
                return None, "제목·본문이 채워진 글이 없음"
            post["posts"] = posts
            # 추천 카드 배열이 JSON 문자열로 오는 경우 한 번 풀어 준다 (실패하면 카드만 비움)
            for key in ("industry_candidates", "company_candidates"):
                val, note = _unwrap_json_array(post.get(key))
                if note:
                    print(f"  [보정] {key}: {note}")
                post[key] = val if val is not None else []
            return post, ""
    return None, "save_blog_post 도구 호출이 없음"


def _unwrap_json_array(val) -> tuple[list | None, str]:
    """배열이면 그대로, 문자열이면 JSON 으로 풀어 배열인지 확인. (값 또는 None, 보정/오류 메모)"""
    if isinstance(val, list):
        # 항목이 문자열로 한 번 더 감싸져 온 경우도 푼다
        if any(isinstance(x, str) for x in val):
            try:
                return [json.loads(x) if isinstance(x, str) else x for x in val], "항목이 문자열로 와서 풀었음"
            except json.JSONDecodeError as e:
                return None, f"항목 문자열 JSON 파싱 실패: {e}"
        return val, ""
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
        except json.JSONDecodeError as e:
            return None, f"문자열로 왔고 JSON 파싱도 실패: {e}"
        if isinstance(parsed, list):
            return parsed, "문자열로 와서 JSON 으로 풀었음"
        return None, f"문자열을 풀었지만 배열이 아님 ({type(parsed).__name__})"
    return None, f"배열이 아님 ({type(val).__name__})"


_BASIS_ALIASES = {
    "한경": "한경", "한국경제": "한경", "한국경제신문": "한경", "hankyung": "한경", "hk": "한경",
    "wsj": "WSJ", "월스트리트저널": "WSJ", "wallstreetjournal": "WSJ",
}


def _normalize_basis(b) -> str | None:
    """'wsj', '월스트리트저널', '한국경제' 등 표기 차이를 '한경' / 'WSJ' 로 맞춘다."""
    if not isinstance(b, str):
        return b
    return _BASIS_ALIASES.get(b.replace(" ", "").lower(), b)


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
    wsj_dates = load_dates(WSJ_DIR)
    latest_news = news_dates[-1] if news_dates else None

    if latest_news == today or (force and latest_news):
        # 1-a. 한경 중심 — WSJ 는 한경보다 0~3일 이전이면 사용, 그보다 오래되면 제외
        news_date = latest_news
        wsj_date, gap = pick_wsj_date(wsj_dates, news_date)
        if wsj_date is None or gap > WSJ_MAX_GAP_DAYS:
            latest = f"최신 {wsj_date}, {gap}일 전" if wsj_date else "사용 가능한 날짜 없음"
            print(f"  WSJ 제외: 한경({news_date}) 기준 {WSJ_MAX_GAP_DAYS}일 이내 WSJ가 없습니다. ({latest})")
            wsj_date = None
    else:
        # 1-b. 오늘 한경이 없음 — 오늘/어제 날짜 WSJ 가 있으면 WSJ 만으로 작성
        news_date = None
        wsj_date, gap = pick_wsj_date(wsj_dates, today)
        if wsj_date is None or gap > WSJ_ONLY_MAX_AGE_DAYS:
            print(f"  오늘({today}) 한경이 없고, 최근 WSJ도 없습니다. "
                  f"(한경 최신: {latest_news or '없음'}, WSJ 최신: {wsj_dates[-1] if wsj_dates else '없음'}) 종료.")
            return
        print(f"  한경 제외: 오늘({today}) 한경이 없어 WSJ({wsj_date})만으로 작성합니다.")

    # 2. 중복 방지
    if os.path.exists(post_path) and not force:
        print(f"  이미 생성됨: {post_path} (다시 생성하려면 --force). 종료.")
        return

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

    news_content = read_json(f"{NEWS_DIR}/{news_date}.json").get("content", "") if news_date else ""
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

    print(f"  한경 날짜:          {news_date or '없음 (WSJ만으로 작성)'}")
    if wsj_date:
        base = "한경보다" if news_date else "오늘보다"
        print(f"  WSJ 날짜:           {wsj_date} ({base} {gap}일 전, 섹션 {len(wsj_sections)}개)")
    else:
        print("  WSJ 날짜:           없음 (WSJ 제외)")
    print(f"  market updated_at:  {market.get('updated_at')}")
    print(f"  예시글 {len(samples)}개:")
    for name, _ in samples:
        print(f"    - {name}")
    limits = post_limits(news_date is not None, wsj_date is not None)
    print(f"  글 편수 상한:       {' + '.join(f'{b} {n}편' for b, n in limits.items())}")
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
        # 워크플로에서 continue-on-error 로 스텝이 '성공'처럼 보이므로 Annotations 에 오류를 남긴다
        print(f"::error title=블로그 생성 실패::응답 처리 실패 — {reason} (원본 응답은 blog-debug 아티팩트)")
        sys.exit(1)

    # 10. 편수 상한 적용 — basis 별 상한을 넘는 글은 버린다
    post["posts"], dropped = enforce_limits(post["posts"], limits)
    for d in dropped:
        print(f"  [보정] 글 제외 — {d}")
    if not post["posts"]:
        print("  [오류] 남은 글이 없습니다. 저장하지 않습니다.")
        print("::error title=블로그 생성 실패::편수 규칙에 맞는 글이 없어 저장하지 않았습니다.")
        sys.exit(1)

    # 11. 저장 — date 는 모델 출력과 무관하게 파일명(오늘, KST)으로 강제
    if post.get("date") != today:
        print(f"  [보정] date 필드 {post.get('date')!r} → {today!r}")
    post["date"] = today
    if news_date is None:
        post.setdefault("source_dates", {})["hankyung"] = None
    if wsj_date is None:
        post.setdefault("source_dates", {})["wsj"] = None
    save_post(today, post)

    print(f"\n  글 {len(post['posts'])}편:")
    for i, p in enumerate(post["posts"], 1):
        print(f"    {i}. [{p.get('basis')}] [{p.get('category')}] {p.get('title')} ({len(p.get('body') or ''):,}자)")
    print(f"  산업 후보:          {len(post.get('industry_candidates') or [])}개")
    print(f"  기업 후보:          {len(post.get('company_candidates') or [])}개")
    print(f"  저장 경로:          {post_path}")

    # 12. 수치 대조 (API 미호출) — 원문에 없는 금액·퍼센트를 경고만 하고 저장은 유지
    sources = ([news_content] if news_date else []) + ([format_wsj_sections(wsj_sections)] if wsj_date else [])
    issues, total = check_post(post, sources, market)
    print(f"\n  수치 대조:          {total}개 중 원문 미확인 {len(issues)}개")
    for i in issues:
        print(f"  [경고] {i['field']}: {i['raw']!r}  … {i['context']} …")


if __name__ == "__main__":
    main()
