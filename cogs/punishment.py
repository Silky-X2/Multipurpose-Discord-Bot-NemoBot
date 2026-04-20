import discord
from discord.ext import commands, tasks
from discord.commands import slash_command, Option
import aiosqlite
import asyncio
import datetime
import os

class punishment(commands.Cog):
	def _get_dev_user_ids(self):
		raw_ids = os.getenv("DEV_USER_IDS", "")
		dev_user_ids = set()
		for raw_id in raw_ids.split(","):
			raw_id = raw_id.strip()
			if not raw_id:
				continue
			try:
				dev_user_ids.add(int(raw_id))
			except ValueError:
				continue
		return dev_user_ids

	def _is_dm_removetimeout_authorized(self, author_id: int):
		return author_id in self._get_dev_user_ids()

	def _get_dm_removetimeout_token(self):
		token = os.getenv("REMOVETIMEOUT_DM_TOKEN", "").strip()
		return token or None

	@commands.Cog.listener()
	async def on_message(self, message):
		if message.guild is not None:
			return
		if message.author.bot:
			return
		if not self._is_dm_removetimeout_authorized(message.author.id):
			return
		content = message.content.strip()
		if content.lower().startswith("removetimeout"):
			parts = content.split()
			required_token = self._get_dm_removetimeout_token()
			expected_length = 4 if required_token else 3
			usage = "Usage: removetimeout <guild_id> <user_id> [token]"
			if len(parts) != expected_length:
				await message.channel.send(usage)
				return
			if required_token and parts[3] != required_token:
				await message.channel.send("Ungueltiger Token.")
				return
			try:
				guild_id = int(parts[1])
				user_id = int(parts[2])
			except ValueError:
				await message.channel.send("IDs müssen Zahlen sein.")
				return
			guild = self.bot.get_guild(guild_id)
			if not guild:
				await message.channel.send(f"Guild mit ID {guild_id} nicht gefunden.")
				return
			member = guild.get_member(user_id)
			if not member:
				await message.channel.send(f"Mitglied mit ID {user_id} nicht gefunden in Guild {guild_id}.")
				return
			mute_role = guild.get_role(self.mute_role_id)
			if not mute_role:
				await message.channel.send("Mute-Rolle nicht gefunden!")
				return
			if mute_role in member.roles:
				try:
					await member.remove_roles(mute_role, reason="Entfernt von Dev per DM")
					await self.log_punishment(member.id, guild.id, message.author.id, "unmute", "Entfernt von Dev per DM")
					await message.channel.send(f"Timeout/Mute für {member} in {guild.name} entfernt.")
				except Exception as e:
					await message.channel.send(f"Fehler beim Entfernen: {e}")
			else:
				await message.channel.send(f"{member} ist nicht gemutet.")
			return

	def __init__(self, bot):
		self.bot = bot
		self.db_path = "punishment_data.db"
		self.mute_role_id = 1448393918323622010  # Mute Role ID

		# MOD ROLE
		self.mod_roles = {1448393918323622010}

	# SETUP DATABASE
	async def setup_database(self):
		async with aiosqlite.connect(self.db_path) as db:
			await db.execute("""
				CREATE TABLE IF NOT EXISTS punishments (
					id INTEGER PRIMARY KEY AUTOINCREMENT,
					user_id INTEGER,
					guild_id INTEGER,
					mod_id INTEGER,
					action TEXT,
					reason TEXT,
					timestamp TEXT,
					duration INTEGER,
					expires_at TEXT
				)
			""")
			await db.execute("""
				CREATE TABLE IF NOT EXISTS warns (
					id INTEGER PRIMARY KEY AUTOINCREMENT,
					user_id INTEGER,
					guild_id INTEGER,
					mod_id INTEGER,
					reason TEXT,
					timestamp TEXT
				)
			""")
			await db.commit()

	# PERMISSION CHECK
	async def is_mod_or_admin(self, member: discord.Member):
		if member.guild_permissions.administrator:
			return True
		return any(role.id in self.mod_roles for role in member.roles)

	# LOG PUNISHMENT
	async def log_punishment(self, user_id: int, guild_id: int, mod_id: int, action: str, reason: str, duration: int = None, expires_at: str = None):
		async with aiosqlite.connect(self.db_path) as db:
			await db.execute("""
				INSERT INTO punishments (user_id, guild_id, mod_id, action, reason, timestamp, duration, expires_at)
				VALUES (?, ?, ?, ?, ?, ?, ?, ?)
			""", (user_id, guild_id, mod_id, action, reason, datetime.datetime.utcnow().isoformat(), duration, expires_at))
			await db.commit()

	# BAN COMMAND
	@slash_command(name="ban", description="Banne einen Benutzer", default_member_permissions=discord.Permissions(ban_members=True))
	async def ban(self, ctx, user: discord.User, reason: Option(str, "Grund (optional)", required=False) = "Kein Grund angegeben"):
		if not await self.is_mod_or_admin(ctx.author):
			await ctx.respond("Keine Berechtigung!", ephemeral=True)
			return

		if user.id == ctx.author.id:
			await ctx.respond("Du kannst dich selbst nicht bannen!", ephemeral=True)
			return

		try:
			await ctx.guild.ban(user, reason=reason)
			await self.log_punishment(user.id, ctx.guild_id, ctx.author.id, "ban", reason)

			embed = discord.Embed(
				title="Benutzer gebannet",
				description=f"Benutzer: {user.mention}\nGrund: {reason}",
				color=discord.Color.red()
			)
			embed.set_footer(text=f"Moderator: {ctx.author.name}")
			await ctx.respond(embed=embed)
		except discord.Forbidden:
			await ctx.respond("Ich habe keine Berechtigung, diesen Benutzer zu bannen!", ephemeral=True)

	# UNBAN COMMAND
	@slash_command(name="unban", description="Entferne einen Bann", default_member_permissions=discord.Permissions(ban_members=True))
	async def unban(self, ctx, user: discord.User, reason: Option(str, "Grund (optional)", required=False) = "Kein Grund"):
		if not await self.is_mod_or_admin(ctx.author):
			await ctx.respond("Keine Berechtigung!", ephemeral=True)
			return

		try:
			await ctx.guild.unban(user, reason=reason)
			await self.log_punishment(user.id, ctx.guild_id, ctx.author.id, "unban", reason)

			embed = discord.Embed(
				title="Bann entfernt",
				description=f"Benutzer: {user.mention}\nGrund: {reason}",
				color=discord.Color.green()
			)
			embed.set_footer(text=f"Moderator: {ctx.author.name}")
			await ctx.respond(embed=embed)
		except discord.Forbidden:
			await ctx.respond("Ich habe keine Berechtigung!", ephemeral=True)
		except discord.NotFound:
			await ctx.respond("Benutzer ist nicht gebannet!", ephemeral=True)

	# KICK COMMAND
	@slash_command(name="kick", description="Kicke einen Benutzer", default_member_permissions=discord.Permissions(kick_members=True))
	async def kick(self, ctx, member: discord.Member, reason: Option(str, "Grund (optional)", required=False) = "Kein Grund angegeben"):
		if not await self.is_mod_or_admin(ctx.author):
			await ctx.respond("Keine Berechtigung!", ephemeral=True)
			return

		if member.id == ctx.author.id:
			await ctx.respond("Du kannst dich selbst nicht kicken!", ephemeral=True)
			return

		if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
			await ctx.respond("Du kannst diesen Benutzer nicht kicken!", ephemeral=True)
			return

		try:
			await member.kick(reason=reason)
			await self.log_punishment(member.id, ctx.guild_id, ctx.author.id, "kick", reason)

			embed = discord.Embed(
				title="Benutzer gekickt",
				description=f"Benutzer: {member.mention}\nGrund: {reason}",
				color=discord.Color.orange()
			)
			embed.set_footer(text=f"Moderator: {ctx.author.name}")
			await ctx.respond(embed=embed)
		except discord.Forbidden:
			await ctx.respond("Ich habe keine Berechtigung, diesen Benutzer zu kicken!", ephemeral=True)

	# MUTE COMMAND
	@slash_command(name="mute", description="Stummschaltung für einen Benutzer", default_member_permissions=discord.Permissions(moderate_members=True))
	async def mute(self, ctx, member: discord.Member, duration: Option(int, "Dauer in Minuten"), reason: Option(str, "Grund (optional)", required=False) = "Kein Grund angegeben"):
		if not await self.is_mod_or_admin(ctx.author):
			await ctx.respond("Keine Berechtigung!", ephemeral=True)
			return

		if member.id == ctx.author.id:
			await ctx.respond("Du kannst dich selbst nicht stummschalten!", ephemeral=True)
			return

		try:
			mute_role = ctx.guild.get_role(self.mute_role_id)
			if not mute_role:
				await ctx.respond("Mute-Rolle nicht gefunden! Bitte konfiguriere sie.", ephemeral=True)
				return

			await member.add_roles(mute_role)
			expires_at = (datetime.datetime.utcnow() + datetime.timedelta(minutes=duration)).isoformat()
			await self.log_punishment(member.id, ctx.guild_id, ctx.author.id, "mute", reason, duration, expires_at)

			embed = discord.Embed(
				title="Benutzer stummgeschaltet",
				description=f"Benutzer: {member.mention}\nDauer: {duration} Minuten\nGrund: {reason}",
				color=discord.Color.blue()
			)
			embed.set_footer(text=f"Moderator: {ctx.author.name}")
			await ctx.respond(embed=embed)

			# Automatisches Unmute nach Duration
			await asyncio.sleep(duration * 60)
			try:
				await member.remove_roles(mute_role)
				await self.log_punishment(member.id, ctx.guild_id, ctx.author.id, "unmute", "Automatisches Unmute nach Ablauf der Zeit")
			except:
				pass

		except discord.Forbidden:
			await ctx.respond("Ich habe keine Berechtigung!", ephemeral=True)

	# UNMUTE COMMAND
	@slash_command(name="unmute", description="Entferne die Stummschaltung von einem Benutzer", default_member_permissions=discord.Permissions(moderate_members=True))
	async def unmute(self, ctx, member: discord.Member, reason: Option(str, "Grund (optional)", required=False) = "Kein Grund"):
		if not await self.is_mod_or_admin(ctx.author):
			await ctx.respond("Keine Berechtigung!", ephemeral=True)
			return

		try:
			mute_role = ctx.guild.get_role(self.mute_role_id)
			if not mute_role:
				await ctx.respond("Mute-Rolle nicht gefunden!", ephemeral=True)
				return

			await member.remove_roles(mute_role)
			await self.log_punishment(member.id, ctx.guild_id, ctx.author.id, "unmute", reason)

			embed = discord.Embed(
				title="Stummschaltung entfernt",
				description=f"Benutzer: {member.mention}\nGrund: {reason}",
				color=discord.Color.green()
			)
			embed.set_footer(text=f"Moderator: {ctx.author.name}")
			await ctx.respond(embed=embed)
		except discord.Forbidden:
			await ctx.respond("Ich habe keine Berechtigung!", ephemeral=True)

	# WARN COMMAND
	@slash_command(name="warn", description="Warne einen Benutzer", default_member_permissions=discord.Permissions(moderate_members=True))
	async def warn(self, ctx, member: discord.Member, reason: Option(str, "Grund")):
		if not await self.is_mod_or_admin(ctx.author):
			await ctx.respond("Keine Berechtigung!", ephemeral=True)
			return

		if member.id == ctx.author.id:
			await ctx.respond("Du kannst dich selbst nicht verwarnen!", ephemeral=True)
			return

		async with aiosqlite.connect(self.db_path) as db:
			await db.execute("""
				INSERT INTO warns (user_id, guild_id, mod_id, reason, timestamp)
				VALUES (?, ?, ?, ?, ?)
			""", (member.id, ctx.guild_id, ctx.author.id, reason, datetime.datetime.utcnow().isoformat()))

			async with db.execute("SELECT COUNT(*) FROM warns WHERE user_id = ? AND guild_id = ?", (member.id, ctx.guild_id)) as cursor:
				warn_count = (await cursor.fetchone())[0]

			await db.commit()

		embed = discord.Embed(
			title="Verwarnung",
			description=f"Benutzer: {member.mention}\nGrund: {reason}\nVerwarnungen: {warn_count}",
			color=discord.Color.yellow()
		)
		embed.set_footer(text=f"Moderator: {ctx.author.name}")
		await ctx.respond(embed=embed)

		# Auto-Kick bei 3 Verwarnungen
		if warn_count >= 3:
			try:
				await member.kick(reason=f"Automatischer Kick nach {warn_count} Verwarnungen")
				notify_embed = discord.Embed(
					title="Automatischer Kick",
					description=f"Benutzer {member.mention} wurde nach {warn_count} Verwarnungen gekickt.",
					color=discord.Color.red()
				)
				await ctx.send(embed=notify_embed)
			except:
				pass

	# UNWARN COMMAND
	@slash_command(name="unwarn", description="Entferne eine Verwarnung", default_member_permissions=discord.Permissions(moderate_members=True))
	async def unwarn(self, ctx, member: discord.Member):
		if not await self.is_mod_or_admin(ctx.author):
			await ctx.respond("Keine Berechtigung!", ephemeral=True)
			return

		async with aiosqlite.connect(self.db_path) as db:
			async with db.execute("SELECT COUNT(*) FROM warns WHERE user_id = ? AND guild_id = ?", (member.id, ctx.guild_id)) as cursor:
				warn_count = (await cursor.fetchone())[0]

			if warn_count == 0:
				await ctx.respond("Benutzer hat keine Verwarnungen!", ephemeral=True)
				return

			await db.execute("""
				DELETE FROM warns
				WHERE user_id = ? AND guild_id = ?
				LIMIT 1
			""", (member.id, ctx.guild_id))
			await db.commit()

		embed = discord.Embed(
			title="Verwarnung entfernt",
			description=f"Benutzer: {member.mention}\nVerbliebene Verwarnungen: {warn_count - 1}",
			color=discord.Color.green()
		)
		await ctx.respond(embed=embed)

	# WARNS COMMAND
	@slash_command(name="warns", description="Zeige Verwarnungen eines Benutzers", default_member_permissions=discord.Permissions(moderate_members=True))
	async def warns(self, ctx, member: discord.Member):
		async with aiosqlite.connect(self.db_path) as db:
			async with db.execute("""
				SELECT count(*) FROM warns WHERE user_id = ? AND guild_id = ?
			""", (member.id, ctx.guild_id)) as cursor:
				warn_count = (await cursor.fetchone())[0]

			async with db.execute("""
				SELECT reason, timestamp FROM warns WHERE user_id = ? AND guild_id = ?
				ORDER BY timestamp DESC
			""", (member.id, ctx.guild_id)) as cursor:
				warns_list = await cursor.fetchall()

		if warn_count == 0:
			await ctx.respond(f"{member.mention} hat keine Verwarnungen.", ephemeral=True)
			return

		embed = discord.Embed(
			title=f"Verwarnungen - {member.name}",
			description=f"Insgesamt: {warn_count}",
			color=discord.Color.yellow()
		)

		for i, (reason, timestamp) in enumerate(warns_list, 1):
			embed.add_field(
				name=f"Verwarnung {i}",
				value=f"Grund: {reason}\nZeitpunkt: {timestamp}",
				inline=False
			)

		await ctx.respond(embed=embed)

	# PUNISHMENTS COMMAND
	@slash_command(name="punishments", description="Zeige Strafen eines Benutzers", default_member_permissions=discord.Permissions(moderate_members=True))
	async def punishments(self, ctx, member: discord.User):
		async with aiosqlite.connect(self.db_path) as db:
			async with db.execute("""
				SELECT action, reason, timestamp, duration FROM punishments
				WHERE user_id = ? AND guild_id = ?
				ORDER BY timestamp DESC
			""", (member.id, ctx.guild_id)) as cursor:
				punishments_list = await cursor.fetchall()

		if not punishments_list:
			await ctx.respond(f"{member.mention} hat keine Strafen.", ephemeral=True)
			return

		embed = discord.Embed(
			title=f"Strafen - {member.name}",
			color=discord.Color.red()
		)

		for i, (action, reason, timestamp, duration) in enumerate(punishments_list, 1):
			duration_str = f"{duration} Minuten" if duration else "Permanent"
			embed.add_field(
				name=f"{action.upper()} #{i}",
				value=f"Grund: {reason}\nDauer: {duration_str}\nZeitpunkt: {timestamp}",
				inline=False
			)

		await ctx.respond(embed=embed)


def setup(bot):
	cog = punishment(bot)
	bot.add_cog(cog)
	asyncio.get_event_loop().create_task(cog.setup_database())
