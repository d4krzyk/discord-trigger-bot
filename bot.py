from __future__ import annotations

import os
import json
import asyncio
from collections import deque
from typing import Optional, NoReturn

import discord
from discord.ext import commands
import wavelink

# --- Web/Render keep-alive (Render Web Service oczekuje nasłuchiwania na porcie) ---
from flask import Flask

app = Flask(__name__)


@app.get("/")
def index():
    return {"ok": True, "service": "discord-trigger-bot"}


@app.get("/health")
def health():
    return {"ok": True}


async def _run_web_server():
    """Uruchamia prosty serwer HTTP w tle (dla Render Web Service)."""
    port = int(os.environ.get("PORT", "10000"))

    # Flask jest synchroniczny; odpalamy go w osobnym wątku przez executor.
    def _run():
        app.run(host="0.0.0.0", port=port)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _run)

# ==========================
# CONFIG
# ==========================
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Brak zmiennej środowiskowej DISCORD_TOKEN")

# Auto-disconnect, gdy nic nie gra i kolejka pusta
IDLE_DISCONNECT_SECONDS = int(os.environ.get("IDLE_DISCONNECT_SECONDS", "300"))  # 5 min

# Domyślne ustawienia (komendami można je ustawić w trakcie działania bota)
VC_CHANNEL_ID = 0       # Kanał głosowy, na którym bot ma działać
TEXT_CHANNEL_ID = 0     # Kanał tekstowy, w którym komendy są akceptowane
ALLOWED_ROLE_NAME = "Nekromanta"  # Rola, która może używać komend

PLAYLISTS_FILE = "playlists.json"

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
# Jeżeli bot ma czytać komendy prefixowe z treści wiadomości, to w części przypadków
# trzeba mieć message_content. Na Render/produkcyjnie lepiej włączyć to jawnie.
if os.environ.get("ENABLE_MESSAGE_CONTENT_INTENT", "1") == "1":
    intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================
# STATE
# ==========================
queue: deque[wavelink.Playable] = deque()
current_track: Optional[wavelink.Playable] = None
playlists: dict[str, list[str]] = {}

_idle_task: Optional[asyncio.Task] = None


def _cancel_idle_task():
    global _idle_task
    if _idle_task and not _idle_task.done():
        _idle_task.cancel()
    _idle_task = None


def _schedule_idle_disconnect(guild: discord.Guild):
    """Uruchamia timer rozłączenia, jeśli przez dłuższy czas nic nie gra i kolejka jest pusta."""
    global _idle_task

    # Nie planuj, jeśli mechanizm jest wyłączony
    if IDLE_DISCONNECT_SECONDS <= 0:
        return

    _cancel_idle_task()

    async def _job():
        try:
            await asyncio.sleep(IDLE_DISCONNECT_SECONDS)
            player = await _get_player(guild)
            if not player:
                return

            # Rozłącz tylko jeśli nadal nic nie gra i brak kolejki
            if (not queue) and (not player.playing) and (not player.paused):
                await player.disconnect()
                print(f"Idle timeout: rozłączono z VC po {IDLE_DISCONNECT_SECONDS}s bezczynności")
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"Błąd idle disconnect: {e}")

    _idle_task = bot.loop.create_task(_job())

# ==========================
# PLAYLIST STORAGE
# ==========================
def load_playlists():
    global playlists
    try:
        with open(PLAYLISTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            playlists = data if isinstance(data, dict) else {}
    except FileNotFoundError:
        playlists = {}
    except Exception:
        playlists = {}


def save_playlists():
    with open(PLAYLISTS_FILE, "w", encoding="utf-8") as f:
        json.dump(playlists, f, indent=4, ensure_ascii=False)

# ==========================
# HELPERS
# ==========================
def _real_users(channel: discord.VoiceChannel):
    return [m for m in channel.members if not m.bot]


def role_only():
    async def predicate(ctx: commands.Context):
        # Jeżeli nie ustawiono kanału tekstowego, pozwól użyć komendy wszędzie.
        if TEXT_CHANNEL_ID and ctx.channel.id != TEXT_CHANNEL_ID:
            return False
        # Jeżeli nie ustawiono roli, pozwól wszystkim (ułatwia pierwszą konfigurację).
        if not ALLOWED_ROLE_NAME:
            return True
        role = discord.utils.get(ctx.author.roles, name=ALLOWED_ROLE_NAME)
        return role is not None

    return commands.check(predicate)


async def _get_player(guild: discord.Guild) -> Optional[wavelink.Player]:
    vc = guild.voice_client
    return vc if isinstance(vc, wavelink.Player) else None


async def join_vc(channel: discord.VoiceChannel) -> wavelink.Player:
    player = await channel.connect(cls=wavelink.Player)
    return player


async def ensure_connected(ctx: commands.Context) -> Optional[wavelink.Player]:
    if not VC_CHANNEL_ID:
        await ctx.send("Nie ustawiono kanału VC. Użyj `!set_vc`.")
        return None

    vc_channel = ctx.guild.get_channel(VC_CHANNEL_ID)
    if not isinstance(vc_channel, discord.VoiceChannel):
        await ctx.send("Ustawiony kanał VC nie istnieje lub nie jest kanałem głosowym.")
        return None

    player = await _get_player(ctx.guild)
    if player is None:
        player = await join_vc(vc_channel)

    return player


async def leave_vc_if_empty(channel: discord.VoiceChannel):
    humans = _real_users(channel)
    if len(humans) == 0:
        player = await _get_player(channel.guild)
        if player:
            await player.disconnect()
        queue.clear()
        global current_track
        current_track = None
        print("VC pusty, bot rozłączony; kolejka wyczyszczona")


async def enqueue_and_maybe_play(ctx: commands.Context, player: wavelink.Player, track: wavelink.Playable):
    queue.append(track)

    e = _music_embed("Dodano do kolejki", _track_line(track))
    e.add_field(name="Pozycja w kolejce", value=str(len(queue)), inline=True)

    dur = _track_duration_ms(track)
    if dur:
        e.add_field(name="Długość", value=_format_duration_ms(dur), inline=True)

    thumb = _guess_youtube_thumbnail(_track_url(track))
    if thumb:
        e.set_thumbnail(url=thumb)

    await ctx.send(embed=e)

    # Mamy aktywność -> anuluj idle timer
    _cancel_idle_task()

    # Jeśli nic nie gra, startuj od razu.
    if not player.playing and not player.paused:
        await play_next(ctx.guild)


async def play_next(guild: discord.Guild):
    player = await _get_player(guild)
    if not player:
        return

    global current_track

    # Loop pojedynczego utworu: odtwarzaj w kółko to samo
    if loop_mode == LOOP_SONG and current_track is not None:
        _cancel_idle_task()
        await player.play(current_track)
        return

    # Loop kolejki: po zakończeniu utworu wrzuć go na koniec
    if loop_mode == LOOP_QUEUE and current_track is not None:
        queue.append(current_track)

    if not queue:
        current_track = None
        _schedule_idle_disconnect(guild)
        return

    _cancel_idle_task()

    next_track = queue.popleft()
    current_track = next_track
    await player.play(next_track)


async def _search_track(query: str) -> Optional[wavelink.Playable]:
    q = query.strip()
    if not q:
        return None

    # Wavelink v2+ – uniwersalne wyszukiwanie.
    try:
        results = await wavelink.Playable.search(q)
    except Exception:
        results = None

    if not results:
        return None

    if isinstance(results, list):
        return results[0] if results else None

    # czasem zwraca Playlist/Track; bierz pierwszy element jeśli się da
    if hasattr(results, "tracks"):
        tracks = getattr(results, "tracks")
        return tracks[0] if tracks else None

    return results

# ==========================
# WAVELINK NODE
# ==========================
@bot.event
async def on_ready():
    print(f"Zalogowany jako {bot.user}")

    # Web server dla Render (tło)
    if os.environ.get("RUN_WEB", "1") == "1":
        bot.loop.create_task(_run_web_server())

    # Node twórz tylko raz
    if not wavelink.NodePool.nodes:
        await wavelink.NodePool.create_node(
            bot=bot,
            host=os.environ.get("LAVALINK_HOST"),
            port=int(os.environ.get("LAVALINK_PORT", "2333")),
            password=os.environ.get("LAVALINK_PASSWORD"),
        )

    load_playlists()
    print("Bot gotowy i połączony z Lavalink")

# ==========================
# EVENTS
# ==========================
@bot.event
async def on_voice_state_update(member, before, after):
    if VC_CHANNEL_ID == 0:
        return

    # Gdy ktoś wejdzie na kanał głosowy
    if after.channel and after.channel.id == VC_CHANNEL_ID and not member.bot:
        vc_channel = after.channel
        player = await _get_player(vc_channel.guild)
        if not player:
            await join_vc(vc_channel)

    # Gdy ktoś wychodzi z kanału
    if before.channel and before.channel.id == VC_CHANNEL_ID:
        await leave_vc_if_empty(before.channel)


@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    # Automatyczne przejście do następnego utworu / loop.
    try:
        await play_next(payload.player.guild)
    except Exception as e:
        print(f"Błąd play_next po zakończeniu utworu: {e}")


@bot.event
async def on_wavelink_track_exception(payload: wavelink.TrackExceptionEventPayload):
    # Gdy track wywali wyjątek, próbuj przejść dalej.
    try:
        print(f"Track exception: {payload.exception}")
        await play_next(payload.player.guild)
    except Exception as e:
        print(f"Błąd play_next po track_exception: {e}")


@bot.event
async def on_wavelink_track_stuck(payload: wavelink.TrackStuckEventPayload):
    # Gdy track utknie, przełącz dalej.
    try:
        print(f"Track stuck: threshold={payload.threshold}")
        await play_next(payload.player.guild)
    except Exception as e:
        print(f"Błąd play_next po track_stuck: {e}")


@bot.event
async def on_wavelink_node_disconnected(node: wavelink.Node, _):
    print(f"Lavalink node rozłączony: {node.identifier}")

# ==========================
# CONFIG COMMANDS
# ==========================
@bot.command()
@role_only()
async def set_vc(ctx, channel: discord.VoiceChannel):
    """Ustaw kanał VC, na którym bot będzie działał"""
    global VC_CHANNEL_ID
    VC_CHANNEL_ID = channel.id
    await ctx.send(f"VC ustawiony na: {channel.name}")


@bot.command()
@role_only()
async def set_text(ctx, channel: discord.TextChannel):
    """Ustaw kanał tekstowy, w którym komendy będą działały"""
    global TEXT_CHANNEL_ID
    TEXT_CHANNEL_ID = channel.id
    await ctx.send(f"Kanał tekstowy ustawiony na: {channel.name}")


@bot.command()
@role_only()
async def set_role(ctx, role: discord.Role):
    """Ustaw rolę, która będzie mogła używać komend"""
    global ALLOWED_ROLE_NAME
    ALLOWED_ROLE_NAME = role.name
    await ctx.send(f"Rola ustawiona na: {role.name}")

# ==========================
# MUSIC COMMANDS
# ==========================
@bot.command()
@role_only()
async def play(ctx, *, query: str):
    """Dodaje utwór do kolejki (URL lub fraza) i startuje odtwarzanie."""
    player = await ensure_connected(ctx)
    if not player:
        return

    track = await _search_track(query)
    if not track:
        await ctx.send("Nie znaleziono utworu dla podanego zapytania.")
        return

    await enqueue_and_maybe_play(ctx, player, track)


@bot.command()
@role_only()
async def now(ctx):
    """Pokazuje aktualnie odtwarzany utwór."""
    if not current_track:
        return await ctx.send(embed=_music_embed("Teraz gra", "Aktualnie nic nie gra."))

    e = _music_embed("Teraz gra", _track_line(current_track))

    dur = _track_duration_ms(current_track)
    if dur:
        e.add_field(name="Długość", value=_format_duration_ms(dur), inline=True)

    thumb = _guess_youtube_thumbnail(_track_url(current_track))
    if thumb:
        e.set_thumbnail(url=thumb)

    await ctx.send(embed=e)


@bot.command()
@role_only()
async def queue_show(ctx):
    """Pokazuje kolejkę."""
    player = await _get_player(ctx.guild)

    if not queue and not current_track:
        return await ctx.send(embed=_music_embed("Kolejka", "Kolejka jest pusta."))

    e = _music_embed("Kolejka")

    if current_track:
        e.add_field(name="Teraz gra", value=_track_line(current_track), inline=False)

    if queue:
        preview = []
        for i, t in enumerate(list(queue)[:10], start=1):
            preview.append(f"{i}. {_track_line(t)}")
        more = len(queue) - 10
        if more > 0:
            preview.append(f"… (+{more} więcej)")
        e.add_field(name="Następne", value="\n".join(preview), inline=False)
    else:
        e.add_field(name="Następne", value="(brak)", inline=False)

    if player:
        status = "pauza" if player.paused else "gra" if player.playing else "stop"
        e.set_footer(text=f"Status: {status} • Loop: {loop_mode}")
    else:
        e.set_footer(text=f"Loop: {loop_mode}")

    await ctx.send(embed=e)


@bot.command()
@role_only()
async def pause(ctx):
    player = await _get_player(ctx.guild)
    if player and player.playing:
        await player.pause(True)
        await ctx.send(embed=_music_embed("Pauza", "Odtwarzanie wstrzymane."))


@bot.command()
@role_only()
async def resume(ctx):
    player = await _get_player(ctx.guild)
    if player and player.paused:
        await player.pause(False)
        await ctx.send(embed=_music_embed("Wznowiono", "Odtwarzanie wznowione."))


@bot.command()
@role_only()
async def skip(ctx):
    player = await _get_player(ctx.guild)
    if not player:
        return
    await player.stop()
    await ctx.send(embed=_music_embed("Pominięto", "Utwór został pominięty."))


@bot.command()
@role_only()
async def stop(ctx):
    player = await _get_player(ctx.guild)
    if player:
        await player.stop()
    queue.clear()
    global current_track
    current_track = None

    # skoro stop i pusto, to zaplanuj rozłączenie
    _schedule_idle_disconnect(ctx.guild)

    await ctx.send(embed=_music_embed("Zatrzymano", "Odtwarzanie zatrzymane, kolejka wyczyszczona."))

# ==========================
# PLAYLIST MANAGEMENT
# ==========================
@bot.command()
@role_only()
async def playlist_create(ctx, name: str):
    if name in playlists:
        return await ctx.send("Taka playlista już istnieje.")
    playlists[name] = []
    save_playlists()
    await ctx.send(f"Stworzono playlistę: {name}")


@bot.command()
@role_only()
async def playlist_list(ctx):
    if not playlists:
        return await ctx.send(embed=_music_embed("Playlisty", "Brak playlist."))

    e = _music_embed("Playlisty")
    e.description = "\n".join(f"• **{name}** ({len(items)} pozycji)" for name, items in sorted(playlists.items()))
    await ctx.send(embed=e)


@bot.command()
@role_only()
async def playlist_add(ctx, playlist_name: str, *, query: str):
    if playlist_name not in playlists:
        return await ctx.send("Nie znaleziono takiej playlisty.")
    playlists[playlist_name].append(query)
    save_playlists()
    await ctx.send(f"Dodano do playlisty {playlist_name}: {query}")


@bot.command()
@role_only()
async def playlist_remove(ctx, playlist_name: str, *, query: str):
    if playlist_name not in playlists:
        return await ctx.send("Nie znaleziono takiej playlisty.")
    if query not in playlists[playlist_name]:
        return await ctx.send("Ten wpis nie istnieje w playlistie.")
    playlists[playlist_name].remove(query)
    save_playlists()
    await ctx.send(f"Usunięto z playlisty {playlist_name}: {query}")


@bot.command()
@role_only()
async def playlist_show(ctx, playlist_name: str):
    if playlist_name not in playlists:
        return await ctx.send(embed=_music_embed("Playlista", "Nie znaleziono takiej playlisty."))

    items = playlists[playlist_name]
    e = _music_embed(f"Playlista: {playlist_name}")

    if not items:
        e.description = "Playlista jest pusta."
        return await ctx.send(embed=e)

    preview = "\n".join(f"{i+1}. {q}" for i, q in enumerate(items[:15]))
    if len(items) > 15:
        preview += f"\n… (+{len(items)-15} więcej)"

    e.description = preview
    await ctx.send(embed=e)


@bot.command()
@role_only()
async def playlist_play(ctx, playlist_name: str):
    if playlist_name not in playlists:
        return await ctx.send(embed=_music_embed("Playlista", "Nie znaleziono takiej playlisty."))

    player = await ensure_connected(ctx)
    if not player:
        return

    items = playlists[playlist_name]
    if not items:
        return await ctx.send(embed=_music_embed("Playlista", "Playlista jest pusta."))

    added = 0
    for q in items:
        track = await _search_track(q)
        if track:
            queue.append(track)
            added += 1

    e = _music_embed(f"Dodano playlistę: {playlist_name}", f"Dodano do kolejki: **{added}**/**{len(items)}**")
    e.add_field(name="Kolejka", value=str(len(queue)), inline=True)
    await ctx.send(embed=e)

    if not player.playing and not player.paused:
        await play_next(ctx.guild)

# ==========================
# LOOP MODES
# ==========================
LOOP_OFF = "off"
LOOP_SONG = "song"
LOOP_QUEUE = "queue"

loop_mode: str = LOOP_OFF


@bot.command()
@role_only()
async def loop(ctx, mode: str = "off"):
    """Ustawia zapętlanie: off | song | queue"""
    global loop_mode

    mode = (mode or "").strip().lower()
    if mode in ("0", "false", "none"):
        mode = LOOP_OFF

    if mode not in (LOOP_OFF, LOOP_SONG, LOOP_QUEUE):
        e = _music_embed("Loop", "Użyj: `!loop off` / `!loop song` / `!loop queue`")
        return await ctx.send(embed=e)

    loop_mode = mode

    if loop_mode == LOOP_OFF:
        msg = "Wyłączono zapętlanie."
    elif loop_mode == LOOP_SONG:
        msg = "Włączono zapętlanie utworu (loop song)."
    else:
        msg = "Włączono zapętlanie kolejki (loop queue)."

    await ctx.send(embed=_music_embed("Loop", msg))


@bot.command()
@role_only()
async def loop_status(ctx):
    """Pokazuje aktualny tryb zapętlania."""
    await ctx.send(embed=_music_embed("Loop", f"Aktualny tryb: **{loop_mode}**"))

# ==========================
# EMBEDS
# ==========================
EMBED_COLOR = int(os.environ.get("EMBED_COLOR", "0x5865F2"), 16)  # Discord blurple


def _music_embed(title: str, description: Optional[str] = None) -> discord.Embed:
    return discord.Embed(title=title, description=description or "", color=EMBED_COLOR)


def _format_duration_ms(ms: Optional[int]) -> str:
    if not ms:
        return "?"
    seconds = int(ms // 1000)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _guess_youtube_thumbnail(url: Optional[str]) -> Optional[str]:
    if not url:
        return None

    # Obsługa najczęstszych formatów: youtube.com/watch?v=, youtu.be/, /shorts/
    try:
        video_id = None

        if "youtu.be/" in url:
            video_id = url.split("youtu.be/", 1)[1].split("?", 1)[0].split("/", 1)[0]
        elif "watch?v=" in url:
            video_id = url.split("watch?v=", 1)[1].split("&", 1)[0]
        elif "/shorts/" in url:
            video_id = url.split("/shorts/", 1)[1].split("?", 1)[0].split("/", 1)[0]

        if not video_id:
            return None

        # maxresdefault nie zawsze istnieje, ale Discord sam fallbackuje na 404 jako brak obrazka.
        return f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
    except Exception:
        return None


def _track_line(track: wavelink.Playable) -> str:
    uri = getattr(track, "uri", None) or getattr(track, "url", None)
    if uri:
        return f"[{track.title}]({uri})"
    return str(track.title)


def _track_url(track: wavelink.Playable) -> Optional[str]:
    return getattr(track, "uri", None) or getattr(track, "url", None)


def _track_duration_ms(track: wavelink.Playable) -> Optional[int]:
    # wavelink zwykle trzyma długość w ms jako `length`
    length = getattr(track, "length", None)
    return int(length) if isinstance(length, (int, float)) and length > 0 else None

# ==========================
# RUN BOT
# ==========================
bot.run(TOKEN)
