#!/usr/bin/env python3
"""LAN deployment server for the simulation data management prototype."""

from __future__ import annotations

import errno
import json
import mimetypes
import os
import posixpath
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 8088,
    "frontend_index": "frontend/index.html",
    "data_dir": "server_data",
}

INDEX_FILE = PROJECT_ROOT / DEFAULT_CONFIG["frontend_index"]
DATA_DIR = PROJECT_ROOT / DEFAULT_CONFIG["data_dir"]
ATTACHMENTS_DIR = DATA_DIR / "attachments"
LEGACY_ATTACHMENTS_DIR = PROJECT_ROOT / "data_attachment"
STATE_FILE = DATA_DIR / "state.json"
USERS_FILE = DATA_DIR / "users.json"
SESSION_COOKIE = "simulation_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
SESSIONS: dict[str, dict] = {}
SESSIONS_LOCK = threading.Lock()
VALID_ROLES = {"admin", "simulation", "structure"}

# API contract used by frontend/index.html:
# - GET /api/state -> {"state": object | null}
# - PUT /api/state -> persisted state object
# - POST /api/upload?cardId=...&folderName=... -> uploaded file metadata
# - GET/DELETE /api/attachments/<relative-folder>/<file>


def resolve_project_path(value: str | Path, fallback: str) -> Path:
    raw = Path(str(value or fallback)).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    return (PROJECT_ROOT / raw).resolve()


def load_runtime_config(config_path: Path) -> dict:
    config = dict(DEFAULT_CONFIG)
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as config_file:
            loaded = json.load(config_file)
        if not isinstance(loaded, dict):
            raise ValueError("Config file must contain a JSON object")
        config.update(loaded)
    return config


def apply_runtime_config(config: dict) -> None:
    global INDEX_FILE, DATA_DIR, ATTACHMENTS_DIR, LEGACY_ATTACHMENTS_DIR, STATE_FILE, USERS_FILE

    INDEX_FILE = resolve_project_path(config.get("frontend_index"), DEFAULT_CONFIG["frontend_index"])
    DATA_DIR = resolve_project_path(config.get("data_dir"), DEFAULT_CONFIG["data_dir"])
    ATTACHMENTS_DIR = DATA_DIR / "attachments"
    LEGACY_ATTACHMENTS_DIR = PROJECT_ROOT / "data_attachment"
    STATE_FILE = DATA_DIR / "state.json"
    USERS_FILE = DATA_DIR / "users.json"


def safe_part(value: str, fallback: str = "item") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", value or "").strip("._")
    return cleaned[:100] or fallback


def safe_relative_path(value: str, fallback: str = "item") -> Path:
    parts = [safe_part(part, fallback) for part in str(value or "").split("/") if part.strip()]
    return Path(*parts) if parts else Path(fallback)


def quote_path(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def write_json(
    handler: SimpleHTTPRequestHandler,
    payload: object,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def default_users_payload() -> dict:
    return {
        "users": [
            {
                "id": "u-admin",
                "username": "admin",
                "password": "admin123",
                "role": "admin",
                "displayName": "管理员",
                "enabled": True,
                "createdAt": now_iso(),
            }
        ]
    }


def sanitize_user(user: dict) -> dict:
    return {
        "id": user.get("id", ""),
        "username": user.get("username", ""),
        "role": user.get("role", "structure"),
        "displayName": user.get("displayName", user.get("username", "")),
        "enabled": bool(user.get("enabled", True)),
        "createdAt": user.get("createdAt", ""),
    }


def normalize_user(user: dict, fallback_id: str) -> dict:
    role = user.get("role") if user.get("role") in VALID_ROLES else "structure"
    username = str(user.get("username") or "").strip()
    return {
        "id": str(user.get("id") or fallback_id),
        "username": username,
        "password": str(user.get("password") or ""),
        "role": role,
        "displayName": str(user.get("displayName") or username),
        "enabled": bool(user.get("enabled", True)),
        "createdAt": str(user.get("createdAt") or now_iso()),
    }


def load_users() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not USERS_FILE.exists():
        payload = default_users_payload()
        save_users(payload)
        return payload
    try:
        raw = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raw = default_users_payload()
    users = raw.get("users") if isinstance(raw, dict) else []
    if not isinstance(users, list):
        users = []
    normalized = [
        normalize_user(user, f"user-{index + 1}")
        for index, user in enumerate(users)
        if isinstance(user, dict) and str(user.get("username") or "").strip()
    ]
    if not normalized:
        normalized = default_users_payload()["users"]
    payload = {"users": normalized}
    save_users(payload)
    return payload


def save_users(payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="users-", suffix=".json", dir=str(DATA_DIR))
    with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
        json.dump(payload, temp_file, ensure_ascii=False, indent=2)
    os.replace(temp_name, USERS_FILE)


def enabled_admin_count(users: list[dict]) -> int:
    return sum(1 for user in users if user.get("role") == "admin" and user.get("enabled", True))


class SimulationHandler(SimpleHTTPRequestHandler):
    """Small standard-library HTTP server for LAN deployment.

    Keep this server dependency-free so the tool can run on an engineering
    workstation with only Python installed.
    """

    server_version = "SimulationDataServer/0.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_ROOT), **kwargs)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/me":
            self.handle_get_me()
            return
        if parsed.path == "/api/users":
            self.handle_list_users()
            return
        if parsed.path == "/api/state":
            self.handle_get_state()
            return
        if parsed.path.startswith("/api/attachments/"):
            self.handle_download_attachment(parsed)
            return
        if parsed.path in {"/", "/index.html"}:
            self.serve_file(INDEX_FILE)
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self.serve_file(INDEX_FILE, head_only=True)
            return
        super().do_HEAD()

    def do_PUT(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/state":
            self.handle_put_state()
            return
        if parsed.path.startswith("/api/users/"):
            self.handle_update_user(parsed.path)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/login":
            self.handle_login()
            return
        if parsed.path == "/api/logout":
            self.handle_logout()
            return
        if parsed.path == "/api/users":
            self.handle_create_user()
            return
        if parsed.path == "/api/upload":
            self.handle_upload(parsed)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/users/"):
            self.handle_delete_user(parsed.path)
            return
        if parsed.path.startswith("/api/attachments/"):
            self.handle_delete_attachment(parsed.path)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON payload must be an object")
        return payload

    def cookie_value(self, name: str) -> str:
        cookie_header = self.headers.get("Cookie", "")
        for item in cookie_header.split(";"):
            key, _, value = item.strip().partition("=")
            if key == name:
                return urllib.parse.unquote(value)
        return ""

    def current_user(self) -> dict | None:
        session_id = self.cookie_value(SESSION_COOKIE)
        if not session_id:
            return None
        now = time.time()
        with SESSIONS_LOCK:
            session = SESSIONS.get(session_id)
            if not session:
                return None
            if session.get("expiresAt", 0) < now:
                SESSIONS.pop(session_id, None)
                return None
            session["expiresAt"] = now + SESSION_TTL_SECONDS
            return dict(session.get("user") or {})

    def require_login(self) -> dict | None:
        user = self.current_user()
        if not user:
            write_json(self, {"error": "请先登录。"}, HTTPStatus.UNAUTHORIZED)
            return None
        return user

    def require_role(self, *roles: str) -> dict | None:
        user = self.require_login()
        if not user:
            return None
        if user.get("role") not in roles:
            write_json(self, {"error": "当前角色没有权限执行该操作。"}, HTTPStatus.FORBIDDEN)
            return None
        return user

    def require_editor(self) -> dict | None:
        return self.require_role("admin", "simulation")

    def serve_file(self, path: Path, head_only: bool = False, download_name: str = "") -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if download_name:
            encoded = urllib.parse.quote(download_name)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded}")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def handle_login(self) -> None:
        try:
            payload = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        users_payload = load_users()
        user = next((item for item in users_payload["users"] if item["username"] == username), None)
        if not user or not user.get("enabled", True) or user.get("password") != password:
            write_json(self, {"error": "账号或密码错误。"}, HTTPStatus.UNAUTHORIZED)
            return
        public_user = sanitize_user(user)
        session_id = secrets.token_urlsafe(32)
        with SESSIONS_LOCK:
            SESSIONS[session_id] = {
                "user": public_user,
                "expiresAt": time.time() + SESSION_TTL_SECONDS,
            }
        cookie = (
            f"{SESSION_COOKIE}={urllib.parse.quote(session_id)}; "
            f"Path=/; Max-Age={SESSION_TTL_SECONDS}; HttpOnly; SameSite=Lax"
        )
        write_json(self, {"user": public_user}, headers={"Set-Cookie": cookie})

    def handle_logout(self) -> None:
        session_id = self.cookie_value(SESSION_COOKIE)
        if session_id:
            with SESSIONS_LOCK:
                SESSIONS.pop(session_id, None)
        cookie = f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
        write_json(self, {"ok": True}, headers={"Set-Cookie": cookie})

    def handle_get_me(self) -> None:
        user = self.current_user()
        if not user:
            write_json(self, {"authenticated": False})
            return
        write_json(self, {"authenticated": True, "user": user})

    def handle_list_users(self) -> None:
        if not self.require_role("admin"):
            return
        payload = load_users()
        write_json(self, {"users": [sanitize_user(user) for user in payload["users"]]})

    def handle_create_user(self) -> None:
        if not self.require_role("admin"):
            return
        try:
            payload = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "").strip()
        role = str(payload.get("role") or "structure")
        if not username or not password:
            write_json(self, {"error": "账号和密码不能为空。"}, HTTPStatus.BAD_REQUEST)
            return
        if role not in VALID_ROLES:
            write_json(self, {"error": "角色无效。"}, HTTPStatus.BAD_REQUEST)
            return
        users_payload = load_users()
        if any(user["username"] == username for user in users_payload["users"]):
            write_json(self, {"error": "账号已存在。"}, HTTPStatus.CONFLICT)
            return
        user = normalize_user(
            {
                "id": f"user-{int(time.time() * 1000)}-{secrets.token_hex(3)}",
                "username": username,
                "password": password,
                "role": role,
                "displayName": str(payload.get("displayName") or username).strip(),
                "enabled": bool(payload.get("enabled", True)),
                "createdAt": now_iso(),
            },
            "user",
        )
        users_payload["users"].append(user)
        save_users(users_payload)
        write_json(self, {"user": sanitize_user(user)}, HTTPStatus.CREATED)

    def handle_update_user(self, path: str) -> None:
        if not self.require_role("admin"):
            return
        user_id = urllib.parse.unquote(path.rsplit("/", 1)[-1])
        try:
            payload = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return
        users_payload = load_users()
        users = users_payload["users"]
        user = next((item for item in users if item["id"] == user_id), None)
        if not user:
            write_json(self, {"error": "用户不存在。"}, HTTPStatus.NOT_FOUND)
            return
        next_user = dict(user)
        if "password" in payload and str(payload.get("password") or "").strip():
            next_user["password"] = str(payload.get("password")).strip()
        if "displayName" in payload:
            next_user["displayName"] = str(payload.get("displayName") or next_user["username"]).strip()
        if "role" in payload:
            role = str(payload.get("role"))
            if role not in VALID_ROLES:
                write_json(self, {"error": "角色无效。"}, HTTPStatus.BAD_REQUEST)
                return
            next_user["role"] = role
        if "enabled" in payload:
            next_user["enabled"] = bool(payload.get("enabled"))
        next_users = [next_user if item["id"] == user_id else item for item in users]
        if enabled_admin_count(next_users) < 1:
            write_json(self, {"error": "至少需要保留一个启用的管理员。"}, HTTPStatus.BAD_REQUEST)
            return
        user.update(next_user)
        save_users(users_payload)
        write_json(self, {"user": sanitize_user(user)})

    def handle_delete_user(self, path: str) -> None:
        if not self.require_role("admin"):
            return
        user_id = urllib.parse.unquote(path.rsplit("/", 1)[-1])
        users_payload = load_users()
        users = users_payload["users"]
        user = next((item for item in users if item["id"] == user_id), None)
        if not user:
            write_json(self, {"error": "用户不存在。"}, HTTPStatus.NOT_FOUND)
            return
        next_users = [item for item in users if item["id"] != user_id]
        if enabled_admin_count(next_users) < 1:
            write_json(self, {"error": "至少需要保留一个启用的管理员。"}, HTTPStatus.BAD_REQUEST)
            return
        users_payload["users"] = next_users
        save_users(users_payload)
        write_json(self, {"ok": True})

    def handle_get_state(self) -> None:
        if not self.require_login():
            return
        if not STATE_FILE.exists():
            write_json(self, {"state": None})
            return
        try:
            write_json(self, {"state": json.loads(STATE_FILE.read_text(encoding="utf-8"))})
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "state.json is not valid JSON")

    def handle_put_state(self) -> None:
        if not self.require_editor():
            return
        try:
            payload = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return
        state_payload = payload.get("state") if isinstance(payload, dict) and isinstance(payload.get("state"), dict) else payload
        if not isinstance(state_payload, dict):
            self.send_error(HTTPStatus.BAD_REQUEST, "State payload must be a JSON object")
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="state-", suffix=".json", dir=str(DATA_DIR))
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            json.dump(state_payload, temp_file, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp_name, STATE_FILE)
        self.export_card_details(state_payload)
        write_json(self, {"ok": True})

    def handle_upload(self, parsed: urllib.parse.ParseResult) -> None:
        if not self.require_editor():
            return
        params = urllib.parse.parse_qs(parsed.query)
        card_id = safe_part(params.get("cardId", [""])[0], "card")
        folder_name = self.upload_folder_name(params, card_id)
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if "multipart/form-data" not in content_type:
            self.send_error(HTTPStatus.BAD_REQUEST, "Expected multipart/form-data")
            return

        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
        )
        target_dir = (ATTACHMENTS_DIR / safe_relative_path(folder_name, card_id)).resolve()
        if not self.is_attachment_path(target_dir):
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid attachment path")
            return
        target_dir.mkdir(parents=True, exist_ok=True)
        self.write_upload_card_detail(target_dir, params)
        uploaded = []
        for part in message.iter_parts():
            filename = part.get_filename()
            if not filename:
                continue
            original_name = Path(filename).name
            stored_name = self.unique_filename(target_dir, original_name)
            data = part.get_payload(decode=True) or b""
            (target_dir / stored_name).write_bytes(data)
            uploaded.append(
                {
                    "id": f"file-{card_id}-{stored_name}",
                    "name": original_name,
                    "storedName": stored_name,
                    "folderName": folder_name,
                    "relativePath": f"{DATA_DIR.name}/attachments/{folder_name}/{stored_name}",
                    "type": "",
                    "size": len(data),
                    "uploadedAt": "",
                    "legacy": False,
                    "serverStored": True,
                    "url": f"/api/attachments/{quote_path(folder_name)}/{quote_path(stored_name)}",
                }
            )
        write_json(self, {"files": uploaded})

    def upload_folder_name(self, params: dict[str, list[str]], card_id: str) -> str:
        provided = params.get("folderName", [""])[0]
        if provided:
            return "/".join(safe_part(part, card_id) for part in provided.split("/") if part.strip())
        part_type = safe_part(params.get("partType", ["未分类"])[0], "未分类")
        part_label = safe_part(
            f"{params.get('partCode', [''])[0]}_{params.get('partName', [''])[0]}",
            "未命名零件",
        )
        card_label = safe_part(
            f"{params.get('cardCode', [''])[0]}_{params.get('cardTitle', [''])[0]}_{card_id}",
            card_id,
        )
        return f"{part_type}/{part_label}/{card_label}"

    def write_upload_card_detail(self, target_dir: Path, params: dict[str, list[str]]) -> None:
        detail = {
            "cardId": params.get("cardId", [""])[0],
            "cardCode": params.get("cardCode", [""])[0],
            "cardTitle": params.get("cardTitle", [""])[0],
            "partType": params.get("partType", [""])[0],
            "partCode": params.get("partCode", [""])[0],
            "partName": params.get("partName", [""])[0],
        }
        (target_dir / "card_detail.json").write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")

    def unique_filename(self, directory: Path, original_name: str) -> str:
        stem = safe_part(Path(original_name).stem, "file")
        suffix = Path(original_name).suffix
        candidate = f"{stem}{suffix}"
        counter = 1
        while (directory / candidate).exists():
            candidate = f"{stem}_{counter}{suffix}"
            counter += 1
        return candidate

    def export_card_details(self, state_payload: dict) -> None:
        parts = {part.get("id"): part for part in state_payload.get("parts", []) if isinstance(part, dict)}
        for card in state_payload.get("cards", []):
            if not isinstance(card, dict):
                continue
            part = parts.get(card.get("partId"), {})
            folder_name = self.card_folder_name(card, part)
            target_dir = (ATTACHMENTS_DIR / safe_relative_path(folder_name, str(card.get("id") or "card"))).resolve()
            if not self.is_attachment_path(target_dir):
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "part": part,
                "card": card,
            }
            (target_dir / "card_detail.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def card_folder_name(self, card: dict, part: dict) -> str:
        card_id = safe_part(str(card.get("id") or ""), "card")
        part_type = safe_part(str(part.get("type") or "未分类"), "未分类")
        part_label = safe_part(f"{part.get('code') or ''}_{part.get('name') or ''}", "未命名零件")
        card_label = safe_part(f"{card.get('code') or ''}_{card.get('title') or ''}_{card_id}", card_id)
        return f"{part_type}/{part_label}/{card_label}"

    def parse_attachment_request(self, path: str) -> tuple[str, str]:
        parts = [urllib.parse.unquote(item) for item in path.split("/") if item]
        if len(parts) < 3 or parts[:2] != ["api", "attachments"]:
            return "", ""
        if len(parts) == 3:
            return parts[2], ""
        return "/".join(parts[2:-1]), parts[-1]

    def handle_download_attachment(self, parsed: urllib.parse.ParseResult) -> None:
        if not self.require_login():
            return
        folder_name, stored_name = self.parse_attachment_request(parsed.path)
        if not folder_name or not stored_name:
            self.send_error(HTTPStatus.NOT_FOUND, "Attachment not found")
            return
        file_path = self.attachment_file_path(folder_name, stored_name)
        if not file_path:
            self.send_error(HTTPStatus.NOT_FOUND, "Attachment not found")
            return
        query = urllib.parse.parse_qs(parsed.query)
        download_name = urllib.parse.unquote(query.get("name", [""])[0]) or stored_name
        force_download = query.get("download", [""])[0] == "1"
        self.serve_file(file_path, download_name=download_name if force_download else "")

    def handle_delete_attachment(self, path: str) -> None:
        if not self.require_editor():
            return
        folder_name, stored_name = self.parse_attachment_request(path)
        if folder_name and not stored_name:
            for root, checker in (
                (ATTACHMENTS_DIR, self.is_attachment_path),
                (LEGACY_ATTACHMENTS_DIR, self.is_legacy_attachment_path),
            ):
                folder = (root / safe_relative_path(folder_name)).resolve()
                if checker(folder) and folder.exists():
                    shutil.rmtree(folder)
            write_json(self, {"ok": True})
            return
        if folder_name and stored_name:
            file_path = self.attachment_file_path(folder_name, stored_name, require_exists=False)
            if file_path and file_path.exists():
                file_path.unlink()
            write_json(self, {"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Attachment not found")

    def is_attachment_path(self, path: Path) -> bool:
        try:
            path.relative_to(ATTACHMENTS_DIR.resolve())
            return True
        except ValueError:
            return False

    def is_legacy_attachment_path(self, path: Path) -> bool:
        try:
            path.relative_to(LEGACY_ATTACHMENTS_DIR.resolve())
            return True
        except ValueError:
            return False

    def attachment_file_path(self, folder_name: str, stored_name: str, require_exists: bool = True) -> Path | None:
        relative_folder = safe_relative_path(folder_name)
        filename = safe_part(stored_name)
        candidates = [
          (ATTACHMENTS_DIR / relative_folder / filename).resolve(),
          (LEGACY_ATTACHMENTS_DIR / relative_folder / filename).resolve(),
        ]
        for candidate in candidates:
            if not (self.is_attachment_path(candidate) or self.is_legacy_attachment_path(candidate)):
                continue
            if not require_exists or candidate.exists():
                return candidate
        return None

    def translate_path(self, path: str) -> str:
        parsed_path = urllib.parse.urlparse(path).path
        parsed_path = posixpath.normpath(urllib.parse.unquote(parsed_path))
        words = [word for word in parsed_path.split("/") if word]
        current = PROJECT_ROOT
        for word in words:
            if word in {os.curdir, os.pardir}:
                continue
            current = current / word
        return str(current)


def local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Serve the simulation data management prototype on the LAN.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Runtime config JSON path.")
    parser.add_argument("--host", help="Override bind host from config.")
    parser.add_argument("--port", type=int, help="Override bind port from config.")
    parser.add_argument("--data-dir", help="Override runtime data directory from config.")
    args = parser.parse_args()

    config_path = resolve_project_path(args.config, "config.json")
    config = load_runtime_config(config_path)
    if args.host:
        config["host"] = args.host
    if args.port:
        config["port"] = args.port
    if args.data_dir:
        config["data_dir"] = args.data_dir
    apply_runtime_config(config)

    host = str(config.get("host") or DEFAULT_CONFIG["host"])
    port = int(config.get("port") or DEFAULT_CONFIG["port"])

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    load_users()
    try:
        server = ThreadingHTTPServer((host, port), SimulationHandler)
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            print(f"端口已被占用：{host}:{port}", file=sys.stderr)
            print("处理方式：", file=sys.stderr)
            print("1. 如果旧服务还在运行，先停止旧的 python3 run.py 进程。", file=sys.stderr)
            print("2. 或者修改 config.json 里的 port，例如改成 8089。", file=sys.stderr)
            print("3. 在 PyCharm 的 Run Configuration 参数里也可以临时填写：--port 8089", file=sys.stderr)
            raise SystemExit(1) from error
        raise
    print(f"Local:   http://127.0.0.1:{port}/")
    print(f"LAN:     http://{local_ip()}:{port}/")
    print(f"Config:  {config_path}")
    print(f"Frontend:{INDEX_FILE}")
    print(f"Data:    {DATA_DIR}")
    print(f"Files:   {ATTACHMENTS_DIR}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
