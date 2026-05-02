from datetime import datetime, timedelta
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

from utils.settings import load_settings




class LevelSystem(commands.Cog):
    # Balanced leveling: moderate start, steady linear growth per level
    # XP needed to go from `L` to `L+1` = XP_BASE + XP_SCALE * L
    XP_BASE = 55
    XP_SCALE = 10

    LEVEL_CARD_MODE_DEFAULT_ONLY = "default_only"
    LEVEL_CARD_MODE_USER_CHOICE = "user_choice"
    LEVEL_CARD_MODE_AUTO_UNLOCK = "auto_unlock"
    LEVEL_CARD_MODES = {
        LEVEL_CARD_MODE_DEFAULT_ONLY,
        LEVEL_CARD_MODE_USER_CHOICE,
        LEVEL_CARD_MODE_AUTO_UNLOCK,
    }
    DEFAULT_LEVEL_CARD_KEY = "default"
    DEFAULT_LEVEL_CARD_DISPLAY_NAME = "default (legacy)"

    DEFAULT_CARD_LAYOUT = {
        "avatar_x": 48,
        "avatar_y": 70,
        "avatar_size": 180,
        "text_x": 270,
        "name_y": 72,
        "stats_y": 150,
        "bar_y": 210,
        "progress_y": 262,
    }

    async def recalculate_all_levels(self, channel=None):
        """
        Recalculate all user levels in the DB based on their XP and update the DB.
        Optionally send progress to a Discord channel.
        """
        if self._decay_enabled():
            await self.apply_decay_all_users()
        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_progress_columns(db)
            async with db.execute("SELECT user_id, xp FROM users") as cursor:
                users = await cursor.fetchall()
            updates = []
            for index, (user_id, xp) in enumerate(users, start=1):
                lvl, xp_current, xp_needed = self.get_level_data(float(xp or 0))
                remaining_xp = xp_needed - xp_current
                if self.has_legacy_remain_xp:
                    updates.append((lvl, remaining_xp, remaining_xp, user_id))
                else:
                    updates.append((lvl, remaining_xp, user_id))
                if channel and index % 25 == 0:
                    await channel.send(f"{index} Nutzer recalculated...")

            if updates:
                if self.has_legacy_remain_xp:
                    await db.executemany(
                        "UPDATE users SET level = ?, remaining_xp = ?, remain_xp = ? WHERE user_id = ?",
                        updates,
                    )
                else:
                    await db.executemany(
                        "UPDATE users SET level = ?, remaining_xp = ? WHERE user_id = ?",
                        updates,
                    )

            updated = len(updates)
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
        self.project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.DB = os.path.join(self.project_root, "level.db")
        self.has_legacy_remain_xp = False
        self.progress_initialized = False
        self.decay_initialized = False
        self._last_decay_run = 0.0
        self.decay_run_min_interval = 900

        self.reload_level_settings()

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
        self._font_cache = {}

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

    def _sanitize_card_mode(self, mode):
        normalized = (mode or "").strip().lower()
        if normalized not in self.LEVEL_CARD_MODES:
            return self.LEVEL_CARD_MODE_DEFAULT_ONLY
        return normalized

    def _sanitize_card_key(self, card_key):
        value = (card_key or "").strip().lower()
        cleaned = "".join(ch for ch in value if ch.isalnum() or ch in {"_", "-"})
        return cleaned[:48]

    def _sanitize_card_display_name(self, display_name, fallback):
        text = (display_name or "").strip()[:48]
        return text or fallback

    def _sanitize_card_layout(self, raw_layout):
        bounds = {
            "avatar_x": (12, 420),
            "avatar_y": (20, 220),
            "avatar_size": (80, 240),
            "text_x": (180, 880),
            "name_y": (30, 220),
            "stats_y": (70, 260),
            "bar_y": (110, 270),
            "progress_y": (150, 300),
        }

        sanitized = {}
        source = raw_layout if isinstance(raw_layout, dict) else {}
        for key, default in self.DEFAULT_CARD_LAYOUT.items():
            raw_value = source.get(key, default)
            try:
                value = int(float(raw_value))
            except (TypeError, ValueError):
                value = int(default)
            low, high = bounds[key]
            sanitized[key] = max(low, min(high, value))

        min_text_x = sanitized["avatar_x"] + sanitized["avatar_size"] + 22
        sanitized["text_x"] = min(bounds["text_x"][1], max(min_text_x, sanitized["text_x"]))
        sanitized["stats_y"] = min(bounds["stats_y"][1], max(sanitized["name_y"] + 28, sanitized["stats_y"]))
        sanitized["bar_y"] = min(bounds["bar_y"][1], max(sanitized["stats_y"] + 36, sanitized["bar_y"]))
        sanitized["progress_y"] = min(bounds["progress_y"][1], max(sanitized["bar_y"] + 44, sanitized["progress_y"]))
        return sanitized

    def _is_path_within(self, path: str, base_dir: str) -> bool:
        try:
            abs_path = os.path.abspath(path)
            abs_base = os.path.abspath(base_dir)
            return os.path.commonpath([abs_path, abs_base]) == abs_base
        except Exception:
            return False

    def _to_project_path(self, value: Optional[str], fallback: Optional[str] = None) -> str:
        raw = (value or fallback or "").strip()
        if not raw:
            return ""

        expanded = os.path.expanduser(raw)
        if os.path.isabs(expanded):
            return os.path.abspath(expanded)

        return os.path.abspath(os.path.join(self.project_root, expanded))

    def reload_level_settings(self):
        settings = load_settings()
        leveling = settings.get("leveling", {})

        self.default_level_card_path = self._to_project_path(
            leveling.get("level_card_background"),
            "assets/level_card_bg.png",
        )
        self.level_card_storage_dir = self._to_project_path(
            leveling.get("level_card_storage_dir"),
            "assets/level_cards",
        )
        self.level_card_custom_dir = os.path.join(self.level_card_storage_dir, "custom")
        self.level_card_builtin_dir = os.path.join(self.level_card_storage_dir, "builtins")
        self._level_card_assets_initialized = False

    def _get_decay_settings(self):
        settings = load_settings()
        leveling = settings.get("leveling", {})

        inactivity = leveling.get("inactivity_decay", {})
        rolling = leveling.get("rolling_decay", {})

        try:
            inactivity_after = int(inactivity.get("start_after_days", 30))
        except (TypeError, ValueError):
            inactivity_after = 30
        try:
            inactivity_percent = float(inactivity.get("percent_per_day", 2.0))
        except (TypeError, ValueError):
            inactivity_percent = 2.0
        try:
            rolling_days = int(rolling.get("expire_days", 30))
        except (TypeError, ValueError):
            rolling_days = 30

        return {
            "inactivity_enabled": bool(inactivity.get("enabled")),
            "inactivity_after_days": max(0, inactivity_after),
            "inactivity_percent": max(0.0, min(100.0, inactivity_percent)),
            "rolling_enabled": bool(rolling.get("enabled")),
            "rolling_days": max(1, rolling_days),
        }

    def _iter_level_card_path_candidates(self, path_value: Optional[str]):
        raw = (path_value or "").strip()
        if not raw:
            return []

        candidates = []
        seen = set()

        def add_path(candidate: Optional[str]):
            if not candidate:
                return
            absolute = os.path.abspath(os.path.expanduser(os.path.normpath(candidate)))
            if absolute in seen:
                return
            seen.add(absolute)
            candidates.append(absolute)

        normalized_separators = raw.replace("\\", os.sep).replace("/", os.sep)

        add_path(raw)
        add_path(normalized_separators)

        if not os.path.isabs(raw):
            add_path(os.path.join(self.project_root, raw))
        if not os.path.isabs(normalized_separators):
            add_path(os.path.join(self.project_root, normalized_separators))

        file_name = os.path.basename(normalized_separators or raw)
        if file_name:
            add_path(os.path.join(self.level_card_custom_dir, file_name))
            add_path(os.path.join(self.level_card_builtin_dir, file_name))
            if self.default_level_card_path:
                add_path(os.path.join(os.path.dirname(self.default_level_card_path), file_name))

        return candidates

    def _resolve_level_card_file_path(self, path_value: Optional[str]):
        fallback = self.default_level_card_path or self._to_project_path("assets/level_card_bg.png")
        all_candidates = self._iter_level_card_path_candidates(path_value)
        all_candidates.extend(self._iter_level_card_path_candidates(fallback))

        for candidate in all_candidates:
            if os.path.isfile(candidate):
                return candidate

        return None

    def _builtin_level_card_specs(self):
        return [
            {
                "card_key": "aurora",
                "display_name": "Aurora",
                "file_name": "aurora.png",
                "palette": [(5, 14, 29), (10, 61, 82), (32, 154, 147)],
                "accents": [(76, 245, 206), (92, 160, 255), (116, 106, 255)],
                "seed": "aurora",
            },
            {
                "card_key": "sunset",
                "display_name": "Sunset",
                "file_name": "sunset.png",
                "palette": [(30, 14, 46), (116, 42, 101), (242, 117, 85)],
                "accents": [(255, 202, 94), (255, 128, 138), (155, 97, 255)],
                "seed": "sunset",
            },
            {
                "card_key": "nebula",
                "display_name": "Nebula",
                "file_name": "nebula.png",
                "palette": [(8, 11, 30), (24, 32, 74), (75, 44, 137)],
                "accents": [(96, 224, 255), (184, 109, 255), (255, 102, 194)],
                "seed": "nebula",
            },
            {
                "card_key": "forest",
                "display_name": "Forest",
                "file_name": "forest.png",
                "palette": [(9, 24, 20), (18, 62, 43), (71, 123, 72)],
                "accents": [(141, 220, 104), (110, 182, 255), (243, 206, 116)],
                "seed": "forest",
            },
        ]

    def _generate_level_card_background(self, path: str, palette, accents, seed: str):
        if os.path.isfile(path):
            return

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        width, height = 1000, 320
        image = Image.new("RGBA", (width, height), palette[0])
        draw = ImageDraw.Draw(image)

        for y in range(height):
            ratio = 0 if height <= 1 else y / (height - 1)
            color = self._sample_gradient_color(palette, ratio)
            draw.line((0, y, width, y), fill=(*color, 255))

        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        accent_cycle = list(accents) or [(255, 255, 255)]

        overlay_draw.ellipse((-120, -70, 360, 260), fill=(*accent_cycle[0], 96))
        overlay_draw.ellipse((650, 10, 1120, 430), fill=(*accent_cycle[1 % len(accent_cycle)], 102))
        overlay_draw.ellipse((210, 175, 520, 500), fill=(*accent_cycle[2 % len(accent_cycle)], 58))

        for index, x in enumerate(range(-160, 1160, 170)):
            color = accent_cycle[index % len(accent_cycle)]
            overlay_draw.rounded_rectangle(
                (x, 0, x + 68, height),
                radius=24,
                fill=(*color, 18 + (index % 5) * 4),
            )

        rng = random.Random(seed)
        for _ in range(64):
            radius = rng.randint(2, 5)
            x = rng.randint(0, width)
            y = rng.randint(0, height)
            color = accent_cycle[rng.randrange(len(accent_cycle))]
            overlay_draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*color, rng.randint(18, 76)))

        image = Image.alpha_composite(image, overlay)
        frame = ImageDraw.Draw(image)
        frame.rounded_rectangle(
            (16, 16, width - 16, height - 16),
            radius=32,
            outline=(255, 255, 255, 42),
            width=2,
        )
        image.save(path, format="PNG")

    def _ensure_level_card_background_files(self):
        if self._level_card_assets_initialized:
            return

        os.makedirs(self.level_card_storage_dir, exist_ok=True)
        os.makedirs(self.level_card_custom_dir, exist_ok=True)
        os.makedirs(self.level_card_builtin_dir, exist_ok=True)

        default_path = self._to_project_path(self.default_level_card_path, "assets/level_card_bg.png")
        self.default_level_card_path = default_path
        self._generate_level_card_background(
            default_path,
            [(6, 12, 28), (18, 37, 71), (36, 87, 151)],
            [(74, 159, 255), (147, 85, 255), (92, 226, 205)],
            "default-level-card",
        )

        for preset in self._builtin_level_card_specs():
            preset_path = os.path.join(self.level_card_builtin_dir, preset["file_name"])
            self._generate_level_card_background(
                preset_path,
                preset["palette"],
                preset["accents"],
                preset["seed"],
            )

        self._level_card_assets_initialized = True

    async def ensure_level_card_tables(self, db):
        self._ensure_level_card_background_files()

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS level_card_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                mode TEXT NOT NULL,
                min_level_for_choice INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS level_cards (
                card_key TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                unlock_level INTEGER NOT NULL DEFAULT 0,
                is_default INTEGER NOT NULL DEFAULT 0,
                is_custom INTEGER NOT NULL DEFAULT 0,
                is_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_level_cards (
                user_id INTEGER PRIMARY KEY,
                equipped_card_key TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS level_card_layout (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                avatar_x INTEGER NOT NULL,
                avatar_y INTEGER NOT NULL,
                avatar_size INTEGER NOT NULL,
                text_x INTEGER NOT NULL,
                name_y INTEGER NOT NULL,
                stats_y INTEGER NOT NULL,
                bar_y INTEGER NOT NULL,
                progress_y INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        now_iso = datetime.utcnow().isoformat()
        await db.execute(
            """
            INSERT OR IGNORE INTO level_card_settings (id, mode, min_level_for_choice, updated_at)
            VALUES (1, ?, 0, ?)
            """,
            (self.LEVEL_CARD_MODE_DEFAULT_ONLY, now_iso),
        )

        default_layout = self._sanitize_card_layout(self.DEFAULT_CARD_LAYOUT)
        await db.execute(
            """
            INSERT OR IGNORE INTO level_card_layout (
                id, avatar_x, avatar_y, avatar_size, text_x, name_y, stats_y, bar_y, progress_y, updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                default_layout["avatar_x"],
                default_layout["avatar_y"],
                default_layout["avatar_size"],
                default_layout["text_x"],
                default_layout["name_y"],
                default_layout["stats_y"],
                default_layout["bar_y"],
                default_layout["progress_y"],
                now_iso,
            ),
        )

        clean_default_path = self._to_project_path(self.default_level_card_path, "assets/level_card_bg.png")
        self.default_level_card_path = clean_default_path
        await db.execute(
            """
            INSERT INTO level_cards (card_key, display_name, file_path, unlock_level, is_default, is_custom, is_enabled, created_at)
            VALUES (?, ?, ?, 0, 1, 0, 1, ?)
            ON CONFLICT(card_key) DO UPDATE SET
                display_name = excluded.display_name,
                file_path = excluded.file_path,
                unlock_level = 0,
                is_default = 1,
                is_enabled = 1
            """,
            (self.DEFAULT_LEVEL_CARD_KEY, self.DEFAULT_LEVEL_CARD_DISPLAY_NAME, clean_default_path, now_iso),
        )

        for preset in self._builtin_level_card_specs():
            preset_path = os.path.join(self.level_card_builtin_dir, preset["file_name"])
            await db.execute(
                """
                INSERT INTO level_cards (card_key, display_name, file_path, unlock_level, is_default, is_custom, is_enabled, created_at)
                VALUES (?, ?, ?, 0, 0, 0, 1, ?)
                ON CONFLICT(card_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    file_path = excluded.file_path,
                    unlock_level = 0,
                    is_default = 0,
                    is_custom = 0,
                    is_enabled = 1
                """,
                (preset["card_key"], preset["display_name"], preset_path, now_iso),
            )

    def _card_row_to_dict(self, row):
        if not row:
            return None
        return {
            "card_key": row[0],
            "display_name": row[1],
            "file_path": row[2],
            "unlock_level": int(row[3] or 0),
            "is_default": bool(row[4]),
            "is_custom": bool(row[5]),
            "is_enabled": bool(row[6]),
            "created_at": row[7],
            "is_builtin": not bool(row[4]) and not bool(row[5]),
        }

    async def _get_card_by_key_db(self, db, card_key):
        normalized = self._sanitize_card_key(card_key)
        if not normalized:
            return None
        async with db.execute(
            """
            SELECT card_key, display_name, file_path, unlock_level, is_default, is_custom, is_enabled, created_at
            FROM level_cards
            WHERE card_key = ?
            LIMIT 1
            """,
            (normalized,),
        ) as cursor:
            row = await cursor.fetchone()
        return self._card_row_to_dict(row)

    async def _get_default_card_db(self, db):
        async with db.execute(
            """
            SELECT card_key, display_name, file_path, unlock_level, is_default, is_custom, is_enabled, created_at
            FROM level_cards
            WHERE card_key = ?
            LIMIT 1
            """,
            (self.DEFAULT_LEVEL_CARD_KEY,),
        ) as cursor:
            row = await cursor.fetchone()

        if row:
            return self._card_row_to_dict(row)

        async with db.execute(
            """
            SELECT card_key, display_name, file_path, unlock_level, is_default, is_custom, is_enabled, created_at
            FROM level_cards
            ORDER BY is_default DESC, unlock_level ASC, card_key ASC
            LIMIT 1
            """
        ) as cursor:
            fallback = await cursor.fetchone()
        return self._card_row_to_dict(fallback)

    async def _fetch_level_card_settings_db(self, db):
        await self.ensure_level_card_tables(db)
        async with db.execute(
            "SELECT mode, min_level_for_choice FROM level_card_settings WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()

        mode = self.LEVEL_CARD_MODE_DEFAULT_ONLY
        min_level = 0
        if row:
            mode = self._sanitize_card_mode(row[0])
            try:
                min_level = max(0, int(row[1] or 0))
            except (TypeError, ValueError):
                min_level = 0

        return {"mode": mode, "min_level_for_choice": min_level}

    async def _fetch_level_card_layout_db(self, db):
        await self.ensure_level_card_tables(db)
        async with db.execute(
            """
            SELECT avatar_x, avatar_y, avatar_size, text_x, name_y, stats_y, bar_y, progress_y
            FROM level_card_layout
            WHERE id = 1
            """
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return self._sanitize_card_layout(self.DEFAULT_CARD_LAYOUT)

        return self._sanitize_card_layout(
            {
                "avatar_x": row[0],
                "avatar_y": row[1],
                "avatar_size": row[2],
                "text_x": row[3],
                "name_y": row[4],
                "stats_y": row[5],
                "bar_y": row[6],
                "progress_y": row[7],
            }
        )

    async def _resolve_level_card_for_user_db(self, db, user_id: int, user_level: int):
        await self.ensure_level_card_tables(db)
        settings = await self._fetch_level_card_settings_db(db)
        default_card = await self._get_default_card_db(db)

        lvl = max(0, int(user_level or 0))
        mode = settings["mode"]

        async with db.execute(
            "SELECT equipped_card_key FROM user_level_cards WHERE user_id = ?",
            (int(user_id),),
        ) as cursor:
            equipped_row = await cursor.fetchone()

        if equipped_row and equipped_row[0]:
            card = await self._get_card_by_key_db(db, equipped_row[0])
            # Respect explicit equips directly; unlock checks are enforced at equip time.
            if card and card["is_enabled"]:
                return card

        if mode == self.LEVEL_CARD_MODE_AUTO_UNLOCK:
            async with db.execute(
                """
                SELECT card_key, display_name, file_path, unlock_level, is_default, is_custom, is_enabled, created_at
                FROM level_cards
                WHERE is_enabled = 1 AND unlock_level <= ?
                ORDER BY unlock_level DESC, is_default ASC, card_key ASC
                LIMIT 1
                """,
                (lvl,),
            ) as cursor:
                row = await cursor.fetchone()
            return self._card_row_to_dict(row) or default_card

        if mode == self.LEVEL_CARD_MODE_USER_CHOICE and lvl < settings["min_level_for_choice"]:
            return default_card

        return default_card

    async def _list_enabled_cards_for_level_db(self, db, user_level: int):
        await self.ensure_level_card_tables(db)
        lvl = max(0, int(user_level or 0))
        async with db.execute(
            """
            SELECT card_key, display_name, file_path, unlock_level, is_default, is_custom, is_enabled, created_at
            FROM level_cards
            WHERE is_enabled = 1 AND unlock_level <= ?
            ORDER BY is_default DESC, unlock_level ASC, card_key ASC
            """,
            (lvl,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._card_row_to_dict(row) for row in rows]

    async def get_level_card_settings(self):
        async with aiosqlite.connect(self.DB) as db:
            settings = await self._fetch_level_card_settings_db(db)
            await db.commit()
        return settings

    async def update_level_card_settings(self, mode: str, min_level_for_choice: int):
        normalized_mode = self._sanitize_card_mode(mode)
        min_level = max(0, int(min_level_for_choice or 0))

        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_level_card_tables(db)
            await db.execute(
                """
                UPDATE level_card_settings
                SET mode = ?, min_level_for_choice = ?, updated_at = ?
                WHERE id = 1
                """,
                (normalized_mode, min_level, datetime.utcnow().isoformat()),
            )
            await db.commit()

        return {"mode": normalized_mode, "min_level_for_choice": min_level}

    async def list_level_cards(self, include_disabled: bool = True):
        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_level_card_tables(db)
            where_clause = "" if include_disabled else "WHERE is_enabled = 1"
            query = (
                "SELECT card_key, display_name, file_path, unlock_level, is_default, is_custom, is_enabled, created_at "
                f"FROM level_cards {where_clause} "
                "ORDER BY is_default DESC, unlock_level ASC, card_key ASC"
            )
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
            await db.commit()

        return [self._card_row_to_dict(row) for row in rows]

    async def set_user_equipped_card(self, user_id: int, card_key: str):
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            raise ValueError("Invalid user id")

        if user_id <= 0:
            raise ValueError("Invalid user id")

        normalized_key = self._sanitize_card_key(card_key)

        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_level_card_tables(db)

            if not normalized_key or normalized_key == self.DEFAULT_LEVEL_CARD_KEY:
                await db.execute("DELETE FROM user_level_cards WHERE user_id = ?", (user_id,))
                await db.commit()
                print(f"[LEVEL] equip_saved db={self.DB} user={user_id} card={self.DEFAULT_LEVEL_CARD_KEY}", flush=True)
                return {"user_id": user_id, "equipped_card_key": self.DEFAULT_LEVEL_CARD_KEY}

            card = await self._get_card_by_key_db(db, normalized_key)
            if not card:
                raise ValueError("Card not found")
            if not card["is_enabled"]:
                raise ValueError("Card is disabled")

            # Manual equips should take effect immediately; promote legacy default-only mode.
            settings = await self._fetch_level_card_settings_db(db)
            if settings["mode"] == self.LEVEL_CARD_MODE_DEFAULT_ONLY:
                await db.execute(
                    """
                    UPDATE level_card_settings
                    SET mode = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    (self.LEVEL_CARD_MODE_USER_CHOICE, datetime.utcnow().isoformat()),
                )

            await db.execute(
                """
                INSERT INTO user_level_cards (user_id, equipped_card_key, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    equipped_card_key = excluded.equipped_card_key,
                    updated_at = excluded.updated_at
                """,
                (user_id, normalized_key, datetime.utcnow().isoformat()),
            )
            await db.commit()
            print(f"[LEVEL] equip_saved db={self.DB} user={user_id} card={normalized_key}", flush=True)

        return {"user_id": user_id, "equipped_card_key": normalized_key}

    async def create_custom_level_card(self, display_name: str, file_path: str, unlock_level: int = 0):
        if not file_path:
            raise ValueError("file_path is required")

        normalized_file_path = self._to_project_path(file_path)
        if not os.path.isfile(normalized_file_path):
            raise ValueError("file_path does not exist")

        base_key = self._sanitize_card_key(display_name)
        if not base_key or base_key == self.DEFAULT_LEVEL_CARD_KEY:
            base_key = "custom_card"

        unlock_level = max(0, int(unlock_level or 0))

        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_level_card_tables(db)

            candidate = base_key
            suffix = 1
            while await self._get_card_by_key_db(db, candidate):
                candidate = f"{base_key}_{suffix}"
                suffix += 1

            safe_name = self._sanitize_card_display_name(display_name, candidate.replace("_", " ").title())
            now_iso = datetime.utcnow().isoformat()
            await db.execute(
                """
                INSERT INTO level_cards (card_key, display_name, file_path, unlock_level, is_default, is_custom, is_enabled, created_at)
                VALUES (?, ?, ?, ?, 0, 1, 1, ?)
                """,
                (candidate, safe_name, normalized_file_path, unlock_level, now_iso),
            )
            await db.commit()

            return await self._get_card_by_key_db(db, candidate)

    async def set_level_card_enabled(self, card_key: str, enabled: bool):
        normalized_key = self._sanitize_card_key(card_key)
        if not normalized_key:
            raise ValueError("Card key is required")
        if normalized_key == self.DEFAULT_LEVEL_CARD_KEY and not enabled:
            raise ValueError("Default card cannot be disabled")

        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_level_card_tables(db)
            card = await self._get_card_by_key_db(db, normalized_key)
            if not card:
                raise ValueError("Card not found")

            removed_equips = 0
            if not enabled:
                async with db.execute(
                    "SELECT COUNT(*) FROM user_level_cards WHERE equipped_card_key = ?",
                    (normalized_key,),
                ) as cursor:
                    row = await cursor.fetchone()
                removed_equips = int((row or [0])[0] or 0)

            await db.execute(
                "UPDATE level_cards SET is_enabled = ? WHERE card_key = ?",
                (1 if enabled else 0, normalized_key),
            )

            if not enabled:
                # Remove stale user equips so users fall back to default/auto logic immediately.
                await db.execute(
                    "DELETE FROM user_level_cards WHERE equipped_card_key = ?",
                    (normalized_key,),
                )

            await db.commit()
            updated = await self._get_card_by_key_db(db, normalized_key)
            if updated is not None:
                updated["removed_equips"] = removed_equips
            return updated

    async def delete_custom_level_card(self, card_key: str, remove_file: bool = True):
        normalized_key = self._sanitize_card_key(card_key)
        if not normalized_key:
            raise ValueError("Card key is required")
        if normalized_key == self.DEFAULT_LEVEL_CARD_KEY:
            raise ValueError("Default card cannot be deleted")

        card = None
        removed_equips = 0
        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_level_card_tables(db)
            card = await self._get_card_by_key_db(db, normalized_key)
            if not card:
                raise ValueError("Card not found")
            if not card["is_custom"]:
                raise ValueError("Only custom cards can be deleted")

            async with db.execute(
                "SELECT COUNT(*) FROM user_level_cards WHERE equipped_card_key = ?",
                (normalized_key,),
            ) as cursor:
                row = await cursor.fetchone()
            removed_equips = int((row or [0])[0] or 0)

            await db.execute("DELETE FROM level_cards WHERE card_key = ?", (normalized_key,))
            await db.execute("DELETE FROM user_level_cards WHERE equipped_card_key = ?", (normalized_key,))
            await db.commit()

        file_deleted = False
        if remove_file and card and card.get("file_path"):
            file_path = self._resolve_level_card_file_path(card["file_path"]) or self._to_project_path(card["file_path"])
            if self._is_path_within(file_path, self.level_card_custom_dir) and os.path.isfile(file_path):
                try:
                    os.remove(file_path)
                    file_deleted = True
                except OSError:
                    file_deleted = False

        return {
            "card_key": normalized_key,
            "display_name": card["display_name"] if card else normalized_key,
            "file_path": card.get("file_path") if card else "",
            "file_deleted": file_deleted,
            "removed_equips": removed_equips,
        }

    async def get_level_card_layout(self):
        async with aiosqlite.connect(self.DB) as db:
            layout = await self._fetch_level_card_layout_db(db)
            await db.commit()
        return layout

    async def update_level_card_layout(self, layout_updates):
        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_level_card_tables(db)
            current = await self._fetch_level_card_layout_db(db)

            merged = dict(current)
            if isinstance(layout_updates, dict):
                merged.update(layout_updates)

            normalized = self._sanitize_card_layout(merged)
            await db.execute(
                """
                UPDATE level_card_layout
                SET avatar_x = ?, avatar_y = ?, avatar_size = ?, text_x = ?, name_y = ?,
                    stats_y = ?, bar_y = ?, progress_y = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    normalized["avatar_x"],
                    normalized["avatar_y"],
                    normalized["avatar_size"],
                    normalized["text_x"],
                    normalized["name_y"],
                    normalized["stats_y"],
                    normalized["bar_y"],
                    normalized["progress_y"],
                    datetime.utcnow().isoformat(),
                ),
            )
            await db.commit()

        return normalized

    def _load_font(self, size: int, bold: bool = False):
        cache_key = ("bold" if bold else "regular", int(size))
        cached = self._font_cache.get(cache_key)
        if cached is not None:
            return cached

        key = "bold" if bold else "regular"
        for path in self._font_candidates[key]:
            try:
                font = ImageFont.truetype(path, size)
                self._font_cache[cache_key] = font
                return font
            except OSError:
                continue

        font = ImageFont.load_default()
        self._font_cache[cache_key] = font
        return font

    def _format_xp(self, value):
        value = float(value or 0)
        if value.is_integer():
            return str(int(value))
        return f"{value:.1f}"

    def _normalize_rgb_color(self, value):
        if value is None:
            return None

        if isinstance(value, discord.Colour):
            rgb = value.to_rgb()
            return None if rgb == (0, 0, 0) else rgb

        if isinstance(value, int):
            value = value & 0xFFFFFF
            if value == 0:
                return None
            return ((value >> 16) & 255, (value >> 8) & 255, value & 255)

        if isinstance(value, (tuple, list)) and len(value) >= 3:
            try:
                r = int(value[0])
                g = int(value[1])
                b = int(value[2])
            except (TypeError, ValueError):
                return None
            r = max(0, min(255, r))
            g = max(0, min(255, g))
            b = max(0, min(255, b))
            return (r, g, b)

        return None

    def _resolve_name_colors(self, member):
        colors = []

        primary = self._normalize_rgb_color(getattr(member, "color", None))
        if primary is None:
            primary = self._normalize_rgb_color(getattr(member, "colour", None))
        if primary:
            colors.append(primary)

        top_role = getattr(member, "top_role", None)
        if top_role is not None:
            secondary_candidates = [
                getattr(top_role, "secondary_color", None),
                getattr(top_role, "secondary_colour", None),
                getattr(top_role, "color2", None),
                getattr(top_role, "colour2", None),
            ]

            role_palette = getattr(top_role, "colors", None)
            if role_palette is None:
                role_palette = getattr(top_role, "colours", None)
            if isinstance(role_palette, (tuple, list)):
                secondary_candidates.extend(role_palette)

            for value in secondary_candidates:
                rgb = self._normalize_rgb_color(value)
                if rgb and rgb not in colors:
                    colors.append(rgb)

        if not colors:
            return [(236, 243, 255)]
        return colors

    def _sample_gradient_color(self, colors, ratio):
        if not colors:
            return (236, 243, 255)
        if len(colors) == 1:
            return colors[0]

        segment_count = len(colors) - 1
        ratio = max(0.0, min(1.0, float(ratio)))
        scaled = ratio * segment_count
        segment_index = min(segment_count - 1, int(scaled))
        local_t = scaled - segment_index

        start = colors[segment_index]
        end = colors[segment_index + 1]
        return (
            int(start[0] + (end[0] - start[0]) * local_t),
            int(start[1] + (end[1] - start[1]) * local_t),
            int(start[2] + (end[2] - start[2]) * local_t),
        )

    def _draw_name_text(self, image, draw, position, text, font, colors):
        # Add a subtle shadow so bright and dark role colors remain legible.
        draw.text((position[0] + 2, position[1] + 2), text, font=font, fill=(6, 11, 22, 185))

        if len(colors) <= 1:
            draw.text(position, text, font=font, fill=(*colors[0], 255))
            return

        try:
            bbox = draw.textbbox((0, 0), text, font=font)
        except Exception:
            try:
                font_bbox = font.getbbox(text)
                text_w = max(1, int(font_bbox[2] - font_bbox[0]))
                text_h = max(1, int(font_bbox[3] - font_bbox[1]))
            except Exception:
                try:
                    text_w = max(1, int(draw.textlength(text, font=font)))
                except Exception:
                    text_w = max(1, len(text) * max(8, int(getattr(font, "size", 20) * 0.55)))
                text_h = max(1, int(getattr(font, "size", 20)))
            bbox = (0, 0, int(text_w), int(text_h))

        text_w = max(1, bbox[2] - bbox[0])
        text_h = max(1, bbox[3] - bbox[1])

        text_mask = Image.new("L", (text_w, text_h), 0)
        mask_draw = ImageDraw.Draw(text_mask)
        mask_draw.text((-bbox[0], -bbox[1]), text, font=font, fill=255)

        gradient = Image.new("RGBA", (text_w, text_h), (0, 0, 0, 0))
        gradient_draw = ImageDraw.Draw(gradient)

        for x in range(text_w):
            ratio = 0.0 if text_w == 1 else x / (text_w - 1)
            r, g, b = self._sample_gradient_color(colors, ratio)
            gradient_draw.line([(x, 0), (x, text_h)], fill=(r, g, b, 255))

        image.paste(
            gradient,
            (int(position[0] + bbox[0]), int(position[1] + bbox[1])),
            text_mask,
        )

    def _draw_rounded_rect(self, draw, xy, radius, fill=None, outline=None, width=1):
        rounded_rect = getattr(draw, "rounded_rectangle", None)
        if callable(rounded_rect):
            rounded_rect(xy, radius=radius, fill=fill, outline=outline, width=width)
            return

        try:
            draw.rectangle(xy, fill=fill, outline=outline, width=width)
        except TypeError:
            draw.rectangle(xy, fill=fill, outline=outline)

    async def render_level_card(self, member, lvl, xp_total, xp_current, xp_needed, background_path=None, layout=None):
        width, height = 1000, 320
        layout = self._sanitize_card_layout(layout or self.DEFAULT_CARD_LAYOUT)
        resolved_background = self._resolve_level_card_file_path(background_path)
        image = Image.new("RGBA", (width, height), (12, 18, 34, 255))

        if resolved_background:
            try:
                bg = Image.open(resolved_background).convert("RGBA")
                image = ImageOps.fit(bg, (width, height), method=getattr(getattr(Image, "Resampling", Image), "LANCZOS"))
            except Exception:
                pass

        draw = ImageDraw.Draw(image)

        # Soft horizontal gradient overlay for readability regardless of background image.
        for x in range(width):
            ratio = x / (width - 1)
            alpha = int(48 + 42 * ratio)
            draw.line([(x, 0), (x, height)], fill=(8, 14, 28, alpha))

        self._draw_rounded_rect(
            draw,
            (18, 18, width - 18, height - 18),
            radius=32,
            fill=(9, 14, 26, 136),
            outline=(111, 152, 235, 180),
            width=2,
        )

        avatar_size = int(layout["avatar_size"])
        avatar_pos = (int(layout["avatar_x"]), int(layout["avatar_y"]))
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

        text_x = int(layout["text_x"])
        name_text = member.display_name[:24]
        max_name_width = (width - 60) - text_x
        name_font = self._load_font(42, bold=True)
        for size in range(64, 40, -2):
            candidate = self._load_font(size, bold=True)
            try:
                bbox = draw.textbbox((0, 0), name_text, font=candidate)
                text_width = bbox[2] - bbox[0]
            except Exception:
                try:
                    text_width = int(draw.textlength(name_text, font=candidate))
                except Exception:
                    try:
                        font_bbox = candidate.getbbox(name_text)
                        text_width = int(font_bbox[2] - font_bbox[0])
                    except Exception:
                        text_width = max(1, len(name_text) * max(8, int(size * 0.55)))
            if text_width <= max_name_width:
                name_font = candidate
                break

        stats_font = self._load_font(26, bold=False)
        progress_font = self._load_font(22, bold=True)

        name_colors = self._resolve_name_colors(member)
        self._draw_name_text(image, draw, (text_x, int(layout["name_y"])), name_text, name_font, name_colors)

        stats_line = (
            f"Level {int(lvl)}   |   Total XP {self._format_xp(xp_total)}   |   "
            f"Next Level {self._format_xp(xp_needed)} XP"
        )
        draw.text((text_x, int(layout["stats_y"])), stats_line, font=stats_font, fill=(199, 214, 248, 255))

        progress_ratio = 0 if xp_needed <= 0 else max(0.0, min(1.0, float(xp_current) / float(xp_needed)))
        progress_percent = int(progress_ratio * 100)

        bar_x1, bar_y1 = text_x, int(layout["bar_y"])
        bar_x2, bar_y2 = width - 60, min(height - 20, int(layout["bar_y"]) + 40)
        if bar_y2 <= bar_y1:
            bar_y2 = bar_y1 + 24
        self._draw_rounded_rect(draw, (bar_x1, bar_y1, bar_x2, bar_y2), radius=16, fill=(32, 44, 69, 255))

        fill_width = int((bar_x2 - bar_x1) * progress_ratio)
        if fill_width > 0:
            self._draw_rounded_rect(
                draw,
                (bar_x1, bar_y1, bar_x1 + fill_width, bar_y2),
                radius=16,
                fill=(72, 159, 255, 255),
            )

        progress_text = f"{self._format_xp(xp_current)} / {self._format_xp(xp_needed)} XP ({progress_percent}%)"
        draw.text((text_x, int(layout["progress_y"])), progress_text, font=progress_font, fill=(224, 236, 255, 255))

        output = BytesIO()
        image.save(output, format="PNG")
        output.seek(0)
        return output

    async def ensure_progress_columns(self, db):
        if self.progress_initialized:
            return

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

        if "last_xp_at" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN last_xp_at REAL DEFAULT 0")
            columns.add("last_xp_at")

        if "last_decay_at" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN last_decay_at REAL DEFAULT 0")
            columns.add("last_decay_at")

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

        self.progress_initialized = True

    async def ensure_decay_tables(self, db):
        if self.decay_initialized:
            return

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS xp_daily (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                xp REAL NOT NULL,
                PRIMARY KEY (user_id, day)
            )
            """
        )

        self.decay_initialized = True

    def normalize_progress(self, xp_total, lvl, remaining_xp):
        xp_total = float(xp_total or 0)
        if lvl is None or remaining_xp is None or float(remaining_xp) <= 0:
            calc_lvl, xp_current, xp_needed = self.get_level_data(xp_total)
            return xp_total, int(calc_lvl), float(xp_needed - xp_current)
        return xp_total, int(lvl), float(remaining_xp)

    def _day_key(self, timestamp: float) -> str:
        return datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")

    def _rolling_cutoff_day(self, timestamp: float, expire_days: int) -> str:
        expire_days = max(1, int(expire_days))
        today = datetime.utcfromtimestamp(timestamp).date()
        cutoff = today - timedelta(days=expire_days - 1)
        return cutoff.isoformat()

    async def _record_daily_xp(self, db, user_id: int, xp: float, timestamp: float):
        if xp <= 0:
            return
        day_key = self._day_key(timestamp)
        await db.execute(
            """
            INSERT INTO xp_daily (user_id, day, xp)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, day) DO UPDATE SET xp = xp + ?
            """,
            (user_id, day_key, float(xp), float(xp)),
        )

    def _decay_enabled(self):
        settings = self._get_decay_settings()
        return settings["inactivity_enabled"] or settings["rolling_enabled"]

    async def _apply_decay_for_user(self, db, user_id: int, timestamp: float):
        settings = self._get_decay_settings()
        if not settings["inactivity_enabled"] and not settings["rolling_enabled"]:
            return False, 0.0, 0, float(self.get_xp_needed_for_level(0)), 0.0, 0.0

        async with db.execute(
            "SELECT xp, level, remaining_xp, last_xp_at, last_decay_at FROM users WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
            xp_total, lvl, remaining_xp, last_xp_at, last_decay_at = 0.0, 0, self.get_xp_needed_for_level(0), 0.0, 0.0
        else:
            xp_total = float(row[0] or 0)
            lvl = int(row[1] or 0)
            remaining_xp = float(row[2] or 0)
            last_xp_at = float(row[3] or 0)
            last_decay_at = float(row[4] or 0)

        xp_total, lvl, remaining_xp = self.normalize_progress(xp_total, lvl, remaining_xp)
        original = (xp_total, lvl, remaining_xp, last_xp_at, last_decay_at)

        if settings["rolling_enabled"]:
            cutoff_day = self._rolling_cutoff_day(timestamp, settings["rolling_days"])
            async with db.execute(
                "SELECT day, xp FROM xp_daily WHERE user_id = ? AND day >= ?",
                (user_id, cutoff_day),
            ) as cursor:
                rows = await cursor.fetchall()

            if not rows and xp_total > 0:
                day_key = self._day_key(timestamp)
                await db.execute(
                    """
                    INSERT INTO xp_daily (user_id, day, xp)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, day) DO UPDATE SET xp = xp + ?
                    """,
                    (user_id, day_key, xp_total, xp_total),
                )
                rows = [(day_key, xp_total)]

            xp_total = float(sum(float(row[1] or 0) for row in rows))
            await db.execute(
                "DELETE FROM xp_daily WHERE user_id = ? AND day < ?",
                (user_id, cutoff_day),
            )

        if settings["inactivity_enabled"] and settings["inactivity_percent"] > 0:
            if last_xp_at <= 0 and xp_total > 0:
                last_xp_at = float(timestamp)

            decay_start = last_xp_at + (settings["inactivity_after_days"] * 86400)
            if last_xp_at > 0 and timestamp > decay_start:
                effective_start = max(last_decay_at, decay_start)
                decay_days = int((timestamp - effective_start) // 86400)
                if decay_days > 0:
                    factor = (1.0 - (settings["inactivity_percent"] / 100.0)) ** decay_days
                    xp_total = max(0.0, xp_total * factor)
                    last_decay_at = effective_start + (decay_days * 86400)

        lvl, xp_current, xp_needed = self.get_level_data(xp_total)
        remaining_xp = float(xp_needed - xp_current)

        updated = (xp_total, int(lvl), float(remaining_xp), last_xp_at, last_decay_at)
        changed = any(
            abs(updated[i] - original[i]) > 1e-6 if isinstance(updated[i], float) else updated[i] != original[i]
            for i in range(len(updated))
        )

        if changed:
            if self.has_legacy_remain_xp:
                await db.execute(
                    """
                    UPDATE users
                    SET xp = ?, level = ?, remaining_xp = ?, remain_xp = ?, last_xp_at = ?, last_decay_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        xp_total,
                        int(lvl),
                        remaining_xp,
                        remaining_xp,
                        last_xp_at,
                        last_decay_at,
                        user_id,
                    ),
                )
            else:
                await db.execute(
                    """
                    UPDATE users
                    SET xp = ?, level = ?, remaining_xp = ?, last_xp_at = ?, last_decay_at = ?
                    WHERE user_id = ?
                    """,
                    (xp_total, int(lvl), remaining_xp, last_xp_at, last_decay_at, user_id),
                )

        return changed, xp_total, int(lvl), float(remaining_xp), last_xp_at, last_decay_at

    async def _maybe_run_decay(self):
        if not self._decay_enabled():
            return
        now = time.time()
        if now - self._last_decay_run < self.decay_run_min_interval:
            return
        await self.apply_decay_all_users()

    # DATABASE
    @commands.Cog.listener()
    async def on_ready(self):
        os.makedirs(self.level_card_storage_dir, exist_ok=True)
        os.makedirs(self.level_card_custom_dir, exist_ok=True)

        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_settings_table(db)
            await self.load_formula_settings(db)
            await self.ensure_level_card_tables(db)

            default_remaining_xp = self.get_xp_needed_for_level(0)
            await db.execute(f"""
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                msg_count INTEGER DEFAULT 0,
                voice_time INTEGER DEFAULT 0,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 0,
                remaining_xp REAL DEFAULT {default_remaining_xp},
                last_xp_at REAL DEFAULT 0,
                last_decay_at REAL DEFAULT 0
            )
            """)
            await self.ensure_progress_columns(db)
            await self.ensure_decay_tables(db)
            await db.commit()

        # Only recalculate if a migration was detected (not every startup)
        # Example: if hasattr(self, 'migration_needed') and self.migration_needed:
        #     await self.recalculate_all_levels()

        if not hasattr(self, 'voice_xp_task_running'):
            self.voice_xp_task.start()
            self.voice_xp_task_running = True

        if not hasattr(self, 'decay_xp_task_running'):
            self.decay_xp_task.start()
            self.decay_xp_task_running = True

        print("LevelSystem geladen", flush=True)

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
            await self.ensure_progress_columns(db)
            await self.ensure_decay_tables(db)

            if self._decay_enabled():
                _, xp_total, _, _, _, _ = await self._apply_decay_for_user(db, user_id, time.time())
                await db.commit()
                return xp_total

            async with db.execute(
                "SELECT xp FROM users WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                result = await cursor.fetchone()
                return result[0] if result else 0

    # XP ADD
    async def add_xp(self, user_id, xp, msg_increment: int = 0, voice_increment: int = 0):
        gained_xp = float(xp)
        msg_increment = max(0, int(msg_increment or 0))
        voice_increment = max(0, int(voice_increment or 0))
        now = time.time()

        async with aiosqlite.connect(self.DB) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                (user_id,),
            )
            await self.ensure_progress_columns(db)
            await self.ensure_decay_tables(db)

            if self._decay_enabled():
                _, xp_total, lvl, remaining_xp, _, _ = await self._apply_decay_for_user(db, user_id, now)
            else:
                async with db.execute(
                    "SELECT xp, level, remaining_xp FROM users WHERE user_id = ?",
                    (user_id,),
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

            await self._record_daily_xp(db, user_id, gained_xp, now)

            if self.has_legacy_remain_xp:
                await db.execute(
                    """
                    UPDATE users
                    SET xp = ?, level = ?, remaining_xp = ?, remain_xp = ?,
                        msg_count = msg_count + ?, voice_time = voice_time + ?,
                        last_xp_at = ?, last_decay_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        xp_total,
                        lvl,
                        remaining_xp,
                        remaining_xp,
                        msg_increment,
                        voice_increment,
                        now,
                        now,
                        user_id,
                    ),
                )
            else:
                await db.execute(
                    """
                    UPDATE users
                    SET xp = ?, level = ?, remaining_xp = ?,
                        msg_count = msg_count + ?, voice_time = voice_time + ?,
                        last_xp_at = ?, last_decay_at = ?
                    WHERE user_id = ?
                    """,
                    (xp_total, lvl, remaining_xp, msg_increment, voice_increment, now, now, user_id),
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

        # Scale XP with message length, but clamp it so very long messages cannot farm unlimited XP.
        content_length = len((message.content or "").strip())
        effective_length = min(content_length, 240)
        xp = 0.8 + (effective_length / 120.0) + random.uniform(0.0, 0.6)
        xp = min(xp, 3.0)

        if message.author.premium_since:
            xp *= 1.1

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
            xp *= boost

        xp = min(xp, 5.0)

        old_level, new_level = await self.add_xp(message.author.id, xp, msg_increment=1)

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

                    # Slight nerf to voice XP while keeping group calls more rewarding than solo.
                    xp = 0.9 if len(members) == 1 else 2.6
                    if member.voice.self_mute or member.voice.mute:
                        xp *= 0.2
                    if member.premium_since:
                        xp *= 1.1

                    # XP Boost for roles
                    boost = 1.0
                    if self.booster_stack_enabled:
                        for role in getattr(member, 'roles', []):
                            if role.id in self.XP_BOOST_ROLES:
                                boost *= self.XP_BOOST_ROLES[role.id]
                    else:
                        for role in getattr(member, 'roles', []):
                            if role.id in self.XP_BOOST_ROLES:
                                boost = max(boost, self.XP_BOOST_ROLES[role.id])
                    xp *= boost
                    old_level, new_level = await self.add_xp(member.id, xp, voice_increment=1)

                    await self.check_level_up(member, old_level, new_level)

    async def apply_decay_all_users(self):
        settings = self._get_decay_settings()
        if not settings["inactivity_enabled"] and not settings["rolling_enabled"]:
            return 0

        now = time.time()
        updated = 0
        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_progress_columns(db)
            await self.ensure_decay_tables(db)
            async with db.execute("SELECT user_id FROM users") as cursor:
                user_rows = await cursor.fetchall()

            for row in user_rows:
                user_id = int(row[0])
                changed, _, _, _, _, _ = await self._apply_decay_for_user(db, user_id, now)
                if changed:
                    updated += 1

            await db.commit()

        self._last_decay_run = now
        return updated

    @tasks.loop(hours=6)
    async def decay_xp_task(self):
        await self.apply_decay_all_users()

    # To toggle stacking, set booster_stack_enabled above to True or False in code.

   # /LEVEL

    @slash_command(name="level", description="Zeigt dein Level und Fortschritt")
    async def level(self, ctx, user: Optional[discord.Member]= None):
        member = user or ctx.author

        # Acknowledge quickly so long DB/image work does not expire the interaction token.
        try:
            await ctx.defer()
        except (discord.InteractionResponded, discord.NotFound):
            pass

        await self.check_user(member.id)
        selected_card = None
        card_layout = self.DEFAULT_CARD_LAYOUT
        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_progress_columns(db)
            await self.ensure_level_card_tables(db)
            await self.ensure_decay_tables(db)

            if self._decay_enabled():
                _, xp_total, lvl, remaining_xp, _, _ = await self._apply_decay_for_user(db, member.id, time.time())
            else:
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

            selected_card = await self._resolve_level_card_for_user_db(db, member.id, lvl)
            card_layout = await self._fetch_level_card_layout_db(db)
            print(
                f"[LEVEL] db={self.DB} user={member.id} card={(selected_card or {}).get('card_key', 'none')} path={(selected_card or {}).get('file_path', 'none')}",
                flush=True,
            )
            await db.commit()

        image_buffer = None
        try:
            image_buffer = await self.render_level_card(
                member,
                lvl,
                xp_total,
                xp_current,
                xp_needed,
                background_path=(selected_card or {}).get("file_path") if selected_card else None,
                layout=card_layout,
            )
        except Exception as exc:
            print(f"[LEVEL] render_level_card failed for user {member.id}: {exc}", flush=True)
            image_buffer = None

        if image_buffer is not None:
            file = discord.File(fp=image_buffer, filename=f"level_{member.id}.png")
            embed = discord.Embed(
                #title=f"Level Status fuer {member.display_name}",
                color=discord.Color.purple()
            )
            embed.set_image(url=f"attachment://{file.filename}")
            if selected_card and selected_card.get("card_key"):
                embed.set_footer(text=f"Card: {selected_card['card_key']}")
            await ctx.respond(embed=embed, file=file)
            return

        percentage = (xp_current / xp_needed) * 100 if xp_needed else 0
        progress_bar_length = 10
        filled_slots = int(xp_current / xp_needed * progress_bar_length) if xp_needed else 0
        filled_slots = max(0, min(progress_bar_length, filled_slots))
        bar = "🟦" * filled_slots + "⬜" * (progress_bar_length - filled_slots)

        embed = discord.Embed(
            #title=f"Level Status fuer {member.display_name}",
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

        if selected_card and selected_card.get("card_key"):
            embed.set_footer(text=f"Card: {selected_card['card_key']}")

        await ctx.respond(embed=embed)

    @slash_command(name="levelcard_list", description="Zeigt verfuegbare Levelkarten")
    async def levelcard_list(self, ctx):
        member = ctx.author
        await self.check_user(member.id)

        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_progress_columns(db)
            await self.ensure_level_card_tables(db)
            await self.ensure_decay_tables(db)

            if self._decay_enabled():
                _, xp_total, lvl, remaining_xp, _, _ = await self._apply_decay_for_user(db, member.id, time.time())
            else:
                async with db.execute(
                    "SELECT xp, level, remaining_xp FROM users WHERE user_id = ?",
                    (member.id,),
                ) as cursor:
                    result = await cursor.fetchone()

                if result:
                    xp_total, lvl, remaining_xp = self.normalize_progress(result[0], result[1], result[2])
                else:
                    xp_total, lvl, remaining_xp = 0.0, 0, float(self.get_xp_needed_for_level(0))

            settings = await self._fetch_level_card_settings_db(db)
            selected_card = await self._resolve_level_card_for_user_db(db, member.id, lvl)
            unlocked_cards = await self._list_enabled_cards_for_level_db(db, lvl)
            await db.commit()

        mode = settings["mode"]
        mode_texts = {
            self.LEVEL_CARD_MODE_DEFAULT_ONLY: "Default-only: everyone uses the default card.",
            self.LEVEL_CARD_MODE_USER_CHOICE: "User-choice: you can equip cards if unlocked.",
            self.LEVEL_CARD_MODE_AUTO_UNLOCK: "Auto-unlock: your card changes automatically with level.",
        }

        description = [
            f"Current level: **{int(lvl)}**",
            f"Mode: **{mode}**",
            mode_texts.get(mode, ""),
            (
                f"Currently used card: **{selected_card['display_name']}** (`{selected_card['card_key']}`)"
                if selected_card
                else f"Currently used card: **{self.DEFAULT_LEVEL_CARD_DISPLAY_NAME}**"
            ),
        ]

        if mode == self.LEVEL_CARD_MODE_USER_CHOICE and lvl < int(settings["min_level_for_choice"]):
            description.append(
                f"You need at least level **{int(settings['min_level_for_choice'])}** to equip your own card."
            )

        if unlocked_cards:
            lines = []
            for card in unlocked_cards[:25]:
                if not card:
                    continue
                origin = "default" if card.get("is_default") else "custom" if card.get("is_custom") else "built-in"
                card_key = str(card.get("card_key", "unknown"))
                display_name = str(card.get("display_name", "Unknown"))
                unlock_level = int(card.get("unlock_level") or 0)
                lines.append(
                    f"- `{card_key}` | {display_name} [{origin}] (unlock {unlock_level})"
                )
            description.append("\nUnlocked cards:\n" + "\n".join(lines))
        else:
            description.append("\nNo cards unlocked yet.")

        if mode == self.LEVEL_CARD_MODE_USER_CHOICE:
            description.append("\nUse `/levelcard_equip card_key:<key>` to equip one.")

        embed = discord.Embed(
            title="Level Cards",
            description="\n".join(description),
            color=discord.Color.purple(),
        )
        await ctx.respond(embed=embed, ephemeral=True)

    @slash_command(name="levelcard_equip", description="Ruestet eine Levelkarte aus")
    async def levelcard_equip(self, ctx, card_key: str):
        member = ctx.author
        await self.check_user(member.id)

        normalized_card_key = self._sanitize_card_key(card_key)
        if not normalized_card_key:
            await ctx.respond("Bitte gib einen gueltigen Card-Key an.", ephemeral=True)
            return

        async with aiosqlite.connect(self.DB) as db:
            await self.ensure_progress_columns(db)
            await self.ensure_level_card_tables(db)
            await self.ensure_decay_tables(db)

            if self._decay_enabled():
                _, xp_total, lvl, remaining_xp, _, _ = await self._apply_decay_for_user(db, member.id, time.time())
            else:
                async with db.execute(
                    "SELECT xp, level, remaining_xp FROM users WHERE user_id = ?",
                    (member.id,),
                ) as cursor:
                    result = await cursor.fetchone()

                if result:
                    xp_total, lvl, remaining_xp = self.normalize_progress(result[0], result[1], result[2])
                else:
                    xp_total, lvl, remaining_xp = 0.0, 0, float(self.get_xp_needed_for_level(0))

            settings = await self._fetch_level_card_settings_db(db)
            if settings["mode"] != self.LEVEL_CARD_MODE_USER_CHOICE:
                await ctx.respond(
                    "Das Auswaehlen von Karten ist aktuell deaktiviert (Modus ist nicht `user_choice`).",
                    ephemeral=True,
                )
                return

            if lvl < int(settings["min_level_for_choice"]):
                await ctx.respond(
                    f"Du brauchst mindestens Level {int(settings['min_level_for_choice'])}, um eigene Karten auszuwaehlen.",
                    ephemeral=True,
                )
                return

            card = await self._get_card_by_key_db(db, normalized_card_key)
            if not card or not card["is_enabled"]:
                await ctx.respond("Diese Karte existiert nicht oder ist deaktiviert.", ephemeral=True)
                return

            if lvl < int(card["unlock_level"]):
                await ctx.respond(
                    f"Diese Karte wird erst ab Level {int(card['unlock_level'])} freigeschaltet.",
                    ephemeral=True,
                )
                return

            await db.commit()

        await self.set_user_equipped_card(member.id, normalized_card_key)
        await ctx.respond(
            f"Levelkarte gesetzt: **{card['display_name']}** (`{card['card_key']}`).",
            ephemeral=True,
        )




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
            await self.cog._maybe_run_decay()
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
