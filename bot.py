import discord
from discord.ext import commands
import os
from threading import Thread

# ==========================
# CONFIG
# ==========================
TOKEN = os.environ["DISCORD_TOKEN"]

ZAKAZANY_LAS_VC = 1470833237013037299
TAJNE_ZAKLECIA_TEXT = 1470635892275286026
AURA_BOT_ID = 1448399268384870663  # Aura Music Bot

# ==========================
# INTENTS
# ==========================
intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================
# FLAGS
# ==========================
playlist_active = False


# ==========================
# FUNKCJE POMOCNICZE
# ==========================
def real_users(channel):
    """Zwraca listę ludzi, którzy są na kanale, ignorując boty"""
    return [m for m in channel.members if not m.bot]


# ==========================
# OPCJONALNY KEEP-ALIVE SERWER (dla Free Web Service)
# ==========================
def start_keep_alive():
    try:
        from flask import Flask
        app = Flask('')

        @app.route('/')
        def home():
            return "Bot działa!"

        def run():
            app.run(host='0.0.0.0', port=10000)

        t = Thread(target=run)
        t.start()
    except ImportError:
        # Flask nie jest zainstalowany → ignorujemy
        pass


# ==========================
# EVENTY
# ==========================
@bot.event
async def on_ready():
    print(f"Zalogowany jako {bot.user}")
    start_keep_alive()  # jeśli używasz Web Service


@bot.event
async def on_voice_state_update(member, before, after):
    global playlist_active

    # === RESET — gdy ostatni człowiek wychodzi ===
    if before.channel and before.channel.id == ZAKAZANY_LAS_VC:
        humans = real_users(before.channel)
        if len(humans) == 0:
            playlist_active = False
            # Rozłącz Aurę z VC
            aura = before.channel.guild.get_member(AURA_BOT_ID)
            if aura and aura.voice:
                try:
                    await aura.move_to(None)
                    print("Aura została rozłączona – kanał pusty")
                except Exception as e:
                    print(f"Błąd przy rozłączaniu Aury: {e}")

    # === TRIGGER — pierwszy człowiek wchodzi ===
    if after.channel and after.channel.id == ZAKAZANY_LAS_VC:
        if member.bot:
            return

        humans = real_users(after.channel)
        if len(humans) == 1 and not playlist_active:
            channel = bot.get_channel(TAJNE_ZAKLECIA_TEXT)
            if channel:
                try:
                    await channel.send("/playlists play playlist: Mgła Zapomnienia")
                    playlist_active = True
                    print(f"Playlist Mgła Zapomnienia uruchomiona przez {member}")
                except Exception as e:
                    print(f"Błąd przy wysyłaniu komendy: {e}")
            else:
                print("Nie znaleziono kanału #tajne-zaklęcia")


# ==========================
# RUN BOT
# ==========================
bot.run(TOKEN)
