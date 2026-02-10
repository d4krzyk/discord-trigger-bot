from __future__ import annotations

import os
import json
import asyncio
from collections import deque
from typing import Optional, NoReturn

import discord
from discord.ext import commands
import wavelink
from discord import app_commands

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

# Slash commands: dla jednego serwera najlepiej użyć guild sync (pojawia się od razu).
# Możesz nadpisać to zmienną środowiskową GUILD_ID na Render.
GUILD_ID = int(os.environ.get("GUILD_ID", "1470577436335931584"))

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
# Jeżeli bot ma czytać komendy prefixowe z treści wiadomości, to w części przypadków
# trzeba mieć message_content. Na Render/produkcyjnie lepiej włączyć to jawnie.
if os.environ.get("ENABLE_MESSAGE_CONTENT_INTENT", "1") == "1":
    intents.message_content = True

# Wyłączamy wbudowaną komendę `help`, bo mamy własną.
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Tree dla slash commands
_tree = bot.tree

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
    try:
        player = await channel.connect(cls=wavelink.Player)
        return player
    except Exception as e:
        print(f"Nie udało się połączyć z VC: {e}")
        raise


async def ensure_connected(ctx: commands.Context) -> Optional[wavelink.Player]:
    if not VC_CHANNEL_ID:
        await _safe_send(ctx, embed=_music_embed("Konfiguracja", "**Nie ustawiono kanału VC.**\nUżyj: `!set_vc <kanał>`"))
        return None

    vc_channel = ctx.guild.get_channel(VC_CHANNEL_ID)
    if not isinstance(vc_channel, discord.VoiceChannel):
        await _safe_send(
            ctx,
            embed=_music_embed(
                "Konfiguracja",
                "**Ustawiony kanał VC jest nieprawidłowy.**\nUstaw ponownie: `!set_vc <kanał>`",
            ),
        )
        return None

    player = await _get_player(ctx.guild)
    if player is None:
        player = await join_vc(vc_channel)

    return player


async def leave_vc_if_empty(channel: discord.VoiceChannel):
    humans = _real_users(channel)
    if len(humans) == 0:
        try:
            player = await _get_player(channel.guild)
            if player:
                await player.disconnect()
        except Exception as e:
            print(f"Błąd disconnect: {e}")
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

    try:
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
    except Exception as e:
        print(f"Błąd play_next/play: {e}")
        # jeśli coś poszło nie tak, spróbuj przejść dalej (bez pętli)
        try:
            if queue:
                current_track = None
                await play_next(guild)
        except Exception:
            pass


async def _search_track(query: str) -> Optional[wavelink.Playable]:
    q = query.strip()
    if not q:
        return None

    # Wavelink v2+ – uniwersalne wyszukiwanie.
    try:
        results = await wavelink.Playable.search(q)
    except Exception as e:
        # To jest najczęstsze miejsce problemów (brak node, błąd Lavalink, brak source).
        print(f"Błąd Playable.search dla '{q}': {type(e).__name__}: {e}")
        return None

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
async def _connect_lavalink():
    """Łączy się z Lavalink w sposób kompatybilny z różnymi wersjami Wavelink."""
    host = os.environ.get("LAVALINK_HOST")
    password = os.environ.get("LAVALINK_PASSWORD")
    port = int(os.environ.get("LAVALINK_PORT", "2333"))
    use_https = os.environ.get("LAVALINK_HTTPS", "0") == "1"

    if not host or not password:
        print("Brak LAVALINK_HOST lub LAVALINK_PASSWORD – muzyka nie będzie działać.")
        return

    # Wavelink v3+: Pool.connect
    try:
        Pool = getattr(wavelink, "Pool", None)
        if Pool is not None:
            # jeśli już istnieją nody, nie łącz ponownie
            nodes = getattr(Pool, "nodes", None)
            if isinstance(nodes, dict) and nodes:
                return
            if isinstance(nodes, list) and nodes:
                return

            node = wavelink.Node(uri=f"{'https' if use_https else 'http'}://{host}:{port}", password=password)
            await Pool.connect(client=bot, nodes=[node])
            return
    except Exception as e:
        print(f"Nie udało się połączyć z Lavalink przez Pool.connect: {e}")

    # Wavelink v2: NodePool.create_node
    try:
        NodePool = getattr(wavelink, "NodePool", None)
        if NodePool is not None:
            nodes = getattr(NodePool, "nodes", None)
            if nodes:
                return

            await NodePool.create_node(
                bot=bot,
                host=host,
                port=port,
                password=password,
                https=use_https,
            )
            return
    except Exception as e:
        print(f"Nie udało się połączyć z Lavalink przez NodePool.create_node: {e}")

    print("Nie znaleziono kompatybilnego API Wavelink do połączenia z Lavalink.")


@bot.event
async def on_ready():
    print(f"Zalogowany jako {bot.user}")

    # Web server dla Render (tło)
    if os.environ.get("RUN_WEB", "1") == "1":
        bot.loop.create_task(_run_web_server())

    await _connect_lavalink()

    load_playlists()

    # Sync robimy w setup_hook() (żeby /komendy pojawiały się poprawnie)

    print("Bot gotowy")

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
@bot.command(name="play", aliases=["p", "add"])
@role_only()
async def play(ctx, *, query: str = ""):
    """Dodaje utwór do kolejki (URL lub fraza) i startuje odtwarzanie."""
    query = (query or "").strip()
    if not query:
        e = _music_embed(
            "Play",
            "**Musisz podać frazę albo link.**\n\n"
            "Przykłady:\n"
            "• `!play dark ambient`\n"
            "• `!p lofi hip hop`\n"
            "• `!play https://youtu.be/...`",
        )
        return await _safe_send(ctx, embed=e)

    player = await ensure_connected(ctx)
    if not player:
        return

    try:
        track = await _search_track(query)
    except Exception as e:
        print(f"Błąd w !play (search) dla '{query}': {type(e).__name__}: {e}")
        return await _safe_send(ctx, embed=_music_embed("Błąd", "Nie udało się wyszukać utworu (błąd po stronie Lavalink/Wavelink)."))

    if not track:
        await _safe_send(ctx, embed=_music_embed("Szukaj", f"**Nie znaleziono utworu** dla: `{query}`"))
        return

    try:
        await enqueue_and_maybe_play(ctx, player, track)
    except Exception as e:
        print(f"Błąd w !play (enqueue/play) dla '{query}': {type(e).__name__}: {e}")
        await _safe_send(ctx, embed=_music_embed("Błąd", "Nie udało się dodać/odtworzyć utworu."))


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
@bot.command(name="playlist_create", aliases=["pl_create"])
@role_only()
async def playlist_create(ctx, *, name: str):
    name = (name or "").strip()
    if not name:
        return await _safe_send(
            ctx,
            embed=_music_embed(
                "Playlisty",
                "**Musisz podać nazwę playlisty.**\nPrzykład: `!playlist_create dark ambient`",
            ),
        )
    if name in playlists:
        return await _safe_send(ctx, embed=_music_embed("Playlisty", f"Playlista **{name}** już istnieje."))
    playlists[name] = []
    save_playlists()
    await _safe_send(ctx, embed=_music_embed("Playlisty", f"Utworzono playlistę: **{name}**"))


@bot.command(name="playlist_list")
@role_only()
async def playlist_list(ctx):
    if not playlists:
        return await ctx.send(embed=_music_embed("Playlisty", "Brak playlist."))

    e = _music_embed("Playlisty")
    e.description = "\n".join(f"• **{name}** ({len(items)} pozycji)" for name, items in sorted(playlists.items()))
    await ctx.send(embed=e)


@bot.command(name="playlist_add", aliases=["pl_add"])
@role_only()
async def playlist_add(ctx, playlist_name: str, *, query: str):
    playlist_name = (playlist_name or "").strip()
    query = (query or "").strip()

    if not playlist_name or not query:
        return await _safe_send(
            ctx,
            embed=_music_embed(
                "Playlisty",
                "**Musisz podać nazwę playlisty i frazę/link do dodania.**\n"
                "Przykład: `!playlist_add moja_playlista dark ambient`",
            ),
        )

    if playlist_name not in playlists:
        return await _safe_send(ctx, embed=_music_embed("Playlisty", "**Nie znaleziono takiej playlisty.**"))

    playlists[playlist_name].append(query)
    save_playlists()
    await _safe_send(ctx, embed=_music_embed("Playlisty", f"Dodano do **{playlist_name}**:\n`{query}`"))


@bot.command(name="playlist_remove", aliases=["pl_remove", "pl_del"])
@role_only()
async def playlist_remove(ctx, playlist_name: str = None, *, query: str = None):
    playlist_name = (playlist_name or "").strip()
    query = (query or "").strip()

    if not playlist_name or not query:
        return await _safe_send(
            ctx,
            embed=_music_embed(
                "Playlisty",
                "**Musisz podać nazwę playlisty i wpis do usunięcia.**\n"
                "Przykład: `!playlist_remove moja_playlista dark ambient`",
            ),
        )

    if playlist_name not in playlists:
        return await _safe_send(ctx, embed=_music_embed("Playlisty", "**Nie znaleziono takiej playlisty.**"))

    # Usuń pierwsze pasujące wystąpienie (case-insensitive), żeby UX był lepszy.
    items = playlists[playlist_name]
    idx = next((i for i, it in enumerate(items) if it.lower() == query.lower()), None)
    if idx is None:
        return await _safe_send(ctx, embed=_music_embed("Playlisty", "**Ten wpis nie istnieje w playlistie.**"))

    removed = items.pop(idx)
    save_playlists()
    await _safe_send(ctx, embed=_music_embed("Playlisty", f"Usunięto z **{playlist_name}**:\n`{removed}`"))


@bot.command(name="playlist_show", aliases=["pl_show"])
@role_only()
async def playlist_show(ctx, *, playlist_name: str):
    playlist_name = (playlist_name or "").strip()
    if not playlist_name:
        return await _safe_send(ctx, embed=_music_embed("Playlisty", "**Musisz podać nazwę playlisty.**"))

    if playlist_name not in playlists:
        return await _safe_send(ctx, embed=_music_embed("Playlista", "Nie znaleziono takiej playlisty."))

    items = playlists[playlist_name]
    e = _music_embed(f"Playlista: {playlist_name}")

    if not items:
        e.description = "Playlista jest pusta."
        return await _safe_send(ctx, embed=e)

    preview = "\n".join(f"{i+1}. {q}" for i, q in enumerate(items[:15]))
    if len(items) > 15:
        preview += f"\n… (+{len(items)-15} więcej)"

    e.description = preview
    await _safe_send(ctx, embed=e)


@bot.command(name="playlist_play", aliases=["pl_play", "pl"])
@role_only()
async def playlist_play(ctx, *, playlist_name: str):
    playlist_name = (playlist_name or "").strip()
    if not playlist_name:
        return await _safe_send(
            ctx,
            embed=_music_embed(
                "Playlisty",
                "**Musisz podać nazwę playlisty.**\nPrzykład: `!playlist_play moja_playlista`",
            ),
        )

    if playlist_name not in playlists:
        return await _safe_send(ctx, embed=_music_embed("Playlista", "Nie znaleziono takiej playlisty."))

    player = await ensure_connected(ctx)
    if not player:
        return

    items = playlists[playlist_name]
    if not items:
        return await _safe_send(ctx, embed=_music_embed("Playlista", "Playlista jest pusta."))

    added = 0
    for q in items:
        track = await _search_track(q)
        if track:
            queue.append(track)
            added += 1

    e = _music_embed(f"Dodano playlistę: {playlist_name}", f"Dodano do kolejki: **{added}**/**{len(items)}**")
    e.add_field(name="Kolejka", value=str(len(queue)), inline=True)
    await _safe_send(ctx, embed=e)

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
# SYNC COMMANDS
# ==========================
async def _sync_app_commands():
    """Synchronizuje slash commands.

    Jeśli GUILD_ID jest ustawione, synchronizuje tylko dla tego serwera (natychmiastowe).
    W przeciwnym razie robi global sync (może propagować się dłużej).
    """
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            synced = await _tree.sync(guild=guild)
            print(f"Zsynchronizowano slash commands dla guild={GUILD_ID}: {len(synced)}")
        else:
            synced = await _tree.sync()
            print(f"Zsynchronizowano slash commands globalnie: {len(synced)}")
    except Exception as e:
        print(f"Nie udało się zsynchronizować slash commands: {e}")


@bot.event
async def setup_hook():
    """Wywoływane raz przy starcie. Najlepsze miejsce na sync slash commands."""
    await _sync_app_commands()

# ==========================
# SLASH COMMANDS (podpowiedzi w Discord)
# ==========================

async def _autocomplete_loop_mode(interaction: discord.Interaction, current: str):
    choices = [
        app_commands.Choice(name="off", value="off"),
        app_commands.Choice(name="song", value="song"),
        app_commands.Choice(name="queue", value="queue"),
    ]
    cur = (current or "").lower()
    return [c for c in choices if cur in c.name][:25]


async def _autocomplete_playlists(interaction: discord.Interaction, current: str):
    cur = (current or "").lower()
    names = sorted(playlists.keys())
    filtered = [n for n in names if cur in n.lower()]
    return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]


@_tree.command(name="help", description="Pokazuje listę komend i co robią")
async def slash_help(interaction: discord.Interaction):
    e = _music_embed("Pomoc • Komendy bota")

    e.add_field(
        name="Konfiguracja",
        value=(
            "**Prefix:** `!`  •  **Slash:** `/`\n"
            "• `!set_vc <kanał>` — ustaw kanał głosowy\n"
            "• `!set_text <kanał>` — ustaw kanał tekstowy (opcjonalnie)\n"
            "• `!set_role <rola>` — ustaw rolę uprawnioną\n"
        ),
        inline=False,
    )

    e.add_field(
        name="Muzyka",
        value=(
            "• `/play query` lub `!play <query>` — dodaj do kolejki\n"
            "• `/pause` / `/resume` lub `!pause` / `!resume`\n"
            "• `/skip` lub `!skip` — pomiń utwór\n"
            "• `/stop` lub `!stop` — zatrzymaj i wyczyść kolejkę\n"
            "\n**Przykład:** `/play never gonna give you up`"
        ),
        inline=False,
    )

    e.add_field(
        name="Kolejka i teraz gra",
        value=(
            "• `/now` lub `!now` — co aktualnie gra\n"
            "• `/queue` lub `!queue_show` — podgląd kolejki\n"
        ),
        inline=False,
    )

    e.add_field(
        name="Loop (zapętlanie)",
        value=(
            "• `/loop mode` lub `!loop <mode>`\n"
            "  Dostępne: **off**, **song**, **queue**\n"
            "• `!loop_status` — aktualny tryb\n"
            "\n**Przykład:** `/loop song`"
        ),
        inline=False,
    )

    e.add_field(
        name="Playlisty",
        value=(
            "• `/playlist_list` lub `!playlist_list` — lista playlist\n"
            "• `/playlist_show name` lub `!playlist_show <name>`\n"
            "• `/playlist_play name` lub `!playlist_play <name>` — dodaj playlistę do kolejki\n"
            "\n**Zarządzanie (prefix):**\n"
            "• `!playlist_create <name>` — utwórz\n"
            "• `!playlist_add <name> <query>` — dodaj wpis\n"
            "• `!playlist_remove <name> <query>` — usuń wpis"
        ),
        inline=False,
    )

    e.set_footer(text="Wskazówka: komendy slash (/) mają podpowiedzi i autouzupełnianie.")

    await interaction.response.send_message(embed=e, ephemeral=True)


# ==========================
# SAFETY / ERROR HANDLING
# ==========================
async def _safe_send(ctx_or_interaction, *, content: Optional[str] = None, embed: Optional[discord.Embed] = None, ephemeral: bool = False):
    """Bezpieczne wysyłanie wiadomości (nie wywala bota, jeśli np. brak uprawnień)."""
    try:
        if isinstance(ctx_or_interaction, discord.Interaction):
            if ctx_or_interaction.response.is_done():
                return await ctx_or_interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
            return await ctx_or_interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
        return await ctx_or_interaction.send(content=content, embed=embed)
    except Exception as e:
        print(f"Nie udało się wysłać wiadomości: {e}")
        return None


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    """Globalny handler błędów dla komend prefixowych (!)."""
    try:
        if isinstance(error, commands.CheckFailure):
            return  # cicho
        if isinstance(error, commands.MissingRequiredArgument):
            return await _safe_send(ctx, embed=_music_embed("Błąd", "Brak argumentu komendy."))
        if isinstance(error, commands.BadArgument):
            return await _safe_send(ctx, embed=_music_embed("Błąd", "Niepoprawny argument."))
        if isinstance(error, commands.CommandNotFound):
            return  # cicho

        print(f"Błąd komendy {getattr(ctx.command, 'qualified_name', '?')}: {error}")
        await _safe_send(ctx, embed=_music_embed("Błąd", "Coś poszło nie tak przy wykonywaniu komendy."))
    except Exception as e:
        print(f"Błąd on_command_error: {e}")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Globalny handler błędów dla slash commands (/)."""
    try:
        # najczęstsze przypadki
        if isinstance(error, app_commands.CheckFailure):
            return
        print(f"Błąd slash command: {error}")
        await _safe_send(interaction, embed=_music_embed("Błąd", "Coś poszło nie tak przy wykonywaniu komendy."), ephemeral=True)
    except Exception as e:
        print(f"Błąd on_app_command_error: {e}")


# ==========================
# HELP COMMAND (prefix)
# ==========================
@bot.command(name="help")
async def prefix_help(ctx: commands.Context):
    """Pomoc dla komend ! (slash jest zalecany)."""
    e = _music_embed("Pomoc • Komendy bota")

    e.add_field(
        name="Najlepsza opcja",
        value="Użyj **`/help`** — tam masz podpowiedzi i autouzupełnianie komend.",
        inline=False,
    )

    e.add_field(
        name="Szybki skrót (prefix)",
        value=(
            "• `!play <query>` — dodaj utwór\n"
            "• `!now` — co gra\n"
            "• `!queue_show` — kolejka\n"
            "• `!pause` / `!resume` / `!skip` / `!stop`\n"
            "• `!loop off|song|queue`\n"
            "• `!playlist_list` / `!playlist_show <name>` / `!playlist_play <name>`"
        ),
        inline=False,
    )

    await _safe_send(ctx, embed=e)


# ==========================
# RUN BOT
# ==========================
# (musi być na samym końcu pliku, po definicjach komend)
bot.run(TOKEN)
