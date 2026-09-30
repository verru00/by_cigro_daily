"""구글 드라이브 지정 폴더에 원본 엑셀 업로드."""
import json
import os
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]

MIME = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls":  "application/vnd.ms-excel",
    ".csv":  "text/csv",
}


def service():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise SystemExit("GOOGLE_SERVICE_ACCOUNT_JSON 누락")
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _escape(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


def _find(svc, folder_id: str, name: str) -> str | None:
    """폴더 안에서 같은 이름의 파일 ID 를 찾는다."""
    q = (f"name = '{_escape(name)}' and '{folder_id}' in parents "
         f"and trashed = false")
    res = svc.files().list(
        q=q, fields="files(id)", pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def upload(svc, folder_id: str, path: Path, name: str | None = None) -> dict:
    """같은 이름이 있으면 내용만 교체(파일 ID·공유링크 유지), 없으면 새로 생성."""
    name = name or path.name
    mimetype = MIME.get(path.suffix.lower(), "application/octet-stream")
    media = MediaFileUpload(str(path), mimetype=mimetype, resumable=False)

    existing = _find(svc, folder_id, name)
    if existing:
        return svc.files().update(
            fileId=existing, media_body=media,
            fields="id,name,webViewLink", supportsAllDrives=True,
        ).execute()

    return svc.files().create(
        body={"name": name, "parents": [folder_id]},
        media_body=media,
        fields="id,name,webViewLink", supportsAllDrives=True,
    ).execute()
