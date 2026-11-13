import json
import hashlib
import hmac
import os
import secrets
import sqlite3
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from database import init_db

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "truth_or_dare.db"
PANEL_PATH = BASE_DIR / "index.html"
load_dotenv(BASE_DIR / ".env")

HOST = os.getenv("ADMIN_HOST", "127.0.0.1")
PORT = int(os.getenv("ADMIN_PORT", "8765"))
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
MEDIA_DIR = BASE_DIR / "media" / "answers"
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Jabulani")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
ADMIN_SESSIONS = set()


def db_rows(query, parameters=()):
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(query, parameters).fetchall()
    finally:
        connection.close()


def display_name(users, user_id):
    if not user_id:
        return "Unknown player"
    user = users.get(int(user_id))
    if not user:
        return str(user_id)
    return user["display_name"] or user["username"] or str(user_id)


def media_url(file_id):
    if not file_id:
        return ""
    return "/api/media/" + quote(file_id, safe="")


def password_matches(password):
    try:
        salt, expected = ADMIN_PASSWORD_HASH.split("$", 1)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 310000
    ).hex()
    return hmac.compare_digest(actual, expected)


def build_sessions():
    users = {
        row["telegram_id"]: row
        for row in db_rows("SELECT telegram_id, username, display_name FROM users")
    }
    games = db_rows("""
        SELECT code, player1_id, player2_id, state, game_type, difficulty, created_at
        FROM games
        ORDER BY datetime(created_at) DESC, rowid DESC
    """)
    history = db_rows("""
        SELECT * FROM game_history
        ORDER BY id
    """)
    by_game = {}
    for row in history:
        by_game.setdefault(row["game_code"], []).append(row)

    sessions = []
    for game in games:
        players = []
        for player_id in (game["player1_id"], game["player2_id"]):
            if player_id:
                user = users.get(player_id)
                players.append({
                    "id": player_id,
                    "name": display_name(users, player_id),
                    "username": user["username"] if user else "",
                })
        rounds = []
        for item in by_game.get(game["code"], []):
            challenged_id = item["challenged_player"]
            selected_id = item["selected_by"] or (
                game["player1_id"] if challenged_id == game["player2_id"] else game["player2_id"]
            )
            answer_file_id = item["answer_file_id"]
            rounds.append({
                "type": item["prompt_type"] or "question",
                "prompt": item["prompt_text"] or "",
                "selectedBy": item["selected_by_name"] or display_name(users, selected_id),
                "selectedById": selected_id or "",
                "challenged": item["challenged_player_name"] or display_name(users, challenged_id),
                "challengedId": challenged_id or "",
                "answerer": item["answerer_name"] or display_name(users, item["answerer_id"]),
                "answererId": item["answerer_id"] or "",
                "answer": item["answer_text"] or "",
                "answerType": item["answer_type"] or ("photo" if answer_file_id else "text"),
                "imageUrl": (
                    "/media/answers/" + quote(Path(item["answer_media_path"]).name, safe="")
                    if item["answer_media_path"]
                    else media_url(answer_file_id)
                ),
                "mediaKind": item["answer_media_kind"] or "",
                "caption": item["answer_caption"] or "",
                "createdAt": item["created_at"] or "",
                "answeredAt": item["answered_at"] or "",
            })
        sessions.append({
            "code": game["code"],
            "status": game["state"],
            "gameType": game["game_type"] or "truth or dare",
            "difficulty": game["difficulty"] or "standard",
            "createdAt": game["created_at"] or "",
            "players": players,
            "rounds": rounds,
        })
    return {"sessions": sessions}


class AdminHandler(BaseHTTPRequestHandler):
    def _send(self, status, content_type, body):
        body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload, extra_headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _session_id(self):
        cookies = self.headers.get("Cookie", "").split(";")
        for cookie in cookies:
            name, _, value = cookie.strip().partition("=")
            if name == "admin_session":
                return value
        return ""

    def _authenticated(self):
        return bool(self._session_id() and self._session_id() in ADMIN_SESSIONS)

    def _require_auth(self):
        if self._authenticated():
            return True
        self._json(401, {"authenticated": False, "error": "Login required"})
        return False

    def _request_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            try:
                payload = self._request_json()
            except (json.JSONDecodeError, ValueError):
                self._json(400, {"error": "Invalid request"})
                return
            username = str(payload.get("username", ""))
            password = str(payload.get("password", ""))
            if hmac.compare_digest(username, ADMIN_USERNAME) and password_matches(password):
                session_id = secrets.token_urlsafe(32)
                ADMIN_SESSIONS.add(session_id)
                self._json(200, {"authenticated": True}, [
                    ("Set-Cookie", f"admin_session={session_id}; HttpOnly; SameSite=Lax; Path=/"),
                ])
            else:
                self._json(401, {"authenticated": False, "error": "Invalid username or password"})
            return
        if path == "/api/logout":
            session_id = self._session_id()
            ADMIN_SESSIONS.discard(session_id)
            self._json(200, {"authenticated": False}, [
                ("Set-Cookie", "admin_session=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/"),
            ])
            return
        self._send(404, "text/plain; charset=utf-8", "Not found")

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/":
                self._send(200, "text/html; charset=utf-8", PANEL_PATH.read_bytes())
                return
            if path == "/api/auth":
                self._json(200, {"authenticated": self._authenticated()})
                return
            if path.startswith("/api/") or path.startswith("/media/"):
                if not self._require_auth():
                    return
            if path == "/api/sessions":
                self._send(200, "application/json; charset=utf-8", json.dumps(build_sessions()))
                return
            if path.startswith("/media/answers/"):
                self._send_local_media(path.removeprefix("/media/answers/"))
                return
            if path.startswith("/api/media/"):
                self._send_media(unquote(path.removeprefix("/api/media/")))
                return
            self._send(404, "text/plain; charset=utf-8", "Not found")
        except Exception as error:
            self._send(500, "application/json; charset=utf-8", json.dumps({"error": str(error)}))

    def _send_media(self, file_id):
        if not TOKEN:
            self._send(503, "text/plain; charset=utf-8", "TELEGRAM_BOT_TOKEN is not configured")
            return
        api_url = f"https://api.telegram.org/bot{TOKEN}/getFile?file_id={quote(file_id, safe='')}"
        with urlopen(Request(api_url), timeout=15) as response:
            result = json.loads(response.read())
        if not result.get("ok") or not result["result"].get("file_path"):
            self._send(404, "text/plain; charset=utf-8", "Telegram media was not found")
            return
        file_path = result["result"]["file_path"]
        with urlopen(f"https://api.telegram.org/file/bot{TOKEN}/{file_path}", timeout=30) as response:
            media = response.read()
            content_type = response.headers.get_content_type() or "application/octet-stream"
        self._send(200, content_type, media)

    def _send_local_media(self, filename):
        candidate = (MEDIA_DIR / Path(unquote(filename)).name).resolve()
        if MEDIA_DIR.resolve() not in candidate.parents or not candidate.is_file():
            self._send(404, "text/plain; charset=utf-8", "Media was not found")
            return
        content_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".mp4": "video/mp4",
            ".mov": "video/quicktime",
            ".webm": "video/webm",
            ".mkv": "video/x-matroska",
            ".avi": "video/x-msvideo",
        }.get(candidate.suffix.lower(), "application/octet-stream")
        self._send(200, content_type, candidate.read_bytes())

    def log_message(self, *_args):
        return


def start_server():
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), AdminHandler)
    thread = Thread(target=server.serve_forever, name="truth-dare-admin", daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    server = start_server()
    print(f"Truth & Dare admin panel: http://{HOST}:{PORT}")
    print(f"Database: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
