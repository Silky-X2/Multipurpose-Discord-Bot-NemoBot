import asyncio
import datetime

import aiosqlite
import discord
from discord.commands import Option, slash_command
from discord.ext import commands

from utils.time_parser import format_duration, parse_duration_to_seconds


class TempRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_path = "temp_roles.db"
        self.mod_roles = [1467922063195902085, 1448393918323622010]
        self.expiry_tasks = {}
        self._scheduler_started = False

    async def setup_database(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS temp_roles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    added_by INTEGER NOT NULL,
                    reason TEXT,
                    added_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_temp_roles_active
                ON temp_roles(active, expires_at)
                """
            )
            await db.commit()

    async def is_mod_or_admin(self, member: discord.Member):
        if member.guild_permissions.administrator:
            return True
        return any(role.id in self.mod_roles for role in member.roles)

    async def add_temp_role_record(self, guild_id, user_id, role_id, added_by, reason, expires_at):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO temp_roles (guild_id, user_id, role_id, added_by, reason, added_at, expires_at, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    guild_id,
                    user_id,
                    role_id,
                    added_by,
                    reason,
                    datetime.datetime.utcnow().isoformat(),
                    expires_at.isoformat(),
                ),
            )
            await db.commit()
            return cursor.lastrowid

    async def deactivate_record(self, record_id):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE temp_roles SET active = 0 WHERE id = ?", (record_id,))
            await db.commit()

    async def deactivate_active_records_for_role(self, guild_id, user_id, role_id):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT id
                FROM temp_roles
                WHERE guild_id = ? AND user_id = ? AND role_id = ? AND active = 1
                """,
                (guild_id, user_id, role_id),
            ) as cursor:
                record_ids = [row[0] for row in await cursor.fetchall()]

            await db.execute(
                """
                UPDATE temp_roles
                SET active = 0
                WHERE guild_id = ? AND user_id = ? AND role_id = ? AND active = 1
                """,
                (guild_id, user_id, role_id),
            )
            await db.commit()

        return record_ids

    async def _revoke_role(self, guild_id, user_id, role_id, reason):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        member = guild.get_member(user_id)
        role = guild.get_role(role_id)
        if not member or not role:
            return

        if role in member.roles:
            try:
                await member.remove_roles(role, reason=reason)
            except discord.Forbidden:
                pass

    def _schedule_expiry_task(self, record_id, guild_id, user_id, role_id, expires_at):
        old_task = self.expiry_tasks.get(record_id)
        if old_task and not old_task.done():
            old_task.cancel()

        delay = max(0, (expires_at - datetime.datetime.utcnow()).total_seconds())
        task = asyncio.get_event_loop().create_task(
            self._expiry_worker(record_id, guild_id, user_id, role_id, delay)
        )
        self.expiry_tasks[record_id] = task

    async def _expiry_worker(self, record_id, guild_id, user_id, role_id, delay):
        try:
            await asyncio.sleep(delay)
            await self._revoke_role(
                guild_id,
                user_id,
                role_id,
                reason="Temp role duration expired"
            )
        finally:
            await self.deactivate_record(record_id)
            self.expiry_tasks.pop(record_id, None)

    async def schedule_existing_records(self):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT id, guild_id, user_id, role_id, expires_at
                FROM temp_roles
                WHERE active = 1
                """
            ) as cursor:
                rows = await cursor.fetchall()

        for record_id, guild_id, user_id, role_id, expires_at_raw in rows:
            try:
                expires_at = datetime.datetime.fromisoformat(expires_at_raw)
            except ValueError:
                await self.deactivate_record(record_id)
                continue

            self._schedule_expiry_task(record_id, guild_id, user_id, role_id, expires_at)

    @commands.Cog.listener()
    async def on_ready(self):
        if self._scheduler_started:
            return
        await self.setup_database()
        await self.schedule_existing_records()
        self._scheduler_started = True

    @slash_command(name="temprole_add", description="Gibt einem User zeitlich begrenzt eine Rolle")
    async def temprole_add(
        self,
        ctx,
        member: Option(discord.Member, "User"),
        role: Option(discord.Role, "Rolle"),
        duration: Option(str, "Dauer z.B. 30min, 2h, 1d 2m"),
        reason: Option(str, "Grund", required=False) = "Kein Grund angegeben",
    ):
        if not await self.is_mod_or_admin(ctx.author):
            await ctx.respond("❌ Keine Berechtigung!", ephemeral=True)
            return

        if role.is_default():
            await ctx.respond("❌ Die @everyone Rolle kann nicht vergeben werden.", ephemeral=True)
            return

        if role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            await ctx.respond("❌ Du kannst keine gleichhohe oder hoehere Rolle vergeben.", ephemeral=True)
            return

        me = ctx.guild.me
        if role >= me.top_role:
            await ctx.respond("❌ Ich kann diese Rolle wegen Hierarchie nicht vergeben.", ephemeral=True)
            return

        try:
            duration_seconds = parse_duration_to_seconds(duration)
        except ValueError as exc:
            await ctx.respond(
                "❌ Ungueltige Dauer. Beispiele: `30min`, `2h`, `1d 2m 5min`.\n"
                f"Details: {exc}",
                ephemeral=True,
            )
            return

        expires_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=duration_seconds)

        try:
            await member.add_roles(role, reason=f"Temp role by {ctx.author}: {reason}")
        except discord.Forbidden:
            await ctx.respond("❌ Ich habe keine Berechtigung fuer diese Rolle.", ephemeral=True)
            return

        record_id = await self.add_temp_role_record(
            guild_id=ctx.guild_id,
            user_id=member.id,
            role_id=role.id,
            added_by=ctx.author.id,
            reason=reason,
            expires_at=expires_at,
        )
        self._schedule_expiry_task(record_id, ctx.guild_id, member.id, role.id, expires_at)

        expires_unix = int(expires_at.timestamp())
        embed = discord.Embed(
            title="✅ Temp Role gesetzt",
            description=(
                f"User: {member.mention}\n"
                f"Rolle: {role.mention}\n"
                f"Dauer: **{format_duration(duration_seconds)}**\n"
                f"Ablauf: <t:{expires_unix}:F> (<t:{expires_unix}:R>)\n"
                f"Grund: {reason}"
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Moderator: {ctx.author.display_name}")
        await ctx.respond(embed=embed)

    @slash_command(name="temprole_remove", description="Entfernt eine temporaere Rolle sofort")
    async def temprole_remove(
        self,
        ctx,
        member: Option(discord.Member, "User"),
        role: Option(discord.Role, "Rolle"),
        reason: Option(str, "Grund", required=False) = "Manuell entfernt",
    ):
        if not await self.is_mod_or_admin(ctx.author):
            await ctx.respond("❌ Keine Berechtigung!", ephemeral=True)
            return

        if role >= ctx.guild.me.top_role:
            await ctx.respond("❌ Ich kann diese Rolle wegen Hierarchie nicht entfernen.", ephemeral=True)
            return

        if role in member.roles:
            try:
                await member.remove_roles(role, reason=f"Manual temp role remove by {ctx.author}: {reason}")
            except discord.Forbidden:
                await ctx.respond("❌ Ich habe keine Berechtigung fuer diese Rolle.", ephemeral=True)
                return

        record_ids = await self.deactivate_active_records_for_role(ctx.guild_id, member.id, role.id)

        for record_id in record_ids:
            task = self.expiry_tasks.get(record_id)
            if task and not task.done():
                task.cancel()

        embed = discord.Embed(
            title="✅ Temp Role entfernt",
            description=f"User: {member.mention}\nRolle: {role.mention}\nGrund: {reason}",
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"Moderator: {ctx.author.display_name}")
        await ctx.respond(embed=embed)


def setup(bot):
    bot.add_cog(TempRoles(bot))