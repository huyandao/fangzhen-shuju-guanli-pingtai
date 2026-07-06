#!/usr/bin/env python3
"""LAN deployment server for the simulation data management prototype."""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import re
import shutil
import socket
import tempfile
import urllib.parse
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "原型设计" / "index.html"
DATA_DIR = ROOT / "server_data"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
STATE_FILE = DATA_DIR / "state.json"

# API contract used by 原型设计/index.html:
# - GET /api/state -> {"state": object | null}
# - PUT /api/state -> persisted state object
# - POST /api/upload?cardId=...&folderName=... -> uploaded file metadata
# - GET/DELETE /api/attachments/<folder>/<file>


def safe_part(value: str, fallback: str = "item") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", value or "").strip("._")
    return cleaned[:100] or fallback


def write_json(handler: SimpleHTTPRequestHandler, payload: object, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class SimulationHandler(SimpleHTTPRequestHandler):
    """Small standard-library HTTP server for LAN deployment.

    Keep this server dependency-free so the tool can run on an engineering
    workstation with only Python installed.
    """

    server_version = "SimulationDataServer/0.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/state":
            self.handle_get_state()
            return
        if parsed.path.startswith("/api/attachments/"):
            self.handle_download_attachment(parsed.path)
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
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/upload":
            self.handle_upload(parsed)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/attachments/"):
            self.handle_delete_attachment(parsed.path)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def serve_file(self, path: Path, head_only: bool = False) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def handle_get_state(self) -> None:
        if not STATE_FILE.exists():
            write_json(self, {"state": None})
            return
        try:
            write_json(self, {"state": json.loads(STATE_FILE.read_text(encoding="utf-8"))})
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "state.json is not valid JSON")

    def handle_put_state(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
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
        write_json(self, {"ok": True})

    def handle_upload(self, parsed: urllib.parse.ParseResult) -> None:
        params = urllib.parse.parse_qs(parsed.query)
        card_id = safe_part(params.get("cardId", [""])[0], "card")
        folder_name = safe_part(params.get("folderName", [card_id])[0], card_id)
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if "multipart/form-data" not in content_type:
            self.send_error(HTTPStatus.BAD_REQUEST, "Expected multipart/form-data")
            return

        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
        )
        target_dir = ATTACHMENTS_DIR / folder_name
        target_dir.mkdir(parents=True, exist_ok=True)
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
                    "relativePath": f"server_data/attachments/{folder_name}/{stored_name}",
                    "type": "",
                    "size": len(data),
                    "uploadedAt": "",
                    "legacy": False,
                    "serverStored": True,
                    "url": f"/api/attachments/{urllib.parse.quote(folder_name)}/{urllib.parse.quote(stored_name)}",
                }
            )
        write_json(self, {"files": uploaded})

    def unique_filename(self, directory: Path, original_name: str) -> str:
        stem = safe_part(Path(original_name).stem, "file")
        suffix = Path(original_name).suffix
        candidate = f"{stem}{suffix}"
        counter = 1
        while (directory / candidate).exists():
            candidate = f"{stem}_{counter}{suffix}"
            counter += 1
        return candidate

    def handle_download_attachment(self, path: str) -> None:
        parts = [urllib.parse.unquote(item) for item in path.split("/") if item]
        if len(parts) != 4:
            self.send_error(HTTPStatus.NOT_FOUND, "Attachment not found")
            return
        _, _, folder_name, stored_name = parts
        file_path = (ATTACHMENTS_DIR / safe_part(folder_name) / safe_part(stored_name)).resolve()
        if not self.is_attachment_path(file_path) or not file_path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Attachment not found")
            return
        self.serve_file(file_path)

    def handle_delete_attachment(self, path: str) -> None:
        parts = [urllib.parse.unquote(item) for item in path.split("/") if item]
        if len(parts) == 3:
            _, _, folder_name = parts
            folder = (ATTACHMENTS_DIR / safe_part(folder_name)).resolve()
            if self.is_attachment_path(folder) and folder.exists():
                shutil.rmtree(folder)
            write_json(self, {"ok": True})
            return
        if len(parts) == 4:
            _, _, folder_name, stored_name = parts
            file_path = (ATTACHMENTS_DIR / safe_part(folder_name) / safe_part(stored_name)).resolve()
            if self.is_attachment_path(file_path) and file_path.exists():
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

    def translate_path(self, path: str) -> str:
        parsed_path = urllib.parse.urlparse(path).path
        parsed_path = posixpath.normpath(urllib.parse.unquote(parsed_path))
        words = [word for word in parsed_path.split("/") if word]
        current = ROOT
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
    parser.add_argument("--host", default="0.0.0.0", help="Bind host, defaults to all interfaces.")
    parser.add_argument("--port", type=int, default=8088, help="Bind port, defaults to 8088.")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), SimulationHandler)
    print(f"Local:   http://127.0.0.1:{args.port}/")
    print(f"LAN:     http://{local_ip()}:{args.port}/")
    print(f"Data:    {DATA_DIR}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
