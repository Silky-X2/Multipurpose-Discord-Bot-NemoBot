import asyncio
import html
import os
import re
import sys
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
        if self.admin_token and not self.view_token:
            self.view_token = self.admin_token
        if self.view_token and not self.admin_token:
            self.admin_token = self.view_token

        if not self.view_token and not self.admin_token:
            # Safe default for first-time setup; should be changed in .env immediately.
            self.view_token = "change-me"
            self.admin_token = "change-me"

        self.console_enabled = os.getenv("DASHBOARD_ENABLE_CONSOLE", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        self.automod_cache = {}
        self._startup_done = False
        self.runner = None
        self.site = None

        self.app = web.Application()
        self.app.add_routes(
            [
                web.get("/", self.home_page),
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

    def _extract_token(self, request: web.Request):
        token = request.query.get("token", "").strip()
        if token:
            return token

        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()

        return ""

    def _permission_for_token(self, token: str):
        if token and token == self.admin_token:
            return "admin"
        if token and token == self.view_token:
            return "viewer"
        return None

    def _token_query(self, token: str):
        return f"token={quote(token)}"

    def _form_text(self, post_data: Any, key: str, default: str = "") -> str:
        value = post_data.get(key, default)
        if value is None:
            return default
        if isinstance(value, str):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    async def _authorize(self, request: web.Request, require_admin: bool = False):
        token = self._extract_token(request)
        permission = self._permission_for_token(token)
        if permission is None:
            raise web.HTTPUnauthorized(text="Unauthorized. Use ?token=YOUR_TOKEN")
        if require_admin and permission != "admin":
            raise web.HTTPForbidden(text="Admin permission required")
        return permission, token

    def _layout(self, title: str, body: str, token: str, permission: str):
        q = self._token_query(token)
        nav = (
            f'<a href="/?{q}">Home</a>'
            f'<a href="/leaderboard?{q}">Leaderboard</a>'
            f'<a href="/level-formula?{q}">Level Formula</a>'
            f'<a href="/automod?{q}">Automod</a>'
            f'<a href="/settings?{q}">Bot Settings</a>'
            f'<a href="/console?{q}">Console</a>'
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
    nav {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 18px; }}
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
    <div class="card">
      <h1>{html.escape(title)}</h1>
      <p>Permission: <strong>{html.escape(permission)}</strong></p>
      <nav>{nav}</nav>
    </div>
    {body}
  </div>
</body>
</html>
"""

    async def home_page(self, request: web.Request):
        permission, token = await self._authorize(request)
        body = f"""
<div class="card">
  <h2>NemoBot Dashboard</h2>
  <p>Dashboard host: {html.escape(self.host)}:{self.port}</p>
  <p>This panel supports level formula controls, leaderboards, automod, bot settings, restart, and optional console access.</p>
  <ul>
    <li>Viewer token: can view statistics and settings pages.</li>
    <li>Admin token: can edit settings, restart the bot, and run console commands when enabled.</li>
  </ul>
  <p>Use this URL with token: <span class="mono">?{self._token_query(token)}</span></p>
</div>
"""
        return web.Response(text=self._layout("Dashboard", body, token, permission), content_type="text/html")

    async def leaderboard_page(self, request: web.Request):
        permission, token = await self._authorize(request)

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
        return web.Response(text=self._layout("Leaderboard", body, token, permission), content_type="text/html")

    async def level_formula_page(self, request: web.Request):
        permission, token = await self._authorize(request)
        level_cog = self.bot.get_cog("LevelSystem")

        if not level_cog:
            return web.Response(status=503, text="LevelSystem cog is not loaded")

        formula = await level_cog.get_level_formula()
        preview_level = max(0, int(request.query.get("preview_level", "10") or 10))
        xp_for_step = level_cog.get_xp_needed_for_level(preview_level)
        xp_to_reach = level_cog.get_total_xp_for_level(preview_level)
        q = self._token_query(token)

        body = f"""
<div class="card">
  <h2>Level Formula</h2>
  <p>Current formula: <strong>XP needed from level L to L+1 = XP_BASE + XP_SCALE * L</strong></p>
  <p>Current values: XP_BASE={formula['xp_base']}, XP_SCALE={formula['xp_scale']}</p>
  <form method="get" action="/level-formula">
    <input type="hidden" name="token" value="{html.escape(token)}" />
    <label>Preview level (L)</label>
    <input type="number" name="preview_level" min="0" value="{preview_level}" />
    <button type="submit">Preview</button>
  </form>
  <p>For L={preview_level}: XP for next level is <strong>{int(xp_for_step)}</strong>.</p>
  <p>Total XP required to reach level {preview_level}: <strong>{int(xp_to_reach)}</strong>.</p>
</div>
"""

        if permission == "admin":
            body += f"""
<div class="card">
  <h2>Update Formula</h2>
  <form method="post" action="/level-formula?{q}">
    <label>XP_BASE</label>
    <input type="number" name="xp_base" step="0.01" min="1" value="{formula['xp_base']}" />
    <label>XP_SCALE</label>
    <input type="number" name="xp_scale" step="0.01" min="0" value="{formula['xp_scale']}" />
    <label><input type="checkbox" name="recalculate" value="1" /> Recalculate all users now</label>
    <button type="submit">Save Formula</button>
  </form>
</div>
"""

        return web.Response(text=self._layout("Level Formula", body, token, permission), content_type="text/html")

    async def level_formula_update(self, request: web.Request):
        _, token = await self._authorize(request, require_admin=True)
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

        raise web.HTTPFound(location=f"/level-formula?{self._token_query(token)}")

    async def automod_page(self, request: web.Request):
        permission, token = await self._authorize(request)

        guilds = sorted(self.bot.guilds, key=lambda g: g.name.lower())
        if not guilds:
            body = "<div class=\"card\"><h2>Automod</h2><p>Bot is not in any guild.</p></div>"
            return web.Response(text=self._layout("Automod", body, token, permission), content_type="text/html")

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

        q = self._token_query(token)
        checked = "checked" if settings["anti_link"] else ""
        blocked_words = ", ".join(settings["blocked_words"])
        automod_submit = (
            '<button type="submit">Save Automod</button>'
            if permission == "admin"
            else "<p>Viewer mode: read-only.</p>"
        )

        body = f"""
<div class="card">
  <h2>Automod Settings</h2>
  <form method="post" action="/automod?{q}">
    <label>Guild</label>
    <select name="guild_id">{guild_options}</select>
    <label><input type="checkbox" name="anti_link" value="1" {checked} /> Delete messages containing links</label>
    <label>Blocked words (comma-separated)</label>
    <textarea name="blocked_words">{html.escape(blocked_words)}</textarea>
        {automod_submit}
  </form>
</div>
"""

        return web.Response(text=self._layout("Automod", body, token, permission), content_type="text/html")

    async def automod_update(self, request: web.Request):
        _, token = await self._authorize(request, require_admin=True)
        data = await request.post()

        try:
            guild_id = int(self._form_text(data, "guild_id", "0"))
        except ValueError:
            return web.Response(status=400, text="Invalid guild id")

        anti_link = self._form_text(data, "anti_link", "") == "1"
        blocked_words = self._form_text(data, "blocked_words", "")
        await self.save_automod_settings(guild_id, anti_link, blocked_words)

        raise web.HTTPFound(location=f"/automod?{self._token_query(token)}&guild_id={guild_id}")

    async def settings_page(self, request: web.Request):
        permission, token = await self._authorize(request)

        current_activity_name = self.bot.activity.name if self.bot.activity and self.bot.activity.name else "NemoBot"
        presence_text = await self.get_setting("presence_text", current_activity_name)
        presence_type = await self.get_setting("presence_type", "watching")
        q = self._token_query(token)

        type_options = "".join(
            f"<option value=\"{opt}\" {'selected' if opt == presence_type else ''}>{opt}</option>"
            for opt in ["watching", "playing", "listening"]
        )
        settings_submit = (
            '<button type="submit">Save Bot Settings</button>'
            if permission == "admin"
            else "<p>Viewer mode: read-only.</p>"
        )

        body = f"""
<div class="card">
  <h2>Bot Settings</h2>
  <form method="post" action="/settings?{q}">
    <label>Presence text</label>
    <input type="text" name="presence_text" value="{html.escape(presence_text)}" maxlength="128" />
    <label>Presence type</label>
    <select name="presence_type">{type_options}</select>
        {settings_submit}
  </form>
</div>
"""

        if permission == "admin":
            body += f"""
<div class="card">
  <h2>Bot Restart</h2>
  <form method="post" action="/restart?{q}">
    <button class="danger" type="submit">Restart Bot Process</button>
  </form>
</div>
"""

        return web.Response(text=self._layout("Bot Settings", body, token, permission), content_type="text/html")

    async def settings_update(self, request: web.Request):
        _, token = await self._authorize(request, require_admin=True)
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

        raise web.HTTPFound(location=f"/settings?{self._token_query(token)}")

    async def restart_bot(self, request: web.Request):
        _, token = await self._authorize(request, require_admin=True)
        asyncio.get_event_loop().create_task(self._delayed_restart())
        return web.Response(
            text=self._layout(
                "Restarting",
                "<div class=\"card\"><h2>Restart requested</h2><p>Bot process is restarting now.</p></div>",
                token,
                "admin",
            ),
            content_type="text/html",
        )

    async def _delayed_restart(self):
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable, *sys.argv])

    async def console_page(self, request: web.Request):
        permission, token = await self._authorize(request)

        if permission != "admin":
            return web.Response(status=403, text="Console requires admin token")

        if not self.console_enabled:
            body = (
                "<div class=\"card\"><h2>Console disabled</h2>"
                "<p>Set DASHBOARD_ENABLE_CONSOLE=true to enable this feature.</p></div>"
            )
            return web.Response(text=self._layout("Console", body, token, permission), content_type="text/html")

        q = self._token_query(token)
        body = f"""
<div class="card">
  <h2>Console</h2>
  <p>Runs shell commands on the bot host. Use with care.</p>
  <form method="post" action="/console?{q}">
    <label>Command</label>
    <input type="text" name="command" maxlength="400" placeholder="echo hello" />
    <button type="submit">Run</button>
  </form>
</div>
"""
        return web.Response(text=self._layout("Console", body, token, permission), content_type="text/html")

    async def console_run(self, request: web.Request):
        permission, token = await self._authorize(request, require_admin=True)
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
  <p><a href="/console?{self._token_query(token)}">Run another command</a></p>
</div>
"""
        return web.Response(text=self._layout("Console Result", body, token, permission), content_type="text/html")

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