#!/usr/bin/env python3
"""
구글 드라이브 연결 진단 스크립트
GitHub Actions에서 workflow_dispatch로 수동 실행하여 에러 원인 확인용.
"""

import json
import os
import sys


def check_credentials():
    print("=" * 60)
    print("1. GOOGLE_CREDENTIALS 파싱 테스트")
    print("=" * 60)

    raw = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not raw:
        print("❌ GOOGLE_CREDENTIALS 환경변수가 없습니다.")
        print("   GitHub 시크릿에 GOOGLE_CREDENTIALS를 등록했는지 확인하세요.")
        return None

    print(f"   환경변수 길이: {len(raw)} 자")
    print(f"   첫 20자: {raw[:20]!r}")

    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"❌ JSON 파싱 실패: {e}")
        print("   시크릿 값이 유효한 JSON인지 확인하세요.")
        return None

    required = ["type", "project_id", "client_email", "private_key"]
    for key in required:
        val = info.get(key, "")
        if key == "private_key":
            print(f"   ✅ {key}: {val[:40]}...")
        else:
            print(f"   ✅ {key}: {val}")
        if not val:
            print(f"   ❌ {key} 값이 비어 있습니다!")

    if info.get("type") != "service_account":
        print(f"   ❌ type이 'service_account'가 아님: {info.get('type')}")
        return None

    print(f"\n   서비스 계정 이메일: {info.get('client_email')}")
    print("   ⚠️  위 이메일로 구글 드라이브 폴더를 공유했는지 확인하세요.")
    return info


def build_service(info):
    print("\n" + "=" * 60)
    print("2. Google Drive API 연결 테스트")
    print("=" * 60)

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        print("   ✅ google-auth 라이브러리 임포트 성공")
    except ImportError as e:
        print(f"   ❌ 라이브러리 임포트 실패: {e}")
        return None

    try:
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        service = build("drive", "v3", credentials=creds)
        print("   ✅ Drive API 서비스 객체 생성 성공")
        return service
    except Exception as e:
        print(f"   ❌ 서비스 생성 실패: {e}")
        return None


def list_all_visible(service):
    print("\n" + "=" * 60)
    print("3. 서비스 계정이 볼 수 있는 모든 항목 (상위 20개)")
    print("=" * 60)

    try:
        res = service.files().list(
            pageSize=20,
            fields="files(id,name,mimeType,parents,sharedWithMe)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute()
        files = res.get("files", [])
        if not files:
            print("   ⚠️  보이는 파일이 없습니다.")
            print("   → 서비스 계정 이메일로 폴더를 공유했는지 확인하세요.")
        for f in files:
            mime = f.get("mimeType", "")
            is_folder = "folder" in mime
            shared = f.get("sharedWithMe", False)
            tag = "📁" if is_folder else "📄"
            print(f"   {tag} {f['name']}  (id={f['id']}, sharedWithMe={shared})")
        return files
    except Exception as e:
        print(f"   ❌ files.list 실패: {e}")
        return []


def search_folder(service, name, parent_id=None):
    print(f"\n   폴더 검색: '{name}'" + (f" (parent={parent_id})" if parent_id else " (전체)"))

    conditions = [
        f"name='{name}'",
        "mimeType='application/vnd.google-apps.folder'",
        "trashed=false",
    ]
    if parent_id:
        conditions.append(f"'{parent_id}' in parents")

    try:
        res = service.files().list(
            q=" and ".join(conditions),
            fields="files(id,name,parents)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute()
        files = res.get("files", [])
        if files:
            print(f"   ✅ 발견: {files[0]['name']} (id={files[0]['id']})")
            return files[0]["id"]
        else:
            print(f"   ❌ '{name}' 폴더를 찾을 수 없음")
            return None
    except Exception as e:
        print(f"   ❌ 검색 실패: {e}")
        return None


def check_folder_tree(service):
    print("\n" + "=" * 60)
    print("4. 폴더 트리 탐색")
    print("=" * 60)

    root_id = search_folder(service, "Joshua 증권")
    if not root_id:
        print("\n   → 'Joshua 증권' 폴더가 공유되지 않았거나 이름이 다릅니다.")
        print("   → 서비스 계정 이메일과 폴더를 다시 확인하세요.")

        # 폴더만 필터링하여 다시 목록 확인
        print("\n   현재 서비스 계정에 공유된 폴더 목록:")
        try:
            res = service.files().list(
                q="mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields="files(id,name)",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                pageSize=30,
            ).execute()
            folders = res.get("files", [])
            if folders:
                for f in folders:
                    print(f"   📁 {f['name']} (id={f['id']})")
            else:
                print("   (공유된 폴더 없음)")
        except Exception as e:
            print(f"   ❌ 폴더 목록 조회 실패: {e}")
        return

    hk_id = search_folder(service, "한경 PDF", root_id)
    if not hk_id:
        print("\n   → 'Joshua 증권' 하위에 '한경 PDF' 폴더가 없습니다.")
        # 하위 항목 나열
        print("\n   'Joshua 증권' 폴더 내 항목:")
        try:
            res = service.files().list(
                q=f"'{root_id}' in parents and trashed=false",
                fields="files(id,name,mimeType)",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            ).execute()
            for f in res.get("files", []):
                tag = "📁" if "folder" in f.get("mimeType","") else "📄"
                print(f"   {tag} {f['name']}")
        except Exception as e:
            print(f"   ❌ {e}")
        return

    import pytz
    from datetime import datetime
    kst = pytz.timezone("Asia/Seoul")
    month_folder = datetime.now(kst).strftime("%Y-%m")
    month_id = search_folder(service, month_folder, hk_id)

    if not month_id:
        print(f"\n   → '한경 PDF' 하위에 '{month_folder}' 폴더가 없습니다.")
        print("\n   '한경 PDF' 폴더 내 항목:")
        try:
            res = service.files().list(
                q=f"'{hk_id}' in parents and trashed=false",
                fields="files(id,name,mimeType)",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            ).execute()
            for f in res.get("files", []):
                tag = "📁" if "folder" in f.get("mimeType","") else "📄"
                print(f"   {tag} {f['name']}")
        except Exception as e:
            print(f"   ❌ {e}")
        return

    # PDF 목록 확인
    print(f"\n   '{month_folder}' 폴더 내 PDF 파일:")
    try:
        from datetime import datetime
        date_str = datetime.now(kst).strftime("%Y%m%d")
        res = service.files().list(
            q=f"'{month_id}' in parents and mimeType='application/pdf' and trashed=false",
            fields="files(id,name,size)",
            orderBy="name",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute()
        pdfs = res.get("files", [])
        print(f"   전체 PDF 수: {len(pdfs)}")
        today_pdfs = [f for f in pdfs if date_str in f["name"]]
        print(f"   오늘({date_str}) PDF 수: {len(today_pdfs)}")
        for f in pdfs[:10]:
            size_kb = int(f.get("size", 0)) // 1024
            marker = " ← 오늘" if date_str in f["name"] else ""
            print(f"   📄 {f['name']} ({size_kb}KB){marker}")
        if len(pdfs) > 10:
            print(f"   ... 외 {len(pdfs)-10}개")
    except Exception as e:
        print(f"   ❌ PDF 목록 조회 실패: {e}")


def main():
    print("\n🔍 구글 드라이브 연결 진단 시작\n")

    info = check_credentials()
    if not info:
        sys.exit(1)

    service = build_service(info)
    if not service:
        sys.exit(1)

    list_all_visible(service)
    check_folder_tree(service)

    print("\n" + "=" * 60)
    print("진단 완료")
    print("=" * 60)


if __name__ == "__main__":
    main()
