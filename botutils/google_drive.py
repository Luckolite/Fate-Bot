"""Small async Google Drive client used by Fate backups and FateControl.

The implementation talks to Google's REST endpoints directly so the bot does
not need a blocking Google SDK.  Token-file reads and writes are dispatched to
worker threads and uploads stream bounded chunks from disk.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import time
from typing import Any, AsyncIterator
from urllib.parse import urlencode

import aiohttp

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API_URL = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive",)
FOLDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class GoogleDriveError(RuntimeError):
    """Raised when Drive credentials, authorization, or an API call fails."""


@dataclass(frozen=True)
class GoogleDriveCredentials:
    client_id: str
    client_secret: str
    redirect_uri: str

    @classmethod
    def load(cls, settings: dict[str, Any], repository_root: Path) -> "GoogleDriveCredentials":
        drive = settings.get("google_drive", {})
        if not isinstance(drive, dict):
            drive = {}
        client_id = os.getenv("GOOGLE_DRIVE_CLIENT_ID", "").strip()
        client_secret = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET", "").strip()
        redirect_uri = os.getenv("GOOGLE_DRIVE_REDIRECT_URI", "").strip()

        configured_path = drive.get("client_credentials_path")
        credentials_path = Path(
            os.path.expandvars(
                os.path.expanduser(
                    configured_path
                    if isinstance(configured_path, str) and configured_path.strip()
                    else str(repository_root / "data" / "google-drive-client.json")
                )
            )
        )
        if not credentials_path.is_absolute():
            credentials_path = repository_root / credentials_path
        if (not client_id or not client_secret or not redirect_uri) and credentials_path.is_file():
            try:
                payload = json.loads(credentials_path.read_text(encoding="utf-8-sig"))
                candidate = payload.get("web") or payload.get("installed") or payload
                if isinstance(candidate, dict):
                    client_id = client_id or str(candidate.get("client_id", "")).strip()
                    client_secret = client_secret or str(candidate.get("client_secret", "")).strip()
                    configured_redirects = candidate.get("redirect_uris", [])
                    if not redirect_uri and isinstance(configured_redirects, list) and configured_redirects:
                        redirect_uri = str(configured_redirects[0]).strip()
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
                raise GoogleDriveError(
                    f"Could not read Google OAuth client credentials: {error}"
                ) from error

        configured_redirect = drive.get("redirect_uri")
        if isinstance(configured_redirect, str) and configured_redirect.strip():
            redirect_uri = configured_redirect.strip()
        if not client_id or not client_secret or not redirect_uri:
            raise GoogleDriveError(
                "Add a Google OAuth web client at data/google-drive-client.json "
                "and configure its callback URL before linking Drive."
            )
        return cls(client_id, client_secret, redirect_uri)


class GoogleDriveTokenStore:
    """Atomic, owner-readable storage for Google's refresh credentials."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise GoogleDriveError(f"Could not read the Google Drive token: {error}") from error
        if not isinstance(payload, dict):
            raise GoogleDriveError("The Google Drive token file is malformed.")
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
        )
        temporary = Path(temporary_name)
        try:
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, separators=(",", ":"), ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                self.path.chmod(0o600)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise GoogleDriveError(f"Could not remove the Google Drive token: {error}") from error


class GoogleDriveClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        credentials: GoogleDriveCredentials,
        token_store: GoogleDriveTokenStore,
    ) -> None:
        self.session = session
        self.credentials = credentials
        self.token_store = token_store
        self._refresh_lock = asyncio.Lock()

    def authorization_url(self, state: str | None = None) -> tuple[str, str]:
        oauth_state = state or secrets.token_urlsafe(32)
        query = urlencode(
            {
                "client_id": self.credentials.client_id,
                "redirect_uri": self.credentials.redirect_uri,
                "response_type": "code",
                "scope": " ".join(DRIVE_SCOPES),
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent",
                "state": oauth_state,
            }
        )
        return f"{GOOGLE_AUTH_URL}?{query}", oauth_state

    async def linked(self) -> bool:
        token = await asyncio.to_thread(self.token_store.load)
        return bool(token.get("refresh_token"))

    async def exchange_code(self, code: str) -> None:
        existing = await asyncio.to_thread(self.token_store.load)
        payload = await self._token_request(
            {
                "code": code,
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
                "redirect_uri": self.credentials.redirect_uri,
                "grant_type": "authorization_code",
            }
        )
        if not payload.get("refresh_token") and existing.get("refresh_token"):
            payload["refresh_token"] = existing["refresh_token"]
        if not payload.get("refresh_token"):
            raise GoogleDriveError(
                "Google did not return a refresh token. Revoke Fate's Drive access and link again."
            )
        payload["expires_at"] = time() + max(0, int(payload.get("expires_in", 3600)))
        await asyncio.to_thread(self.token_store.save, payload)

    async def unlink(self) -> None:
        token = await asyncio.to_thread(self.token_store.load)
        access_token = token.get("access_token")
        refresh_token = token.get("refresh_token")
        revoke_token = refresh_token or access_token
        if revoke_token:
            try:
                async with self.session.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": revoke_token},
                ) as response:
                    await response.read()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
        await asyncio.to_thread(self.token_store.clear)

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        try:
            async with self.session.post(GOOGLE_TOKEN_URL, data=data) as response:
                payload = await _json_response(response)
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise GoogleDriveError(f"Google authorization is unavailable: {error}") from error
        if response.status < 200 or response.status >= 300:
            raise GoogleDriveError(_google_error(payload, "Google rejected the authorization request."))
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise GoogleDriveError("Google returned an incomplete authorization response.")
        return payload

    async def access_token(self, *, force_refresh: bool = False) -> str:
        async with self._refresh_lock:
            token = await asyncio.to_thread(self.token_store.load)
            access_token = token.get("access_token")
            expires_at = float(token.get("expires_at", 0) or 0)
            if access_token and not force_refresh and expires_at > time() + 60:
                return str(access_token)
            refresh_token = token.get("refresh_token")
            if not refresh_token:
                raise GoogleDriveError("Google Drive is not linked.")
            refreshed = await self._token_request(
                {
                    "refresh_token": str(refresh_token),
                    "client_id": self.credentials.client_id,
                    "client_secret": self.credentials.client_secret,
                    "grant_type": "refresh_token",
                }
            )
            refreshed["refresh_token"] = refresh_token
            refreshed["expires_at"] = time() + max(
                0, int(refreshed.get("expires_in", 3600))
            )
            await asyncio.to_thread(self.token_store.save, refreshed)
            return str(refreshed["access_token"])

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        for attempt in range(2):
            token = await self.access_token(force_refresh=attempt == 1)
            try:
                async with self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    payload = await _json_response(response)
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                raise GoogleDriveError(f"Google Drive is unavailable: {error}") from error
            if response.status == 401 and attempt == 0:
                continue
            if response.status not in expected:
                raise GoogleDriveError(_google_error(payload, "Google Drive rejected the request."))
            return payload if isinstance(payload, dict) else {}
        raise GoogleDriveError("Google Drive authorization expired.")

    async def list_folders(self, parent_id: str = "root") -> list[dict[str, str]]:
        _validate_folder_id(parent_id)
        query = (
            f"'{parent_id}' in parents and trashed = false and "
            f"mimeType = '{DRIVE_FOLDER_MIME}'"
        )
        folders: list[dict[str, str]] = []
        page_token = ""
        while True:
            params = {
                "q": query,
                "fields": "nextPageToken,files(id,name)",
                "orderBy": "name_natural",
                "pageSize": "100",
            }
            if page_token:
                params["pageToken"] = page_token
            payload = await self._request_json("GET", f"{DRIVE_API_URL}/files", params=params)
            for folder in payload.get("files", []):
                if isinstance(folder, dict) and folder.get("id") and folder.get("name"):
                    folders.append({"id": str(folder["id"]), "name": str(folder["name"])})
            page_token = str(payload.get("nextPageToken", ""))
            if not page_token:
                return folders

    async def resolve_folder_path(self, folder_id: str) -> str:
        _validate_folder_id(folder_id)
        if folder_id == "root":
            return "My Drive"
        names: list[str] = []
        current = folder_id
        for _ in range(64):
            payload = await self._request_json(
                "GET",
                f"{DRIVE_API_URL}/files/{current}",
                params={"fields": "id,name,mimeType,parents"},
            )
            if payload.get("mimeType") != DRIVE_FOLDER_MIME:
                raise GoogleDriveError("The selected Drive item is not a folder.")
            names.append(str(payload.get("name") or "Unnamed folder"))
            parents = payload.get("parents")
            if not isinstance(parents, list) or not parents or parents[0] == "root":
                return "My Drive / " + " / ".join(reversed(names))
            current = str(parents[0])
            _validate_folder_id(current)
        raise GoogleDriveError("The selected Drive folder path is too deep.")

    async def upload_backup(self, path: Path, folder_id: str) -> dict[str, Any]:
        _validate_folder_id(folder_id)
        size = await asyncio.to_thread(lambda: path.stat().st_size)
        token = await self.access_token()
        metadata = {
            "name": path.name,
            "parents": [folder_id],
            "mimeType": "application/zip",
            "appProperties": {"fateBackup": "true"},
        }
        try:
            async with self.session.post(
                DRIVE_UPLOAD_URL,
                params={"uploadType": "resumable", "fields": "id,name,size,createdTime"},
                json=metadata,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Upload-Content-Type": "application/zip",
                    "X-Upload-Content-Length": str(size),
                },
            ) as response:
                payload = await _json_response(response)
                if response.status not in (200, 201):
                    raise GoogleDriveError(_google_error(payload, "Could not start the Drive upload."))
                upload_url = response.headers.get("Location")
            if not upload_url:
                raise GoogleDriveError("Google did not return a resumable upload URL.")
            async with self.session.put(
                upload_url,
                data=_file_chunks(path),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/zip",
                    "Content-Length": str(size),
                },
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300),
            ) as response:
                payload = await _json_response(response)
                if response.status not in (200, 201):
                    raise GoogleDriveError(_google_error(payload, "The Drive upload failed."))
                return payload
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise GoogleDriveError(f"The Drive upload failed: {error}") from error

    async def prune_backups(
        self,
        folder_id: str,
        *,
        retention_days: int | None,
        max_backups: int | None,
        max_storage_bytes: int | None,
    ) -> list[str]:
        _validate_folder_id(folder_id)
        query = (
            f"'{folder_id}' in parents and trashed = false and "
            "appProperties has { key='fateBackup' and value='true' }"
        )
        files: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params = {
                "q": query,
                "fields": "nextPageToken,files(id,name,size,createdTime)",
                "orderBy": "createdTime",
                "pageSize": "1000",
            }
            if page_token:
                params["pageToken"] = page_token
            payload = await self._request_json(
                "GET", f"{DRIVE_API_URL}/files", params=params
            )
            files.extend(
                item for item in payload.get("files", []) if isinstance(item, dict)
            )
            page_token = str(payload.get("nextPageToken", ""))
            if not page_token:
                break
        files.sort(key=lambda item: str(item.get("createdTime", "")))
        now = datetime.now(timezone.utc)
        remove_ids: set[str] = set()
        if retention_days is not None:
            for item in files:
                created = _drive_time(item.get("createdTime"))
                if created and (now - created).total_seconds() > retention_days * 86400:
                    remove_ids.add(str(item.get("id", "")))

        remaining = [item for item in files if str(item.get("id", "")) not in remove_ids]
        while max_backups is not None and len(remaining) > max_backups:
            remove_ids.add(str(remaining.pop(0).get("id", "")))
        total = sum(int(item.get("size", 0) or 0) for item in remaining)
        while max_storage_bytes is not None and total > max_storage_bytes and remaining:
            oldest = remaining.pop(0)
            total -= int(oldest.get("size", 0) or 0)
            remove_ids.add(str(oldest.get("id", "")))

        removed: list[str] = []
        for item in files:
            file_id = str(item.get("id", ""))
            if not file_id or file_id not in remove_ids:
                continue
            await self._request_json(
                "DELETE", f"{DRIVE_API_URL}/files/{file_id}", expected=(204,)
            )
            removed.append(str(item.get("name") or file_id))
        return removed


async def _file_chunks(path: Path, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    stream = await asyncio.to_thread(path.open, "rb")
    try:
        while True:
            chunk = await asyncio.to_thread(stream.read, chunk_size)
            if not chunk:
                return
            yield chunk
    finally:
        await asyncio.to_thread(stream.close)


async def _json_response(response: aiohttp.ClientResponse) -> Any:
    raw = await response.text()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": {"message": raw[:500]}}


def _google_error(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error:
            description = payload.get("error_description")
            return f"{error}: {description}" if description else error
    return fallback


def _validate_folder_id(folder_id: str) -> None:
    if folder_id != "root" and not FOLDER_ID_PATTERN.fullmatch(folder_id):
        raise GoogleDriveError("The Google Drive folder ID is invalid.")


def _drive_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
