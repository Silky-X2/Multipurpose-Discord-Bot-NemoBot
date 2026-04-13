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
    debug_guilds=[1223748094886412420],
    status=status,
    activity=activity
)

@bot.event
async def on_ready():
    print(f"{bot.user} ist online!")

# LOAD COGS
for file in os.listdir("./cogs"):
    if file.endswith(".py"):
        bot.load_extension(f"cogs.{file[:-3]}")


load_dotenv()
bot.run(os.getenv("Token"))