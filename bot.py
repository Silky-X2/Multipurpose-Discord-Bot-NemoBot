import discord
import os
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

status = discord.Status.online
activity = discord.Activity(
    type=discord.ActivityType.watching,
    name="https://www.twitch.tv/nurnemo_"
)

bot = discord.Bot (
    intents=intents,
    debug_guilds=[1257572919153004575],
    status=status,
    activity=activity
)

@bot.event
async def on_ready():
    print(f"{bot.user} ist online!", flush=True)

# LOAD COGS
for file in sorted(os.listdir("./cogs")):
    if file.endswith(".py"):
        extension = f"cogs.{file[:-3]}"
        try:
            bot.load_extension(extension)
            print(f"[LOAD] {extension} geladen", flush=True)
        except Exception as exc:
            print(f"[LOAD] {extension} Fehler: {exc}", flush=True)
            raise


load_dotenv()
bot.run(os.getenv("Token"))