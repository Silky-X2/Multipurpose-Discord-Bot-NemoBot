import discord
from discord.ext import commands

class Debug(commands.Cog):
    DEBUG_USERS = {1174069790718034082, 1151934321142280282}
    debug_mode = False

    def __init__(self, bot):
        self.bot = bot

    @discord.slash_command(name="debug", description="Toggle debug mode")
    async def debug(self, ctx):
        if ctx.author.id not in self.DEBUG_USERS:
            await ctx.respond("Du hast keine Berechtigung für diesen Befehl.", ephemeral=True)
            return
        Debug.debug_mode = not Debug.debug_mode
        status = "aktiviert" if Debug.debug_mode else "deaktiviert"
        await ctx.respond(f"Debug {status}")
        if Debug.debug_mode:
            # Turn off debug mode after 15 minutes
            self.bot.loop.create_task(self._auto_off_debug())

    async def _auto_off_debug(self):
        import asyncio
        await asyncio.sleep(900)  # 15 minutes
        Debug.debug_mode = False

    @commands.Cog.listener()
    async def on_message(self, message):
        if not Debug.debug_mode:
            return
        if message.author.id not in self.DEBUG_USERS:
            return
        if not message.content.startswith('%'):
            return
        cmd = message.content[1:].strip().lower()
        if cmd == "recalculate":
            await message.channel.send("Level werden neu berechnet ...")
            level_cog = self.bot.get_cog("LevelSystem")
            if level_cog:
                try:
                    await level_cog.recalculate_all_levels(channel=message.channel)
                except Exception as e:
                    import traceback
                    tb = traceback.format_exc()
                    # Truncate traceback to avoid exceeding Discord message limits
                    if len(tb) > 1900:
                        tb = tb[-1900:]
                    await message.channel.send(f"Fehler beim Neuberechnen:\n```py\n{tb}\n```")
            else:
                await message.channel.send("LevelSystem Cog nicht gefunden!")
        elif cmd == "dashboard":
            dashboard_cog = self.bot.get_cog("Dashboard")
            if not dashboard_cog:
                await message.channel.send("Dashboard Cog nicht gefunden!")
                return

            urls = await dashboard_cog.dashboard_access_urls()
            lan_line = f"Network: {urls['lan']}\n" if urls.get("lan") else "Network: not detected\n"
            try:
                await message.author.send(
                    f"Local: {urls['local']}\n"
                    f"{lan_line}"
                    f"Public: {urls['public']}\n\n"
                )
                await message.channel.send("Look DM.")
            except discord.Forbidden:
                await message.channel.send("Ich kann dir keine DM senden. Bitte aktiviere DMs und versuche es erneut.")
        # elif cmd == "restart":
        #     await message.channel.send("Bot wird neugestartet ...")
        #     import sys, os, signal
        #     # Properly stop the bot and exit the process for a restart (external process manager should restart it)
        #     await message.guild.me.edit(nick=None) if hasattr(message.guild.me, 'edit') else None
        #     await message.channel.send("Bot-Prozess wird beendet. Bitte stelle sicher, dass ein Prozessmanager (z.B. pm2, systemd, Docker) den Bot neu startet.")
        #     os.kill(os.getpid(), signal.SIGTERM)
        else:
            await message.channel.send("Unbekannter Debug-Befehl.")

def setup(bot):
    bot.add_cog(Debug(bot))
