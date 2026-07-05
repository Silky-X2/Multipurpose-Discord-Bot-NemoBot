"""
Twitch Live Notifier
--------------------
Benachrichtigt einen Discord-Kanal, wenn ein Twitch-Kanal live geht.

Konfiguration in settings.json:
{
  "twitch": {
    "notifications": [
      {
        "twitch_channel": "kanalname",
        "discord_channel_id": 123456789012345678,
        "message": "@everyone **{name}** ist jetzt live!\n**{title}**\n{url}"
      }
    ]
  }
}

Umgebungsvariablen (.env):
  TWITCH_CLIENT_ID      - Twitch Application Client ID
  TWITCH_CLIENT_SECRET  - Twitch Application Client Secret

Twitch App erstellen: https://dev.twitch.tv/console/apps
"""

import os
import aiohttp
import discord
from discord.ext import commands, tasks

from utils.settings import load_settings


TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_STREAM_URL = "https://api.twitch.tv/helix/streams"

POLL_INTERVAL_SECONDS = 120  # Alle 2 Minuten prüfen


class TwitchNotifier(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.client_id = os.getenv("TWITCH_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
        self._access_token: str | None = None
        self._live_channels: set[str] = set()  # Verhindert Doppel-Benachrichtigungen
        self._session: aiohttp.ClientSession | None = None

        settings = load_settings()
        self._notifications: list[dict] = (
            settings.get("twitch", {}).get("notifications", [])
        )

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def cog_unload(self):
        self._check_streams.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.client_id or not self.client_secret:
            print(
                "[Twitch] TWITCH_CLIENT_ID oder TWITCH_CLIENT_SECRET fehlen – Notifier deaktiviert.",
                flush=True,
            )
            return
        if not self._notifications:
            print("[Twitch] Keine Benachrichtigungen in settings.json konfiguriert.", flush=True)
            return
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        if not self._check_streams.is_running():
            self._check_streams.start()

    # ------------------------------------------------------------------ #
    #  Twitch API helpers                                                  #
    # ------------------------------------------------------------------ #

    async def _get_token(self) -> str | None:
        """Holt einen neuen App-Access-Token von Twitch."""
        try:
            async with self._session.post(
                TWITCH_TOKEN_URL,
                params={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("access_token")
                print(f"[Twitch] Token-Fehler: HTTP {resp.status}", flush=True)
        except Exception as e:
            print(f"[Twitch] Token-Request fehlgeschlagen: {e}", flush=True)
        return None

    async def _fetch_streams(self, user_logins: list[str]) -> list[dict]:
        """Gibt die Stream-Objekte aller aktuell live-Kanäle zurück."""
        if not self._access_token:
            self._access_token = await self._get_token()
        if not self._access_token:
            return []

        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {self._access_token}",
        }
        params = [("user_login", name.lower()) for name in user_logins]
        try:
            async with self._session.get(
                TWITCH_STREAM_URL, headers=headers, params=params
            ) as resp:
                if resp.status == 401:
                    # Token abgelaufen – neu holen
                    self._access_token = await self._get_token()
                    return []
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", [])
                print(f"[Twitch] Stream-Abfrage fehlgeschlagen: HTTP {resp.status}", flush=True)
        except Exception as e:
            print(f"[Twitch] Stream-Request fehlgeschlagen: {e}", flush=True)
        return []

    # ------------------------------------------------------------------ #
    #  Background task                                                     #
    # ------------------------------------------------------------------ #

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def _check_streams(self):
        if not self._notifications:
            return

        user_logins = [n["twitch_channel"].lower() for n in self._notifications]
        live_streams = await self._fetch_streams(user_logins)
        live_now: set[str] = {s["user_login"].lower() for s in live_streams}

        # Kanäle, die neu live gegangen sind
        newly_live = live_now - self._live_channels

        for stream in live_streams:
            login = stream["user_login"].lower()
            if login not in newly_live:
                continue

            # Passende Konfiguration suchen
            for cfg in self._notifications:
                if cfg["twitch_channel"].lower() != login:
                    continue

                channel = self.bot.get_channel(int(cfg["discord_channel_id"]))
                if channel is None:
                    print(
                        f"[Twitch] Discord-Kanal {cfg['discord_channel_id']} nicht gefunden.",
                        flush=True,
                    )
                    continue

                url = f"https://twitch.tv/{stream['user_login']}"
                msg_template = cfg.get(
                    "message",
                    "**{name}** ist jetzt live auf Twitch!\n**{title}**\n{url}",
                )
                message = msg_template.format(
                    name=stream.get("user_name", stream["user_login"]),
                    title=stream.get("title", ""),
                    game=stream.get("game_name", ""),
                    url=url,
                    viewers=stream.get("viewer_count", 0),
                )
                try:
                    await channel.send(message)
                    print(f"[Twitch] Benachrichtigung gesendet: {login} ist live.", flush=True)
                except discord.Forbidden:
                    print(f"[Twitch] Kein Zugriff auf Kanal {channel.id}.", flush=True)
                except Exception as e:
                    print(f"[Twitch] Fehler beim Senden: {e}", flush=True)

        self._live_channels = live_now

    @_check_streams.before_loop
    async def _before_check(self):
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot):
    bot.add_cog(TwitchNotifier(bot))
