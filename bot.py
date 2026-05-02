import discord
import os
from dotenv import load_dotenv
from utils.settings import load_settings

load_dotenv()


def _resolve_presence(settings):
    presence = settings.get("presence", {})
    presence_text = (presence.get("text") or "NemoBot").strip()[:128] or "NemoBot"
    presence_type = (presence.get("type") or "watching").strip().lower()
    type_map = {
        "watching": discord.ActivityType.watching,
        "playing": discord.ActivityType.playing,
        "listening": discord.ActivityType.listening,
    }
    return type_map.get(presence_type, discord.ActivityType.watching), presence_text

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

status = discord.Status.online
settings = load_settings()
presence_type, presence_text = _resolve_presence(settings)
activity = discord.Activity(type=presence_type, name=presence_text)

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


bot.run(os.getenv("Token"))