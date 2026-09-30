# 🔥 ViralClip AI

**Vindt automatisch de momenten met het hoogste viral potential in lange YouTube-video's en zet ze klaar als verticale TikTok/Reels/Shorts-clips (9:16, captions, 12–18 sec).**

Je voegt creators toe (bijv. Enzo Knol, Bankzitters, Hanwe, Gio, StukTV — of elk ander kanaal). ViralClip AI controleert elke 2 uur op nieuwe uploads, filtert ze op metadata, analyseert transcript + audio, laat een AI denken als *een TikTok-kijker die aan het scrollen is*, rankt de momenten met een eigen **Viral Score (0–100)**, verwijdert dubbele momenten, bepaalt hook-first start- en eindpunten, rendert een verticale clip met captions en zet alles in een dashboard met preview, uitleg en downloadknop. Jouw feedback (🔥 / 👍 / 👎 / ❌) en echte TikTok-statistieken trainen een persoonlijk model dat de score bijstuurt.

> ⚠️ De Viral Score is een **voorspelling** op basis van kenmerken — nooit een garantie. De UI spreekt daarom van *"High viral potential"* en *"Viral Score 94/100"*, nooit van *"deze video gaat viral"*.

---

## Inhoud

1. [Wat het project doet](#1-wat-het-project-doet)
2. [Architectuur](#2-architectuur)
3. [Installatie](#3-installatie)
4. [Dependencies](#4-dependencies)
5. [API keys](#5-api-keys)
6. [Database setup](#6-database-setup)
7. [Environment variables](#7-environment-variables)
8. [Lokale development](#8-lokale-development)
9. [YouTube API setup](#9-youtube-api-setup)
10. [AI API setup & kosten](#10-ai-api-setup--kosten)
11. [Video processing](#11-video-processing)
12. [Deployment](#12-deployment)
13. [Troubleshooting](#13-troubleshooting)
14. [Kritische ontwerpkeuzes](#14-kritische-ontwerpkeuzes-waar-ik-afweek-van-het-oorspronkelijke-plan)
15. [Juridisch](#15-juridisch-youtube-terms-of-service)
16. [Roadmap](#16-roadmap)

---

## 1. Wat het project doet

| Functie | Status |
|---|---|
| Creators zoeken & toevoegen (naam, @handle of kanaal-URL) | ✅ |
| Automatische scan op nieuwe uploads (standaard elke 2 uur) | ✅ |
| Metadata-filters: periode, aantal video's, min/max lengte, views, Shorts/live, titelwoorden | ✅ |
| Prioriteit per video (over-performance vs. kanaalgemiddelde, engagement, recency, creator-prioriteit) | ✅ |
| **Audience hotspots**: tijdstempels die kijkers in YouTube-comments noemen ("3:42 😂") | ✅ |
| Transcriptie met woord-timestamps (Whisper API, lokaal faster-whisper, of SRT/VTT-upload) | ✅ |
| Audio-signalen: volume-pieken, reacties/gelach, stiltes, spreektempo; camerawissels | ✅ |
| 10-staps selectie-algoritme met 12 scoringsdimensies en funnel-score | ✅ |
| Hook-first clipgrenzen, 12–18 s, dode lucht eruit (jump cuts) | ✅ |
| Duplicate-detectie (tijd-overlap + semantische gelijkenis, MMR) | ✅ |
| 9:16 reframing: gezicht volgen, actieve spreker, split-screen, blur-fit | ✅ |
| Captions: 3 presets (Dynamic TikTok-stijl, grote witte, minimalistisch), woord-gesynchroniseerd, niet over gezichten | ✅ |
| Dashboard, Creators, Videos, Analysis Queue, Clips, Analytics, Settings | ✅ |
| Feedback + performance tracking + persoonlijk leermodel met inzichten | ✅ |
| Kosten- en quota-tracking per analyse en per dag | ✅ |
| Optionele vision-analyse van alleen de beste kandidaten | ✅ (beta, standaard uit) |
| Werkt zonder API keys (lokale heuristiek + demo) | ✅ |

**Dagelijkse workflow:** creators toevoegen → ViralClip vindt nieuwe video's → jij levert (automatisch via de inbox-map) het bronbestand aan → je opent het Dashboard en ziet *"🔥 18 new potential viral clips"* → bekijken, 🔥/👍/👎 geven, **Download Clip**, posten.

## 2. Architectuur

```mermaid
flowchart LR
  subgraph Browser
    UI[Next.js dashboard]
  end
  UI -- same-origin /api/* --> PX[Next.js route handler<br/>proxy + auth + streaming]
  PX --> API[FastAPI]
  API <--> DB[(PostgreSQL / SQLite)]
  API <--> ST[(Storage: lokaal of S3/R2/Supabase)]
  W[Worker] <--> DB
  W <--> ST
  W -- RSS / Data API v3 --> YT[YouTube]
  W -- pass 1 + 2 --> LLM[Claude / OpenAI / heuristiek]
  W -- STT --> STT[Whisper API / faster-whisper]
  W -- ffmpeg + OpenCV --> R[Render 9:16 + captions]
  INBOX[/data/inbox/] --> W
```

**De analysepipeline** (`backend/app/ai/pipeline.py`):

```
YouTube-video (metadata + comments via officiële API)
 → bronbestand (upload / inbox) → transcript met woord-timestamps
 → zin-segmentatie + audio-luidheid + camerawissels + comment-hotspots        (lokaal, gratis)
 → Stage 1  kandidaten: signaal-vensters  +  LLM pass 1 (snel model)          → 30–50 momenten
 → Stage 2–7 LLM pass 2 (slim model): kijkersimulatie, 12 dimensies, vlaggen, verdict, betere edit
 → Stage 10 hook-first grenzen, context-reparatie, payoff-bewust einde, dode lucht eruit
 → Stage 9  Viral Score = funnel (Stop × Hold × Engage) + signalen + crowd + persoonlijk model
 → (optioneel) vision-check van de top N
 → Stage 8  duplicate-detectie + MMR-diversiteit → Top 5
 → render: 9:16 reframing + captions + loudness → mp4 + thumbnail
```

Het algoritme is in detail uitgelegd in **[docs/ALGORITHM.md](docs/ALGORITHM.md)**.

**Repositorystructuur**

```
viral-clip-ai/
├── backend/                  Python 3.11+ · FastAPI · SQLAlchemy 2 · Alembic
│   ├── app/
│   │   ├── ai/               transcriptie, signalen, kandidaten, prompts, LLM-providers, scoring,
│   │   │                     evaluator, boundaries, dedupe, vision, pipeline
│   │   ├── video/            ffmpeg-wrappers, audio-features, scènedetectie, reframing, captions, render
│   │   ├── services/         YouTube, discovery, queue, storage, media/inbox, settings, learning, usage
│   │   ├── api/              REST-endpoints
│   │   ├── worker/           job-handlers + worker-loop (scheduler, inbox, recovery)
│   │   ├── models.py         database-schema
│   │   └── demo.py           demo-data zonder API keys
│   ├── alembic/              migraties (SQLite + PostgreSQL)
│   └── tests/                85 tests (unit, API, end-to-end met echte ffmpeg-render)
├── frontend/                 Next.js 16 · React 19 · Tailwind 4 · TypeScript
│   ├── app/                  pagina's + /api proxy-route
│   ├── components/ lib/
│   └── proxy.ts              optionele Basic-auth voor het dashboard
├── docs/ALGORITHM.md         uitleg selectie-algoritme & scoring
├── docker-compose.yml        Postgres + API + worker + dashboard
├── .env.example
├── Makefile · scripts/dev.sh
└── .github/workflows/ci.yml
```

## 3. Installatie

### Snelste route: Docker (aanbevolen)

Vereist: [Docker Desktop](https://www.docker.com/products/docker-desktop/) of Docker Engine + Compose v2.

```bash
git clone https://github.com/karstenhiemstra/viral-clip-ai.git
cd viral-clip-ai
cp .env.example .env          # vul (minimaal) YOUTUBE_API_KEY en OPENAI_API_KEY of ANTHROPIC_API_KEY in
docker compose up -d --build  # of: make up
```

Open **http://localhost:3000**. Keys kun je ook later invullen op de Settings-pagina.

### Zonder Docker

Vereist: Python 3.11+, Node.js 20.9+ (22 aanbevolen), **ffmpeg** (met libass — standaard in de meeste builds).

```bash
# macOS: brew install ffmpeg python@3.12 node
# Ubuntu/Debian: sudo apt install ffmpeg python3-venv nodejs npm fonts-montserrat
make setup     # venv + pip install + npm install + .env aanmaken
make demo      # optioneel: demo-video + clips zonder API keys
make dev       # API (8000) + worker + dashboard (3000)
```

## 4. Dependencies

**Backend** (`backend/pyproject.toml`): FastAPI, Uvicorn, SQLAlchemy 2, Alembic, Pydantic 2, httpx, NumPy, OpenCV (headless, 4.x — bevat de Haar-gezichtsdetector), cryptography (versleutelde API keys), OpenAI SDK, Anthropic SDK, psycopg 3. Optioneel: `boto3` (S3), `faster-whisper` (gratis lokale transcriptie).

**Systeem:** ffmpeg + ffprobe (knippen, audio-analyse, scènedetectie, rendering, captions via libass). Fonts: Montserrat (Debian/Ubuntu `fonts-montserrat`; valt anders terug op een systeemfont).

**Frontend** (`frontend/package.json`): Next.js 16, React 19, Tailwind CSS 4, SWR, lucide-react, Inter (self-hosted via @fontsource). Geen chart-library: de grafieken zijn lichte SVG-componenten.

## 5. API keys

| Key | Waarvoor | Verplicht? |
|---|---|---|
| `YOUTUBE_API_KEY` | Creators zoeken op naam, video-metadata (duur, views), comment-hotspots | Aanbevolen. Zonder key: kanaal-URL met `/channel/UC…` plakken; scannen via RSS werkt wel |
| `OPENAI_API_KEY` | Whisper-transcriptie (en/of GPT-scoring, embeddings) | Nodig voor automatische transcriptie, tenzij je faster-whisper installeert of SRT/VTT aanlevert |
| `ANTHROPIC_API_KEY` | Claude-scoring (pass 1 + 2) | Optioneel (OpenAI kan ook). Zonder LLM-key: lokale heuristiek |

Keys kunnen in `.env` óf via **Settings → API keys** (versleuteld met `APP_SECRET_KEY` opgeslagen, nooit teruggestuurd naar de browser). Een key uit Settings wint van `.env`.

## 6. Database setup

- **Lokaal (standaard):** SQLite in `data/viralclip.db` — niets te doen.
- **Docker Compose:** PostgreSQL 16 draait mee; `DATABASE_URL` wordt automatisch gezet.
- **Supabase / eigen Postgres:** zet `DATABASE_URL=postgresql://user:wachtwoord@host:5432/postgres` (Supabase: *Project Settings → Database → Connection string*, gebruik de *session pooler* of directe verbinding).

Migraties draaien automatisch bij het starten van de API. Handmatig: `make migrate` (of `python -m app.migrate` in `backend/`). Nieuwe migratie na een modelwijziging: `cd backend && alembic revision --autogenerate -m "..."`.

## 7. Environment variables

Alle variabelen staan met uitleg in [`.env.example`](.env.example). De belangrijkste:

| Variabele | Standaard | Uitleg |
|---|---|---|
| `YOUTUBE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | — | zie §5 |
| `OPENAI_BASE_URL` | — | OpenAI-compatibele endpoint (OpenRouter, lokale Ollama/vLLM) |
| `LLM_PROVIDER` | `auto` | `auto` / `openai` / `anthropic` / `heuristic` |
| `LLM_MODEL_FAST` / `LLM_MODEL_SMART` | provider-default | modellen voor pass 1 / pass 2 |
| `TRANSCRIBER` | `auto` | `openai` / `faster_whisper` / `none` |
| `DATABASE_URL` | SQLite | zie §6 |
| `STORAGE_BACKEND` | `local` | `local` of `s3` (+ `S3_*` variabelen) |
| `APP_SECRET_KEY` | automatisch | versleutelt API keys uit het dashboard |
| `API_AUTH_TOKEN` | — | bearer-token tussen dashboard en API (productie) |
| `DASHBOARD_PASSWORD` / `DASHBOARD_USER` | — / `admin` | Basic-auth op het hele dashboard (productie) |
| `APP_TIMEZONE` | `Europe/Amsterdam` | voor de filter "vandaag" |

Alles wat je dagelijks wilt tweaken (gewichten, clipduur, captions, filters, interval, modellen) staat op de **Settings-pagina** en wordt in de database bewaard.

## 8. Lokale development

```bash
make dev        # API met auto-reload, worker en Next.js dev-server
make api | make worker | make web   # los starten
make test       # ruff + pytest + eslint + next build
make demo       # demo-data
```

- API-documentatie (OpenAPI): http://localhost:8000/docs
- Worker eenmalig de wachtrij laten leegdraaien: `python -m app.worker.runner --once`
- Tests tegen PostgreSQL: `TEST_DATABASE_URL=postgresql://… pytest`

## 9. YouTube API setup

1. Ga naar [console.cloud.google.com](https://console.cloud.google.com/) → nieuw project.
2. *APIs & Services → Library* → **YouTube Data API v3** → *Enable*.
3. *APIs & Services → Credentials* → *Create credentials → API key*. Beperk de key tot de YouTube Data API.
4. Zet de key in `.env` of in **Settings** en klik **Test**.

**Quota (10.000 units/dag gratis) — zo zuinig gebruikt ViralClip ze:**

| Actie | Kosten |
|---|---|
| Nieuwe uploads checken (publieke RSS-feed) | **0** |
| Creator toevoegen via @handle of URL | 1 |
| Creator zoeken op naam (`search.list`) | 100 |
| Metadata van nieuwe video's (`videos.list`, 50 per call) | 1 |
| Top-comments voor hotspots (`commentThreads.list`) | 1 per video |
| Kanaalstatistieken verversen | 1 per scan |

Met 20 creators en elke 2 uur een scan blijf je ruim onder de 1.000 units per dag.

## 10. AI API setup & kosten

**Anthropic (Claude):** maak een key op [console.anthropic.com](https://console.anthropic.com/). Standaard: `claude-haiku-4-5` voor pass 1 (kandidaten zoeken) en `claude-opus-5-5` voor pass 2 (strenge ranking). Structured outputs garanderen geldige JSON; de rubric wordt via prompt caching hergebruikt; server-side *refusal fallbacks* zijn aangezet zodat een zeldzame weigering op bv. een controversieel transcript automatisch op een fallback-model opnieuw draait.

**OpenAI:** key via [platform.openai.com](https://platform.openai.com/api-keys). Standaard `gpt-5-mini` (pass 1) en `gpt-5` (pass 2), Whisper `whisper-1` voor transcriptie (~$0,006/min), `text-embedding-3-small` voor duplicate-detectie. Via `OPENAI_BASE_URL` werkt elke OpenAI-compatibele server.

**Kosten-indicatie per video van 20 minuten** (werkelijke kosten worden per analyse bijgehouden: video-detailpagina en Analytics):

| Onderdeel | Indicatie |
|---|---|
| Transcriptie Whisper API | ~$0,12 (lokaal faster-whisper: $0) |
| Pass 1 (snel model, hele transcript) | ~$0,01–0,03 |
| Pass 2 (slim model, alleen ~30 kandidaten) | ~$0,10–0,60 afhankelijk van model (Sonnet 5.5 ≈ helft van Opus 5.5) |
| Vision (optioneel, alleen top N) | ~$0,02–0,10 |
| Heuristische modus | $0 |

Kostenknoppen (Settings): slim model (bv. `claude-sonnet-5-5`), aantal kandidaten, batchgrootte, vision aan/uit, lokale transcriptie, min. videolengte en *aantal video's per scan*.

## 11. Video processing

**Waar komt het videobestand vandaan?** ViralClip downloadt **niets** van YouTube (zie §15). Bronbestanden komen via:

1. **Upload** in het dashboard (Videos → *Upload bron*, of *Video toevoegen → Bestand uploaden*). Streaming upload, ook voor bestanden van meerdere GB.
2. **Inbox-map** `data/inbox/`: zet er een bestand in met het YouTube-video-ID in de naam, bv. `Enzo Knol - mijn vlog [dQw4w9WgXcQ].mp4`. De worker koppelt het automatisch aan de juiste video en start de analyse. Ideaal met een gedeelde Google Drive/Dropbox-map van een creator. Ook `.srt`/`.vtt` met het ID in de naam wordt herkend.
3. **Transcript eerst**: upload een SRT/VTT — de video wordt dan al geanalyseerd en gerankt; de clip-preview gebruikt dan de officiële YouTube-embed op het juiste tijdstip, en renderen gebeurt zodra het bestand er is.

**Rendering** (`backend/app/video/`): knippen met stilte-verwijdering → reframing (YuNet- of Haar-gezichtsdetectie, tracking, actieve-spreker-heuristiek via mondbeweging, stabiele crops die alleen wisselen bij spreker/camera-wissel; split-screen bij twee actieve sprekers; blur-fit zonder gezichten) → ASS-captions (woord-gesynchroniseerd, nadruk-woorden, positie buiten gezichten en TikTok-UI) → loudness-normalisatie (−14 LUFS) → H.264 1080×1920 + thumbnail. Per clip kun je captions, layout en start/einde (±0,5 s) aanpassen en opnieuw renderen.

## 12. Deployment

**VPS (bv. Hetzner, DigitalOcean) met Docker Compose — aanbevolen**

```bash
# op de server
git clone … && cd viral-clip-ai && cp .env.example .env
# zet minimaal: POSTGRES_PASSWORD, APP_SECRET_KEY, API_AUTH_TOKEN, DASHBOARD_PASSWORD en je API keys
docker compose up -d --build
```

Zet er een reverse proxy met HTTPS voor, bv. Caddy (`/etc/caddy/Caddyfile`):

```
clips.jouwdomein.nl {
    reverse_proxy localhost:3000
}
```

De API luistert alleen op `127.0.0.1:8000`; het dashboard praat server-side met de API. Meer rekenkracht: `docker compose up -d --scale worker=3` (de queue gebruikt `SKIP LOCKED`, dus workers bijten elkaar niet).

**Managed onderdelen:** database op Supabase/Neon (`DATABASE_URL`), opslag op Cloudflare R2/S3/Supabase Storage (`STORAGE_BACKEND=s3` + `S3_*`). Het dashboard kan desgewenst op Vercel (`BACKEND_URL` naar je API); API + worker horen op een machine met ffmpeg en voldoende CPU (Railway/Render/Fly met de meegeleverde Dockerfile werkt ook).

**Lokale transcriptie in Docker:** `INSTALL_LOCAL_WHISPER=true docker compose build` en `TRANSCRIBER=faster_whisper`.

## 13. Troubleshooting

| Probleem | Oplossing |
|---|---|
| *"ffmpeg is niet gevonden"* | Installeer ffmpeg (`brew install ffmpeg` / `apt install ffmpeg`) of gebruik Docker. |
| Video blijft op **Wacht op bron** | Normaal: upload het bestand of zet het in `data/inbox/` met het video-ID in de naam. |
| *"Geen spraak-naar-tekst beschikbaar"* | Zet `OPENAI_API_KEY`, installeer `faster-whisper`, of upload een SRT/VTT. |
| *"Zoeken op naam vereist een YouTube API key"* | Key instellen, of plak de kanaal-URL (`youtube.com/channel/UC…`). |
| *"YouTube API quota is op"* | Wacht tot middernacht Pacific Time; vermijd zoeken op naam (100 units), gebruik @handles. |
| Scores voelen willekeurig | Zonder LLM-key draait de heuristiek. Stel een AI-key in; geef feedback zodat het persoonlijke model gaat leren (vanaf 12 beoordelingen). |
| Captions in een ander lettertype | Installeer Montserrat (`fonts-montserrat`) of zet `.ttf`-bestanden in `backend/assets/fonts/`. |
| Reframing volgt het gezicht niet goed | De YuNet-detector wordt automatisch gedownload (in Docker tijdens de build); zonder internet valt het terug op Haar. Kies per clip *Volg spreker*, *Midden crop* of *Blur-fit*. |
| Jobs blijven hangen | Controleer of de worker draait (`docker compose logs worker`). Vastgelopen jobs worden na 30 min automatisch opnieuw ingepland; retry/annuleer op de Queue-pagina. |
| 401 in het dashboard | `DASHBOARD_PASSWORD` staat aan (Basic-auth), of `API_AUTH_TOKEN` verschilt tussen `web` en `api`. |
| `Backend niet bereikbaar` | Draait de API? Klopt `BACKEND_URL` (lokaal `http://localhost:8000`, in Docker `http://api:8000`)? |

## 14. Kritische ontwerpkeuzes (waar ik afweek van het oorspronkelijke plan)

1. **Geen YouTube-downloads.** YouTube's voorwaarden verbieden downloaden buiten YouTube's eigen functies om, en jij vroeg expliciet om geen beveiligingen te omzeilen. Daarom: officiële API + RSS voor discovery, en het bronbestand via een toegestane route (creator-clippingprogramma's, eigen content, gedeelde map). Om de automatisering toch zo groot mogelijk te houden: de inbox-map, transcript-eerst-analyse en YouTube-embed-previews.
2. **Comment-hotspots als gratis "crowd signal".** Tijdstempels die kijkers in comments noemen zijn het sterkste externe bewijs dat een moment deelbaar is — en ze kosten 1 quota-unit.
3. **RSS in plaats van `search.list` voor het scannen**: 0 quota i.p.v. 100 units per creator per scan.
4. **Funnel-score in plaats van een gemiddelde.** Een clip moet eerst stoppen, dan vasthouden, dan engagen. Een meetkundig gemiddelde van die drie stadia straft een zwakke hook af, zoals TikTok dat ook doet.
5. **Het LLM rekent niet met tijden.** Het verwijst naar zin-ID's; code rekent die om naar woord-exacte tijden. Dat voorkomt de meest voorkomende fout: clips die midden in een woord beginnen.
6. **Twee generatoren voor kandidaten** (LLM + audio/tekst/crowd-signalen): het LLM "hoort" geen gelach of geschreeuw; de signalen missen subtiele humor. Samen hogere recall.
7. **Database-queue i.p.v. Celery/Redis**: één minder onderdeel, de queue ís de Analysis Queue-pagina, en overleeft herstarts.
8. **Eén Python-package** (`backend/app/{ai,video,services}`) i.p.v. losse top-level mappen `ai/` en `video-processing/`: eenvoudiger importeren, testen en deployen.
9. **Same-origin API-proxy in Next.js**: geen CORS, de API-token blijft server-side, en grote uploads worden gestreamd.
10. **Werkt zonder keys**: de heuristische modus en `make demo` maken het hele systeem direct bekijkbaar en testbaar.

## 15. Juridisch (YouTube Terms of Service)

- Discovery gebruikt uitsluitend de **YouTube Data API v3**, de **publieke RSS-feeds** en **oEmbed**; previews gebruiken de **officiële embed-player**.
- ViralClip **downloadt, scrapet of omzeilt niets**. Het bronbestand lever je zelf aan vanuit een bron waarvoor je toestemming hebt.
- Clips van andermans content publiceren vereist toestemming van de rechthebbende (veel grote creators hebben daarvoor een clipping-programma). Jij bent verantwoordelijk voor het naleven van auteursrecht en de voorwaarden van TikTok/YouTube/Instagram.

## 16. Roadmap

- TikTok/YouTube Analytics-koppeling om statistieken automatisch op te halen (nu handmatig per clip).
- Uitgebreidere vision: gezichtsexpressies en reacties van meerdere personen per frame.
- "Cold open"-edits: de punchline als flash-forward vóór de opbouw zetten.
- Captions-editor met woordcorrectie in de browser.
- Multi-user/teams met rollen.
