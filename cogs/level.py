from asyncio import wait

import discord
from discord.ext import commands, tasks
from discord.commands import slash_command
import aiosqlite
import random
import time


class LevelSystem(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.DB = "level.db"

        # COOLDOWN
        self.cooldowns = {}
        self.cooldown_time = 20

        # Wich role should be giving to the user when they reach a specific level
        # Here: Level 2 reached, will get the role with the id 1467922063195902085
        self.level_roles = {
            2: 1467922063195902085,
            5: 1453439052459151473,
            10: 1453439227877396644,
            15: 1453439277714378935,
            20: 1453439312686350406,
            25: 1453439344227385538,
            30: 1453439375856893994,
        }

        # Where the level up message should get posted. 
        # Here: will get posted in #level-up with the id 1482463203966455818 !The id is random for every channel regards of theier name!
        self.level_channel = 1482463203966455818

    # Level Calculation, every level needs 17.5% more xp than the level before.
    @staticmethod
    def get_level_data(xp):
        lvl = 1
        current_lvl_xp = 100
        while xp >= current_lvl_xp:
            xp -= current_lvl_xp
            lvl += 1
            current_lvl_xp = int(current_lvl_xp * 1.175) 
            
        return lvl, xp, current_lvl_xp

    # DATABASE
    @commands.Cog.listener()
    async def on_ready(self):
        async with aiosqlite.connect(self.DB) as db:
            await db.execute("""
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                msg_count INTEGER DEFAULT 0,
                voice_time INTEGER DEFAULT 0,
                xp INTEGER DEFAULT 0
            )
            """)
            await db.commit()

        
        if not hasattr(self, 'voice_xp_task_running'):
            self.voice_xp_task.start()
            self.voice_xp_task_running = True

        print("LevelSystem geladen")

    # USER CHECK
    async def check_user(self, user_id):
        async with aiosqlite.connect(self.DB) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                (user_id,)
            )
            await db.commit()

    
    async def get_xp(self, user_id):
        await self.check_user(user_id)
        async with aiosqlite.connect(self.DB) as db:
            async with db.execute(
                "SELECT xp FROM users WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                result = await cursor.fetchone()
                return result[0] if result else 0

    # XP ADD
    async def add_xp(self, user_id, xp):
        await self.check_user(user_id)
        async with aiosqlite.connect(self.DB) as db:
            await db.execute(
                "UPDATE users SET xp = xp + ? WHERE user_id = ?",
                (xp, user_id)
            )
            await db.commit()

    # LEVEL UP CHECK
    async def check_level_up(self, member, gained_xp):
        new_xp = await self.get_xp(member.id)
        old_xp = new_xp - gained_xp
        old_level = self.get_level(old_xp)
        new_level = self.get_level(new_xp)

        for level in range(old_level + 1, new_level + 1):
            channel = self.bot.get_channel(self.level_channel)
            if channel:
                embed = discord.Embed(
                    title="🎉 Level Up!",
                    description=f"{member.mention} hat **Level {level}** erreicht!",
                    color=discord.Color.purple()
                )
                embed.set_thumbnail(url=member.display_avatar.url)
                await channel.send(embed=embed)

            
            if level in self.level_roles:
                role = member.guild.get_role(self.level_roles[level])
                if role and role not in member.roles:
                    await member.add_roles(role)
                    if channel:
                        embed = discord.Embed(
                            title="Neue Rolle!",
                            description=f"{member.mention} hat die Rolle {role.mention} erhalten!",
                            color=discord.Color.purple()
                        )
                        await channel.send(embed=embed)

    # MESSAGE XP
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return

        now = time.time()
        if message.author.id in self.cooldowns and now - self.cooldowns[message.author.id] < self.cooldown_time:
            return
        self.cooldowns[message.author.id] = now

        xp = random.randint(1, 3)
        if message.author.premium_since:
            xp = int(xp * 1.1)

        await self.add_xp(message.author.id, xp)

        async with aiosqlite.connect(self.DB) as db:
            await db.execute(
                "UPDATE users SET msg_count = msg_count + 1 WHERE user_id = ?",
                (message.author.id,)
            )
            await db.commit()

        await self.check_level_up(message.author, xp)

    # VOICE XP
    @tasks.loop(minutes=1)
    async def voice_xp_task(self):
        for guild in self.bot.guilds:
            for vc in guild.voice_channels:
                members = vc.members
                if not members:
                    continue
                for member in members:
                    if member.bot or member.voice.self_deaf or member.voice.deaf:
                        continue

                    xp = 1 if len(members) == 1 else 3
                    if member.voice.self_mute or member.voice.mute:
                        xp = int(xp * 0.2)
                    if member.premium_since:
                        xp = int(xp * 1.1)
                    if member.id == 1340370441390522398:
                        xp = int(xp * 0.5)

                    await self.add_xp(member.id, xp)

                    async with aiosqlite.connect(self.DB) as db:
                        await db.execute(
                            "UPDATE users SET voice_time = voice_time + 1 WHERE user_id = ?",
                            (member.id,)
                        )
                        await db.commit()

                    await self.check_level_up(member, xp)

   # /LEVEL
    @slash_command(name="level", description="Zeigt dein Level und Fortschritt")
    async def level(self, ctx):
        xp_total = await self.get_xp(ctx.author.id)
        lvl, xp_current, xp_needed = self.get_level_data(xp_total)
        percentage = (xp_current / xp_needed) * 100
        progress_bar_length = 10
        filled_slots = int(xp_current / xp_needed * progress_bar_length)
        bar = "🟦" * filled_slots + "⬜" * (progress_bar_length - filled_slots)

        embed = discord.Embed(
            title=f"Level Status für {ctx.author.name}",
            color=discord.Color.purple()
        )
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        
        embed.add_field(name="Level", value=f"✨ **{lvl}**", inline=True)
        embed.add_field(name="Gesamt XP", value=f"📈 **{xp_total}**", inline=True)
        embed.add_field(
            name=f"Fortschritt bis Level {lvl + 1}", 
            value=f"{bar} ({int(percentage)}%)\n`{xp_current} / {xp_needed} XP`", 
            inline=False
        )
        
        await ctx.respond(embed=embed)


    # LEADERBOARD OVERALL
    @slash_command(name="leaderboard", description="Top 10 höchste Level")
    async def leaderboard(self, ctx):
        desc = ""
        async with aiosqlite.connect(self.DB) as db:
            async with db.execute(
                "SELECT user_id, xp FROM users ORDER BY xp DESC LIMIT 10"
            ) as cursor:
                i = 1
                async for user_id, xp in cursor:
                    desc += f"**{i}.** <@{user_id}> — `{xp} XP`\n"
                    i += 1
        embed = discord.Embed(
            title="🏆 Gesamt Leaderboard",
            description=desc,
            color=discord.Color.purple()
        )
        await ctx.respond(embed=embed)

    # MESSAGE LEADERBOARD
    @slash_command(name="leaderboard_messages", description="Top 10 meist gesendete Nachrichten")
    async def leaderboard_messages(self, ctx):
        desc = ""
        async with aiosqlite.connect(self.DB) as db:
            async with db.execute(
                "SELECT user_id, msg_count FROM users ORDER BY msg_count DESC LIMIT 10"
            ) as cursor:
                i = 1
                async for user_id, msgs in cursor:
                    desc += f"**{i}.** <@{user_id}> — `{msgs} Nachrichten`\n"
                    i += 1
        embed = discord.Embed(
            title="💬 Nachrichten Leaderboard",
            description=desc,
            color=discord.Color.purple()
        )
        await ctx.respond(embed=embed)
        
    # VOICE LEADERBOARD
    @slash_command(name="leaderboard_voice", description="Top 10 Voice Zeiten")
    async def leaderboard_voice(self, ctx):

        desc = ""

        async with aiosqlite.connect(self.DB) as db:

            async with db.execute(
            "SELECT user_id, voice_time FROM users ORDER BY voice_time DESC LIMIT 10"
            ) as cursor:

                i = 1

                async for user_id, vt in cursor:

                    desc += f"**{i}.** <@{user_id}> — `{vt} Minuten`\n"
                    i += 1

        embed = discord.Embed(
            title="🎤 Voice Leaderboard",
                description=desc,
            color=discord.Color.purple()
    )

        await ctx.respond(embed=embed)


def setup(bot):
    bot.add_cog(LevelSystem(bot))
