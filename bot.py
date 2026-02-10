import discord
from discord.ext import commands
import os

TOKEN = os.environ["DISCORD_TOKEN"]

ZAKAZANY_LAS_VC = 1470621135556186182
TAJNE_ZAKLECIA_TEXT = 1470635892275286026
AURA_BOT_ID = 1448399268384870663  # ID Aura Music Bot

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

playlist_active = False

@bot.event
async def on_ready():
    print(f"Zalogowany jako {bot.user}")

def real_users(channel):
    return [m for m in channel.members if not m.bot]

@bot.event
async def on_voice_state_update(member, before, after):
    global playlist_active

    # RESET — gdy ostatni człowiek wychodzi
    if before.channel and before.channel.id == ZAKAZANY_LAS_VC:
        humans = real_users(before.channel)
        if len(humans) == 0:
            playlist_active = False

    # TRIGGER — pierwszy człowiek wchodzi
    if after.channel and after.channel.id == ZAKAZANY_LAS_VC:
        if member.bot:
            return

        humans = real_users(after.channel)

        if len(humans) == 1 and not playlist_active:
            channel = bot.get_channel(TAJNE_ZAKLECIA_TEXT)
            await channel.send("/playlists play playlist: Mgła Zapomnienia")
            playlist_active = True
