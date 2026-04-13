import discord
from discord.ext import commands
from discord.commands import slash_command, Option
import asyncio
import random
import aiosqlite
import json
import os
from datetime import datetime, timedelta


class Fishing(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.db_path = "fishing_data.db"
        self.fishing_cooldowns = {}

        # FISCHE DATENBANK (name, seltenheit, gewicht_min, gewicht_max, emoji)
        self.fishes = {
            "🐟 Regenbogenfisch": {"rarity": "legendary", "min": 8, "max": 15, "emoji": "🐟", "wert": 500},
            "🦈 Hai": {"rarity": "epic", "min": 150, "max": 300, "emoji": "🦈", "wert": 300},
            "🐠 Clownfisch": {"rarity": "rare", "min": 5, "max": 10, "emoji": "🐠", "wert": 150},
            "🐡 Kugelfisch": {"rarity": "rare", "min": 3, "max": 8, "emoji": "🐡", "wert": 120},
            "🦑 Tintenfisch": {"rarity": "epic", "min": 20, "max": 40, "emoji": "🦑", "wert": 250},
            "🦐 Garnele": {"rarity": "common", "min": 1, "max": 3, "emoji": "🦐", "wert": 30},
            "🦞 Hummer": {"rarity": "epic", "min": 25, "max": 50, "emoji": "🦞", "wert": 280},
            "🎣 Anglerfisch": {"rarity": "legendary", "min": 30, "max": 60, "emoji": "🎣", "wert": 600},
            "🦕 Seeschlange": {"rarity": "epic", "min": 50, "max": 100, "emoji": "🦕", "wert": 350},
            "🐚 Muschel": {"rarity": "common", "min": 2, "max": 5, "emoji": "🐚", "wert": 40},
        }

        # RARITY FARBEN
        self.rarity_colors = {
            "common": discord.Color.from_rgb(128, 128, 128),      
            "rare": discord.Color.from_rgb(65, 105, 225),         
            "epic": discord.Color.from_rgb(138, 43, 226),        
            "legendary": discord.Color.from_rgb(255, 215, 0)   
        }

    # SETUP DATABASE
    async def setup_database(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS fishing_inventory (
                    user_id INTEGER,
                    guild_id INTEGER,
                    fish_name TEXT,
                    weight REAL,
                    caught_date TEXT,
                    PRIMARY KEY (user_id, guild_id, fish_name, caught_date)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS fishing_stats (
                    user_id INTEGER PRIMARY KEY,
                    total_catches INTEGER,
                    total_weight REAL,
                    legendary_count INTEGER,
                    epic_count INTEGER,
                    rare_count INTEGER,
                    common_count INTEGER
                )
            """)
            await db.commit()

    # FISCH FANGEN
    async def catch_fish(self, user_id: int, guild_id: int):
        """Zufällige Fisch-Auswahl mit Rarity-Gewichtung"""
        # Rarity Gewichtung
        rarity_weights = {
            "common": 50,
            "rare": 30,
            "epic": 15,
            "legendary": 5
        }

        # Wähle zufällige Rarity
        rarities = list(rarity_weights.keys())
        weights = list(rarity_weights.values())
        chosen_rarity = random.choices(rarities, weights=weights, k=1)[0]

        # Finde alle Fische mit dieser Rarity
        available_fishes = {name: data for name, data in self.fishes.items() if data["rarity"] == chosen_rarity}
        
        if not available_fishes:
            return None

        # Wähle zufälligen Fisch
        fish_name = random.choice(list(available_fishes.keys()))
        fish_data = available_fishes[fish_name]

        # Generiere Gewicht
        weight = round(random.uniform(fish_data["min"], fish_data["max"]), 2)

        return {
            "name": fish_name,
            "weight": weight,
            "rarity": fish_data["rarity"],
            "wert": fish_data["wert"],
            "emoji": fish_data["emoji"]
        }

    # SPEICHERE FISCH IN DATENBANK
    async def save_fish_to_db(self, user_id: int, guild_id: int, fish: dict):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO fishing_inventory (user_id, guild_id, fish_name, weight, caught_date)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, guild_id, fish["name"], fish["weight"], datetime.utcnow().isoformat()))

            # Update Stats
            rarity_column = f"{fish['rarity']}_count"
            await db.execute(f"""
                INSERT INTO fishing_stats (user_id, total_catches, total_weight, {rarity_column})
                VALUES (?, 1, ?, 1)
                ON CONFLICT(user_id) DO UPDATE SET
                    total_catches = total_catches + 1,
                    total_weight = total_weight + ?,
                    {rarity_column} = {rarity_column} + 1
            """, (user_id, fish["weight"], fish["weight"]))

            await db.commit()

    # FISCHE-BUTTON VIEW
    class FishingView(discord.ui.View):
        def __init__(self, cog, user_id, fish_data):
            super().__init__(timeout=30)
            self.cog = cog
            self.user_id = user_id
            self.fish_data = fish_data
            self.caught = False

        @discord.ui.button(label="⏱️ Fass zu!", style=discord.ButtonStyle.danger)
        async def catch_button(self, button, interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("Das ist nicht dein Fang!", ephemeral=True)
                return

            if self.caught:
                await interaction.response.send_message("Diese Fisch wurde schon gefangen!", ephemeral=True)
                return

            self.caught = True

            # 80% Erfolgsrate
            if random.random() < 0.8:
                emoji = self.fish_data["emoji"]
                embed = discord.Embed(
                    title="Fisch gefangen!",
                    description=f"{emoji} **{self.fish_data['name']}**\n"
                                f"Gewicht: **{self.fish_data['weight']} kg**\n"
                                f"Wert: **{self.fish_data['wert']}💰**",
                    color=self.cog.rarity_colors[self.fish_data["rarity"]]
                )

                await interaction.response.send_message(embed=embed)

                # Speichere Fisch
                await self.cog.save_fish_to_db(interaction.user.id, interaction.guild_id, self.fish_data)
            else:
                embed = discord.Embed(
                    title="Fisch entkommen!",
                    description="Der Fisch war zu schnell und ist entkommen!",
                    color=discord.Color.red()
                )
                await interaction.response.send_message(embed=embed)

            # Deaktiviere Button
            button.disabled = True
            await interaction.message.edit(view=self)

        async def on_timeout(self):
            # Button timeout nach 30 Sekunden
            pass

    # FISHING COMMAND
    @slash_command(name="fish", description="Geh angeln! 🎣")
    async def fishing(self, ctx):
        user_id = ctx.author.id
        guild_id = ctx.guild_id

        # Cooldown Check (30 Sekunden)
        if user_id in self.fishing_cooldowns:
            cooldown_time = self.fishing_cooldowns[user_id]
            if datetime.utcnow() < cooldown_time:
                remaining = int((cooldown_time - datetime.utcnow()).total_seconds())
                await ctx.respond(f"⏳ Du musst noch {remaining} Sekunden warten!", ephemeral=True)
                return

        # Setze Cooldown
        self.fishing_cooldowns[user_id] = datetime.utcnow() + timedelta(seconds=30)

        # Zufällige Wartezeit (2-8 Sekunden)
        wait_time = random.randint(2, 8)

        embed = discord.Embed(
            title="🎣 Du wirfst deine Angel aus...",
            description=f"Warte {wait_time} Sekunden bis ein Fisch anbeißt!",
            color=discord.Color.blue()
        )

        msg = await ctx.respond(embed=embed)

        # Warte
        await asyncio.sleep(wait_time)

        # Fisch fangen
        fish = await self.catch_fish(user_id, guild_id)

        if not fish:
            embed = discord.Embed(
                title="Nichts gebissen!",
                description="Es war zu lange ruhig und du gibst auf.",
                color=discord.Color.red()
            )
            await msg.edit(embed=embed)
            return

        # Zeige Fisch und Button
        embed = discord.Embed(
            title="🎣 Ein Fisch beißt an!",
            description="Klick schnell auf den Button! ⏱️",
            color=discord.Color.gold()
        )

        view = self.FishingView(self, user_id, fish)
        await msg.edit(embed=embed, view=view)

    # MY FISHES COMMAND
    @slash_command(name="meine_fische", description="Zeige deine gefangenen Fische")
    async def my_fishes(self, ctx):
        user_id = ctx.author.id
        guild_id = ctx.guild_id

        async with aiosqlite.connect(self.db_path) as db:
            # Hole Stats
            async with db.execute("""
                SELECT total_catches, total_weight, legendary_count, epic_count, rare_count, common_count
                FROM fishing_stats WHERE user_id = ?
            """, (user_id,)) as cursor:
                stats_row = await cursor.fetchone()

            if not stats_row:
                await ctx.respond("Du hast noch keine Fische gefangen! 🎣", ephemeral=True)
                return

            total_catches, total_weight, legendary, epic, rare, common = stats_row

            # Hole Fische
            async with db.execute("""
                SELECT fish_name, weight, COUNT(*) as count
                FROM fishing_inventory
                WHERE user_id = ? AND guild_id = ?
                GROUP BY fish_name
                ORDER BY weight DESC
            """, (user_id, guild_id)) as cursor:
                fishes = await cursor.fetchall()

        embed = discord.Embed(
            title=f"🎣 {ctx.author.name}'s Fische",
            color=discord.Color.blue()
        )

        fish_list = ""
        for fish_name, weight, count in fishes:
            fish_list += f"{fish_name} - **{weight} kg** (x{count})\n"

        embed.add_field(name="📋 Gefangene Fische", value=fish_list or "Keine", inline=False)

        stats_text = f"""
        **Gesamt gefangen:** {total_catches}
        **Gesamtgewicht:** {total_weight:.1f} kg
        
        **Nach Seltenheit:**
        🟨 Legendary: {legendary}
        🟪 Epic: {epic}
        🟦 Rare: {rare}
        ⬜ Common: {common}
        """

        embed.add_field(name="📊 Statistiken", value=stats_text, inline=False)
        embed.set_thumbnail(url=ctx.author.display_avatar.url)

        await ctx.respond(embed=embed)

    # LEADERBOARD COMMAND
    @slash_command(name="fishing_leaderboard", description="Fishing Leaderboard")
    async def fishing_leaderboard(self, ctx):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("""
                SELECT user_id, total_catches, total_weight, legendary_count
                FROM fishing_stats
                ORDER BY total_catches DESC
                LIMIT 10
            """) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await ctx.respond("Noch keine Fische gefangen!", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎣 Fishing Leaderboard",
            color=discord.Color.gold()
        )

        leaderboard_text = ""
        for i, (user_id, catches, weight, legendary) in enumerate(rows, 1):
            user = self.bot.get_user(user_id)
            username = user.name if user else f"User {user_id}"
            leaderboard_text += f"{i}. **{username}** - {catches} Fische | {weight:.1f} kg | 🟨 {legendary}\n"

        embed.description = leaderboard_text
        await ctx.respond(embed=embed)

    # SELL FISHES COMMAND
    @slash_command(name="fische_verkaufen", description="Verkaufe deine Fische für Coins")
    async def sell_fishes(self, ctx):
        user_id = ctx.author.id
        guild_id = ctx.guild_id

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("""
                SELECT fish_name, COUNT(*) as count
                FROM fishing_inventory
                WHERE user_id = ? AND guild_id = ?
                GROUP BY fish_name
            """, (user_id, guild_id)) as cursor:
                fishes = await cursor.fetchall()

        if not fishes:
            await ctx.respond("Du hast keine Fische zum Verkaufen!", ephemeral=True)
            return

        total_value = 0
        for fish_name, count in fishes:
            if fish_name in self.fishes:
                total_value += self.fishes[fish_name]["wert"] * count

        # Lösche Fische
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                DELETE FROM fishing_inventory
                WHERE user_id = ? AND guild_id = ?
            """, (user_id, guild_id))
            await db.commit()

        embed = discord.Embed(
            title="Fische verkauft!",
            description=f"Du hast {len(fishes)} verschiedene Arten verkauft!\n\n💰 **Wert: {total_value} Coins**",
            color=discord.Color.green()
        )

        await ctx.respond(embed=embed)


def setup(bot):
    cog = Fishing(bot)
    bot.add_cog(cog)
    asyncio.create_task(cog.setup_database())
