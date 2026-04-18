import asyncio
import html
import os
import re
import secrets
import sys
import time
from typing import Any
from urllib.parse import quote

import aiosqlite
import discord
from aiohttp import web
from discord.ext import commands


class Dashboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dashboard_db = "dashboard_data.db"
        self.level_db = "level.db"

        self.host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
        self.port = int(os.getenv("DASHBOARD_PORT", "8080"))

        self.view_token = os.getenv("DASHBOARD_VIEW_TOKEN", "")
        self.admin_token = os.getenv("DASHBOARD_ADMIN_TOKEN", "")
        self.dev_token = os.getenv("DASHBOARD_DEV_TOKEN", "")
        if self.admin_token and not self.view_token:
            self.view_token = self.admin_token
        if self.view_token and not self.admin_token:
            self.admin_token = self.view_token

        if not self.view_token and not self.admin_token:
            # Safe default for first-time setup; should be changed in .env immediately.
            self.view_token = "change-me"
            self.admin_token = "change-me"
        if not self.dev_token:
            self.dev_token = "change-me-dev"

        self.console_enabled = os.getenv("DASHBOARD_ENABLE_CONSOLE", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.session_cookie_name = "nemo_dashboard_session"
        self.session_ttl_seconds = int(os.getenv("DASHBOARD_SESSION_TTL_SECONDS", "43200"))
        self.sessions = {}

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
        async with aiosqlite.connect(self.dashboard_db) as db:
            async with db.execute(
                "SELECT value FROM dashboard_settings WHERE key = ?",
                (key,),
            ) as cursor:
                row = await cursor.fetchone()
        return row[0] if row else default_value

    async def set_setting(self, key: str, value: str):
        async with aiosqlite.connect(self.dashboard_db) as db:
            await db.execute(
                """
                INSERT INTO dashboard_settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            await db.commit()

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
    input, select, textarea, button {{
      width: 100%;
      background: #0e1628;
      color: var(--text);
      border: 1px solid #314567;
      border-radius: 10px;
      padding: 10px 12px;
      margin-top: 6px;
      margin-bottom: 12px;
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

        type_options = "".join(
            f"<option value=\"{opt}\" {'selected' if opt == presence_type else ''}>{opt}</option>"
            for opt in ["watching", "playing", "listening"]
        )
        settings_submit = (
            '<button type="submit">Save Bot Settings</button>'
            if self._permission_allows(permission, "admin")
            else "<p>Viewer mode: read-only.</p>"
        )

        body = f"""
<div class="card">
  <h2>Bot Settings</h2>
  <form method="post" action="/settings">
    <label>Presence text</label>
    <input type="text" name="presence_text" value="{html.escape(presence_text)}" maxlength="128" />
    <label>Presence type</label>
    <select name="presence_type">{type_options}</select>
    {settings_submit}
  </form>
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
        await self._authorize(request, required_permission="admin")
        data = await request.post()

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

        raise web.HTTPFound(location="/settings")

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
                '<p>Set DASHBOARD_ENABLE_CONSOLE=true to enable this feature.</p></div>'
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
