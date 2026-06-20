#!/usr/bin/env python3
"""
구글 드라이브 연결 진단 스크립트 (파일 목록 + 날짜 로직 확인)
GitHub Actions > 드라이브 연결 진단 > Run workflow 로 실행
"""

import json
import os
import sys
from datetime import datetime, timedelta

import pytz

KST = pytz.timezone("Asia/Seoul")

# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def section(title):
    print("\n" + "=" * 65)
    print(f"  {title}")
    print("=" * 65)


def ok(msg):  print(f"  ✅ {msg}")
def err(msg): print(f"  ❌ {msg}")
def info(msg):print(f"  ℹ️  {msg}")
def warn(msg):print(f"  ⚠️  {msg}")


# ── 1. 자격증명 파싱 ──────────────────────────────────────────────────────────

def parse_credentials():
    section("1. GOOGLE_CREDENTIALS 파싱")
    raw = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not raw:
        err("환경변수 없음 — GitHub 시크릿 등록 여부 확인")
        return None

    info(f"환경변수 길이: {len(raw)}자  /  첫 30자: {raw[:30]!r}")

    try:
        creds = json.loads(raw)
    except json.JSONDecodeError as e:
        err(f"JSON 파싱 실패: {e}")
        return None

    email = creds.get("client_email", "")
    ok(f"파싱 성공")
    ok(f"서비스 계정: {email}")
    warn("위 이메일로 'Joshua 증권' 폴더가 공유되어 있어야 합니다")
    return creds


# ── 2. Drive 서비스 생성 ──────────────────────────────────────────────────────

def build_service(creds_info):
    section("2. Drive API 서비스 생성")
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as e:
        err(f"라이브러리 임포트 실패: {e}")
        return None

    try:
        creds = service_account.Credentials.from_service_account_info(
            creds_info,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        svc = build("drive", "v3", credentials=creds)
        ok("Drive API 서비스 객체 생성 성공")
        return svc
    except Exception as e:
        err(f"서비스 생성 실패: {e}")
        return None


# ── 3. 날짜 로직 확인 ─────────────────────────────────────────────────────────

def check_date_logic():
    section("3. 날짜 검색 로직 확인")
    now = datetime.now(KST)
    yesterday = now - timedelta(days=1)

    date_str   = now.strftime("%Y%m%d")
    date_disp  = now.strftime("%Y-%m-%d")
    month_str  = now.strftime("%Y-%m")

    info(f"현재 KST 시각  : {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    info(f"검색할 date_str: '{date_str}'  (파일명에서 이 문자열 포함 여부 검색)")
    info(f"어제 date_str  : '{yesterday.strftime('%Y%m%d')}'")
    info(f"월 폴더명      : '{month_str}'")
    print()
    info("파일명 예시 매칭 테스트:")

    samples = [
        f"한국경제신문.{date_str}.A001.pdf",
        f"한국경제신문.{date_str}.A024.pdf",
        f"한국경제신문.{yesterday.strftime('%Y%m%d')}.A001.pdf",
        "한국경제신문.20260620.A001.pdf",
        "20260620_한경.pdf",
    ]
    for s in samples:
        match = date_str in s
        sym = "✅" if match else "  "
        print(f"    {sym}  '{s}'  → contains '{date_str}': {match}")

    return date_str, month_str


# ── 4. 폴더 탐색 ──────────────────────────────────────────────────────────────

_DRIVE_PARAMS = dict(includeItemsFromAllDrives=True, supportsAllDrives=True)


def find_folder(svc, name, parent_id=None):
    conds = [
        f"name='{name}'",
        "mimeType='application/vnd.google-apps.folder'",
        "trashed=false",
    ]
    if parent_id:
        conds.append(f"'{parent_id}' in parents")
    res = svc.files().list(
        q=" and ".join(conds),
        fields="files(id,name)",
        **_DRIVE_PARAMS,
    ).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def list_folder_contents(svc, folder_id, folder_name, page_size=100):
    """폴더 내 모든 항목 나열 (필터 없음)"""
    try:
        res = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id,name,mimeType,size,modifiedTime)",
            orderBy="name",
            pageSize=page_size,
            **_DRIVE_PARAMS,
        ).execute()
        items = res.get("files", [])
        print(f"\n  📂 '{folder_name}' 내 항목 ({len(items)}개):")
        for f in items:
            is_dir = "folder" in f.get("mimeType", "")
            tag = "📁" if is_dir else "📄"
            size = f.get("size")
            size_str = f"  {int(size)//1024}KB" if size else ""
            mod = f.get("modifiedTime","")[:10]
            print(f"    {tag} {f['name']}{size_str}  [{mod}]")
        return items
    except Exception as e:
        err(f"폴더 목록 조회 실패: {e}")
        return []


def navigate_folders(svc, date_str, month_str):
    section("4. 폴더 탐색")

    root_id = find_folder(svc, "Joshua 증권")
    if not root_id:
        err("'Joshua 증권' 폴더를 찾을 수 없음")
        warn("서비스 계정 이메일과 폴더 공유 여부 확인")
        # 공유된 폴더 전체 출력
        print("\n  서비스 계정에 공유된 폴더 전체:")
        res = svc.files().list(
            q="mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id,name)", pageSize=50, **_DRIVE_PARAMS,
        ).execute()
        for f in res.get("files", []):
            print(f"    📁 {f['name']}  (id={f['id']})")
        return None, None, None
    ok(f"'Joshua 증권' 발견 (id={root_id})")

    hk_id = find_folder(svc, "한경 PDF", root_id)
    if not hk_id:
        err("'한경 PDF' 폴더를 찾을 수 없음")
        list_folder_contents(svc, root_id, "Joshua 증권")
        return root_id, None, None
    ok(f"'한경 PDF' 발견 (id={hk_id})")

    month_id = find_folder(svc, month_str, hk_id)
    if not month_id:
        err(f"'{month_str}' 폴더를 찾을 수 없음")
        list_folder_contents(svc, hk_id, "한경 PDF")
        return root_id, hk_id, None
    ok(f"'{month_str}' 발견 (id={month_id})")

    return root_id, hk_id, month_id


# ── 5. PDF 파일 목록 전체 출력 ────────────────────────────────────────────────

def list_all_pdfs(svc, month_id, month_str, date_str):
    section(f"5. '{month_str}' 폴더 내 전체 파일 목록")

    all_items = list_folder_contents(svc, month_id, month_str, page_size=200)

    if not all_items:
        err("파일이 없거나 조회 실패")
        return

    pdfs = [f for f in all_items if f.get("mimeType") == "application/pdf"]
    non_pdfs = [f for f in all_items if f.get("mimeType") != "application/pdf"]

    print(f"\n  요약: 전체 {len(all_items)}개 / PDF {len(pdfs)}개 / 기타 {len(non_pdfs)}개")

    section("6. 오늘 날짜 검색 시뮬레이션")
    info(f"검색 조건: 파일명에 '{date_str}' 포함 AND mimeType=application/pdf")
    print()

    matched = [f for f in pdfs if date_str in f["name"]]
    unmatched = [f for f in pdfs if date_str not in f["name"]]

    if matched:
        ok(f"매칭된 PDF: {len(matched)}개")
        for f in matched:
            print(f"    📄 {f['name']}")
    else:
        err(f"'{date_str}' 포함 PDF 없음")
        if pdfs:
            # 실제 파일명의 날짜 패턴 추출
            print("\n  실제 파일명 샘플 (최대 5개):")
            for f in pdfs[:5]:
                print(f"    📄 {f['name']}")

            # 날짜 후보 추출
            import re
            dates_found = set()
            for f in pdfs:
                m = re.findall(r'\d{8}', f["name"])
                dates_found.update(m)
            if dates_found:
                print(f"\n  파일명에서 발견된 날짜 패턴: {sorted(dates_found, reverse=True)}")
                warn(f"코드가 찾는 날짜: '{date_str}' — 위 날짜와 다르면 날짜 불일치")


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    print("\n🔍 구글 드라이브 진단 (파일 목록 + 날짜 로직)\n")

    creds = parse_credentials()
    if not creds:
        sys.exit(1)

    svc = build_service(creds)
    if not svc:
        sys.exit(1)

    date_str, month_str = check_date_logic()

    root_id, hk_id, month_id = navigate_folders(svc, date_str, month_str)
    if not month_id:
        sys.exit(1)

    list_all_pdfs(svc, month_id, month_str, date_str)

    print("\n" + "=" * 65)
    print("  진단 완료 — 위 출력 결과를 공유해주세요")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
