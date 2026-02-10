# discord-trigger-bot (Render Web Service)

Ten projekt uruchamia bota Discord (muzyka przez Lavalink/Wavelink) jako **Render Web Service**.
Render wymaga, żeby proces nasłuchiwał na porcie HTTP — dlatego w `bot.py` jest prosty serwer Flask (`/` i `/health`).

## TL;DR (co musisz mieć na Render)

Potrzebujesz **dwóch usług**:

1) **Bot** (ten repo) jako Render **Web Service**
2) **Lavalink** jako osobny serwis (najwygodniej: Render **Web Service** / **Private Service** z Dockera)

Bot łączy się do Lavalinka po `LAVALINK_HOST/PORT/PASSWORD`.

## Wymagania

- Python (zgodnie z `runtime.txt`)
- Bot Discord z tokenem
- Działający serwer **Lavalink** (osobno)

> Render **nie** dostarcza Lavalinka automatycznie. Najczęstszy układ to:
> - osobny Render Service (Docker) z Lavalink **albo**
> - Lavalink gdzieś indziej (VPS)

## Konfiguracja Render (BOT)

### 1) Utwórz Web Service

- Build Command:
  - `pip install -r requirements.txt`
- Start Command:
  - `python bot.py`

### 2) Ustaw zmienne środowiskowe (Environment)

W Render → `Environment` dodaj:

- `DISCORD_TOKEN` — token Twojego bota
- `LAVALINK_HOST` — host Lavalinka (np. domena Lavalink service)
- `LAVALINK_PORT` — zwykle `2333`
- `LAVALINK_PASSWORD` — hasło z konfiguracji Lavalinka

Opcjonalnie:

- `PORT` — Render ustawia sam (HTTP)
- `RUN_WEB=1` — domyślnie włączone (serwer HTTP)
- `IDLE_DISCONNECT_SECONDS=300` — po ilu sekundach bezczynności bot ma się rozłączyć (0 wyłącza)
- `ENABLE_MESSAGE_CONTENT_INTENT=1` — jeśli używasz komend prefixowych (`!play` itd.), to warto mieć to włączone

### 3) Discord Developer Portal → Intents

Jeśli komendy `!` nie działają, włącz:

- **MESSAGE CONTENT INTENT**

W kodzie jest to sterowane `ENABLE_MESSAGE_CONTENT_INTENT`.

## Konfiguracja Render (LAVALINK)

### Opcja A (polecana): Lavalink jako osobny serwis Docker na Render

1. Utwórz w Render nowy serwis typu **Web Service** (albo **Private Service**, jeśli masz taki plan).
2. Źródło: osobne repo z Lavalink + `Dockerfile` **albo** gotowy obraz (jeśli korzystasz z własnego Dockera).

**Minimalny Dockerfile dla Lavalink (przykład):**

- Użyj oficjalnego obrazu Lavalink (lub zaufanego, aktualnego obrazu).
- Wystaw port `2333`.
- Podepnij plik `application.yml`.

> Render nie ma “one click” Lavalinka w tym repo — Lavalink to osobna aplikacja.

**application.yml (ważne elementy):**

- `server.port: 2333`
- `lavalink.server.password: <twoje_haslo>`

W Render ustaw env dla Lavalinka (jeśli konfigurujesz przez env) albo wrzuć `application.yml` do repo.

3. Po deployu sprawdź, czy Lavalink odpowiada po HTTP:

- `http(s)://<twoj-lavalink-host>/version`

Powinno zwrócić wersję.

4. W serwisie bota ustaw:

- `LAVALINK_HOST=<twoj-lavalink-host-bez-http>`
- `LAVALINK_PORT=2333`
- `LAVALINK_PASSWORD=<twoje_haslo>`

### Opcja B: Lavalink poza Render (VPS)

Jeśli masz VPS, to często jest prościej i taniej postawić Lavalink tam.
Wtedy w bocie ustawiasz `LAVALINK_HOST` na IP/domenę VPS.

## Checklista: gdy muzyka nie działa

1) Czy bot wystartował na Render? (logi bota)
- powinno być: "Bot gotowy i połączony z Lavalink"

2) Czy Lavalink działa i jest dostępny z internetu?
- wejdź w przeglądarce na: `https://<host>/version`

3) Czy hasło się zgadza?
- `LAVALINK_PASSWORD` w bocie musi być identyczne jak w Lavalink

4) Czy bot ma uprawnienia na kanale VC?
- Connect
- Speak

## Komendy bota

Konfiguracja:

- `!set_vc <kanał>` — ustaw kanał głosowy
- `!set_text <kanał>` — ustaw kanał tekstowy dla komend
- `!set_role <rola>` — ustaw rolę uprawnioną

Muzyka:

- `!play <url/fraza>`
- `!pause`, `!resume`, `!skip`, `!stop`
- `!now`, `!queue_show`

Loop:

- `!loop off|song|queue`
- `!loop_status`

Playlisty:

- `!playlist_create <nazwa>`
- `!playlist_list`
- `!playlist_add <nazwa> <url/fraza>`
- `!playlist_remove <nazwa> <url/fraza>`
- `!playlist_show <nazwa>`
- `!playlist_play <nazwa>`

## Healthcheck

Render może pingować HTTP:

- `/health` — zwraca `{"ok": true}`

## Najczęstsze problemy

### Bot nie łączy się z VC

- upewnij się, że `!set_vc` zostało wykonane
- bot musi mieć uprawnienia do:
  - Connect
  - Speak

### Lavalink unreachable / brak muzyki

- sprawdź `LAVALINK_HOST/PORT/PASSWORD`
- sprawdź, czy Lavalink jest dostępny z internetu i czy port jest otwarty

## Lavalink na Render: "Authorization missing" w logach

Jeśli w logach Lavalinka widzisz wpisy typu:

- `Authorization missing for ... on GET /` lub `HEAD /`

To zazwyczaj są **pingi/healthchecki Render** albo skanery, które wchodzą na `/` **bez nagłówka `Authorization`**.
To jest normalne i nie oznacza, że Lavalink nie działa.

### Jak poprawnie testować Lavalink

Lavalink wymaga autoryzacji hasłem. Żeby sprawdzić wersję w przeglądarce/CLI, użyj endpointu `/version` z nagłówkiem `Authorization`.

Przykład (PowerShell):

```powershell
$headers = @{ Authorization = "TWOJE_HASLO" }
Invoke-WebRequest -Uri "http://TWOJ_HOST:2333/version" -Headers $headers
```

### Ustawienia bota

Jeśli Lavalink działa po zwykłym HTTP (w logach masz `Undertow started on port 2333 (http)`), to:

- ustaw `LAVALINK_HTTPS=0` (albo nie ustawiaj wcale)

Jeśli masz reverse proxy i HTTPS, ustaw:

- `LAVALINK_HTTPS=1`

## Wersja Pythona (ważne na Render)

Używaj **Python 3.12** (ustawione w `runtime.txt`).

> Na Python 3.13 możesz dostać błąd `ModuleNotFoundError: No module named 'audioop'`, bo `audioop` został usunięty ze standardowej biblioteki, a `discord.py` nadal go używa.
