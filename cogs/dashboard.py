import asyncio
import html
import ipaddress
import os
import re
import secrets
import socket
import sys
import time
from io import BytesIO
from typing import Any
from urllib.parse import quote

import aiosqlite
import discord
from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.web_request import FileField
from discord.ext import commands
from PIL import Image

from utils.settings import load_settings, update_settings


class Dashboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.dashboard_db = os.path.join(self.project_root, "dashboard_data.db")
        self.level_db = os.path.join(self.project_root, "level.db")

        self._load_dashboard_settings()

        self.level_card_upload_limit = int(os.getenv("LEVEL_CARD_UPLOAD_MAX_BYTES", "6291456"))
        self.level_card_allowed_ext = {".png", ".jpg", ".jpeg", ".webp"}
        self.session_cookie_name = "nemo_dashboard_session"
        self.sessions = {}
        self._cached_public_ip = ""
        self._public_ip_cache_expires_at = 0.0

        self.automod_cache = {}
        self._startup_done = False
        self.runner = None
        self.site = None

        self.app = web.Application()
        self.app.add_routes(
            [
                web.get("/", self.home_page),
                web.get("/login", self.login_page),
                web.post("/login", self.login_submit),
                web.post("/logout", self.logout_submit),
                web.get("/leaderboard", self.leaderboard_page),
                web.get("/level-formula", self.level_formula_page),
                web.post("/level-formula", self.level_formula_update),
                web.get("/level-cards", self.level_cards_page),
                web.post("/level-cards/settings", self.level_cards_settings_update),
                web.post("/level-cards/equip", self.level_cards_equip_update),
                web.post("/level-cards/toggle", self.level_cards_toggle_update),
                web.post("/level-cards/delete", self.level_cards_delete),
                web.post("/level-cards/upload", self.level_cards_upload),
                web.post("/level-cards/layout", self.level_cards_layout_update),
                web.post("/level-cards/layout-reset", self.level_cards_layout_reset),
                web.get("/automod", self.automod_page),
                web.post("/automod", self.automod_update),
                web.get("/settings", self.settings_page),
                web.post("/settings", self.settings_update),
                web.post("/restart", self.restart_bot),
                web.get("/console", self.console_page),
                web.post("/console", self.console_run),
            ]
        )

    async def setup_database(self):
        async with aiosqlite.connect(self.dashboard_db) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS automod_settings (
                    guild_id INTEGER PRIMARY KEY,
                    anti_link INTEGER NOT NULL DEFAULT 0,
                    blocked_words TEXT NOT NULL DEFAULT ''
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS dashboard_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            await db.commit()

    def _load_dashboard_settings(self):
        def as_int(value, default):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def as_bool(value, default):
            if value is None:
                return default
            return str(value).strip().lower() in {"1", "true", "yes", "on"}

        self.host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
        self.port = as_int(os.getenv("DASHBOARD_PORT"), 8080)
        self.public_url = os.getenv("DASHBOARD_PUBLIC_URL", "").strip()
        self.public_host = os.getenv("DASHBOARD_PUBLIC_HOST", "").strip()
        self.public_ip = os.getenv("DASHBOARD_PUBLIC_IP", "").strip()
        self.public_port = as_int(os.getenv("DASHBOARD_PUBLIC_PORT"), self.port)
        self.public_scheme = os.getenv("DASHBOARD_PUBLIC_SCHEME", "http").strip().lower()
        self.public_ip_endpoint = os.getenv("DASHBOARD_PUBLIC_IP_ENDPOINT", "").strip()
        self.public_ip_cache_ttl_seconds = as_int(
            os.getenv("DASHBOARD_PUBLIC_IP_CACHE_TTL_SECONDS"), 300
        )
        self.session_ttl_seconds = as_int(os.getenv("DASHBOARD_SESSION_TTL_SECONDS"), 43200)
        self.console_enabled = as_bool(os.getenv("DASHBOARD_ENABLE_CONSOLE"), False)

        self.view_token = os.getenv("DASHBOARD_VIEW_TOKEN", "")
        self.admin_token = os.getenv("DASHBOARD_ADMIN_TOKEN", "")
        self.dev_token = os.getenv("DASHBOARD_DEV_TOKEN", "")

        if self.public_scheme not in {"http", "https"}:
            self.public_scheme = "http"

        if self.admin_token and not self.view_token:
            self.view_token = self.admin_token
        if self.view_token and not self.admin_token:
            self.admin_token = self.view_token

        if not self.view_token and not self.admin_token:
            self.view_token = "change-me"
            self.admin_token = "change-me"
        if not self.dev_token:
            self.dev_token = "change-me-dev"

    async def load_automod_cache(self):
        async with aiosqlite.connect(self.dashboard_db) as db:
            async with db.execute(
                "SELECT guild_id, anti_link, blocked_words FROM automod_settings"
            ) as cursor:
                rows = await cursor.fetchall()

        self.automod_cache = {}
        for guild_id, anti_link, blocked_words in rows:
            words = [word.strip().lower() for word in (blocked_words or "").split(",") if word.strip()]
            self.automod_cache[guild_id] = {
                "anti_link": bool(anti_link),
                "blocked_words": words,
            }

    async def get_automod_settings(self, guild_id: int):
        if guild_id in self.automod_cache:
            return self.automod_cache[guild_id]

        async with aiosqlite.connect(self.dashboard_db) as db:
            async with db.execute(
                "SELECT anti_link, blocked_words FROM automod_settings WHERE guild_id = ?",
                (guild_id,),
            ) as cursor:
                row = await cursor.fetchone()

        if row:
            settings = {
                "anti_link": bool(row[0]),
                "blocked_words": [w.strip().lower() for w in (row[1] or "").split(",") if w.strip()],
            }
        else:
            settings = {"anti_link": False, "blocked_words": []}

        self.automod_cache[guild_id] = settings
        return settings

    async def save_automod_settings(self, guild_id: int, anti_link: bool, blocked_words_text: str):
        words = [word.strip().lower() for word in (blocked_words_text or "").split(",") if word.strip()]
        normalized = ", ".join(words)

        async with aiosqlite.connect(self.dashboard_db) as db:
            await db.execute(
                """
                INSERT INTO automod_settings (guild_id, anti_link, blocked_words)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    anti_link = excluded.anti_link,
                    blocked_words = excluded.blocked_words
                """,
                (guild_id, int(anti_link), normalized),
            )
            await db.commit()

        self.automod_cache[guild_id] = {"anti_link": anti_link, "blocked_words": words}

    async def get_setting(self, key: str, default_value: str = ""):
        mapping = {
            "presence_text": ("presence", "text"),
            "presence_type": ("presence", "type"),
        }
        target = mapping.get(key)
        if not target:
            return default_value

        settings = load_settings()
        section, field = target
        section_data = settings.get(section, {})
        return section_data.get(field, default_value)

    async def set_setting(self, key: str, value: str):
        mapping = {
            "presence_text": ("presence", "text"),
            "presence_type": ("presence", "type"),
        }
        target = mapping.get(key)
        if not target:
            return

        section, field = target
        update_settings({section: {field: value}})

    def _permission_rank(self, permission: str) -> int:
        return {"viewer": 1, "admin": 2, "dev": 3}.get(permission, 0)

    def _permission_allows(self, permission: str, required_permission: str) -> bool:
        return self._permission_rank(permission) >= self._permission_rank(required_permission)

    def _permission_for_login(self, username: str, passcode: str):
        normalized_username = (username or "").strip().lower()
        expected_passcodes = {
            "viewer": self.view_token,
            "admin": self.admin_token,
            "dev": self.dev_token,
        }
        expected = expected_passcodes.get(normalized_username, "")
        if expected and passcode == expected:
            return normalized_username
        return None

    def _safe_next_path(self, raw_next: str) -> str:
        next_path = (raw_next or "/").strip()
        if not next_path.startswith("/"):
            return "/"
        if next_path.startswith("//"):
            return "/"
        if next_path.startswith("/login"):
            return "/"
        return next_path

    def _dashboard_public_host(self):
        if self.public_host:
            return self.public_host
        if self.public_ip:
            return self.public_ip

        normalized_host = (self.host or "").strip()
        if not normalized_host:
            return ""
        if normalized_host in {"0.0.0.0", "::", "localhost", "127.0.0.1", "::1"}:
            return ""
        return normalized_host

    def _dashboard_public_scheme(self):
        scheme = (self.public_scheme or "http").strip().lower()
        if scheme in {"http", "https"}:
            return scheme
        return "http"

    def _dashboard_public_port(self):
        public_port = self.public_port
        try:
            public_port = int(public_port)
        except (TypeError, ValueError):
            return self.port
        if public_port <= 0 or public_port > 65535:
            return self.port
        return public_port

    def _is_global_ip(self, value: str) -> bool:
        try:
            return ipaddress.ip_address((value or "").strip()).is_global
        except ValueError:
            return False

    def _should_try_public_ip_detection(self, host: str) -> bool:
        normalized = (host or "").strip().lower()
        if normalized in {"", "localhost", "127.0.0.1", "::1", "0.0.0.0", "::"}:
            return True
        if self._is_global_ip(normalized):
            return False
        try:
            return not ipaddress.ip_address(normalized).is_global
        except ValueError:
            # Hostnames can still be valid public targets; only force detection for obvious local values.
            return False

    async def _detect_public_ip(self) -> str:
        now = time.time()
        if self._cached_public_ip and now < self._public_ip_cache_expires_at:
            return self._cached_public_ip

        custom_endpoint = (self.public_ip_endpoint or "").strip()
        endpoints = [
            custom_endpoint,
            "https://api.ipify.org",
            "https://ifconfig.me/ip",
            "https://checkip.amazonaws.com",
        ]
        timeout = ClientTimeout(total=4)

        try:
            async with ClientSession(timeout=timeout) as session:
                for endpoint in endpoints:
                    if not endpoint:
                        continue
                    try:
                        async with session.get(endpoint, headers={"User-Agent": "NemoBot/1.0"}) as response:
                            if response.status != 200:
                                continue
                            body = (await response.text()).strip()
                            candidate = body.splitlines()[0].strip() if body else ""
                            if self._is_global_ip(candidate):
                                self._cached_public_ip = candidate
                                self._public_ip_cache_expires_at = now + self.public_ip_cache_ttl_seconds
                                return candidate
                    except Exception:
                        continue
        except Exception:
            return ""

        return ""

    async def dashboard_public_url(self):
        explicit_public_url = (self.public_url or "").strip()
        if explicit_public_url:
            if not explicit_public_url.startswith(("http://", "https://")):
                explicit_public_url = f"{self._dashboard_public_scheme()}://{explicit_public_url}"
            return explicit_public_url.rstrip("/") + "/"

        public_host = self._dashboard_public_host()
        if not public_host and self._should_try_public_ip_detection(self.host):
            detected_public_ip = await self._detect_public_ip()
            if detected_public_ip:
                public_host = detected_public_ip
        elif self._should_try_public_ip_detection(public_host):
            detected_public_ip = await self._detect_public_ip()
            if detected_public_ip:
                public_host = detected_public_ip

        if not public_host:
            return ""

        public_scheme = self._dashboard_public_scheme()
        public_port = self._dashboard_public_port()

        show_port = not (
            (public_scheme == "http" and public_port == 80)
            or (public_scheme == "https" and public_port == 443)
        )
        port_suffix = f":{public_port}" if show_port else ""
        return f"{public_scheme}://{public_host}{port_suffix}/"

    def dashboard_local_url(self):
        return f"http://127.0.0.1:{self.port}/"

    def _detect_lan_ip(self) -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                candidate = sock.getsockname()[0]
        except Exception:
            return ""

        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            return ""

        if address.is_loopback or address.is_unspecified:
            return ""
        return candidate

    def dashboard_lan_url(self):
        lan_ip = self._detect_lan_ip()
        if not lan_ip:
            return ""
        return f"http://{lan_ip}:{self.port}/"

    async def dashboard_access_urls(self):
        return {
            "local": self.dashboard_local_url(),
            "lan": self.dashboard_lan_url(),
            "public": await self.dashboard_public_url(),
        }

    def _level_mode_label(self, mode: str) -> str:
        labels = {
            "default_only": "Everyone uses the default card",
            "user_choice": "Users can choose cards (optional minimum level)",
            "auto_unlock": "Cards auto-change by reached level",
        }
        return labels.get(mode or "", "Unknown")

    async def _get_level_cog(self):
        level_cog = self.bot.get_cog("LevelSystem")
        if not level_cog:
            raise web.HTTPServiceUnavailable(text="LevelSystem cog is not loaded")
        return level_cog

    def _level_cards_redirect(self, status: str = "", error: str = "") -> str:
        params = []
        if status:
            params.append(f"status={quote(status, safe='')}")
        if error:
            params.append(f"error={quote(error, safe='')}")
        if params:
            return "/level-cards?" + "&".join(params)
        return "/level-cards"

    def _nsfw_skin_ratios(self, image: Image.Image):
        sample = image.convert("RGB").resize((224, 224))
        width, height = sample.size
        pixels = list(sample.getdata())
        total = len(pixels)
        if total == 0:
            return 0.0, 0.0

        center_x1, center_x2 = int(width * 0.25), int(width * 0.75)
        center_y1, center_y2 = int(height * 0.2), int(height * 0.85)

        skin_pixels = 0
        center_skin_pixels = 0
        center_pixels = 0

        for index, (r, g, b) in enumerate(pixels):
            x = index % width
            y = index // width

            max_channel = max(r, g, b)
            min_channel = min(r, g, b)
            skin_like = (
                r > 95
                and g > 40
                and b > 20
                and (max_channel - min_channel) > 15
                and abs(r - g) > 15
                and r > g
                and r > b
            )

            if skin_like:
                skin_pixels += 1

            if center_x1 <= x <= center_x2 and center_y1 <= y <= center_y2:
                center_pixels += 1
                if skin_like:
                    center_skin_pixels += 1

        overall_ratio = skin_pixels / total
        center_ratio = (center_skin_pixels / center_pixels) if center_pixels else 0.0
        return overall_ratio, center_ratio

    def _fails_nsfw_filter(self, image: Image.Image) -> bool:
        overall_ratio, center_ratio = self._nsfw_skin_ratios(image)
        return overall_ratio >= 0.62 and center_ratio >= 0.68

    def _prune_sessions(self):
        now = time.time()
        expired_session_ids = [
            session_id
            for session_id, (_, expires_at) in self.sessions.items()
            if expires_at <= now
        ]
        for session_id in expired_session_ids:
            self.sessions.pop(session_id, None)

    def _create_session(self, permission: str) -> str:
        self._prune_sessions()
        session_id = secrets.token_urlsafe(32)
        self.sessions[session_id] = (permission, time.time() + self.session_ttl_seconds)
        return session_id

    def _session_permission(self, request: web.Request):
        self._prune_sessions()
        session_id = request.cookies.get(self.session_cookie_name, "").strip()
        if not session_id:
            return None, None

        session_data = self.sessions.get(session_id)
        if not session_data:
            return None, None

        permission, _ = session_data
        self.sessions[session_id] = (permission, time.time() + self.session_ttl_seconds)
        return permission, session_id

    def _destroy_session(self, session_id):
        if session_id:
            self.sessions.pop(session_id, None)

    def _form_text(self, post_data: Any, key: str, default: str = "") -> str:
        value = post_data.get(key, default)
        if value is None:
            return default
        if isinstance(value, str):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    async def _authorize(self, request: web.Request, required_permission: str = "viewer"):
        permission, _ = self._session_permission(request)
        if permission is None:
            next_path = quote(self._safe_next_path(request.rel_url.path_qs or "/"), safe="")
            raise web.HTTPFound(location=f"/login?next={next_path}")
        if not self._permission_allows(permission, required_permission):
            raise web.HTTPForbidden(text=f"{required_permission.capitalize()} permission required")
        return permission

    def _layout(self, title: str, body: str, permission: str, show_header: bool = True):
        nav = (
            '<a href="/">Home</a>'
            '<a href="/leaderboard">Leaderboard</a>'
            '<a href="/level-formula">Level Formula</a>'
            '<a href="/level-cards">Level Cards</a>'
            '<a href="/automod">Automod</a>'
            '<a href="/settings">Bot Settings</a>'
            '<a href="/console">Console</a>'
        )
        header_html = (
            f"""
    <div class="card">
      <h1>{html.escape(title)}</h1>
      <p>Permission: <strong>{html.escape(permission)}</strong></p>
      <div class="topbar">
        <nav>{nav}</nav>
        <form class="logout-form" method="post" action="/logout">
          <button type="submit">Logout</button>
        </form>
      </div>
    </div>
"""
            if show_header
            else ""
        )

        return f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg-a: #0b1020;
      --bg-b: #111b34;
      --panel: #121b2f;
      --panel-border: #273556;
      --text: #ebf1ff;
      --muted: #aebee3;
      --accent: #4fa8ff;
      --good: #32c48d;
      --warn: #ffb020;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Avenir Next", "Helvetica Neue", Helvetica, Arial, sans-serif;
      color: var(--text);
      background: radial-gradient(1200px 600px at 20% -10%, #213768 0%, transparent 60%),
                  linear-gradient(180deg, var(--bg-a) 0%, var(--bg-b) 100%);
      min-height: 100vh;
      padding: 24px;
    }}
    .wrap {{ max-width: 1080px; margin: 0 auto; }}
    .card {{
      background: linear-gradient(180deg, rgba(18,27,47,.96), rgba(12,20,36,.95));
      border: 1px solid var(--panel-border);
      border-radius: 16px;
      padding: 20px;
      margin-bottom: 18px;
      box-shadow: 0 20px 40px rgba(0,0,0,.25);
    }}
    h1, h2 {{ margin: 0 0 12px 0; letter-spacing: .2px; }}
    p, li, label {{ color: var(--muted); }}
    .topbar {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
    }}
    nav {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 0; }}
    nav a {{
      color: var(--text);
      text-decoration: none;
      background: #1b2948;
      border: 1px solid #30446f;
      border-radius: 999px;
      padding: 8px 14px;
      font-size: 14px;
    }}
        input:not([type="checkbox"]):not([type="radio"]), select, textarea, button {{
            width: 100%;
            background: #0e1628;
            color: var(--text);
            border: 1px solid #314567;
            border-radius: 10px;
            padding: 10px 12px;
            margin-top: 6px;
            margin-bottom: 12px;
        }}
        label {{ display: block; }}
        label > input[type="checkbox"] {{
            width: auto;
            margin: 0 8px 0 0;
            padding: 0;
            accent-color: var(--accent);
            vertical-align: middle;
        }}
    textarea {{ min-height: 90px; resize: vertical; }}
    button {{
      background: linear-gradient(90deg, #2c7df0, #35a7ff);
      border: 0;
      font-weight: 600;
      cursor: pointer;
    }}
    .logout-form {{ margin: 0; }}
    .logout-form button {{ width: auto; margin: 0; padding: 8px 14px; }}
    .danger {{ background: linear-gradient(90deg, #be3f3f, #d95a5a); }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #2f4063; padding: 10px; text-align: left; }}
    .mono {{
      font-family: Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      white-space: pre-wrap;
      background: #0b1323;
      border: 1px solid #2a3e63;
      border-radius: 10px;
      padding: 12px;
      color: #d3def8;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    {header_html}
    {body}
  </div>
</body>
</html>
"""
    async def login_page(self, request: web.Request):
        permission, _ = self._session_permission(request)
        if permission:
            raise web.HTTPFound(location="/")

        next_path = self._safe_next_path(request.query.get("next", "/"))
        error = request.query.get("error", "").strip()
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""

        body = f"""
<div class="card">
  <h2>Dashboard Login</h2>
  <p>Use your dashboard username and passcode.</p>
  {error_html}
  <form method="post" action="/login">
    <input type="hidden" name="next" value="{html.escape(next_path)}" />
    <label>Username</label>
    <input type="text" name="username" placeholder="Username" required />
    <label>Passcode</label>
    <input type="password" name="passcode" placeholder="Passcode" required />
    <button type="submit">Login</button>
  </form>
</div>
"""
        return web.Response(
            text=self._layout("Dashboard Login", body, "guest", show_header=False),
            content_type="text/html",
        )

    async def login_submit(self, request: web.Request):
        data = await request.post()
        username = self._form_text(data, "username", "")
        passcode = self._form_text(data, "passcode", "")
        next_path = self._safe_next_path(self._form_text(data, "next", "/"))

        permission = self._permission_for_login(username, passcode)
        if permission is None:
            error = quote("Invalid username or passcode", safe="")
            safe_next = quote(next_path, safe="")
            raise web.HTTPFound(location=f"/login?error={error}&next={safe_next}")

        session_id = self._create_session(permission)
        response = web.HTTPFound(location=next_path)
        response.set_cookie(
            self.session_cookie_name,
            session_id,
            max_age=self.session_ttl_seconds,
            httponly=True,
            samesite="Lax",
        )
        raise response

    async def logout_submit(self, request: web.Request):
        _, session_id = self._session_permission(request)
        self._destroy_session(session_id)

        response = web.HTTPFound(location="/login")
        response.del_cookie(self.session_cookie_name)
        raise response

    async def home_page(self, request: web.Request):
        permission = await self._authorize(request)
        body = f"""
<div class="card">
    <h2>NemoBot Dashboard (Beta)</h2>
  <p>Dashboard host: {html.escape(self.host)}:{self.port}</p>
  <p>This panel supports level formula controls, leaderboards, automod, bot settings, restart, and optional console access.</p>
  <ul>
    <li>Viewer: can view statistics and settings pages.</li>
    <li>Admin: can edit settings and run console commands when enabled.</li>
    <li>Dev: highest access level for dev-only actions.</li>
  </ul>
</div>
"""
        return web.Response(text=self._layout("Dashboard (Beta)", body, permission), content_type="text/html")

    async def leaderboard_page(self, request: web.Request):
        permission = await self._authorize(request)

        rows_html = ""
        async with aiosqlite.connect(self.level_db) as db:
            async with db.execute(
                "SELECT user_id, level, xp, remaining_xp FROM users ORDER BY xp DESC LIMIT 25"
            ) as cursor:
                rows = await cursor.fetchall()

        for index, (user_id, lvl, xp, remaining_xp) in enumerate(rows, start=1):
            user = self.bot.get_user(user_id)
            display_name = user.name if user else f"User {user_id}"
            rows_html += (
                "<tr>"
                f"<td>{index}</td>"
                f"<td>{html.escape(display_name)}</td>"
                f"<td>{user_id}</td>"
                f"<td>{int(lvl or 0)}</td>"
                f"<td>{int(float(xp or 0))}</td>"
                f"<td>{int(float(remaining_xp or 0))}</td>"
                "</tr>"
            )

        body = f"""
<div class="card">
  <h2>Leaderboard</h2>
  <table>
    <thead>
      <tr>
        <th>#</th><th>User</th><th>User ID</th><th>Level</th><th>Total XP</th><th>Remaining XP</th>
      </tr>
    </thead>
    <tbody>
      {rows_html or '<tr><td colspan="6">No data available yet.</td></tr>'}
    </tbody>
  </table>
</div>
"""
        return web.Response(text=self._layout("Leaderboard", body, permission), content_type="text/html")

    async def level_formula_page(self, request: web.Request):
        permission = await self._authorize(request)
        level_cog = self.bot.get_cog("LevelSystem")

        if not level_cog:
            return web.Response(status=503, text="LevelSystem cog is not loaded")

        formula = await level_cog.get_level_formula()
        preview_level = max(0, int(request.query.get("preview_level", "10") or 10))
        xp_for_step = level_cog.get_xp_needed_for_level(preview_level)
        xp_to_reach = level_cog.get_total_xp_for_level(preview_level)

        body = f"""
<div class="card">
  <h2>Level Formula</h2>
  <p>Current formula: <strong>XP needed from level L to L+1 = XP_BASE + XP_SCALE * L</strong></p>
  <p>Current values: XP_BASE={formula['xp_base']}, XP_SCALE={formula['xp_scale']}</p>
  <form method="get" action="/level-formula">
    <label>Preview level (L)</label>
    <input type="number" name="preview_level" min="0" value="{preview_level}" />
    <button type="submit">Preview</button>
  </form>
  <p>For L={preview_level}: XP for next level is <strong>{int(xp_for_step)}</strong>.</p>
  <p>Total XP required to reach level {preview_level}: <strong>{int(xp_to_reach)}</strong>.</p>
</div>
"""

        if self._permission_allows(permission, "admin"):
            body += f"""
<div class="card">
  <h2>Update Formula</h2>
  <form method="post" action="/level-formula">
    <label>XP_BASE</label>
    <input type="number" name="xp_base" step="0.01" min="1" value="{formula['xp_base']}" />
    <label>XP_SCALE</label>
    <input type="number" name="xp_scale" step="0.01" min="0" value="{formula['xp_scale']}" />
    <label><input type="checkbox" name="recalculate" value="1" /> Recalculate all users now</label>
    <button type="submit">Save Formula</button>
  </form>
</div>
"""

        return web.Response(text=self._layout("Level Formula", body, permission), content_type="text/html")

    async def level_formula_update(self, request: web.Request):
        await self._authorize(request, required_permission="admin")
        level_cog = self.bot.get_cog("LevelSystem")
        if not level_cog:
            return web.Response(status=503, text="LevelSystem cog is not loaded")

        data = await request.post()
        try:
            xp_base = float(self._form_text(data, "xp_base", "0"))
            xp_scale = float(self._form_text(data, "xp_scale", "0"))
            recalculate = self._form_text(data, "recalculate", "") == "1"
            await level_cog.update_level_formula(xp_base, xp_scale, recalculate=recalculate)
        except Exception as exc:
            return web.Response(status=400, text=f"Failed to update formula: {exc}")

        raise web.HTTPFound(location="/level-formula")

    async def level_cards_page(self, request: web.Request):
        permission = await self._authorize(request)
        level_cog = await self._get_level_cog()

        settings = await level_cog.get_level_card_settings()
        cards = await level_cog.list_level_cards(include_disabled=True)
        layout = await level_cog.get_level_card_layout()

        status = request.query.get("status", "").strip()
        error = request.query.get("error", "").strip()
        status_html = f'<p style="color:#9be7c2">{html.escape(status)}</p>' if status else ""
        error_html = f'<p style="color:#ffb8b8">{html.escape(error)}</p>' if error else ""

        mode_options = "".join(
            f'<option value="{mode}" {"selected" if mode == settings["mode"] else ""}>{html.escape(self._level_mode_label(mode))}</option>'
            for mode in ["default_only", "user_choice", "auto_unlock"]
        )

        cards_rows = ""
        for card in cards:
            card_flags = []
            if card["is_default"]:
                card_flags.append("default")
            elif card["is_custom"]:
                card_flags.append("custom")
            else:
                card_flags.append("built-in")
            if not card["is_enabled"]:
                card_flags.append("disabled")
            flags_text = ", ".join(card_flags) if card_flags else "-"

            cards_rows += (
                "<tr>"
                f"<td>{html.escape(card['card_key'])}</td>"
                f"<td>{html.escape(card['display_name'])}</td>"
                f"<td>{int(card['unlock_level'])}</td>"
                f"<td>{html.escape(flags_text)}</td>"
                f"<td>{html.escape(card['file_path'])}</td>"
                "</tr>"
            )

        equip_options = ['<option value="default">default (legacy)</option>']
        for card in cards:
            if str(card.get("card_key", "")).strip().lower() == "default":
                continue
            if not card["is_enabled"]:
                continue
            equip_options.append(
                f'<option value="{html.escape(card["card_key"])}">{html.escape(card["card_key"])} - {html.escape(card["display_name"])} (unlock {int(card["unlock_level"])})</option>'
            )
        equip_options_html = "".join(equip_options)

        body = f"""
<div class="card">
  <h2>Level Card Modes</h2>
  <p>Current mode: <strong>{html.escape(settings['mode'])}</strong> ({html.escape(self._level_mode_label(settings['mode']))})</p>
  <p>User-choice minimum level: <strong>{int(settings['min_level_for_choice'])}</strong></p>
  {status_html}
  {error_html}
"""

        if self._permission_allows(permission, "admin"):
            body += f"""
  <form method="post" action="/level-cards/settings">
    <label>Mode</label>
    <select name="mode">{mode_options}</select>
    <label>Minimum level required in user-choice mode</label>
    <input type="number" name="min_level_for_choice" min="0" value="{int(settings['min_level_for_choice'])}" />
    <button type="submit">Save Level Card Settings</button>
  </form>
"""
        else:
            body += "<p>Viewer mode: read-only.</p>"

        body += "</div>"

        body += f"""
<div class="card">
  <h2>Available Level Cards</h2>
  <p>Built-in backgrounds are labeled <strong>built-in</strong>; uploaded ones are labeled <strong>custom</strong>.</p>
  <table>
    <thead>
      <tr>
        <th>Key</th><th>Name</th><th>Unlock Level</th><th>Flags</th><th>File Path</th>
      </tr>
    </thead>
    <tbody>
      {cards_rows or '<tr><td colspan="5">No cards available.</td></tr>'}
    </tbody>
  </table>
</div>
"""

        if self._permission_allows(permission, "admin"):
            body += f"""
<div class="card">
  <h2>Set Equipped Card For A User</h2>
    <p>Manual equip is preferred whenever a valid equipped card is set for the user.</p>
  <form method="post" action="/level-cards/equip">
    <label>Discord User ID</label>
    <input type="number" name="user_id" min="1" required />
    <label>Card key</label>
    <select name="card_key">{equip_options_html}</select>
    <button type="submit">Save Equipped Card</button>
  </form>
</div>
"""

            body += """
<div class="card">
    <h2>Enable / Disable Card</h2>
    <p>Disabling a card removes it from user equips and prevents it from being selected.</p>
    <form method="post" action="/level-cards/toggle">
        <label>Card key</label>
        <input type="text" name="card_key" maxlength="48" required />
        <label>State</label>
        <select name="enabled">
            <option value="1">Enabled</option>
            <option value="0">Disabled</option>
        </select>
        <button type="submit">Save Card State</button>
    </form>
</div>
"""

        if self._permission_allows(permission, "dev"):
            body += f"""
<div class="card">
    <h2>Delete Custom Card (Dev Only)</h2>
    <p>Only custom cards can be deleted. Default card is protected.</p>
    <form method="post" action="/level-cards/delete">
        <label>Card key</label>
        <input type="text" name="card_key" maxlength="48" required />
        <label>Confirm card key (must match exactly)</label>
        <input type="text" name="confirm_card_key" maxlength="48" required />
        <label><input type="checkbox" name="remove_file" value="1" checked /> Also delete image file from storage</label>
        <button class="danger" type="submit">Delete Custom Card</button>
    </form>
</div>

<div class="card">
  <h2>Upload Custom Level Card (Dev Only)</h2>
  <p>Upload limit: {int(self.level_card_upload_limit // (1024 * 1024))} MB. Files go through a basic NSFW screen before saving.</p>
  <form method="post" action="/level-cards/upload" enctype="multipart/form-data">
    <label>Display name</label>
    <input type="text" name="display_name" maxlength="48" required />
    <label>Unlock level</label>
    <input type="number" name="unlock_level" min="0" value="0" />
    <label>Image file (png/jpg/jpeg/webp)</label>
    <input type="file" name="card_file" accept=".png,.jpg,.jpeg,.webp" required />
    <button type="submit">Upload Card</button>
  </form>
</div>

<div class="card">
  <h2>Level Card Layout (Dev Only)</h2>
  <p>Changes the element positions used when rendering `/level` cards.</p>
  <form method="post" action="/level-cards/layout">
    <label>avatar_x</label>
    <input type="number" name="avatar_x" value="{int(layout['avatar_x'])}" />
    <label>avatar_y</label>
    <input type="number" name="avatar_y" value="{int(layout['avatar_y'])}" />
    <label>avatar_size</label>
    <input type="number" name="avatar_size" value="{int(layout['avatar_size'])}" />
    <label>text_x</label>
    <input type="number" name="text_x" value="{int(layout['text_x'])}" />
    <label>name_y</label>
    <input type="number" name="name_y" value="{int(layout['name_y'])}" />
    <label>stats_y</label>
    <input type="number" name="stats_y" value="{int(layout['stats_y'])}" />
    <label>bar_y</label>
    <input type="number" name="bar_y" value="{int(layout['bar_y'])}" />
    <label>progress_y</label>
    <input type="number" name="progress_y" value="{int(layout['progress_y'])}" />
    <button type="submit">Save Layout</button>
  </form>
    <form method="post" action="/level-cards/layout-reset">
        <button class="danger" type="submit">Reset Layout To Defaults</button>
    </form>
</div>
"""

        return web.Response(text=self._layout("Level Cards", body, permission), content_type="text/html")

    async def level_cards_settings_update(self, request: web.Request):
        await self._authorize(request, required_permission="admin")
        level_cog = await self._get_level_cog()

        data = await request.post()
        mode = self._form_text(data, "mode", "default_only")
        try:
            min_level = int(self._form_text(data, "min_level_for_choice", "0"))
        except ValueError:
            raise web.HTTPFound(location=self._level_cards_redirect(error="Minimum level must be an integer"))

        try:
            await level_cog.update_level_card_settings(mode, min_level)
        except Exception as exc:
            raise web.HTTPFound(location=self._level_cards_redirect(error=f"Failed to save settings: {exc}"))

        raise web.HTTPFound(location=self._level_cards_redirect(status="Level card settings updated"))

    async def level_cards_equip_update(self, request: web.Request):
        await self._authorize(request, required_permission="admin")
        level_cog = await self._get_level_cog()

        data = await request.post()
        try:
            user_id = int(self._form_text(data, "user_id", "0"))
        except ValueError:
            raise web.HTTPFound(location=self._level_cards_redirect(error="User ID must be numeric"))

        card_key = self._form_text(data, "card_key", "default")

        try:
            result = await level_cog.set_user_equipped_card(user_id, card_key)
        except Exception as exc:
            raise web.HTTPFound(location=self._level_cards_redirect(error=f"Failed to set equipped card: {exc}"))

        raise web.HTTPFound(
            location=self._level_cards_redirect(
                status=f"Saved equipped card for user {result['user_id']}: {result['equipped_card_key']}"
            )
        )

    async def level_cards_toggle_update(self, request: web.Request):
        await self._authorize(request, required_permission="admin")
        level_cog = await self._get_level_cog()

        data = await request.post()
        card_key = self._form_text(data, "card_key", "")
        enabled = self._form_text(data, "enabled", "1") == "1"

        try:
            card = await level_cog.set_level_card_enabled(card_key, enabled)
        except Exception as exc:
            raise web.HTTPFound(location=self._level_cards_redirect(error=f"Failed to update card state: {exc}"))

        state = "enabled" if card and card.get("is_enabled") else "disabled"
        cleared = int((card or {}).get("removed_equips", 0) or 0)
        raise web.HTTPFound(
            location=self._level_cards_redirect(
                status=(
                    f"Card {card['card_key']} is now {state} (cleared equips: {cleared})"
                    if card
                    else "Card state updated"
                )
            )
        )

    async def level_cards_delete(self, request: web.Request):
        await self._authorize(request, required_permission="dev")
        level_cog = await self._get_level_cog()

        data = await request.post()
        card_key = self._form_text(data, "card_key", "")
        confirm_card_key = self._form_text(data, "confirm_card_key", "")
        remove_file = self._form_text(data, "remove_file", "") == "1"

        if (card_key or "").strip() != (confirm_card_key or "").strip():
            raise web.HTTPFound(location=self._level_cards_redirect(error="Confirmation card key does not match"))

        try:
            result = await level_cog.delete_custom_level_card(card_key, remove_file=remove_file)
        except Exception as exc:
            raise web.HTTPFound(location=self._level_cards_redirect(error=f"Failed to delete card: {exc}"))

        file_note = "file deleted" if result.get("file_deleted") else "file kept/not deleted"
        removed_equips = int(result.get("removed_equips", 0) or 0)
        raise web.HTTPFound(
            location=self._level_cards_redirect(
                status=f"Deleted card {result['card_key']} ({file_note}, cleared equips: {removed_equips})"
            )
        )

    async def level_cards_upload(self, request: web.Request):
        await self._authorize(request, required_permission="dev")
        level_cog = await self._get_level_cog()

        data = await request.post()
        upload_field = data.get("card_file")
        if not isinstance(upload_field, FileField):
            raise web.HTTPFound(location=self._level_cards_redirect(error="No file uploaded"))

        filename = (getattr(upload_field, "filename", "") or "").strip()
        extension = os.path.splitext(filename.lower())[1]
        if extension not in self.level_card_allowed_ext:
            raise web.HTTPFound(location=self._level_cards_redirect(error="Unsupported file format"))

        raw_bytes = upload_field.file.read()
        if not raw_bytes:
            raise web.HTTPFound(location=self._level_cards_redirect(error="Uploaded file is empty"))
        if len(raw_bytes) > self.level_card_upload_limit:
            raise web.HTTPFound(location=self._level_cards_redirect(error="Uploaded file is too large"))

        try:
            image = Image.open(BytesIO(raw_bytes)).convert("RGBA")
        except Exception:
            raise web.HTTPFound(location=self._level_cards_redirect(error="Uploaded file is not a valid image"))

        if self._fails_nsfw_filter(image):
            raise web.HTTPFound(location=self._level_cards_redirect(error="Upload blocked by NSFW filter"))

        display_name = self._form_text(data, "display_name", "").strip()[:48] or "Custom Card"
        try:
            unlock_level = max(0, int(self._form_text(data, "unlock_level", "0")))
        except ValueError:
            raise web.HTTPFound(location=self._level_cards_redirect(error="Unlock level must be an integer"))

        safe_stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", os.path.splitext(filename)[0]).strip("_").lower()
        if not safe_stem:
            safe_stem = "level_card"

        os.makedirs(level_cog.level_card_custom_dir, exist_ok=True)
        saved_filename = f"{safe_stem}_{int(time.time())}.png"
        saved_path = os.path.join(level_cog.level_card_custom_dir, saved_filename)

        try:
            image.save(saved_path, format="PNG")
            card = await level_cog.create_custom_level_card(display_name, saved_path, unlock_level=unlock_level)
        except Exception as exc:
            raise web.HTTPFound(location=self._level_cards_redirect(error=f"Failed to save card: {exc}"))

        raise web.HTTPFound(
            location=self._level_cards_redirect(
                status=f"Uploaded card {card['display_name']} with key {card['card_key']}"
            )
        )

    async def level_cards_layout_update(self, request: web.Request):
        await self._authorize(request, required_permission="dev")
        level_cog = await self._get_level_cog()

        data = await request.post()
        layout_updates = {}
        for key in ["avatar_x", "avatar_y", "avatar_size", "text_x", "name_y", "stats_y", "bar_y", "progress_y"]:
            raw_value = self._form_text(data, key, "")
            if raw_value == "":
                continue
            layout_updates[key] = raw_value

        try:
            await level_cog.update_level_card_layout(layout_updates)
        except Exception as exc:
            raise web.HTTPFound(location=self._level_cards_redirect(error=f"Failed to update layout: {exc}"))

        raise web.HTTPFound(location=self._level_cards_redirect(status="Level card layout updated"))

    async def level_cards_layout_reset(self, request: web.Request):
        await self._authorize(request, required_permission="dev")
        level_cog = await self._get_level_cog()

        try:
            await level_cog.update_level_card_layout(dict(level_cog.DEFAULT_CARD_LAYOUT))
        except Exception as exc:
            raise web.HTTPFound(location=self._level_cards_redirect(error=f"Failed to reset layout: {exc}"))

        raise web.HTTPFound(location=self._level_cards_redirect(status="Level card layout reset to defaults"))

    async def automod_page(self, request: web.Request):
        permission = await self._authorize(request)

        guilds = sorted(self.bot.guilds, key=lambda g: g.name.lower())
        if not guilds:
            body = '<div class="card"><h2>Automod</h2><p>Bot is not in any guild.</p></div>'
            return web.Response(text=self._layout("Automod", body, permission), content_type="text/html")

        try:
            selected_guild_id = int(request.query.get("guild_id", str(guilds[0].id)))
        except ValueError:
            selected_guild_id = guilds[0].id
        selected_guild = next((g for g in guilds if g.id == selected_guild_id), guilds[0])
        settings = await self.get_automod_settings(selected_guild.id)

        guild_options = "".join(
            f"<option value=\"{g.id}\" {'selected' if g.id == selected_guild.id else ''}>{html.escape(g.name)} ({g.id})</option>"
            for g in guilds
        )

        checked = "checked" if settings["anti_link"] else ""
        blocked_words = ", ".join(settings["blocked_words"])
        automod_submit = (
            '<button type="submit">Save Automod</button>'
            if self._permission_allows(permission, "admin")
            else "<p>Viewer mode: read-only.</p>"
        )

        body = f"""
<div class="card">
  <h2>Automod Settings</h2>
  <form method="post" action="/automod">
    <label>Guild</label>
    <select name="guild_id">{guild_options}</select>
    <label><input type="checkbox" name="anti_link" value="1" {checked} /> Delete messages containing links</label>
    <label>Blocked words (comma-separated)</label>
    <textarea name="blocked_words">{html.escape(blocked_words)}</textarea>
    {automod_submit}
  </form>
</div>
"""

        return web.Response(text=self._layout("Automod", body, permission), content_type="text/html")

    async def automod_update(self, request: web.Request):
        await self._authorize(request, required_permission="admin")
        data = await request.post()

        try:
            guild_id = int(self._form_text(data, "guild_id", "0"))
        except ValueError:
            return web.Response(status=400, text="Invalid guild id")

        anti_link = self._form_text(data, "anti_link", "") == "1"
        blocked_words = self._form_text(data, "blocked_words", "")
        await self.save_automod_settings(guild_id, anti_link, blocked_words)

        raise web.HTTPFound(location=f"/automod?guild_id={guild_id}")

    async def settings_page(self, request: web.Request):
        permission = await self._authorize(request)

        current_activity_name = self.bot.activity.name if self.bot.activity and self.bot.activity.name else "NemoBot"
        presence_text = await self.get_setting("presence_text", current_activity_name)
        presence_type = await self.get_setting("presence_type", "watching")

        settings = load_settings()
        leveling_settings = settings.get("leveling", {})
        inactivity_settings = leveling_settings.get("inactivity_decay", {})
        rolling_settings = leveling_settings.get("rolling_decay", {})

        presence_status = request.query.get("presence_status", "").strip()
        presence_error = request.query.get("presence_error", "").strip()
        presence_status_html = f'<p style="color:#9be7c2">{html.escape(presence_status)}</p>' if presence_status else ""
        presence_error_html = f'<p style="color:#ffb8b8">{html.escape(presence_error)}</p>' if presence_error else ""

        decay_status = request.query.get("decay_status", "").strip()
        decay_error = request.query.get("decay_error", "").strip()
        decay_status_html = f'<p style="color:#9be7c2">{html.escape(decay_status)}</p>' if decay_status else ""
        decay_error_html = f'<p style="color:#ffb8b8">{html.escape(decay_error)}</p>' if decay_error else ""

        card_status = request.query.get("card_status", "").strip()
        card_error = request.query.get("card_error", "").strip()
        card_status_html = f'<p style="color:#9be7c2">{html.escape(card_status)}</p>' if card_status else ""
        card_error_html = f'<p style="color:#ffb8b8">{html.escape(card_error)}</p>' if card_error else ""

        type_options = "".join(
            f"<option value=\"{opt}\" {'selected' if opt == presence_type else ''}>{opt}</option>"
            for opt in ["watching", "playing", "listening"]
        )
        settings_submit = (
            '<button type="submit">Save Bot Settings</button>'
            if self._permission_allows(permission, "admin")
            else "<p>Viewer mode: read-only.</p>"
        )

        decay_submit = (
            '<button type="submit">Save XP Decay Settings</button>'
            if self._permission_allows(permission, "admin")
            else "<p>Viewer mode: read-only.</p>"
        )

        card_submit = (
            '<button type="submit">Save Level Card Paths</button>'
            if self._permission_allows(permission, "admin")
            else "<p>Viewer mode: read-only.</p>"
        )

        body = f"""
<div class="card">
  <h2>Bot Settings</h2>
    {presence_status_html}
    {presence_error_html}
  <form method="post" action="/settings">
        <input type="hidden" name="section" value="presence" />
    <label>Presence text</label>
    <input type="text" name="presence_text" value="{html.escape(presence_text)}" maxlength="128" />
    <label>Presence type</label>
    <select name="presence_type">{type_options}</select>
    {settings_submit}
  </form>
</div>
"""

        inactivity_enabled = "checked" if inactivity_settings.get("enabled") else ""
        inactivity_after_days = html.escape(str(inactivity_settings.get("start_after_days", 30)))
        inactivity_percent = html.escape(str(inactivity_settings.get("percent_per_day", 2.0)))
        rolling_enabled = "checked" if rolling_settings.get("enabled") else ""
        rolling_days = html.escape(str(rolling_settings.get("expire_days", 30)))

        body += f"""
<div class="card">
    <h2>XP Decay</h2>
    {decay_status_html}
    {decay_error_html}
    <form method="post" action="/settings">
        <input type="hidden" name="section" value="level_decay" />
        <label><input type="checkbox" name="inactivity_decay_enabled" value="1" {inactivity_enabled} /> Enable inactivity decay</label>
        <label>Start after days</label>
        <input type="number" name="inactivity_decay_after_days" value="{inactivity_after_days}" min="0" max="3650" />
        <label>Percent per day</label>
        <input type="number" name="inactivity_decay_percent" value="{inactivity_percent}" min="0" max="100" step="0.1" />
        <label><input type="checkbox" name="rolling_decay_enabled" value="1" {rolling_enabled} /> Enable rolling decay (per-day XP buckets)</label>
        <label>Expire XP after days</label>
        <input type="number" name="rolling_decay_days" value="{rolling_days}" min="1" max="3650" />
        {decay_submit}
    </form>
</div>
"""

        level_card_background = html.escape(str(leveling_settings.get("level_card_background", "assets/level_card_bg.png")))
        level_card_storage_dir = html.escape(str(leveling_settings.get("level_card_storage_dir", "assets/level_cards")))

        body += f"""
<div class="card">
    <h2>Level Card Storage</h2>
    {card_status_html}
    {card_error_html}
    <form method="post" action="/settings">
        <input type="hidden" name="section" value="level_cards" />
        <label>Default background path</label>
        <input type="text" name="level_card_background" value="{level_card_background}" maxlength="220" />
        <label>Storage directory</label>
        <input type="text" name="level_card_storage_dir" value="{level_card_storage_dir}" maxlength="220" />
        {card_submit}
    </form>
</div>
"""

        birthday_cog = self.bot.get_cog("Birthdays")
        guilds = sorted(self.bot.guilds, key=lambda g: g.name.lower())

        if birthday_cog and guilds:
            try:
                selected_guild_id = int(request.query.get("birthday_guild_id", str(guilds[0].id)))
            except ValueError:
                selected_guild_id = guilds[0].id
            selected_guild = next((g for g in guilds if g.id == selected_guild_id), guilds[0])

            birthday_settings = await birthday_cog.get_guild_settings(selected_guild.id)
            birthday_status = request.query.get("birthday_status", "").strip()
            birthday_error = request.query.get("birthday_error", "").strip()

            birthday_status_html = f'<p style="color:#9be7c2">{html.escape(birthday_status)}</p>' if birthday_status else ""
            birthday_error_html = f'<p style="color:#ffb8b8">{html.escape(birthday_error)}</p>' if birthday_error else ""

            guild_options = "".join(
                f"<option value=\"{g.id}\" {'selected' if g.id == selected_guild.id else ''}>{html.escape(g.name)} ({g.id})</option>"
                for g in guilds
            )

            selected_channel_id = birthday_settings.get("channel_id")
            selected_role_id = birthday_settings.get("role_id")
            selected_channel = selected_guild.get_channel(selected_channel_id) if selected_channel_id else None
            selected_role = selected_guild.get_role(selected_role_id) if selected_role_id else None

            channel_options = ['<option value="">System channel fallback</option>']
            for channel in sorted(selected_guild.text_channels, key=lambda c: (c.position, c.id)):
                channel_selected = "selected" if channel.id == selected_channel_id else ""
                channel_options.append(
                    f'<option value="{channel.id}" {channel_selected}>#{html.escape(channel.name)} ({channel.id})</option>'
                )
            channel_options_html = "".join(channel_options)

            role_options = ['<option value="">No birthday role</option>']
            available_roles = [role for role in selected_guild.roles if not role.is_default()]
            for role in sorted(available_roles, key=lambda r: (r.position, r.id), reverse=True):
                role_selected = "selected" if role.id == selected_role_id else ""
                role_options.append(
                    f'<option value="{role.id}" {role_selected}>{html.escape(role.name)} ({role.id})</option>'
                )
            role_options_html = "".join(role_options)

            birthday_submit = (
                '<button type="submit">Save Birthday Settings</button>'
                if self._permission_allows(permission, "admin")
                else "<p>Viewer mode: read-only.</p>"
            )

            body += f"""
<div class="card">
    <h2>Birthday Settings</h2>
    {birthday_status_html}
    {birthday_error_html}
    <form method="get" action="/settings">
        <label>Guild</label>
        <select name="birthday_guild_id">{guild_options}</select>
        <button type="submit">Load Guild</button>
    </form>
    <p>Current channel: <strong>{html.escape(selected_channel.name) if selected_channel else 'System channel fallback'}</strong></p>
    <p>Current timezone: <strong>{html.escape(str(birthday_settings.get('timezone', 'UTC')))}</strong></p>
    <p>Current role: <strong>{html.escape(selected_role.name) if selected_role else 'No birthday role'}</strong></p>
    <form method="post" action="/settings">
        <input type="hidden" name="section" value="birthday" />
        <input type="hidden" name="birthday_guild_id" value="{selected_guild.id}" />
        <label>Birthday timezone (IANA)</label>
        <input type="text" name="birthday_timezone" value="{html.escape(str(birthday_settings.get('timezone', 'UTC')))}" maxlength="64" />
        <label>Announcement channel</label>
        <select name="birthday_channel_id">{channel_options_html}</select>
        <label>Birthday role (optional)</label>
        <select name="birthday_role_id">{role_options_html}</select>
        {birthday_submit}
    </form>
</div>
"""
        elif birthday_cog and not guilds:
            body += """
<div class="card">
    <h2>Birthday Settings</h2>
    <p>Bot is not in any guild.</p>
</div>
"""
        else:
            body += """
<div class="card">
    <h2>Birthday Settings</h2>
    <p>Birthdays cog is not loaded.</p>
</div>
"""

        if self._permission_allows(permission, "dev"):
            body += """
<div class="card">
  <h2>Bot Restart</h2>
  <form method="post" action="/restart">
    <button class="danger" type="submit">Restart Bot Process</button>
  </form>
</div>
"""

        return web.Response(text=self._layout("Bot Settings", body, permission), content_type="text/html")

    async def settings_update(self, request: web.Request):
        data = await request.post()

        section = self._form_text(data, "section", "presence").strip().lower()
        if section == "level_decay":
            await self._authorize(request, required_permission="admin")
            current = load_settings().get("leveling", {})
            inactivity = current.get("inactivity_decay", {})
            rolling = current.get("rolling_decay", {})

            try:
                inactivity_after = int(
                    self._form_text(
                        data,
                        "inactivity_decay_after_days",
                        str(inactivity.get("start_after_days", 30)),
                    )
                )
                inactivity_percent = float(
                    self._form_text(
                        data,
                        "inactivity_decay_percent",
                        str(inactivity.get("percent_per_day", 2.0)),
                    )
                )
                rolling_days = int(
                    self._form_text(
                        data,
                        "rolling_decay_days",
                        str(rolling.get("expire_days", 30)),
                    )
                )
            except ValueError:
                error = quote("Invalid decay settings", safe="")
                raise web.HTTPFound(location=f"/settings?decay_error={error}")

            inactivity_after = max(0, min(3650, inactivity_after))
            inactivity_percent = max(0.0, min(100.0, inactivity_percent))
            rolling_days = max(1, min(3650, rolling_days))

            inactivity_enabled = self._form_text(data, "inactivity_decay_enabled", "") == "1"
            rolling_enabled = self._form_text(data, "rolling_decay_enabled", "") == "1"

            update_settings(
                {
                    "leveling": {
                        "inactivity_decay": {
                            "enabled": inactivity_enabled,
                            "start_after_days": inactivity_after,
                            "percent_per_day": inactivity_percent,
                        },
                        "rolling_decay": {
                            "enabled": rolling_enabled,
                            "expire_days": rolling_days,
                        },
                    }
                }
            )

            level_cog = self.bot.get_cog("LevelSystem")
            if level_cog:
                level_cog.reload_level_settings()
                try:
                    await level_cog.apply_decay_all_users()
                except Exception:
                    pass

            status = quote("XP decay settings updated", safe="")
            raise web.HTTPFound(location=f"/settings?decay_status={status}")

        if section == "level_cards":
            await self._authorize(request, required_permission="admin")
            current = load_settings().get("leveling", {})
            background = self._form_text(
                data,
                "level_card_background",
                str(current.get("level_card_background", "assets/level_card_bg.png")),
            ).strip()
            storage_dir = self._form_text(
                data,
                "level_card_storage_dir",
                str(current.get("level_card_storage_dir", "assets/level_cards")),
            ).strip()

            if not background:
                error = quote("Background path is required", safe="")
                raise web.HTTPFound(location=f"/settings?card_error={error}")
            if not storage_dir:
                error = quote("Storage directory is required", safe="")
                raise web.HTTPFound(location=f"/settings?card_error={error}")

            update_settings(
                {
                    "leveling": {
                        "level_card_background": background,
                        "level_card_storage_dir": storage_dir,
                    }
                }
            )

            level_cog = self.bot.get_cog("LevelSystem")
            if level_cog:
                level_cog.reload_level_settings()

            status = quote("Level card paths updated", safe="")
            raise web.HTTPFound(location=f"/settings?card_status={status}")

        if section == "birthday":
            await self._authorize(request, required_permission="admin")
            birthday_cog = self.bot.get_cog("Birthdays")
            if not birthday_cog:
                return web.Response(status=503, text="Birthdays cog is not loaded")

            try:
                guild_id = int(self._form_text(data, "birthday_guild_id", "0"))
            except ValueError:
                error = quote("Invalid guild id", safe="")
                raise web.HTTPFound(location=f"/settings?birthday_error={error}")

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                error = quote("Guild not found", safe="")
                raise web.HTTPFound(location=f"/settings?birthday_error={error}")

            timezone_name = self._form_text(data, "birthday_timezone", "UTC").strip() or "UTC"
            channel_raw = self._form_text(data, "birthday_channel_id", "").strip()
            role_raw = self._form_text(data, "birthday_role_id", "").strip()

            channel_id = None
            role_id = None

            if channel_raw:
                try:
                    channel_id = int(channel_raw)
                except ValueError:
                    error = quote("Birthday channel id must be numeric", safe="")
                    raise web.HTTPFound(location=f"/settings?birthday_guild_id={guild_id}&birthday_error={error}")

                channel = guild.get_channel(channel_id)
                if not isinstance(channel, discord.TextChannel):
                    error = quote("Selected birthday channel is invalid", safe="")
                    raise web.HTTPFound(location=f"/settings?birthday_guild_id={guild_id}&birthday_error={error}")

            if role_raw:
                try:
                    role_id = int(role_raw)
                except ValueError:
                    error = quote("Birthday role id must be numeric", safe="")
                    raise web.HTTPFound(location=f"/settings?birthday_guild_id={guild_id}&birthday_error={error}")

                role = guild.get_role(role_id)
                if role is None or role.is_default():
                    error = quote("Selected birthday role is invalid", safe="")
                    raise web.HTTPFound(location=f"/settings?birthday_guild_id={guild_id}&birthday_error={error}")

            try:
                await birthday_cog.update_guild_settings(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    timezone=timezone_name,
                    role_id=role_id,
                )
            except Exception as exc:
                error = quote(f"Failed to update birthday settings: {exc}", safe="")
                raise web.HTTPFound(location=f"/settings?birthday_guild_id={guild_id}&birthday_error={error}")

            status = quote("Birthday settings updated", safe="")
            raise web.HTTPFound(location=f"/settings?birthday_guild_id={guild_id}&birthday_status={status}")

        await self._authorize(request, required_permission="admin")
        presence_text = self._form_text(data, "presence_text", "NemoBot").strip()[:128] or "NemoBot"
        presence_type = self._form_text(data, "presence_type", "watching").strip().lower() or "watching"

        type_map = {
            "watching": discord.ActivityType.watching,
            "playing": discord.ActivityType.playing,
            "listening": discord.ActivityType.listening,
        }
        activity_type = type_map.get(presence_type, discord.ActivityType.watching)

        await self.set_setting("presence_text", presence_text)
        await self.set_setting("presence_type", presence_type)

        await self.bot.change_presence(
            activity=discord.Activity(type=activity_type, name=presence_text)
        )

        status = quote("Bot settings updated", safe="")
        raise web.HTTPFound(location=f"/settings?presence_status={status}")

    async def restart_bot(self, request: web.Request):
        permission = await self._authorize(request, required_permission="dev")
        asyncio.get_event_loop().create_task(self._delayed_restart())
        return web.Response(
            text=self._layout(
                "Restarting",
                '<div class="card"><h2>Restart requested</h2><p>Bot process is restarting now.</p></div>',
                permission,
            ),
            content_type="text/html",
        )

    async def _delayed_restart(self):
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable, *sys.argv])

    async def console_page(self, request: web.Request):
        permission = await self._authorize(request, required_permission="admin")

        if not self.console_enabled:
            body = (
                '<div class="card"><h2>Console disabled</h2>'
                '<p>Set DASHBOARD_ENABLE_CONSOLE=true in .env to enable this feature.</p></div>'
            )
            return web.Response(text=self._layout("Console", body, permission), content_type="text/html")

        body = """
<div class="card">
  <h2>Console</h2>
  <p>Runs shell commands on the bot host. Use with care.</p>
  <form method="post" action="/console">
    <label>Command</label>
    <input type="text" name="command" maxlength="400" placeholder="echo hello" />
    <button type="submit">Run</button>
  </form>
</div>
"""
        return web.Response(text=self._layout("Console", body, permission), content_type="text/html")

    async def console_run(self, request: web.Request):
        permission = await self._authorize(request, required_permission="admin")
        if not self.console_enabled:
            return web.Response(status=403, text="Console is disabled")

        data = await request.post()
        command = self._form_text(data, "command", "").strip()
        if not command:
            return web.Response(status=400, text="Command is required")

        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd(),
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=12)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            stdout, stderr = b"", b"Command timed out after 12 seconds."

        output = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
        output = output[-6000:] if len(output) > 6000 else output

        body = f"""
<div class="card">
  <h2>Console Result</h2>
  <p><strong>Command:</strong> {html.escape(command)}</p>
  <div class="mono">{html.escape(output or '(no output)')}</div>
  <p><a href="/console">Run another command</a></p>
</div>
"""
        return web.Response(text=self._layout("Console Result", body, permission), content_type="text/html")

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return

        member = message.author
        if member.guild_permissions.administrator or member.guild_permissions.manage_messages:
            return

        settings = await self.get_automod_settings(message.guild.id)
        if not settings["anti_link"] and not settings["blocked_words"]:
            return

        content = (message.content or "").lower()
        should_delete = False
        reason = ""

        if settings["anti_link"] and re.search(r"(https?://|www\.|discord\.gg/)", content):
            should_delete = True
            reason = "links are not allowed"

        if not should_delete:
            for blocked_word in settings["blocked_words"]:
                if blocked_word and blocked_word in content:
                    should_delete = True
                    reason = f"blocked word: {blocked_word}"
                    break

        if not should_delete:
            return

        try:
            await message.delete()
        except discord.Forbidden:
            return

        try:
            notice = await message.channel.send(
                f"{message.author.mention}, your message was removed ({reason})."
            )
            await asyncio.sleep(6)
            await notice.delete()
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_ready(self):
        if self._startup_done:
            return

        await self.setup_database()
        await self.load_automod_cache()

        try:
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            self.site = web.TCPSite(self.runner, host=self.host, port=self.port)
            await self.site.start()
        except Exception as exc:
            print(f"Dashboard failed to start on {self.host}:{self.port}: {exc}")
            return

        self._startup_done = True
        print(f"Dashboard available on http://{self.host}:{self.port}")

    def cog_unload(self):
        if self.runner:
            asyncio.get_event_loop().create_task(self.runner.cleanup())


def setup(bot):
    bot.add_cog(Dashboard(bot))
