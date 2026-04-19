import discord
from discord.ext import commands
from discord.commands import slash_command, Option
import asyncio
import datetime
import random
import aiosqlite
import json
import os

from utils.time_parser import format_duration, parse_duration_to_seconds


class Giveaway(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.giveaways = {}
        self.db_path = "giveaway_data.db"

        # BOOSTER BONUS 
        self.bonus_role = 1365793303429382154
        self.bonus_multiplier = 1.05

        # MOD ROLE
        self.mod_roles = [1467922063195902085]

    # SETUP DATABASE
    async def setup_database(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS giveaways (
                    message_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    guild_id INTEGER,
                    prize TEXT,
                    host_id INTEGER,
                    end_time TEXT,
                    winners_count INTEGER,
                    participants TEXT
                )
            """)
            await db.commit()

    # LOAD GIVEAWAYS FROM DATABASE
    async def load_giveaways(self):
        if not os.path.exists(self.db_path):
            return

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT * FROM giveaways") as cursor:
                rows = await cursor.fetchall()
                
                for row in rows:
                    msg_id, channel_id, guild_id, prize, host_id, end_time, winners_count, participants_json = row
                    
                    try:
                        guild = self.bot.get_guild(guild_id)
                        if not guild:
                            continue
                            
                        channel = guild.get_channel(channel_id)
                        if not channel:
                            continue

                        host = guild.get_member(host_id)
                        if not host:
                            continue

                        participants = []
                        if participants_json:
                            try:
                                participant_ids = json.loads(participants_json)
                                for user_id in participant_ids:
                                    user = guild.get_member(user_id)
                                    if user:
                                        participants.append(user)
                            except json.JSONDecodeError:
                                pass

                        end_time_obj = datetime.datetime.fromisoformat(end_time)
                        self.giveaways[msg_id] = {
                            "channel": channel,
                            "prize": prize,
                            "host": host,
                            "end_time": end_time_obj,
                            "winners_count": winners_count,
                            "participants": participants
                        }

                        # Aktualisiere Embed mit Teilnehmerzahl nach Neustart
                        await self.update_giveaway_embed(channel, msg_id, self.giveaways[msg_id])

                        # Starte Timer für dieses Giveaway
                        time_left = (end_time_obj - datetime.datetime.utcnow()).total_seconds()
                        if time_left > 0:
                            asyncio.get_event_loop().create_task(self.giveaway_timer(msg_id, time_left))
                    except Exception as e:
                        print(f"Fehler beim Laden von Giveaway {msg_id}: {e}")

    # GIVEAWAY TIMER
    async def giveaway_timer(self, msg_id, duration):
        await asyncio.sleep(max(0, duration))
        await self.end_giveaway(msg_id)

    # SAVE GIVEAWAY TO DATABASE
    async def save_giveaway_to_db(self, msg_id, channel, prize, host, end_time, winners_count, participants):
        participant_ids = [p.id for p in participants]
        participants_json = json.dumps(participant_ids)
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO giveaways 
                (message_id, channel_id, guild_id, prize, host_id, end_time, winners_count, participants)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (msg_id, channel.id, channel.guild.id, prize, host.id, end_time.isoformat(), winners_count, participants_json))
            await db.commit()

    # UPDATE PARTICIPANTS IN DATABASE
    async def update_participants_in_db(self, msg_id, participants):
        participant_ids = [p.id for p in participants]
        participants_json = json.dumps(participant_ids)
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE giveaways SET participants = ? WHERE message_id = ?
            """, (participants_json, msg_id))
            await db.commit()

    # DELETE GIVEAWAY FROM DATABASE
    async def delete_giveaway_from_db(self, msg_id):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM giveaways WHERE message_id = ?", (msg_id,))
            await db.commit()

    # UPDATE GIVEAWAY EMBED WITH PARTICIPANT COUNT
    async def update_giveaway_embed(self, channel, msg_id, data):
        """Aktualisiert die Giveaway-Nachricht mit aktueller Teilnehmerzahl"""
        try:
            msg = await channel.fetch_message(msg_id)
            participants_count = len(data["participants"])
            remaining_seconds = max(
                0,
                int((data["end_time"] - datetime.datetime.utcnow()).total_seconds())
            )
            readable_remaining = format_duration(remaining_seconds)
            
            embed = discord.Embed(
                title="🎉 Giveaway aktiv!",
                description=(
                    f"Preis: **{data['prize']}**\n"
                    f"Verbleibend: **{readable_remaining}**\n"
                    f"Gewinner: **{data['winners_count']}**\n\n"
                    f"**Teilnehmer: {participants_count}**"
                ),
                color=discord.Color.purple()
            )
            embed.set_footer(text=f"Von {data['host'].name}")
            await msg.edit(embed=embed)
        except Exception as e:
            print(f"Fehler beim Update der Giveaway-Embed: {e}")

    async def is_mod_or_admin(self, member: discord.Member):
        if member.guild_permissions.administrator:
            return True

        for role in member.roles:
            if role.id in self.mod_roles:
                return True

        return False


   
    # BONUS CHECK
    def has_bonus_role(self, member: discord.Member):
        role = member.guild.get_role(self.bonus_role)
        return role in member.roles if role else False


    
    # GIVEAWAY JOIN VIEW
    class GiveawayView(discord.ui.View):

        def __init__(self, cog, msg_id):
            super().__init__(timeout=None)
            self.cog = cog
            self.msg_id = msg_id

        @discord.ui.button(
            label="Teilnehmen",
            style=discord.ButtonStyle.green,
            custom_id="giveaway_join"
        )
        async def join_button(self, button, interaction: discord.Interaction):

            if self.msg_id not in self.cog.giveaways:
                await interaction.response.send_message(
                    "Dieses Giveaway ist beendet.",
                    ephemeral=True
                )
                return

            users = self.cog.giveaways[self.msg_id]["participants"]

            if interaction.user in users:
                await interaction.response.send_message(
                    "Du hast bereits teilgenommen!",
                    ephemeral=True
                )
                return

            users.append(interaction.user)

            # Speichere neue Participants in Datenbank
            await self.cog.update_participants_in_db(self.msg_id, users)

            # Update Giveaway Embed mit neuer Teilnehmerzahl
            data = self.cog.giveaways[self.msg_id]
            await self.cog.update_giveaway_embed(data["channel"], self.msg_id, data)

            await interaction.response.send_message(
                f"✅ Du hast erfolgreich teilgenommen! (Insgesamt: {len(users)} Teilnehmer)",
                ephemeral=True
            )


    
    # GIVEAWAY START
    @slash_command(name="giveaway_start", description="Starte ein Giveaway")
    async def giveaway_start(
        self,
        ctx,
        prize: Option(str, "Preis"),
        duration: Option(str, "Dauer (z.B. 1d 2m 5min)"),
        winners_count: Option(int, "Gewinner Anzahl"),
        channel: Option(discord.abc.GuildChannel, "Channel")
    ):

        if not await self.is_mod_or_admin(ctx.author):
            await ctx.respond("❌ Keine Berechtigung!", ephemeral=True)
            return

        try:
            duration_seconds = parse_duration_to_seconds(duration)
        except ValueError as exc:
            await ctx.respond(
                "❌ Ungueltige Dauer. Beispiele: `30min`, `2h`, `1d 2m 5min`.\n"
                f"Details: {exc}",
                ephemeral=True
            )
            return

        if winners_count < 1:
            await ctx.respond("❌ Gewinner Anzahl muss mindestens 1 sein.", ephemeral=True)
            return

        readable_duration = format_duration(duration_seconds)

        embed = discord.Embed(
            title="🎉 Giveaway aktiv!",
            description=(
                f"Preis: **{prize}**\n"
                f"Dauer: **{readable_duration}**\n"
                f"Gewinner: **{winners_count}**\n\n"
                "👥 **Teilnehmer: 0**"
            ),
            color=discord.Color.purple()
        )

        embed.set_footer(text=f"Von {ctx.author.name}")

        view = self.GiveawayView(self, 0)
        msg = await channel.send(embed=embed, view=view)

        end_time = datetime.datetime.utcnow() + datetime.timedelta(seconds=duration_seconds)
        
        self.giveaways[msg.id] = {
            "channel": channel,
            "prize": prize,
            "host": ctx.author,
            "end_time": end_time,
            "winners_count": winners_count,
            "participants": []
        }

        view.msg_id = msg.id

        # Speichere in Datenbank
        await self.save_giveaway_to_db(msg.id, channel, prize, ctx.author, end_time, winners_count, [])

        await ctx.respond("Giveaway gestartet!", ephemeral=True)

        asyncio.get_event_loop().create_task(self.giveaway_timer(msg.id, duration_seconds))


    
    # GIVEAWAY END
    async def end_giveaway(self, msg_id):

        if msg_id not in self.giveaways:
            return

        data = self.giveaways[msg_id]
        channel = data["channel"]
        participants = data["participants"]

        if not participants:
            embed = discord.Embed(
                title="🎉 Giveaway beendet!",
                description=f"Keine Teilnehmer für **{data['prize']}**",
                color=discord.Color.red()
            )

            await channel.send(embed=embed)
            del self.giveaways[msg_id]
            await self.delete_giveaway_from_db(msg_id)
            return

        weighted = []

        for user in participants:
            weight = self.bonus_multiplier if self.has_bonus_role(user) else 1.0
            weighted.extend([user] * int(weight * 10))

        winners = []
        count = min(data["winners_count"], len(participants))

        while len(winners) < count:
            w = random.choice(weighted)
            if w not in winners:
                winners.append(w)

        winner_mentions = ", ".join(w.mention for w in winners)

        embed = discord.Embed(
            title="🎉 Giveaway beendet!",
            description=f"Gewinner von **{data['prize']}**: {winner_mentions}",
            color=discord.Color.purple()
        )

        await channel.send(embed=embed)

        del self.giveaways[msg_id]
        await self.delete_giveaway_from_db(msg_id)


    
    # GIVEAWAY END COMMAND
    @slash_command(name="giveaway_end", description="Beendet Giveaway")
    async def giveaway_end(self, ctx, message_id: str):

        if not await self.is_mod_or_admin(ctx.author):
            await ctx.respond("❌ Keine Berechtigung!", ephemeral=True)
            return

        await self.end_giveaway(int(message_id))
        await ctx.respond("Giveaway beendet.", ephemeral=True)


    
    # REROLL UI SELECT
    class RerollSelect(discord.ui.Select):

        def __init__(self, cog, message_id, participants):
            self.cog = cog
            self.message_id = message_id

            options = []

            for user in participants[:25]:
                options.append(
                    discord.SelectOption(
                        label=user.name,
                        value=str(user.id)
                    )
                )

            super().__init__(
                placeholder="Wähle Gewinner zum rerollen...",
                min_values=1,
                max_values=len(options),
                options=options
            )

        async def callback(self, interaction: discord.Interaction):

            self.view.selected_users = [int(v) for v in self.values]

            await interaction.response.send_message(
                f"✅ Ausgewählt: {len(self.values)} Gewinner",
                ephemeral=True
            )


    
    # REROLL VIEW
    class RerollView(discord.ui.View):

        def __init__(self, cog, message_id, participants):
            super().__init__(timeout=60)
            self.cog = cog
            self.message_id = message_id
            self.selected_users = []

            self.add_item(self.cog.RerollSelect(cog, message_id, participants))

        @discord.ui.button(
            label="🔄 Reroll starten",
            style=discord.ButtonStyle.green
        )
        async def reroll_button(self, button, interaction: discord.Interaction):

            data = self.cog.giveaways.get(self.message_id)

            if not data:
                await interaction.response.send_message("❌ Giveaway nicht gefunden", ephemeral=True)
                return

            participants = data["participants"]

            if not self.selected_users:
                await interaction.response.send_message("❌ Keine Auswahl getroffen", ephemeral=True)
                return

            weighted = []

            for user in participants:
                weight = self.cog.bonus_multiplier if self.cog.has_bonus_role(user) else 1.0
                weighted.extend([user] * int(weight * 10))

            new_winners = []

            for _ in self.selected_users:
                while True:
                    w = random.choice(weighted)
                    if w not in new_winners:
                        new_winners.append(w)
                        break

            await interaction.response.send_message(
                "🎉 Neue Gewinner:\n" +
                "\n".join(w.mention for w in new_winners)
            )


    
    # REROLL COMMAND
    @slash_command(name="giveaway_reroll", description="Giveaway Reroll")
    async def giveaway_reroll(self, ctx, message_id: str):

        if not await self.is_mod_or_admin(ctx.author):
            await ctx.respond("❌ Keine Berechtigung!", ephemeral=True)
            return

        data = self.giveaways.get(int(message_id))

        if not data:
            await ctx.respond("❌ Giveaway nicht gefunden", ephemeral=True)
            return

        view = self.RerollView(self, int(message_id), data["participants"])

        await ctx.respond("Wähle Gewinner zum rerollen:", view=view, ephemeral=True)


    # HELP COMMAND (Ignore that i added it to this cog. I was too lazy to create a new one for it lol:3)
    @slash_command(name="help", description="Zeigt alle Befehle und Infos")
    async def help(self, ctx):

        embed = discord.Embed(
            title="Bot Hilfe",
            description="Alle verfügbaren Commands und Infos",
            color=discord.Color.purple()
        )

        embed.add_field(
            name="📈 Level",
            value=(
                "/level → Dein Level & XP\n"
                "/leaderboard → Gesamt XP Ranking\n"
                "/leaderboard_messages → Nachrichten Ranking\n"
                "/leaderboard_voice → Voice Ranking"
            ),
            inline=False
        )

        embed.add_field(
            name="🎉 Giveaway",
            value=(
                "/giveaway_start → Giveaway starten\n"
                "/giveaway_reroll → Gewinner neu auswählen\n"
                "/giveaway_end → Giveaway manuell beenden"
            ),
            inline=False
        )

        embed.add_field(
            name="🎣 Angeln",
            value=(
                "/fish → Angeln gehen (30s Cooldown)\n"
                "/meine_fische → Deine gefangenen Fische + Statistiken\n"
                "/fishing_leaderboard → Top 10 Angler\n"
                "/fische_verkaufen → Alle Fische verkaufen"
            ),
            inline=False
        )

        embed.add_field(
            name="⚡ XP System",
            value=(
                "Nachrichten: 1–3 XP (Cooldown: 20s)\n"
                "Voice alleine: 1 XP/Min | Mit anderen: 3 XP/Min\n"
                "Gemutet: weniger XP | Deaf: 0 XP\n"
                "Booster-Rollen geben XP-Multiplikatoren"
            ),
            inline=False
        )

        embed.add_field(
            name="🧭 Dashboard",
            value=(
                "Aktiviere /debug und nutze dann %dashboard\n"
                "Der Link wird dir per DM geschickt"
            ),
            inline=False
        )

        embed.add_field(
            name="ℹ️ Bot Info",
            value="Erstellt von: Silky mit hilfe von Otter",
            inline=False
        )

        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.set_footer(text=f"{ctx.guild.name}")

        await ctx.respond(embed=embed)

def setup(bot):
    cog = Giveaway(bot)
    bot.add_cog(cog)
    # Initialisiere Datenbank und lade gespeicherte Giveaways
    asyncio.get_event_loop().create_task(cog.setup_database())
    asyncio.get_event_loop().create_task(cog.load_giveaways())