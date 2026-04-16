from datetime import datetime
from io import BytesIO

import discord
from discord.ext import commands, tasks
from discord.commands import slash_command
from discord.ui import View, Button
from discord import Interaction
import aiosqlite
import os
import random
import time
from typing import Optional
from PIL import Image, ImageDraw, ImageFont, ImageOps




class LevelSystem(commands.Cog):
    # Balanced leveling: moderate start, steady linear growth per level
    # XP needed to go from level `L` to `L+1` = XP_BASE + XP_SCALE * L
    XP_BASE = 55
    XP_SCALE = 10

    async def recalculate_all_levels(self, channel=None):
        """
        Recalculate all user levels in the DB based on their XP and update the DB.
        Optionally send progress to a Discord channel.
        """
        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_progress_columns(db)
            async with db.execute("SELECT user_id, xp FROM users") as cursor:
                users = await cursor.fetchall()
            updated = 0
            for user_id, xp in users:
                lvl, xp_current, xp_needed = self.get_level_data(float(xp or 0))
                remaining_xp = xp_needed - xp_current
                await db.execute(
                    "UPDATE users SET level = ?, remaining_xp = ? WHERE user_id = ?",
                    (lvl, remaining_xp, user_id)
                )
                if self.has_legacy_remain_xp:
                    await db.execute(
                        "UPDATE users SET remain_xp = ? WHERE user_id = ?",
                        (remaining_xp, user_id)
                    )
                updated += 1
                if channel and updated % 25 == 0:
                    await channel.send(f"{updated} Nutzer recalculated...")
            await db.commit()
            if channel:
                await channel.send(f"Recalculation abgeschlossen für {updated} Nutzer. (Level und remaining_xp wurden aktualisiert)")
        return updated

    # XP Boost roles (placeholder IDs)
    XP_BOOST_ROLES = {
        1365793303429382154: 1.20,  # Server Booster
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
        old_level, new_level = await self.add_xp(user.id, xp)
        if member:
            await self.check_level_up(member, old_level, new_level)

    def __init__(self, bot):
        self.bot = bot
        self.DB = "level.db"
        self.has_legacy_remain_xp = False
        self.progress_initialized = False

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

        self._font_candidates = {
            "regular": [
                "/System/Library/Fonts/Supplemental/Arial.ttf",
                "/System/Library/Fonts/Supplemental/Helvetica.ttc",
            ],
            "bold": [
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "/System/Library/Fonts/Supplemental/Helvetica Bold.ttf",
            ],
        }

    # Level Calculation using linear per-level increment for smooth progression.
    @classmethod
    def get_level_data(cls, xp):
        # Closed-form solution for arithmetic progression: XP needed for level L is XP_BASE + XP_SCALE * L
        # Total XP to reach level L: S = XP_BASE * L + XP_SCALE * (L * (L-1)) / 2
        # Solve quadratic: S <= xp < S_next
        xp = float(xp or 0)
        a = cls.XP_SCALE / 2
        b = cls.XP_BASE - (cls.XP_SCALE / 2)
        c = -xp
        # Quadratic formula: L = (-b + sqrt(b^2 - 4ac)) / (2a)
        import math
        if a == 0:
            lvl = int(xp // cls.XP_BASE)
        else:
            disc = b * b - 4 * a * c
            lvl = int(max(0, (-b + math.sqrt(disc)) / (2 * a))) if disc >= 0 else 0
        # Compute XP used for full levels
        xp_used = cls.XP_BASE * lvl + cls.XP_SCALE * (lvl * (lvl - 1)) / 2
        xp_current = xp - xp_used
        xp_for_next = float(cls.XP_BASE + cls.XP_SCALE * lvl)
        return lvl, xp_current, xp_for_next

    def get_xp_needed_for_level(self, lvl):
        # XP needed to progress from `lvl` to `lvl+1`.
        return float(self.XP_BASE + self.XP_SCALE * int(lvl))

    @classmethod
    def get_total_xp_for_level(cls, lvl: int) -> float:
        lvl = max(0, int(lvl))
        return float(cls.XP_BASE * lvl + cls.XP_SCALE * (lvl * (lvl - 1)) / 2)

    def _set_formula_runtime(self, xp_base: float, xp_scale: float):
        type(self).XP_BASE = float(xp_base)
        type(self).XP_SCALE = float(xp_scale)

    async def ensure_settings_table(self, db):
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS level_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                xp_base REAL NOT NULL,
                xp_scale REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            INSERT OR IGNORE INTO level_settings (id, xp_base, xp_scale, updated_at)
            VALUES (1, ?, ?, ?)
            """,
            (float(self.XP_BASE), float(self.XP_SCALE), datetime.utcnow().isoformat())
        )

    async def load_formula_settings(self, db):
        async with db.execute(
            "SELECT xp_base, xp_scale FROM level_settings WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()

        if row:
            self._set_formula_runtime(row[0], row[1])

    async def get_level_formula(self):
        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_settings_table(db)
            await self.load_formula_settings(db)
            await db.commit()
        return {"xp_base": float(self.XP_BASE), "xp_scale": float(self.XP_SCALE)}

    async def update_level_formula(self, xp_base: float, xp_scale: float, recalculate: bool = False):
        xp_base = float(xp_base)
        xp_scale = float(xp_scale)

        if xp_base <= 0:
            raise ValueError("xp_base must be greater than 0")
        if xp_scale < 0:
            raise ValueError("xp_scale must be at least 0")

        self._set_formula_runtime(xp_base, xp_scale)

        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_settings_table(db)
            await db.execute(
                """
                UPDATE level_settings
                SET xp_base = ?, xp_scale = ?, updated_at = ?
                WHERE id = 1
                """,
                (xp_base, xp_scale, datetime.utcnow().isoformat())
            )
            await db.commit()

        if recalculate:
            await self.recalculate_all_levels()

    def _load_font(self, size: int, bold: bool = False):
        key = "bold" if bold else "regular"
        for path in self._font_candidates[key]:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        return ImageFont.load_default()

    def _format_xp(self, value):
        value = float(value or 0)
        if value.is_integer():
            return str(int(value))
        return f"{value:.1f}"

    async def render_level_card(self, member, lvl, xp_total, xp_current, xp_needed):
        width, height = 1000, 320
        background_path = os.getenv("LEVEL_CARD_BACKGROUND", "assets/level_card_bg.png")
        image = Image.new("RGBA", (width, height), (12, 18, 34, 255))

        if os.path.exists(background_path):
            try:
                bg = Image.open(background_path).convert("RGBA")
                image = ImageOps.fit(bg, (width, height), method=getattr(getattr(Image, "Resampling", Image), "LANCZOS"))
            except Exception:
                pass

        draw = ImageDraw.Draw(image)

        # Soft horizontal gradient overlay for readability regardless of background image.
        for x in range(width):
            ratio = x / (width - 1)
            alpha = int(125 + 70 * ratio)
            draw.line([(x, 0), (x, height)], fill=(8, 14, 28, alpha))

        draw.rounded_rectangle(
            (18, 18, width - 18, height - 18),
            radius=32,
            fill=(9, 14, 26, 220),
            outline=(111, 152, 235, 180),
            width=2,
        )

        avatar_size = 180
        avatar_pos = (48, 70)
        avatar_image = Image.new("RGBA", (avatar_size, avatar_size), (90, 98, 120, 255))

        try:
            avatar_asset = member.display_avatar.with_size(256)
            avatar_bytes = await avatar_asset.read()
            raw_avatar = Image.open(BytesIO(avatar_bytes)).convert("RGBA")
            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            avatar_image = ImageOps.fit(raw_avatar, (avatar_size, avatar_size), method=resampling)
        except Exception:
            pass

        mask = Image.new("L", (avatar_size, avatar_size), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.ellipse((0, 0, avatar_size, avatar_size), fill=255)
        image.paste(avatar_image, avatar_pos, mask)

        arc_box = (
            avatar_pos[0] - 6,
            avatar_pos[1] - 6,
            avatar_pos[0] + avatar_size + 6,
            avatar_pos[1] + avatar_size + 6,
        )
        draw.arc(arc_box, start=0, end=360, fill=(129, 178, 255, 255), width=4)

        title_font = self._load_font(30, bold=True)
        name_font = self._load_font(42, bold=True)
        stats_font = self._load_font(26, bold=False)
        progress_font = self._load_font(22, bold=True)

        text_x = 270
        draw.text((text_x, 50), "LEVEL CARD", font=title_font, fill=(152, 184, 255, 255))
        draw.text((text_x, 90), member.display_name[:24], font=name_font, fill=(236, 243, 255, 255))

        stats_line = (
            f"Level {int(lvl)}   |   Total XP {self._format_xp(xp_total)}   |   "
            f"Next Level {self._format_xp(xp_needed)} XP"
        )
        draw.text((text_x, 150), stats_line, font=stats_font, fill=(199, 214, 248, 255))

        progress_ratio = 0 if xp_needed <= 0 else max(0.0, min(1.0, float(xp_current) / float(xp_needed)))
        progress_percent = int(progress_ratio * 100)

        bar_x1, bar_y1 = text_x, 210
        bar_x2, bar_y2 = width - 60, 250
        draw.rounded_rectangle((bar_x1, bar_y1, bar_x2, bar_y2), radius=16, fill=(32, 44, 69, 255))

        fill_width = int((bar_x2 - bar_x1) * progress_ratio)
        if fill_width > 0:
            draw.rounded_rectangle(
                (bar_x1, bar_y1, bar_x1 + fill_width, bar_y2),
                radius=16,
                fill=(72, 159, 255, 255),
            )

        progress_text = f"{self._format_xp(xp_current)} / {self._format_xp(xp_needed)} XP ({progress_percent}%)"
        draw.text((text_x, 262), progress_text, font=progress_font, fill=(224, 236, 255, 255))

        output = BytesIO()
        image.save(output, format="PNG")
        output.seek(0)
        return output

    async def ensure_progress_columns(self, db):
        async with db.execute("PRAGMA table_info(users)") as cursor:
            table_info = await cursor.fetchall()

        columns = {row[1] for row in table_info}

        default_remaining_xp = self.get_xp_needed_for_level(0)

        if "level" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN level INTEGER DEFAULT 0")
            columns.add("level")

        if "remaining_xp" not in columns:
            await db.execute(
                f"ALTER TABLE users ADD COLUMN remaining_xp REAL DEFAULT {default_remaining_xp}"
            )
            columns.add("remaining_xp")

        self.has_legacy_remain_xp = "remain_xp" in columns
        if self.has_legacy_remain_xp:
            # Migrate legacy remain_xp to remaining_xp using the correct formula
            await db.execute(
                """
                UPDATE users
                SET remaining_xp = CASE
                    WHEN remaining_xp IS NULL OR remaining_xp <= 0 THEN MAX(0, (? + (? * COALESCE(level, 0))) - COALESCE(remain_xp, 0))
                    ELSE remaining_xp
                END
                """,
                (self.XP_BASE, self.XP_SCALE)
            )

    def normalize_progress(self, xp_total, lvl, remaining_xp):
        xp_total = float(xp_total or 0)
        if lvl is None or remaining_xp is None or float(remaining_xp) <= 0:
            calc_lvl, xp_current, xp_needed = self.get_level_data(xp_total)
            return xp_total, int(calc_lvl), float(xp_needed - xp_current)
        return xp_total, int(lvl), float(remaining_xp)

    # DATABASE
    @commands.Cog.listener()
    async def on_ready(self):
        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_settings_table(db)
            await self.load_formula_settings(db)

            default_remaining_xp = self.get_xp_needed_for_level(0)
            await db.execute(f"""
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                msg_count INTEGER DEFAULT 0,
                voice_time INTEGER DEFAULT 0,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 0,
                remaining_xp REAL DEFAULT {default_remaining_xp}
            )
            """)
            await self.ensure_progress_columns(db)
            await db.commit()

        # Only recalculate if a migration was detected (not every startup)
        # Example: if hasattr(self, 'migration_needed') and self.migration_needed:
        #     await self.recalculate_all_levels()

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
        gained_xp = float(xp)

        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_progress_columns(db)
            async with db.execute(
                "SELECT xp, level, remaining_xp FROM users WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                result = await cursor.fetchone()

            if result:
                xp_total, lvl, remaining_xp = self.normalize_progress(result[0], result[1], result[2])
            else:
                xp_total, lvl, remaining_xp = 0.0, 0, float(self.get_xp_needed_for_level(0))

            old_level = lvl
            xp_total += gained_xp
            remaining_xp -= gained_xp

            while remaining_xp <= 0:
                lvl += 1
                remaining_xp += self.get_xp_needed_for_level(lvl)

            await db.execute(
                "UPDATE users SET xp = ?, level = ?, remaining_xp = ? WHERE user_id = ?",
                (xp_total, lvl, remaining_xp, user_id)
            )
            if self.has_legacy_remain_xp:
                await db.execute(
                    "UPDATE users SET remain_xp = ? WHERE user_id = ?",
                    (remaining_xp, user_id)
                )
            await db.commit()

        return old_level, lvl

    # LEVEL UP CHECK
    async def check_level_up(self, member, old_level, new_level):
        if new_level <= old_level:
            return

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

        old_level, new_level = await self.add_xp(message.author.id, xp)

        async with aiosqlite.connect(self.DB) as db:
            await db.execute(
                "UPDATE users SET msg_count = msg_count + 1 WHERE user_id = ?",
                (message.author.id,)
            )
            await db.commit()

        await self.check_level_up(message.author, old_level, new_level)

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
                    old_level, new_level = await self.add_xp(member.id, xp)

                    async with aiosqlite.connect(self.DB) as db:
                        await db.execute(
                            "UPDATE users SET voice_time = voice_time + 1 WHERE user_id = ?",
                            (member.id,)
                        )
                        await db.commit()

                    await self.check_level_up(member, old_level, new_level)

    # To toggle stacking, set booster_stack_enabled above to True or False in code.

   # /LEVEL

    @slash_command(name="level", description="Zeigt dein Level und Fortschritt")
    async def level(self, ctx, user: Optional[discord.Member]= None):
        member = user or ctx.author
        await self.check_user(member.id)
        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_progress_columns(db)
            async with db.execute(
                "SELECT xp, level, remaining_xp FROM users WHERE user_id = ?",
                (member.id,)
            ) as cursor:
                result = await cursor.fetchone()

            if result:
                xp_total, lvl, remaining_xp = self.normalize_progress(result[0], result[1], result[2])
            else:
                xp_total, lvl, remaining_xp = 0.0, 0, float(self.get_xp_needed_for_level(0))

            xp_needed = self.get_xp_needed_for_level(lvl)
            xp_current = max(0.0, xp_needed - remaining_xp)

            await db.execute(
                "UPDATE users SET level = ?, remaining_xp = ? WHERE user_id = ?",
                (lvl, remaining_xp, member.id)
            )
            if self.has_legacy_remain_xp:
                await db.execute(
                    "UPDATE users SET remain_xp = ? WHERE user_id = ?",
                    (remaining_xp, member.id)
                )
            await db.commit()

        try:
            image_buffer = await self.render_level_card(member, lvl, xp_total, xp_current, xp_needed)
            file = discord.File(fp=image_buffer, filename=f"level_{member.id}.png")
            embed = discord.Embed(
                title=f"Level Status fuer {member.display_name}",
                color=discord.Color.purple()
            )
            embed.set_image(url=f"attachment://{file.filename}")
            await ctx.respond(embed=embed, file=file)
        except Exception:
            percentage = (xp_current / xp_needed) * 100 if xp_needed else 0
            progress_bar_length = 10
            filled_slots = int(xp_current / xp_needed * progress_bar_length) if xp_needed else 0
            filled_slots = max(0, min(progress_bar_length, filled_slots))
            bar = "🟦" * filled_slots + "⬜" * (progress_bar_length - filled_slots)

            embed = discord.Embed(
                title=f"Level Status fuer {member.display_name}",
                color=discord.Color.purple()
            )
            embed.set_thumbnail(url=member.display_avatar.url)

            embed.add_field(name="Level", value=f"✨ **{lvl}**", inline=True)
            embed.add_field(name="Gesamt XP", value=f"📈 **{self._format_xp(xp_total)}**", inline=True)
            embed.add_field(
                name=f"Fortschritt bis Level {lvl + 1}",
                value=f"{bar} ({int(percentage)}%)\n`{self._format_xp(xp_current)} / {self._format_xp(xp_needed)} XP`",
                inline=False
            )

            await ctx.respond(embed=embed)




    @slash_command(name="leaderboard", description="Show the leaderboard")
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

            self.messages_btn = Button(label="Messages", style=discord.ButtonStyle.secondary)
            self.messages_btn.callback = self.messages_button

            self.voice_btn = Button(label="Voice", style=discord.ButtonStyle.secondary)
            self.voice_btn.callback = self.voice_button

            self.toggle_btn = Button(label="Toggle Level/XP", style=discord.ButtonStyle.success)
            self.toggle_btn.callback = self.toggle_level_xp
            self.update_buttons()

        def update_buttons(self):
            self.level_btn.style = discord.ButtonStyle.primary if self.mode == "level" else discord.ButtonStyle.secondary
            self.messages_btn.style = discord.ButtonStyle.primary if self.mode == "messages" else discord.ButtonStyle.secondary
            self.voice_btn.style = discord.ButtonStyle.primary if self.mode == "voice" else discord.ButtonStyle.secondary
            self.toggle_btn.label = "Show Total XP" if self.level_display == "level" else "Show Level"

            self.clear_items()
            self.add_item(self.level_btn)
            self.add_item(self.messages_btn)
            self.add_item(self.voice_btn)
            if self.mode == "level":
                self.add_item(self.toggle_btn)

        async def send_initial(self, ctx):
            embed = await self.get_embed()
            self.update_buttons()
            await ctx.respond(embed=embed, view=self)

        async def interaction_check(self, interaction: Interaction) -> bool:
            return True

        async def level_button(self, interaction: Interaction):
            self.mode = "level"
            embed = await self.get_embed()
            self.update_buttons()
            await interaction.response.edit_message(embed=embed, view=self)

        async def messages_button(self, interaction: Interaction):
            self.mode = "messages"
            embed = await self.get_embed()
            self.update_buttons()
            await interaction.response.edit_message(embed=embed, view=self)

        async def voice_button(self, interaction: Interaction):
            self.mode = "voice"
            embed = await self.get_embed()
            self.update_buttons()
            await interaction.response.edit_message(embed=embed, view=self)

        async def toggle_level_xp(self, interaction: Interaction):
            if self.level_display == "level":
                self.level_display = "total_xp"
            else:
                self.level_display = "level"
            embed = await self.get_embed()
            self.update_buttons()
            await interaction.response.edit_message(embed=embed, view=self)

        async def get_embed(self):
            if self.mode == "level":
                return await self.get_level_embed()
            elif self.mode == "messages":
                return await self.get_messages_embed()
            elif self.mode == "voice":
                return await self.get_voice_embed()

        async def get_level_embed(self):
            def ordinal(n):
                return "%d%s" % (n, "tsnrhtdd"[(n//10%10!=1)*(n%10<4)*n%10::4])

            desc = ""
            async with aiosqlite.connect(self.cog.DB) as db:
                if self.level_display == "level":
                    async with db.execute("SELECT user_id, level, xp, remaining_xp FROM users ORDER BY level DESC, xp DESC LIMIT 10") as cursor:
                        i = 1
                        async for user_id, lvl, xp, remaining_xp in cursor:
                            xp_needed = self.cog.get_xp_needed_for_level(lvl)
                            xp_current = max(0.0, xp_needed - float(remaining_xp or 0))
                            percent = int((xp_current / xp_needed) * 100) if xp_needed else 0
                            desc += f"{ordinal(i)}: <@{user_id}> | Level {int(lvl)} | {int(xp_current)}/{int(xp_needed)} XP ({percent}%)\n"
                            i += 1
                else:
                    async with db.execute("SELECT user_id, xp, level FROM users ORDER BY xp DESC LIMIT 10") as cursor:
                        i = 1
                        async for user_id, xp, lvl in cursor:
                            desc += f"{ordinal(i)}: <@{user_id}> | XP: {int(float(xp))} | Level {int(lvl)}\n"
                            i += 1
            embed = discord.Embed(
                title="🏆 Level Leaderboard",
                description=desc or "Noch keine Daten verfügbar.",
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
