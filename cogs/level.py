from asyncio import wait

import discord
from discord.ext import commands, tasks
from discord.commands import slash_command
from discord.ui import View, Button
from discord import Interaction
import aiosqlite
import random
import time
from typing import Optional

@slash_command(name="level", description="Zeigt dein Level und Fortschritt")
async def level(self, ctx, user: Optional[discord.Member] = None):
    member = user or ctx.author
    # Fetch both XP and level from the database
    async with aiosqlite.connect(self.DB) as db:
        async with db.execute("SELECT xp, level FROM users WHERE user_id = ?", (member.id,)) as cursor:
            result = await cursor.fetchone()
            if result:
                xp_total, lvl = result
            else:
                xp_total, lvl = 0, 0

    # Calculate progress bar based on XP and current level's XP requirement
    _, xp_current, xp_needed = self.get_level_data(xp_total)
    progress_bar_length = 10
    filled_slots = int(xp_current / xp_needed * progress_bar_length) if xp_needed else 0
    bar = "🟦" * filled_slots + "⬜" * (progress_bar_length - filled_slots)

    def fmt(x):
        return f"{x:.1f}" if isinstance(x, float) else str(x)

    embed = discord.Embed(
        title=f"Level Status für {member.display_name}",
        color=discord.Color.purple()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Level", value=f"✨ **{lvl}**", inline=True)
    embed.add_field(name="Gesamt XP", value=f"📈 **{fmt(xp_total)}**", inline=True)
    embed.add_field(name="Fortschritt", value=f"{bar} ({fmt(xp_current)}/{fmt(xp_needed)})", inline=False)
    await ctx.respond(embed=embed)


class LevelSystem(commands.Cog):
    XP_GROWTH = 1.175  # 17.5% growth per level

    async def recalculate_all_levels(self, channel=None):
        """
        Recalculate all user levels in the DB based on their XP and update the DB.
        Optionally send progress to a Discord channel.
        """
        async with aiosqlite.connect(self.DB) as db:
            # Add remain_xp column if not present
            try:
                await db.execute("ALTER TABLE users ADD COLUMN remain_xp INTEGER DEFAULT 0")
                await db.commit()
            except Exception:
                pass
            async with db.execute("SELECT user_id, xp, level, remain_xp FROM users") as cursor:
                users = await cursor.fetchall()
            updated = 0
            for user_id, xp, old_level, old_remain in users:
                lvl, remain_xp, xp_needed = self.get_level_data(xp, self.XP_GROWTH)
                await db.execute("UPDATE users SET level = ?, remain_xp = ? WHERE user_id = ?", (lvl, remain_xp, user_id))
                await db.commit()
                updated += 1
                if channel and updated % 25 == 0:
                    await channel.send(f"{updated} Nutzer recalculated...")
            if channel:
                await channel.send(f"Recalculation abgeschlossen für {updated} Nutzer. (Level werden dynamisch aus XP berechnet)")
        return updated

    # XP Boost roles (placeholder IDs)
    XP_BOOST_ROLES = {
        111111111111111111: 1.50,  # 1.50x boost
        222222222222222222: 1.75,  # 1.75x boost
        333333333333333333: 2.00,  # 2.00x boost
        444444444444444444: 1.25,  # 1.25x boost
    }

    # Booster stacking toggle (set True to enable stacking, False for only highest boost)
    booster_stack_enabled = False  # Set to True for stacking, False for only highest

    # Cooldown for reaction XP (user_id: last_timestamp)
    reaction_xp_cooldowns = {}

    def get_level(self, xp):
        lvl, _, _ = self.get_level_data(xp)
        return lvl

    # REACTION XP
    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        if user.bot or not reaction.message.guild:
            return
        now = time.time()
        last = self.reaction_xp_cooldowns.get(user.id, 0)
        if now - last < 3600:  # 1 hour cooldown
            return
        self.reaction_xp_cooldowns[user.id] = now
        xp = 0.1
        # XP Boost for roles (skip excluded user)
        member = reaction.message.guild.get_member(user.id)
        if member and hasattr(member, 'roles') and member.id != 1340370441390522398:
            boost = 1.0
            if self.booster_stack_enabled:
                for role in member.roles:
                    if role.id in self.XP_BOOST_ROLES:
                        boost *= self.XP_BOOST_ROLES[role.id]
            else:
                for role in member.roles:
                    if role.id in self.XP_BOOST_ROLES:
                        boost = max(boost, self.XP_BOOST_ROLES[role.id])
            xp = xp * boost
        await self.add_xp(user.id, xp)

    def __init__(self, bot):
        self.bot = bot
        self.DB = "level.db"

        # COOLDOWN
        self.cooldowns = {}
        self.cooldown_time = 20

        # Which role should be given to the user when they reach a specific level
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
        # Here: will get posted in #level-up with the id 1482463203966455818 !The id is random for every channel regards of their name!
        self.level_channel = 1482463203966455818

    # Level Calculation, start at level 0, need 10 XP for level 1, then every level needs 17.5% more XP than the previous.
    @staticmethod
    def get_level_data(xp, growth=1.175):
        import math
        lvl = 0
        current_lvl_xp = 10
        while xp >= current_lvl_xp:
            xp -= current_lvl_xp
            lvl += 1
            current_lvl_xp = math.ceil(current_lvl_xp * growth)
        return lvl, xp, current_lvl_xp
    @slash_command(name="level", description="Zeigt dein Level und Fortschritt")
    async def level(self, ctx, user: Optional[discord.Member] = None):
        member = user or ctx.author
        # Fetch XP, level, and remain_xp from the database
        async with aiosqlite.connect(self.DB) as db:
            async with db.execute("SELECT xp, level, remain_xp FROM users WHERE user_id = ?", (member.id,)) as cursor:
                result = await cursor.fetchone()
                if result:
                    xp_total, lvl, xp_current = result
                else:
                    xp_total, lvl, xp_current = 0, 0, 0

        # Calculate the XP needed for the user's current level
        import math
        base_xp = 10
        xp_needed = base_xp
        for i in range(lvl):
            xp_needed = math.ceil(xp_needed * self.XP_GROWTH)
        progress_bar_length = 10
        filled_slots = int(xp_current / xp_needed * progress_bar_length) if xp_needed else 0
        bar = "🟦" * filled_slots + "⬜" * (progress_bar_length - filled_slots)

        def fmt(x):
            return f"{x:.1f}" if isinstance(x, float) else str(x)

        embed = discord.Embed(
            title=f"Level Status für {member.display_name}",
            color=discord.Color.purple()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Level", value=f"✨ **{lvl}**", inline=True)
        embed.add_field(name="Gesamt XP", value=f"📈 **{fmt(xp_total)}**", inline=True)
        embed.add_field(name="Fortschritt", value=f"{bar} ({fmt(xp_current)}/{fmt(xp_needed)})", inline=False)
        await ctx.respond(embed=embed)

    # DATABASE
    @commands.Cog.listener()
    async def on_ready(self):
        async with aiosqlite.connect(self.DB) as db:
            await db.execute("""
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                msg_count INTEGER DEFAULT 0,
                voice_time INTEGER DEFAULT 0,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 0
            )
            """)
            # Try to add the level column if it doesn't exist (safe migration)
            try:
                await db.execute("ALTER TABLE users ADD COLUMN level INTEGER DEFAULT 0")
                await db.commit()
            except Exception:
                pass  # Ignore if already exists
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

        # XP Boost for roles (skip excluded user)
        if hasattr(message.author, 'roles') and message.author.id != 1340370441390522398:
            boost = 1.0
            if self.booster_stack_enabled:
                for role in message.author.roles:
                    if role.id in self.XP_BOOST_ROLES:
                        boost *= self.XP_BOOST_ROLES[role.id]
            else:
                for role in message.author.roles:
                    if role.id in self.XP_BOOST_ROLES:
                        boost = max(boost, self.XP_BOOST_ROLES[role.id])
            xp = int(xp * boost)

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

                    # XP Boost for roles (skip excluded user)
                    boost = 1.0
                    if member.id != 1340370441390522398:
                        if self.booster_stack_enabled:
                            for role in getattr(member, 'roles', []):
                                if role.id in self.XP_BOOST_ROLES:
                                    boost *= self.XP_BOOST_ROLES[role.id]
                        else:
                            for role in getattr(member, 'roles', []):
                                if role.id in self.XP_BOOST_ROLES:
                                    boost = max(boost, self.XP_BOOST_ROLES[role.id])
                    xp = int(xp * boost)
                    await self.add_xp(member.id, xp)

                    async with aiosqlite.connect(self.DB) as db:
                        await db.execute(
                            "UPDATE users SET voice_time = voice_time + 1 WHERE user_id = ?",
                            (member.id,)
                        )
                        await db.commit()

                    await self.check_level_up(member, xp)

    # To toggle stacking, set booster_stack_enabled above to True or False in code.

   # /LEVEL

    @slash_command(name="level", description="Zeigt dein Level und Fortschritt")
    async def level(self, ctx, user: Optional[discord.Member]= None):
        member = user or ctx.author
        xp_total = await self.get_xp(member.id)
        lvl, xp_current, xp_needed = self.get_level_data(xp_total)
        percentage = (xp_current / xp_needed) * 100
        progress_bar_length = 10
        filled_slots = int(xp_current / xp_needed * progress_bar_length)
        bar = "🟦" * filled_slots + "⬜" * (progress_bar_length - filled_slots)

        # Format XP values to one decimal place if needed
        def fmt(x):
            return f"{x:.1f}" if isinstance(x, float) and not x.is_integer() else str(int(x))

        embed = discord.Embed(
            title=f"Level Status für {member.display_name}",
            color=discord.Color.purple()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        
        embed.add_field(name="Level", value=f"✨ **{lvl}**", inline=True)
        embed.add_field(name="Gesamt XP", value=f"📈 **{fmt(xp_total)}**", inline=True)
        embed.add_field(
            name=f"Fortschritt bis Level {lvl + 1}", 
            value=f"{bar} ({int(percentage)}%)\n`{fmt(xp_current)} / {fmt(xp_needed)} XP`", 
            inline=False
        )
        
        await ctx.respond(embed=embed)




    @slash_command(name="leaderboard", description="Show interactive leaderboard")
    async def leaderboard(self, ctx):
        view = self.LeaderboardView(self)
        await view.send_initial(ctx)

    class LeaderboardView(View):
        def __init__(self, cog):
            super().__init__(timeout=120)
            self.cog = cog
            self.mode = "level"  # level, messages, voice
            self.level_display = "level"  # level, total_xp

            self.level_btn = Button(label="Level", style=discord.ButtonStyle.primary)
            self.level_btn.callback = self.level_button
            self.add_item(self.level_btn)

            self.messages_btn = Button(label="Messages", style=discord.ButtonStyle.secondary)
            self.messages_btn.callback = self.messages_button
            self.add_item(self.messages_btn)

            self.voice_btn = Button(label="Voice", style=discord.ButtonStyle.secondary)
            self.voice_btn.callback = self.voice_button
            self.add_item(self.voice_btn)

            self.toggle_btn = Button(label="Toggle Level/XP", style=discord.ButtonStyle.success)
            self.toggle_btn.callback = self.toggle_level_xp
            self.add_item(self.toggle_btn)

        async def send_initial(self, ctx):
            embed = await self.get_embed()
            await ctx.respond(embed=embed, view=self)

        async def interaction_check(self, interaction: Interaction) -> bool:
            return True

        async def level_button(self, interaction):
            self.mode = "level"
            embed = await self.get_embed()
            await interaction.response.edit_message(embed=embed, view=self)

        async def messages_button(self, interaction):
            self.mode = "messages"
            embed = await self.get_embed()
            await interaction.response.edit_message(embed=embed, view=self)

        async def voice_button(self, interaction):
            self.mode = "voice"
            embed = await self.get_embed()
            await interaction.response.edit_message(embed=embed, view=self)

        async def toggle_level_xp(self, interaction):
            if self.level_display == "level":
                self.level_display = "total_xp"
            else:
                self.level_display = "level"
            embed = await self.get_embed()
            await interaction.response.edit_message(embed=embed, view=self)

        async def get_embed(self):
            if self.mode == "level":
                return await self.get_level_embed()
            elif self.mode == "messages":
                return await self.get_messages_embed()
            elif self.mode == "voice":
                return await self.get_voice_embed()

        async def get_level_embed(self):
            desc = ""
            async with aiosqlite.connect(self.cog.DB) as db:
                async with db.execute("SELECT user_id, xp FROM users ORDER BY xp DESC LIMIT 10") as cursor:
                    i = 1
                    async for user_id, xp in cursor:
                        lvl, _, _ = self.cog.get_level_data(xp)
                        if self.level_display == "level":
                            desc += f"{i}. <@{user_id}> Level {lvl}\n"
                        else:
                            desc += f"{i}. <@{user_id}> Total XP: {xp}\n"
                        i += 1
            embed = discord.Embed(
                title="🏆 Level Leaderboard",
                description=desc,
                color=discord.Color.purple()
            )
            return embed

        async def get_messages_embed(self):
            desc = ""
            async with aiosqlite.connect(self.cog.DB) as db:
                async with db.execute("SELECT user_id, msg_count FROM users ORDER BY msg_count DESC LIMIT 10") as cursor:
                    i = 1
                    async for user_id, msgs in cursor:
                        desc += f"{i}. <@{user_id}> — {msgs} messages\n"
                        i += 1
            embed = discord.Embed(
                title="💬 Message Leaderboard",
                description=desc,
                color=discord.Color.purple()
            )
            return embed

        async def get_voice_embed(self):
            desc = ""
            async with aiosqlite.connect(self.cog.DB) as db:
                async with db.execute("SELECT user_id, voice_time FROM users ORDER BY voice_time DESC LIMIT 10") as cursor:
                    i = 1
                    async for user_id, vt in cursor:
                        desc += f"{i}. <@{user_id}> — {vt} minutes\n"
                        i += 1
            embed = discord.Embed(
                title="🎤 Voice Leaderboard",
                description=desc,
                color=discord.Color.purple()
            )
            return embed


def setup(bot):
    bot.add_cog(LevelSystem(bot))
