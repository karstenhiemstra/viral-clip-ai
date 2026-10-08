# 🔥 ViralClip AI

**Vindt automatisch de momenten met het hoogste viral potential in lange YouTube-video's en zet ze klaar als verticale TikTok/Reels/Shorts-clips (9:16, captions, 10–15 sec: complete mini-gesprekken met de hook in de eerste 2 seconden).**

Je voegt creators toe (bijv. Enzo Knol, Bankzitters, Hanwe, Gio, StukTV — of elk ander kanaal). ViralClip AI controleert elke 2 uur of er nieuwe video's zijn, kiest de interessantste, analyseert het transcript en de audio, laat een AI denken als *een TikTok-kijker die aan het scrollen is*, geeft elk moment een **Viral Score (0–100)**, verwijdert dubbele momenten, kiest een start die meteen pakt (hook-first), rendert een verticale clip met captions en zet alles in een dashboard met preview, uitleg en downloadknop. Jouw feedback (🔥 / 👍 / 👎 / ❌) traint een persoonlijk model dat de score bijstuurt.

> ⚠️ De Viral Score is een **voorspelling** op basis van kenmerken — nooit een garantie. De app zegt daarom *"High viral potential"* en *"Viral Score: 94/100"*, nooit *"deze video gaat viral"*.

---

## Inhoud

- [START HIER — in 8 stappen aan de slag](#start-hier)
- [Welke API keys heb je nodig?](#welke-api-keys-heb-je-nodig)
- [Waarom moet ik de video zelf aanleveren?](#waarom-moet-ik-de-video-zelf-aanleveren)
- [Hoe kiest de app de clips? (eerlijk: wat is AI en wat niet)](#hoe-kiest-de-app-de-clips)
- [Wat kost het?](#wat-kost-het)
- [Auto Edit — gratis edits met één zin](#auto-edit--gratis-edits-met-één-zin)
- [Automatisch volgen van creators](#automatisch-volgen-van-creators)
- [Beveiliging](#beveiliging)
- [Voor ontwikkelaars](#voor-ontwikkelaars) — architectuur, lokaal draaien, tests, database, deployment
- [Problemen oplossen](#problemen-oplossen)
- [Ontwerpkeuzes & juridisch](#ontwerpkeuzes)

---

## START HIER

Geen technische kennis nodig. Je hebt een Windows-pc of Mac en ongeveer 20 minuten nodig. Je hoeft geen terminalcommando's te typen.

### STAP 1 — Installeer Docker
Ga naar [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/), klik op **Download** voor jouw computer, installeer het en open **Docker Desktop**. Wacht tot linksonder *Engine running* staat.

### STAP 2 — Maak een YouTube API key (gratis)
1. Ga naar [console.cloud.google.com](https://console.cloud.google.com/) en log in met je Google-account.
2. Klik bovenaan op de projectkiezer → **Nieuw project** → naam `viralclip` → **Maken**.
3. Open [deze pagina](https://console.cloud.google.com/apis/library/youtube.googleapis.com) en klik op **Inschakelen**.
4. Klik links op **Inloggegevens / Credentials** → **+ Inloggegevens maken** → **API-sleutel**. Kopieer de sleutel en bewaar hem even (bijv. in Kladblok).

### STAP 3 — Maak een OpenAI API key (aanbevolen)
1. Ga naar [platform.openai.com/api-keys](https://platform.openai.com/api-keys) en log in of maak een account.
2. Klik op **Create new secret key** → **Create secret key** en kopieer de sleutel (je ziet hem maar één keer).
3. Ga naar [Billing](https://platform.openai.com/settings/organization/billing/overview) → **Add payment details** en zet er bijv. **$5** op. Dat is genoeg voor ongeveer 20–30 video's (zie [kosten](#wat-kost-het)).

Zonder OpenAI-key werkt de app ook, maar dan met de gratis basisanalyse ([zie hieronder](#zonder-ai-key-basisanalyse)).

### STAP 4 — Start de applicatie
1. Klik op deze GitHub-pagina op de groene knop **Code** → **Download ZIP**. Pak het ZIP-bestand uit, bijvoorbeeld in *Documenten*.
2. Open de uitgepakte map en **dubbelklik**:
   - **Windows:** `start-windows.bat`
   - **Mac:** `start-mac.command`. De eerste keer **rechtsklik** je erop → **Open** → **Open** (macOS vraagt dan toestemming).
   - **Linux:** voer `./start-linux.sh` uit.

De eerste keer duurt het 5–10 minuten: de app wordt gebouwd. Daarna opent je browser vanzelf. De volgende keren start hij in een paar seconden.

### STAP 5 — Open het dashboard en vul je keys in
1. Je browser opent **http://localhost:3000**. Gebeurt dat niet, typ het adres dan zelf in.
2. Klik links op **Settings** → **API keys**.
3. Plak de YouTube-sleutel en klik **Opslaan**. De app test hem meteen en toont **Verbonden** of uitleg wat er mis is.
4. Doe hetzelfde met de OpenAI-sleutel.

### STAP 6 — Voeg een creator toe
Klik links op **Creators** → **Creator toevoegen** → typ `Enzo Knol` → **Zoek**. Kies bij *Direct ophalen* een periode (laatste 24 uur, 7 of 30 dagen) en een aantal video's (5, 10 of 25). Klik bij het juiste kanaal op **Kies**.

Per creator zie je daarna de stappen *bron nodig → bezig → geanalyseerd → clips*.

### STAP 7 — Lever een video aan
Klik links op **Videos**. Bij een video met **Bron nodig** klik je op **Bron aanleveren**, en dan één van deze:
- **sleep het videobestand** in het vak, of klik **Bestand kiezen**;
- **plak een deel-link** van Google Drive, Dropbox of OneDrive;
- upload alleen **ondertitels (.srt)**: de analyse start dan al, en de clips worden gemaakt zodra de video er is;
- of zet het bestand in de map `data/inbox`. De app toont de exacte bestandsnaam die je moet gebruiken.

Daarna gaat alles vanzelf: transcriptie → AI-analyse → beste clips → 9:16 met captions. Volg de voortgang op **Analysis Queue**.
*Waarom niet automatisch van YouTube? Dat staat YouTube niet toe; zie [hieronder](#waarom-moet-ik-de-video-zelf-aanleveren).*

### STAP 8 — Bekijk en download je clips
Het **Dashboard** toont **🔥 Top Viral Clips**: #1, #2, #3… met creator, Viral Score (bijv. *Enzo Knol · 84/100*), een **Preview**-knop en een **Download**-knop. Geef feedback (🔥 / 👍 / 👎) op de clippagina; zo leert de app jouw smaak.

**Stoppen:** dubbelklik `stop-windows.bat` (Windows), voer `./stop.sh` uit (Mac/Linux), of klik in Docker Desktop bij *viralclip* op **Stop**. Je clips en instellingen blijven bewaard in de map `data`.
**Opnieuw starten:** dubbelklik weer het startbestand.

---

## Welke API keys heb je nodig?

| Key | Nodig? | Waarvoor | Kosten | Waar zet je hem |
|---|---|---|---|---|
| **YouTube Data API v3** | **Ja** | Creators zoeken, nieuwe video's vinden, metadata, comment-tijdstempels | Gratis (10.000 units/dag, je gebruikt er ~400) | `.env` → `YOUTUBE_API_KEY=` of Settings |
| **OpenAI** | Aanbevolen | Transcriptie (Whisper) **én** de slimme clipselectie (GPT-5) — één key voor alles | Betaald, ca. $0,15–0,55 per video | `.env` → `OPENAI_API_KEY=` of Settings |
| Anthropic (Claude) | Nee, weglaten | Alternatief voor OpenAI als je liever Claude gebruikt. Kan **niet** transcriberen | Betaald | `.env` → `ANTHROPIC_API_KEY=` |

**Advies: gebruik alleen YouTube + OpenAI.** Eén betaalde provider is genoeg. Laat `ANTHROPIC_API_KEY`, `OPENAI_BASE_URL`, alle `S3_*`-regels en `DATABASE_URL` leeg.

Keys die je in **Settings** invult worden versleuteld opgeslagen en nooit naar de browser teruggestuurd; een key uit Settings wint van `.env`.

---

## Waarom moet ik de video zelf aanleveren?

Omdat het **niet mag** om video's van YouTube te downloaden, en jij terecht vroeg om geen beveiligingen van YouTube te omzeilen:

- De **officiële YouTube API** geeft titels, beschrijvingen, duur, views, likes en comments — maar **geen video, geen audio en geen ondertitels** van andermans video's (ondertitels downloaden kan alleen voor je eigen kanaal).
- Zonder beeld of tekst kan geen enkele tool de momenten vinden. Daarom lever je de bron aan via een toegestane route: upload, deel-link, inbox-map of ondertitelbestand. Veel grote creators hebben een clipping-programma dat precies deze bestanden aanlevert.

Wat de app **wel** volledig automatisch doet: nieuwe video's vinden, filteren en prioriteren, comment-tijdstempels verzamelen ("1:02 de pan 😂"), en zodra het bestand er is: transcriberen, analyseren, scoren, renderen. Een video die al geanalyseerd is, wordt nooit opnieuw verwerkt (ook niet als je opnieuw scant), tenzij je zelf op *Opnieuw analyseren* klikt.

---

## Hoe kiest de app de clips?

### De stappen

```
1. Nieuwe video gevonden (YouTube API)  → filter: periode, lengte, Shorts/livestreams, titelwoorden
2. Prioriteit per video                 → views t.o.v. kanaalgemiddelde, engagement, recentheid, comment-tijdstempels
3. Transcript met woord-timestamps     → Whisper (OpenAI) of je eigen .srt/.vtt
4. Signalen (lokaal, gratis)            → zinnen, luidheidspieken (gelach/geschreeuw), stiltes, camerawissels
5. PASS 1  goedkoop model (gpt-5-mini)  → leest het HELE transcript en stelt ~30–50 momenten voor
6. PASS 2  lokaal, gratis               → voegt AI-momenten en signaal-momenten samen, verwijdert dubbelingen,
                                           houdt de beste ~30 kandidaten over
7. PASS 3  slim model (gpt-5)           → beoordeelt ALLEEN die ~30 kandidaten als "TikTok-scroller + editor":
                                           12 scores, vlaggen (bijv. context nodig), oordeel, betere begin-/eindzin
8. Clipgrenzen                          → begint bij de hook, repareert ontbrekende context, eindigt na de payoff,
                                           haalt dode stiltes weg, 12–18 s (flexibel, bijv. 00:13:41.2 → 00:13:56.4)
9. Viral Score                          → formule hieronder
10. Dubbelingen weg + variatie (MMR)    → top 5 per video, beste bovenaan
11. Render                              → 9:16 met gezicht-volgen, CapCut-captions, −14 LUFS → MP4 1080×1920
```

Het dure model ziet dus nooit het hele transcript, alleen de shortlist. Dat maakt het goedkoop.

### De Viral Score-formule

```
Stop   = gewogen gemiddelde van  Hook ×1.6, Hook Strength ×1.3, Curiosity ×1.2        ("stopt iemand met scrollen?")
Hold   = gewogen gemiddelde van  Retention ×1.5, Payoff ×1.3, Context ×1.2, Surprise, Emotion   ("kijkt hij door?")
Engage = gewogen gemiddelde van  Shareability ×1.3, Comment Potential, Humor, Rewatch ×0.7        ("deelt/reageert hij?")

Inhoud      = meetkundig gemiddelde van Stop (40%), Hold (35%), Engage (25%)
Viral Score = (80% Inhoud + 20% signalen) × strafpunten + comment-bonus (max +6) + persoonlijke bijsturing
              met een plafond per oordeel (skip ≤ 45, maybe ≤ 76, good ≤ 92)
```

- *Meetkundig* gemiddelde: een slechte hook kan niet worden goedgemaakt door deelbaarheid — net als op TikTok.
- *Strafpunten*: sponsor/reclame ×0,55, intro/outro ×0,6, context nodig ×0,85, begint/eindigt midden in een zin ×0,93, zwakke payoff ×0,92.
- Alle gewichten zijn aan te passen in **Settings → Viral Score**. Per clip zie je de volledige opbouw ("Score per factor" en "Waarom de AI deze clip koos").
- Meer detail: [docs/ALGORITHM.md](docs/ALGORITHM.md).

### Waar zit de AI precies?

| Onderdeel | Wat gebruikt het | Waar in de code |
|---|---|---|
| Transcriptie | OpenAI **whisper-1** (API, in stukken van 20 min) · of lokaal **faster-whisper** · of jouw .srt/.vtt | `backend/app/ai/transcription.py` |
| Pass 1 (momenten zoeken) | **gpt-5-mini** (reasoning low) · Claude: claude-haiku-4-5 | `backend/app/ai/candidates.py`, `prompts.py` |
| Pass 2 (shortlist) | Geen AI: lokale code | `backend/app/ai/candidates.py` (`merge_candidates`) |
| Pass 3 (beoordelen) | **gpt-5** (reasoning low; "Beste": medium) · Claude: claude-sonnet-5-5 / claude-opus-5-5 | `backend/app/ai/evaluator.py`, `prompts.py` |
| Dubbelingen | OpenAI text-embedding-3-small, anders TF-IDF (lokaal) | `backend/app/ai/dedupe.py` |
| Gezichten volgen | OpenCV (YuNet, anders Haar) — lokaal | `backend/app/video/reframe.py` |
| Renderen & captions | ffmpeg + libass — lokaal | `backend/app/video/render.py`, `captions.py` |
| Persoonlijk leren | Ridge-regressie op jouw beoordelingen — lokaal | `backend/app/services/learning.py` |
| Beeldanalyse (vision) | Optioneel, standaard **uit** (beta), alleen de top-N | `backend/app/ai/vision.py` |

**Kwaliteit/kosten** kies je in **Settings → AI & modellen**: *Budget* (gpt-5-mini voor alles), *Gebalanceerd* (aanbevolen) of *Beste kwaliteit*. Is een model niet beschikbaar voor jouw key, dan schakelt de app automatisch over naar het volgende (gpt-5 → gpt-5-mini → gpt-4.1). Is je tegoed op of je key ongeldig, dan zie je dat op het dashboard en gaat de analyse gratis verder met de heuristiek.

### Zonder AI-key: basisanalyse

Zonder OpenAI/Anthropic-key werkt alles, maar dan met vuistregels in plaats van een taalmodel: momenten komen uit luidheidspieken, comment-tijdstempels, scènewissels en "hook-woorden" (bijv. *wacht, wat?!*, vragen, uitroepen, getallen, emotiewoorden), en de 12 scores worden daaruit berekend. Dat is gratis en transparant, maar het **begrijpt geen humor of verhaal**. In de clipuitleg staat altijd welke modus is gebruikt (*Modus: heuristic* of *llm*).

### Wat is (nog) niet gebouwd

- Video's downloaden van YouTube: bewust niet (zie hierboven).
- TikTok/Instagram-statistieken automatisch ophalen: je vult ze nu per clip in (*Prestaties na publicatie*).
- Vision (beeldanalyse) staat standaard uit.

---

## Wat kost het?

**YouTube API: gratis.** AI-kosten per video, geschat op basis van **gemeten** tokengebruik van deze app en de publieke prijslijst (september 2026; bron: LiteLLM-prijstabel). De marge komt door verborgen "denk"-tokens die per video verschillen.

| Videolengte | OpenAI · Gebalanceerd (aanbevolen) | waarvan Whisper | Zelfde, met eigen .srt (geen Whisper) | OpenAI · Budget | OpenAI · Beste |
|---|---|---|---|---|---|
| 10 min | **$0,15 – 0,20** | $0,06 | $0,09 – 0,14 | $0,08 – 0,10 | $0,19 – 0,31 |
| 30 min | **$0,28 – 0,34** | $0,18 | $0,10 – 0,16 | $0,21 – 0,23 | $0,32 – 0,45 |
| 60 min | **$0,48 – 0,54** | $0,36 | $0,12 – 0,18 | $0,41 – 0,44 | $0,52 – 0,66 |

Ter vergelijking, alleen Claude (transcriptie lokaal, gratis): Gebalanceerd $0,10–0,21, Beste (Opus) $0,28–0,57 per video.

**Waar komt dat vandaan?**

| Onderdeel | Gemeten tokens | Prijs (per 1M tokens) |
|---|---|---|
| Pass 1 — hele transcript | ~460 in + ~170 uit per videominuut | gpt-5-mini: $0,25 in / $2,00 uit |
| Pass 3 — alleen ~30 kandidaten | ~10.500 in + ~4.500 uit per video (**onafhankelijk van de lengte**) | gpt-5: $1,25 in / $10,00 uit |
| Transcriptie | per audiominuut | whisper-1: $0,006 per minuut |

**Voorbeeld per maand:** 10 creators × 3 nieuwe video's per week van 20 minuten ≈ 130 video's × $0,21–0,27 ≈ **$27–35 per maand**. Levert de creator ondertitels (.srt) aan, dan ongeveer de helft.

De werkelijke kosten worden per analyse bijgehouden (videopagina → *Laatste analyse*) en per dag op het dashboard. **Settings → AI & modellen** toont dezelfde tabel voor jouw instellingen.

**Besparen:** kwaliteit *Budget*, minder kandidaten (Settings → Pipeline), minimale videolengte per creator, maximaal aantal video's per scan, ondertitels aanleveren.

---

## Auto Edit — gratis edits met één zin

Typ op de pagina **Auto Edit** bijvoorbeeld *"Maak een edit van Neymar"* of *"Maak een cinematic edit van Messi"* en klik **Maak edit**. De edit wordt **volledig lokaal** gemaakt met FFmpeg: geen OpenAI/Claude, geen transcriptie, geen kosten.

1. **Beeldmateriaal:** voeg video's toe via *Beeldmateriaal toevoegen* (op die pagina; er start dan géén clip-analyse) of gebruik video's die al in de app staan. Zet de naam in de titel ("Neymar skills"), of kies zelf de video's onder *Kies zelf video's*. De editor zoekt niets op internet.
2. **Muziek (optioneel):** voeg een nummer toe via *Muziek toevoegen* of zet audiobestanden in `data/music`. De cuts vallen dan op de beat. Zonder muziek gebruikt de edit het originele geluid. Gebruik alleen muziek die je mag gebruiken.
3. **Stijlen:** Hype, Cinematic, Fast / Aggressive, Clean en Football Edit. Noem je geen stijl, dan kiest de editor zelf (voetballers → Football Edit, anders Hype).
4. **Resultaat:** een preview (1080×1920 MP4, standaard 15 s), met **Opnieuw genereren** (andere momenten, timing en effecten), **Download** en **Edit aanpassen** (volgorde, verwijderen, inkorten, transitie, zoom/shake/slow-mo/freeze, muziek aan/uit).

Hoe het werkt: de beelden worden geanalyseerd op beweging, cameracuts, waar de actie in beeld is en hoe hard het geluid is (eenmalig, daarna uit de cache). De beste momenten komen aan het begin en het einde. Effecten: snelle cuts op de beat, zoom in/uit, punch-in, camera shake, speed ramps, slow motion (met frame blending), motion blur, flits-, zoom-, whip-, glitch- en fade-transities, freeze frames, kleurgrading per stijl, cinema-balken (Cinematic), fade in/uit en de naam als titel.

---

## Automatisch volgen van creators

- De worker kijkt standaard **elke 120 minuten** per creator of er nieuwe uploads zijn (Settings → Discovery). 10 creators is geen probleem: elke scan kost ~3 YouTube-quota-units (+1 per nieuwe video voor comments), dus ~400 van de 10.000 gratis units per dag.
- Filters (globaal én per creator): periode (vandaag / 24 uur / 7 dagen / 30 dagen / eigen periode / alles), max. aantal video's per scan (5/10/25/50/alle), min./max. lengte, geen Shorts, geen livestreams, titelwoorden uitsluiten, minimale views.
- Een video die al geanalyseerd is, wordt **nooit opnieuw** geanalyseerd door een scan. Een video die door een filter werd overgeslagen, kun je terughalen via **Ophalen** bij de creator (dan worden de filters opnieuw toegepast), of door zelf de bronvideo aan te leveren.
- Per creator: prioriteit (bepaalt de volgorde in de wachtrij), taal, clipduur, max. clips per video, automatisch analyseren aan/uit.

---

## Beveiliging

| Maatregel | Hoe |
|---|---|
| **Geen secrets in Git** | `.env` en `data/` staan in `.gitignore`; `.env.example` bevat alleen lege waarden. `make secrets` en de CI-stap *Secret scan* stoppen als er iets op een API key lijkt; `make hooks` installeert dezelfde controle als pre-commit hook. |
| API keys in de app | Versleuteld (Fernet) met `APP_SECRET_KEY` (automatisch aangemaakt in `data/.secret_key` als je hem leeg laat); de browser krijgt alleen `sk-…xyz` te zien. |
| Dashboard-wachtwoord | `DASHBOARD_PASSWORD` → inlogvenster op alle pagina's én de API-proxy. |
| API-token | `API_AUTH_TOKEN` → de API accepteert alleen verzoeken met `Authorization: Bearer …`; het dashboard voegt dit server-side toe (nooit in de browser of in URL's). |
| Netwerk | Docker publiceert het dashboard en de API **alleen op 127.0.0.1**. Wil je er van buitenaf bij: eerst wachtwoord + token instellen, dan een reverse proxy met HTTPS (zie [deployment](#deployment)). |
| Database | Docker: PostgreSQL is niet van buitenaf bereikbaar; zet `POSTGRES_PASSWORD` op iets sterks voor productie. |
| CORS | Alleen `CORS_ORIGINS` (standaard `http://localhost:3000`); het dashboard gebruikt een same-origin proxy. |
| Invoer | Pydantic-validatie op alle endpoints, bestandstype- en groottelimiet op uploads (`MAX_UPLOAD_GB`), SSRF-bescherming bij deel-links (geen interne adressen, geen YouTube-downloads). |
| Headers | `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`. |

**Automatisch geregeld door het startscript:** bij de eerste start maakt het een `.env` met willekeurige waarden voor `APP_SECRET_KEY`, `API_AUTH_TOKEN` en `POSTGRES_PASSWORD`. Op je eigen computer is verder niets nodig; het dashboard is alleen vanaf die computer bereikbaar.
**Wil je het op een server of in je netwerk zetten:** zet ook `DASHBOARD_PASSWORD` (en volg [deployment](#deployment)). Zelf een sterke waarde maken: `python3 -c "import secrets;print(secrets.token_urlsafe(48))"`.

---

## Voor ontwikkelaars

### Architectuur

```mermaid
flowchart LR
  subgraph Browser
    UI[Next.js dashboard]
  end
  UI -- same-origin /api/* --> PX[Next.js route handler<br/>proxy + auth + streaming]
  PX -- Bearer token --> API[FastAPI]
  API <--> DB[(PostgreSQL / SQLite)]
  API <--> ST[(Storage: lokaal of S3/R2/Supabase)]
  W[Worker] <--> DB
  W <--> ST
  W -- Data API v3 / RSS / oEmbed --> YT[YouTube]
  W -- pass 1 + 3 --> LLM[OpenAI / Claude / heuristiek]
  W -- STT --> STT[Whisper API / faster-whisper]
  W -- ffmpeg + OpenCV --> R[Render 9:16 + captions]
  INBOX[/data/inbox/ · deel-links/] --> W
```

```
viral-clip-ai/
├── backend/                  Python 3.11+ · FastAPI · SQLAlchemy 2 · Alembic
│   ├── app/
│   │   ├── ai/               transcriptie, signalen, kandidaten (pass 1+2), evaluator (pass 3), scoring,
│   │   │                     boundaries, dedupe, vision, costs, pipeline, providers/ (OpenAI, Anthropic)
│   │   ├── video/            ffmpeg, audio-features, scènedetectie, reframing, captions, render
│   │   ├── services/         YouTube, discovery, queue, storage, media/inbox, deel-links, settings, learning, usage
│   │   ├── api/              REST-endpoints
│   │   ├── worker/           job-handlers + worker-loop (scheduler, inbox, recovery)
│   │   ├── models.py         database-schema
│   │   └── demo.py           demo-data zonder API keys
│   ├── alembic/              migraties (SQLite + PostgreSQL)
│   └── tests/                110+ tests (unit, API, YouTube-API-stub, end-to-end met echte ffmpeg-render)
├── frontend/                 Next.js 16 · React 19 · Tailwind 4 · TypeScript
│   ├── app/                  pagina's + /api proxy-route
│   ├── components/ lib/
│   └── proxy.ts              Basic-auth voor het dashboard
├── docs/ALGORITHM.md         uitleg selectie-algoritme & scoring
├── start-windows.bat · start-mac.command · start-linux.sh · stop-windows.bat · stop.sh
│                             één-klik starten/stoppen (roepen scripts/start.ps1 / scripts/start.sh aan)
├── scripts/                  start.sh, start.ps1, dev.sh, check_secrets.py
├── docker-compose.yml        Postgres + API + worker + dashboard
├── .env.example · Makefile · .github/workflows/ci.yml
```

### Lokaal draaien zonder Docker

Vereist: Python 3.11+, Node.js 20.9+ (22 aanbevolen), **ffmpeg** (met libass).

```bash
# macOS: brew install ffmpeg python@3.12 node
# Ubuntu/Debian: sudo apt install ffmpeg python3-venv nodejs npm fonts-montserrat
make setup     # venv + pip install + npm install + .env + pre-commit secret check
make demo      # optioneel: demo-video + clips zonder API keys
make dev       # API (8000) + worker + dashboard (3000)
```

| Commando | Wat |
|---|---|
| `make test` | secret scan + ruff + pytest + eslint + next build |
| `make api` / `make worker` / `make web` | los starten |
| `make secrets` / `make hooks` | secret scan / pre-commit hook installeren |
| `python -m app.worker.runner --once` | wachtrij één keer leegdraaien (in `backend/`) |
| `TEST_DATABASE_URL=postgresql://… pytest` | tests tegen PostgreSQL |

API-documentatie: http://localhost:8000/docs. De tests gebruiken een getrouwe stub van de YouTube Data API (`backend/tests/youtube_stub.py`, zelfde JSON- en foutformaat); met `YOUTUBE_API_BASE`/`YOUTUBE_WEB_BASE` kun je de hele app tegen die stub draaien.

### Database

- **Lokaal:** SQLite in `data/viralclip.db` — niets te doen.
- **Docker Compose:** PostgreSQL 16 draait mee; `DATABASE_URL` wordt automatisch gezet.
- **Supabase / eigen Postgres:** `DATABASE_URL=postgresql://user:wachtwoord@host:5432/postgres`.

Migraties draaien automatisch bij het starten van de API (`make migrate` handmatig; nieuwe migratie: `cd backend && alembic revision --autogenerate -m "..."`).

### Environment variables

Alle variabelen staan met uitleg in [`.env.example`](.env.example). Alles wat je dagelijks wilt aanpassen (gewichten, clipduur, captions, filters, interval, kwaliteit, modellen) staat in **Settings** en wordt in de database bewaard.

| Variabele | Standaard | Uitleg |
|---|---|---|
| `YOUTUBE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | — | zie [API keys](#welke-api-keys-heb-je-nodig) |
| `LLM_PROVIDER` | `auto` | `auto` / `openai` / `anthropic` / `heuristic` |
| `LLM_QUALITY` | `balanced` | `budget` / `balanced` / `best` |
| `LLM_MODEL_FAST` / `LLM_MODEL_SMART` | volgt `LLM_QUALITY` | model voor pass 1 / pass 3 overschrijven |
| `TRANSCRIBER` | `auto` | `openai` / `faster_whisper` / `none` |
| `TRANSLATE_CAPTIONS` | `false` | captions blijven altijd in de gesproken taal (automatisch herkend, ook per zin); niet vertalen |
| `OPENAI_BASE_URL` | — | OpenAI-compatibele server (OpenRouter, lokale Ollama/vLLM) |
| `DATABASE_URL` | SQLite | zie hierboven |
| `STORAGE_BACKEND` | `local` | `local` of `s3` (+ `S3_*`) |
| `APP_SECRET_KEY`, `API_AUTH_TOKEN`, `DASHBOARD_PASSWORD` | — | zie [beveiliging](#beveiliging) |
| `MAX_UPLOAD_GB` | `20` | limiet voor uploads en deel-links |
| `APP_TIMEZONE` | `Europe/Amsterdam` | voor de filter "vandaag" |

### Video processing

Bronnen: **upload** (gestreamd, ook bestanden van meerdere GB), **deel-link** (Google Drive / Dropbox / OneDrive / directe link, met SSRF-bescherming), **inbox-map** `data/inbox/` (bestandsnaam met het video-ID, ook `.srt`/`.vtt`), of **transcript eerst** (analyse draait direct; preview via de officiële YouTube-embed; renderen zodra het bestand er is — zonder opnieuw te analyseren).

Rendering (`backend/app/video/`): knippen met stilte-verwijdering → reframing die **de persoon die praat in het midden houdt** (YuNet-gezichtsdetectie, anders Haar; gezichten volgen per shot, ook als iemand even wegkijkt; de spreker = wiens lippen meebewegen met het stemvolume, plus de transcript-timing; een wissel pas na genoeg bewijs, dus een kort "ja" of een knikkende luisteraar verplaatst het beeld niet; vloeiende camerabewegingen naar de nieuwe spreker, een harde cut alleen waar de video zelf knipt; altijd volle hoogte en binnen het beeld; split-screen alleen bij twee ver uit elkaar zittende mensen die heel snel om en om praten; blur-fit zonder gezichten; golfvorm bij alleen audio — details in [docs/ALGORITHM.md](docs/ALGORITHM.md#rendering--wie-praat-er)) → captions in de gesproken taal (nooit vertaald) in de CapCut-stijl: Poppins ExtraBold in hoofdletters, wit met zwarte rand, het uitgesproken woord op een blauw blok, woord-gesynchroniseerd → loudness −14 LUFS → H.264/AAC 1080×1920 (faststart) + thumbnail. Per clip pas je captions, layout en start/einde (±0,5 s) aan en render je opnieuw.

### Deployment

**VPS (bv. Hetzner, DigitalOcean) met Docker Compose**

```bash
git clone https://github.com/karstenhiemstra/viral-clip-ai.git && cd viral-clip-ai && cp .env.example .env
# zet: POSTGRES_PASSWORD, APP_SECRET_KEY, API_AUTH_TOKEN, DASHBOARD_PASSWORD en je API keys
docker compose up -d --build
```

Het dashboard luistert op `127.0.0.1:3000`. Zet er een reverse proxy met HTTPS voor, bv. Caddy (`/etc/caddy/Caddyfile`):

```
clips.jouwdomein.nl {
    reverse_proxy localhost:3000
}
```

Meer rekenkracht: `docker compose up -d --scale worker=3` (de queue gebruikt `SKIP LOCKED`). Managed onderdelen: database op Supabase/Neon (`DATABASE_URL`), opslag op Cloudflare R2/S3/Supabase Storage (`STORAGE_BACKEND=s3`). Lokale transcriptie in Docker: `INSTALL_LOCAL_WHISPER=true docker compose build` en `TRANSCRIBER=faster_whisper`.

---

## Problemen oplossen

| Probleem | Oplossing |
|---|---|
| Settings toont **Niet verbonden** bij YouTube | Lees de melding eronder. Meestal: sleutel opnieuw kopiëren (zonder spaties) → **Opslaan**. Of de API staat nog niet aan (STAP 2.3). |
| *"De YouTube Data API v3 staat nog niet aan"* | STAP 2.3: de API inschakelen in Google Cloud, in hetzelfde project als de key. |
| *"YouTube API quota is op"* | Wacht tot 09:00 Nederlandse tijd (middernacht in Californië). Zoek creators op @handle of URL i.p.v. op naam (1 i.p.v. 100 units). |
| *"Je OpenAI-tegoed is op"* | platform.openai.com → Settings → Billing → tegoed toevoegen, daarna **Verbinding testen**. Intussen werkt de gratis basisanalyse. |
| *"Ongeldige OPENAI_API_KEY"* | Nieuwe key maken (STAP 3) en in Settings plakken → **Opslaan**. |
| Video blijft op **Bron nodig** | Normaal: klik **Bron aanleveren** en lever het bestand, een deel-link of ondertitels aan (STAP 7). |
| Deel-link werkt niet | Google Drive: zet delen op *"Iedereen met de link"*. Dropbox/OneDrive: gebruik de deel-link van het bestand, niet van de map. |
| Preview speelt niet af | Gebruik Chrome, Edge, Safari of Firefox (H.264). Download werkt altijd. |
| *"ffmpeg is niet gevonden"* | Gebruik Docker, of installeer ffmpeg (`brew install ffmpeg` / `apt install ffmpeg`). |
| Clips zijn matig | Zonder AI-key draait de basisanalyse. Stel een OpenAI-key in en geef feedback (het persoonlijke model start vanaf 12 beoordelingen). |
| Reframing volgt het gezicht niet goed | Kies per clip *Volg spreker*, *Midden crop* of *Blur-fit* en render opnieuw. |
| *"api is unhealthy"* / *"dependency failed to start"* | Start de app vanuit je **oorspronkelijke map** (waar je `.env` en `data/` staan). Het database-wachtwoord uit `.env` wordt bij elke start automatisch overgenomen. Details: `docker compose logs api`. |
| Start-script blijft wachten / foutmelding | Staat Docker Desktop aan (*Engine running*)? Start het script opnieuw. Details: `docker compose logs --tail 50`. |
| Jobs blijven hangen | `docker compose logs -f worker`. Vastgelopen jobs worden na 30 min opnieuw ingepland; retry/annuleer op de Queue-pagina. |
| Mac: *"start-mac.command kan niet worden geopend"* | Rechtsklik → **Open** → **Open**. Of open Terminal in de map en typ `bash start-mac.command`. |
| Inlogvenster | Alleen als je `DASHBOARD_PASSWORD` in `.env` hebt gezet: gebruikersnaam `admin` met dat wachtwoord. |
| `Backend niet bereikbaar` | `docker compose ps` — draait `api`? Lokaal moet `BACKEND_URL=http://localhost:8000` zijn. |

---

## Ontwerpkeuzes

1. **Geen YouTube-downloads.** YouTube's voorwaarden verbieden het en jij vroeg om geen beveiligingen te omzeilen. Automatisering zit daarom in discovery, de inbox-map, deel-links, transcript-eerst-analyse en YouTube-embed-previews.
2. **API-first discovery** via de uploads-playlist (1 unit per pagina, stopt bij de gekozen periode) met RSS als gratis terugval — zoeken (`search.list`, 100 units) alleen bij het toevoegen op naam.
3. **Comment-tijdstempels als gratis "crowd signal"**: het sterkste externe bewijs dat een moment deelbaar is.
4. **3 passes:** goedkoop model leest alles, lokale code maakt de shortlist, duur model beoordeelt alleen die shortlist.
5. **Funnel-score** (meetkundig gemiddelde van Stop/Hold/Engage) in plaats van een gewoon gemiddelde.
6. **Het taalmodel rekent niet met tijden**: het verwijst naar zin-nummers; code rekent die om naar woord-exacte tijden. Geen clips die midden in een woord beginnen.
7. **Twee bronnen voor kandidaten** (AI + audio/tekst/comment-signalen): het taalmodel "hoort" geen gelach; de signalen missen subtiele humor.
8. **Database-queue** i.p.v. Celery/Redis: één onderdeel minder, de queue ís de Analysis Queue-pagina en overleeft herstarts.
9. **Same-origin API-proxy**: geen CORS-problemen, het API-token blijft server-side, grote uploads worden gestreamd.
10. **Werkt zonder betaalde keys**: heuristiek + `make demo` maken alles direct bekijkbaar.

### Juridisch (YouTube Terms of Service)

- Discovery gebruikt uitsluitend de **YouTube Data API v3**, de **publieke RSS-feeds** en **oEmbed**; previews gebruiken de **officiële embed-player**.
- ViralClip **downloadt, scrapet of omzeilt niets** op YouTube. Het bronbestand lever je zelf aan vanuit een bron waarvoor je toestemming hebt.
- Clips van andermans content publiceren vereist toestemming van de rechthebbende (veel creators hebben daarvoor een clipping-programma). Jij bent verantwoordelijk voor auteursrecht en de voorwaarden van TikTok/YouTube/Instagram.

### Roadmap

- TikTok/YouTube Analytics-koppeling om statistieken automatisch op te halen.
- Uitgebreidere vision: gezichtsexpressies en reacties per frame.
- "Cold open"-edits: de punchline als flash-forward vóór de opbouw.
- Captions-editor met woordcorrectie in de browser.
- Multi-user/teams met rollen.
