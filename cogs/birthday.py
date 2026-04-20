import asyncio
import datetime
from typing import Dict, Optional, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite
import discord
from discord.commands import Option, slash_command
from discord.ext import commands, tasks


class GuildBirthdaySettings(TypedDict):
    channel_id: Optional[int]
    timezone: str
    role_id: Optional[int]


class Birthdays(commands.Cog):
    birthday_settings_group = discord.SlashCommandGroup(
        "birthday_settings",
        "Einstellungen fuer Birthday Nachrichten",
    )

    def __init__(self, bot):
        self.bot = bot
        self.db_path = "birthdays.db"
        self._db_initialized = False
        self._db_lock = asyncio.Lock()
        self._guild_settings_cache: Dict[int, GuildBirthdaySettings] = {}

    async def cog_load(self):
        if not self.birthday_check_loop.is_running():
            self.birthday_check_loop.start()

    def cog_unload(self):
        if self.birthday_check_loop.is_running():
            self.birthday_check_loop.cancel()

    async def setup_database(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS birthdays (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    day INTEGER NOT NULL,
                    last_announced_year INTEGER,
                    last_role_year INTEGER,
                    PRIMARY KEY (guild_id, user_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS birthday_settings (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    role_id INTEGER
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_birthdays_today
                ON birthdays(guild_id, month, day)
                """
            )

            await self._migrate_schema(db)
            await db.commit()

    async def _migrate_schema(self, db):
        async with db.execute("PRAGMA table_info(birthdays)") as cursor:
            birthday_columns = {row[1] for row in await cursor.fetchall()}

        if "last_announced_year" not in birthday_columns:
            await db.execute("ALTER TABLE birthdays ADD COLUMN last_announced_year INTEGER")
        if "last_role_year" not in birthday_columns:
            await db.execute("ALTER TABLE birthdays ADD COLUMN last_role_year INTEGER")

        async with db.execute("PRAGMA table_info(birthday_settings)") as cursor:
            settings_columns = {row[1] for row in await cursor.fetchall()}

        if "channel_id" not in settings_columns:
            await db.execute("ALTER TABLE birthday_settings ADD COLUMN channel_id INTEGER")
        if "timezone" not in settings_columns:
            await db.execute("ALTER TABLE birthday_settings ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'")
        if "role_id" not in settings_columns:
            await db.execute("ALTER TABLE birthday_settings ADD COLUMN role_id INTEGER")

        await db.execute(
            """
            UPDATE birthday_settings
            SET timezone = 'UTC'
            WHERE timezone IS NULL OR TRIM(timezone) = ''
            """
        )

    async def ensure_database(self):
        if self._db_initialized:
            return

        async with self._db_lock:
            if self._db_initialized:
                return
            await self.setup_database()
            self._db_initialized = True

    @staticmethod
    def _is_valid_month_day(month: int, day: int) -> bool:
        try:
            datetime.date(2000, month, day)
            return True
        except ValueError:
            return False

    @staticmethod
    def _format_month_day(month: int, day: int) -> str:
        return f"{day:02d}.{month:02d}"

    @staticmethod
    def _next_occurrence(today: datetime.date, month: int, day: int):
        for year in range(today.year, today.year + 9):
            try:
                candidate = datetime.date(year, month, day)
            except ValueError:
                continue
            if candidate >= today:
                return candidate
        return None

    @staticmethod
    def _delta_label(delta_days: int) -> str:
        if delta_days <= 0:
            return "heute"
        if delta_days == 1:
            return "morgen"
        return f"in {delta_days} Tagen"

    def _validate_timezone_name(self, timezone_name: str) -> str:
        cleaned = (timezone_name or "UTC").strip() or "UTC"
        try:
            zone = ZoneInfo(cleaned)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Ungueltige Zeitzone. Beispiel: Europe/Berlin") from exc
        return getattr(zone, "key", cleaned)

    def _timezone_or_utc(self, timezone_name: str):
        cleaned = (timezone_name or "UTC").strip() or "UTC"
        try:
            return ZoneInfo(cleaned)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")

    def _today_for_timezone(self, timezone_name: str):
        return datetime.datetime.now(self._timezone_or_utc(timezone_name)).date()

    def _normalize_guild_settings(
        self,
        channel_id: Optional[int],
        timezone_name: Optional[str],
        role_id: Optional[int],
    ) -> GuildBirthdaySettings:
        normalized_timezone = (timezone_name or "UTC").strip() or "UTC"
        try:
            normalized_timezone = self._validate_timezone_name(normalized_timezone)
        except ValueError:
            normalized_timezone = "UTC"

        return {
            "channel_id": channel_id,
            "timezone": normalized_timezone,
            "role_id": role_id,
        }

    async def _set_birthday(self, guild_id: int, user_id: int, month: int, day: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO birthdays (guild_id, user_id, month, day, last_announced_year, last_role_year)
                VALUES (?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    month = excluded.month,
                    day = excluded.day,
                    last_announced_year = NULL,
                    last_role_year = NULL
                """,
                (guild_id, user_id, month, day),
            )
            await db.commit()

    async def _remove_birthday(self, guild_id: int, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM birthdays WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def _get_birthday(self, guild_id: int, user_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT month, day FROM birthdays WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ) as cursor:
                return await cursor.fetchone()

    async def _get_birthdays_for_guild(self, guild_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT user_id, month, day FROM birthdays WHERE guild_id = ?",
                (guild_id,),
            ) as cursor:
                return await cursor.fetchall()

    async def _get_guild_settings_db(self, db, guild_id: int) -> GuildBirthdaySettings:
        async with db.execute(
            "SELECT channel_id, timezone, role_id FROM birthday_settings WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return self._normalize_guild_settings(None, "UTC", None)

        return self._normalize_guild_settings(row[0], row[1], row[2])

    async def _get_guild_settings(self, guild_id: int) -> GuildBirthdaySettings:
        cached = self._guild_settings_cache.get(guild_id)
        if cached is not None:
            return {
                "channel_id": cached["channel_id"],
                "timezone": cached["timezone"],
                "role_id": cached["role_id"],
            }

        async with aiosqlite.connect(self.db_path) as db:
            settings = await self._get_guild_settings_db(db, guild_id)

        self._guild_settings_cache[guild_id] = settings
        return {
            "channel_id": settings["channel_id"],
            "timezone": settings["timezone"],
            "role_id": settings["role_id"],
        }

    async def get_guild_settings(self, guild_id: int):
        await self.ensure_database()
        return await self._get_guild_settings(guild_id)

    async def update_guild_settings(
        self,
        guild_id: int,
        channel_id: Optional[int],
        timezone: str,
        role_id: Optional[int],
    ):
        await self.ensure_database()
        timezone_name = self._validate_timezone_name(timezone)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO birthday_settings (guild_id, channel_id, timezone, role_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    timezone = excluded.timezone,
                    role_id = excluded.role_id
                """,
                (guild_id, channel_id, timezone_name, role_id),
            )
            await db.commit()

        normalized = self._normalize_guild_settings(channel_id, timezone_name, role_id)
        self._guild_settings_cache[guild_id] = normalized
        return {
            "channel_id": normalized["channel_id"],
            "timezone": normalized["timezone"],
            "role_id": normalized["role_id"],
        }

    async def _ensure_manage_guild(self, ctx) -> bool:
        if ctx.author.guild_permissions.manage_guild:
            return True
        await ctx.respond("Keine Berechtigung. Du brauchst Server verwalten.", ephemeral=True)
        return False

    async def _set_birthday_channel(self, guild_id: int, channel_id: int):
        settings = await self._get_guild_settings(guild_id)
        await self.update_guild_settings(
            guild_id,
            channel_id=channel_id,
            timezone=settings["timezone"],
            role_id=settings["role_id"],
        )

    async def _clear_birthday_channel(self, guild_id: int):
        settings = await self._get_guild_settings(guild_id)
        await self.update_guild_settings(
            guild_id,
            channel_id=None,
            timezone=settings["timezone"],
            role_id=settings["role_id"],
        )

    async def _set_birthday_timezone(self, guild_id: int, timezone_name: str):
        settings = await self._get_guild_settings(guild_id)
        await self.update_guild_settings(
            guild_id,
            channel_id=settings["channel_id"],
            timezone=timezone_name,
            role_id=settings["role_id"],
        )

    async def _set_birthday_role(self, guild_id: int, role_id: int):
        settings = await self._get_guild_settings(guild_id)
        await self.update_guild_settings(
            guild_id,
            channel_id=settings["channel_id"],
            timezone=settings["timezone"],
            role_id=role_id,
        )

    async def _clear_birthday_role(self, guild_id: int):
        settings = await self._get_guild_settings(guild_id)
        await self.update_guild_settings(
            guild_id,
            channel_id=settings["channel_id"],
            timezone=settings["timezone"],
            role_id=None,
        )

    def _resolve_announcement_channel(self, guild: discord.Guild, channel_id: Optional[int]):
        channel = None
        if channel_id:
            candidate = guild.get_channel(channel_id)
            if isinstance(candidate, discord.TextChannel):
                channel = candidate
        if channel is None:
            channel = guild.system_channel
        return channel

    def _can_send_in_channel(self, guild: discord.Guild, channel: Optional[discord.TextChannel]) -> bool:
        me = guild.me
        if not me or not channel:
            return False
        permissions = channel.permissions_for(me)
        return permissions.send_messages

    async def _process_guild_birthdays(self, guild: discord.Guild):
        settings = await self._get_guild_settings(guild.id)
        today = self._today_for_timezone(settings["timezone"])
        today_year = today.year

        due_rows = []
        cleanup_user_ids = []
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT user_id, last_announced_year, last_role_year
                FROM birthdays
                WHERE guild_id = ? AND month = ? AND day = ?
                """,
                (guild.id, today.month, today.day),
            ) as cursor:
                due_rows = await cursor.fetchall()

            if settings["role_id"]:
                async with db.execute(
                    """
                    SELECT user_id
                    FROM birthdays
                    WHERE guild_id = ? AND last_role_year = ? AND NOT (month = ? AND day = ?)
                    """,
                    (guild.id, today_year, today.month, today.day),
                ) as cursor:
                    cleanup_user_ids = [row[0] for row in await cursor.fetchall()]

        if not due_rows and not cleanup_user_ids:
            return

        due_announce_user_ids = [
            user_id
            for user_id, last_announced_year, _ in due_rows
            if last_announced_year != today_year
        ]
        due_role_user_ids = [
            user_id
            for user_id, _, last_role_year in due_rows
            if last_role_year != today_year
        ]

        announcement_updates = []
        role_updates = []

        if due_announce_user_ids:
            channel = self._resolve_announcement_channel(guild, settings["channel_id"])
            if channel and self._can_send_in_channel(guild, channel):
                mentions = [f"<@{user_id}>" for user_id in due_announce_user_ids]
                embed = discord.Embed(
                    title="Happy Birthday!",
                    description=f"Alles Gute zum Geburtstag an {', '.join(mentions)}!",
                    color=discord.Color.gold(),
                )
                embed.set_footer(text=f"Geburtstagsgruesse von NemoBot - {settings['timezone']}")

                try:
                    await channel.send(embed=embed)
                    for user_id in due_announce_user_ids:
                        announcement_updates.append((today_year, guild.id, user_id))
                except (discord.Forbidden, discord.HTTPException):
                    pass

        role_id = settings["role_id"]
        if role_id:
            role = guild.get_role(role_id)
            me = guild.me
            if role is not None and me is not None and me.guild_permissions.manage_roles and role < me.top_role:
                for user_id in due_role_user_ids:
                    member = guild.get_member(user_id)
                    if member is None:
                        continue

                    try:
                        if role not in member.roles:
                            await member.add_roles(role, reason="Birthday role for birthday day")
                        role_updates.append((today_year, guild.id, user_id))
                    except (discord.Forbidden, discord.HTTPException):
                        continue

                for user_id in cleanup_user_ids:
                    member = guild.get_member(user_id)
                    if member is None or role not in member.roles:
                        continue

                    try:
                        await member.remove_roles(role, reason="Birthday role expired")
                    except (discord.Forbidden, discord.HTTPException):
                        continue

        if not announcement_updates and not role_updates:
            return

        async with aiosqlite.connect(self.db_path) as db:
            if announcement_updates:
                await db.executemany(
                    "UPDATE birthdays SET last_announced_year = ? WHERE guild_id = ? AND user_id = ?",
                    announcement_updates,
                )
            if role_updates:
                await db.executemany(
                    "UPDATE birthdays SET last_role_year = ? WHERE guild_id = ? AND user_id = ?",
                    role_updates,
                )
            await db.commit()

    @commands.Cog.listener()
    async def on_ready(self):
        await self.ensure_database()

    @tasks.loop(hours=1)
    async def birthday_check_loop(self):
        await self.ensure_database()

        for guild in self.bot.guilds:
            try:
                await self._process_guild_birthdays(guild)
            except Exception:
                continue

    @birthday_check_loop.before_loop
    async def before_birthday_check_loop(self):
        await self.bot.wait_until_ready()

    @slash_command(name="birthday_set", description="Setzt deinen Geburtstag")
    async def birthday_set(
        self,
        ctx,
        day: Option(int, "Tag (1-31)"),
        month: Option(int, "Monat (1-12)"),
    ):
        if not ctx.guild_id:
            await ctx.respond("Dieser Command funktioniert nur in einem Server.", ephemeral=True)
            return

        if not self._is_valid_month_day(month, day):
            await ctx.respond("Ungueltiges Datum. Bitte nutze ein gueltiges Kalenderdatum.", ephemeral=True)
            return

        await self.ensure_database()
        await self._set_birthday(ctx.guild_id, ctx.author.id, month, day)
        await ctx.respond(
            f"Dein Geburtstag wurde auf **{self._format_month_day(month, day)}** gesetzt.",
            ephemeral=True,
        )

    @slash_command(name="birthday_remove", description="Entfernt deinen gespeicherten Geburtstag")
    async def birthday_remove(self, ctx):
        if not ctx.guild_id:
            await ctx.respond("Dieser Command funktioniert nur in einem Server.", ephemeral=True)
            return

        await self.ensure_database()
        deleted = await self._remove_birthday(ctx.guild_id, ctx.author.id)
        if deleted:
            await ctx.respond("Dein Geburtstag wurde entfernt.", ephemeral=True)
        else:
            await ctx.respond("Du hast aktuell keinen gespeicherten Geburtstag.", ephemeral=True)

    @slash_command(name="birthday", description="Zeigt einen gespeicherten Geburtstag")
    async def birthday(
        self,
        ctx,
        member: Option(discord.Member, "User", required=False) = None,
    ):
        if not ctx.guild_id:
            await ctx.respond("Dieser Command funktioniert nur in einem Server.", ephemeral=True)
            return

        await self.ensure_database()
        target = member or ctx.author
        row = await self._get_birthday(ctx.guild_id, target.id)
        if not row:
            await ctx.respond(f"Fuer {target.mention} ist kein Geburtstag gespeichert.", ephemeral=True)
            return

        month, day = row
        settings = await self._get_guild_settings(ctx.guild_id)
        today = self._today_for_timezone(settings["timezone"])
        next_date = self._next_occurrence(today, month, day)
        if next_date is None:
            await ctx.respond("Geburtstag konnte nicht berechnet werden.", ephemeral=True)
            return

        delta_days = (next_date - today).days
        embed = discord.Embed(
            title="Geburtstag",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="User", value=target.mention, inline=True)
        embed.add_field(name="Datum", value=self._format_month_day(month, day), inline=True)
        embed.add_field(name="Naechster", value=self._delta_label(delta_days), inline=False)
        embed.set_footer(text=f"Zeitzone: {settings['timezone']}")
        await ctx.respond(embed=embed)

    @slash_command(name="birthdays", description="Zeigt kommende Geburtstage")
    async def birthdays(
        self,
        ctx,
        days: Option(int, "Wie viele Tage voraus", required=False) = 30,
    ):
        if not ctx.guild_id:
            await ctx.respond("Dieser Command funktioniert nur in einem Server.", ephemeral=True)
            return

        await self.ensure_database()
        lookahead_days = max(1, min(int(days or 30), 365))
        rows = await self._get_birthdays_for_guild(ctx.guild_id)
        settings = await self._get_guild_settings(ctx.guild_id)
        today = self._today_for_timezone(settings["timezone"])

        upcoming = []
        for user_id, month, day in rows:
            next_date = self._next_occurrence(today, month, day)
            if next_date is None:
                continue
            delta_days = (next_date - today).days
            if delta_days > lookahead_days:
                continue

            member = ctx.guild.get_member(user_id)
            mention = member.mention if member else f"<@{user_id}>"
            upcoming.append((delta_days, mention, month, day))

        upcoming.sort(key=lambda item: (item[0], item[2], item[3], item[1]))

        if not upcoming:
            await ctx.respond(
                f"Keine Geburtstage in den naechsten {lookahead_days} Tagen gefunden.",
                ephemeral=True,
            )
            return

        lines = []
        for delta_days, mention, month, day in upcoming[:20]:
            lines.append(
                f"{mention} - {self._format_month_day(month, day)} ({self._delta_label(delta_days)})"
            )

        embed = discord.Embed(
            title=f"Kommende Geburtstage ({lookahead_days} Tage)",
            description="\n".join(lines),
            color=discord.Color.purple(),
        )
        embed.set_footer(text=f"Zeitzone: {settings['timezone']}")

        if len(upcoming) > 20:
            embed.set_footer(
                text=f"Zeitzone: {settings['timezone']} | Und {len(upcoming) - 20} weitere"
            )

        await ctx.respond(embed=embed)

    @birthday_settings_group.command(name="show", description="Zeigt die Birthday Einstellungen des Servers")
    async def birthday_settings_show(self, ctx):
        if not ctx.guild_id:
            await ctx.respond("Dieser Command funktioniert nur in einem Server.", ephemeral=True)
            return

        await self.ensure_database()
        settings = await self._get_guild_settings(ctx.guild_id)

        guild = ctx.guild
        channel = guild.get_channel(settings["channel_id"]) if settings["channel_id"] else None
        role = guild.get_role(settings["role_id"]) if settings["role_id"] else None

        embed = discord.Embed(
            title="Birthday Einstellungen",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Channel",
            value=channel.mention if channel else "System-Channel (Fallback)",
            inline=False,
        )
        embed.add_field(name="Zeitzone", value=settings["timezone"], inline=False)
        embed.add_field(name="Birthday Rolle", value=role.mention if role else "Keine", inline=False)
        await ctx.respond(embed=embed, ephemeral=True)

    @birthday_settings_group.command(name="channel_set", description="Setzt den Channel fuer Geburtstagsgruesse")
    async def birthday_settings_channel_set(
        self,
        ctx,
        channel: Option(discord.TextChannel, "Channel fuer Geburtstagsnachrichten"),
    ):
        if not ctx.guild_id:
            await ctx.respond("Dieser Command funktioniert nur in einem Server.", ephemeral=True)
            return

        if not await self._ensure_manage_guild(ctx):
            return

        await self.ensure_database()
        await self._set_birthday_channel(ctx.guild_id, channel.id)
        await ctx.respond(f"Geburtstagsgruesse werden jetzt in {channel.mention} gepostet.")

    @birthday_settings_group.command(name="channel_clear", description="Entfernt den Birthday Channel")
    async def birthday_settings_channel_clear(self, ctx):
        if not ctx.guild_id:
            await ctx.respond("Dieser Command funktioniert nur in einem Server.", ephemeral=True)
            return

        if not await self._ensure_manage_guild(ctx):
            return

        await self.ensure_database()
        await self._clear_birthday_channel(ctx.guild_id)
        await ctx.respond(
            "Birthday Channel entfernt. Geburtstage werden jetzt im System-Channel gepostet, falls verfuegbar."
        )

    @birthday_settings_group.command(name="timezone_set", description="Setzt die Zeitzone fuer Birthday Checks")
    async def birthday_settings_timezone_set(
        self,
        ctx,
        timezone: Option(str, "IANA Zone, z.B. Europe/Berlin"),
    ):
        if not ctx.guild_id:
            await ctx.respond("Dieser Command funktioniert nur in einem Server.", ephemeral=True)
            return

        if not await self._ensure_manage_guild(ctx):
            return

        await self.ensure_database()
        try:
            normalized = self._validate_timezone_name(timezone)
        except ValueError as exc:
            await ctx.respond(str(exc), ephemeral=True)
            return

        await self._set_birthday_timezone(ctx.guild_id, normalized)
        await ctx.respond(f"Birthday Zeitzone wurde auf **{normalized}** gesetzt.")

    @birthday_settings_group.command(name="role_set", description="Setzt die Birthday Rolle fuer den Geburtstag")
    async def birthday_settings_role_set(
        self,
        ctx,
        role: Option(discord.Role, "Rolle fuer Geburtstage"),
    ):
        if not ctx.guild_id:
            await ctx.respond("Dieser Command funktioniert nur in einem Server.", ephemeral=True)
            return

        if not await self._ensure_manage_guild(ctx):
            return

        if role.is_default():
            await ctx.respond("@everyone kann nicht als Birthday Rolle genutzt werden.", ephemeral=True)
            return

        me = ctx.guild.me
        if me and (not me.guild_permissions.manage_roles or role >= me.top_role):
            await ctx.respond(
                "Ich kann diese Rolle aktuell nicht verwalten (Hierarchie/Berechtigungen).",
                ephemeral=True,
            )
            return

        await self.ensure_database()
        await self._set_birthday_role(ctx.guild_id, role.id)
        await ctx.respond(f"Birthday Rolle wurde auf {role.mention} gesetzt.")

    @birthday_settings_group.command(name="role_clear", description="Entfernt die Birthday Rolle")
    async def birthday_settings_role_clear(self, ctx):
        if not ctx.guild_id:
            await ctx.respond("Dieser Command funktioniert nur in einem Server.", ephemeral=True)
            return

        if not await self._ensure_manage_guild(ctx):
            return

        await self.ensure_database()
        await self._clear_birthday_role(ctx.guild_id)
        await ctx.respond("Birthday Rolle wurde entfernt.")


def setup(bot):
    bot.add_cog(Birthdays(bot))