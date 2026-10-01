# Het selectie-algoritme

> Kan de AI daadwerkelijk de goede momenten herkennen?

Dit document beschrijft hoe ViralClip AI van een lange video naar een Top 5 komt, waarom elke stap bestaat en waar je in de code kunt bijsturen. Het is bewust **geen** `LLM → "is dit viral?"`-workflow, maar een ranking-pipeline waarin een taalmodel één van meerdere signaalbronnen is.

## Overzicht

| Stage | Wat | Code |
|---|---|---|
| 0 | Voorbereiding: woorden → zinnen, audio-luidheid, camerawissels, comment-hotspots | `ai/transcript.py`, `video/audio_features.py`, `video/scenes.py`, `services/youtube.py` |
| 1 | **Pass 1** — kandidaten genereren (recall): signaal-vensters + goedkoop model over het hele transcript; **Pass 2** — lokaal samenvoegen + shortlist | `ai/candidates.py` |
| 2–7 | **Pass 3** — content-, hook-, context-, retentie-, emotie- en engagement-analyse van alleen de shortlist (slim model of heuristiek) | `ai/evaluator.py`, `ai/prompts.py`, `ai/signals.py` |
| 10 | Clip-optimalisatie: exacte, hook-first grenzen | `ai/boundaries.py` |
| 9 | Viral Score (funnel) + persoonlijke bijsturing | `ai/scoring.py`, `services/learning.py` |
| 5* | Optionele vision-check van alleen de beste kandidaten | `ai/vision.py` |
| 8 | Duplicate-detectie + diverse Top-K (MMR) | `ai/dedupe.py` |

## Stage 0 — Voorbereiding (gratis, lokaal)

- **Zin-eenheden.** Woorden met timestamps worden gesplitst op leestekens, pauzes ≥ 0,7 s en een maximum van ~11 s / 32 woorden. Deze eenheden zijn de "atomen" waarover het LLM redeneert.
- **Audio-profiel.** Luidheid per 0,25 s (dBFS), robuuste z-score t.o.v. de hele video (mediaan/MAD over spraak). Hoge z = geschreeuw, gelach, reacties. Stiltes = > 18 dB onder de mediaan.
- **Camerawissels** via ffmpeg's scene score (helpt reframing en is een tempo-signaal).
- **Audience hotspots.** Tijdstempels uit de top-100 YouTube-comments, gewogen met `1 + log(1 + likes)`, geclusterd binnen 8 s. Hoofdstuk-lijsten (veel tijdstempels in één comment) tellen minder.

## Stage 1 — Kandidaten (recall boven precisie)

Twee onafhankelijke generatoren, samengevoegd op tijd-overlap (IoU ≥ 0,5):

1. **Signaal-vensters.** Voor elke zin als start wordt de beste eindzin gezocht binnen 0,8×min … 1,3×max duur. Score = audio-pieken + hook-woorden (NL/EN-lexicon: intensiteit, verrassing, humor, controverse, verhaal, teaser, inzet) + comment-hotspots − stiltes − intro/outro. Non-maximum suppression houdt diverse vensters over. Dit vangt momenten die een LLM niet kan "horen".
2. **Pass 1 — LLM** (snel/goedkoop model, standaard `gpt-5-mini` / `claude-haiku-4-5`) leest het transcript in blokken van ~6 min met 30 s overlap, krijgt de hotspots en audio-pieken als hints, en noemt per blok 3–12 momenten als **zin-ID's** (start, eind, hook-zin), met categorie en korte reden.

**Pass 2 (lokaal, gratis):** de samengevoegde lijst wordt ontdubbeld, gesorteerd op prioriteit (LLM-inschatting × signaal) en de top `candidate_count` (standaard 30) gaat door.

## Stages 2–7 — Pass 3: beoordelen (precisie)

**Pass 3** (slim model, standaard `gpt-5` met reasoning *low*; kwaliteit *Beste*: *medium*; Claude: `claude-sonnet-5-5` / `claude-opus-5-5`) beoordeelt kandidaten in batches van 6 — batching laat het model vergelijken, wat de kalibratie verbetert, en deelt de gecachete rubric. Per kandidaat ziet het model de exacte zinnen + 3 zinnen context ervoor/erna. Het vult in:

- `first_seconds` — wat een kijker letterlijk hoort in de eerste ~2 s (**hook-analyse**),
- `viewer_reaction` — de eerlijke reactie van een willekeurige scroller (**het TikTok-kijkersperspectief**),
- 12 scores 0–100 met ankers: *50 = gemiddeld moment, 90+ = zeldzaam*,
- vlaggen (`needs_context`, `inside_joke`, `weak_payoff`, `sponsor_or_ad`, `intro_or_outro`, …),
- een **verdict** (`skip/maybe/good/great`) als consistentiecheck,
- een verbeterde **edit** (start/eind-zin), titel, uitleg en nadrukwoorden.

De 12 dimensies: Hook, Hook Strength (hoe snel komt de info), Curiosity, Emotion, Surprise, Humor, Shareability, Comment Potential, Retention, Context (begrijpelijk zonder video), Payoff, Rewatch.

**Heuristische modus** (zonder API key) levert exact hetzelfde formaat uit deterministische features: hook-woorden in de eerste zin, tijd tot het eerste sterke woord, stopwoord-/verwijswoord-starts, luidheid in de eerste 2 s, piekpositie (payoff laat = goed), spreektempo, stiltes, lach-tokens, controverse-woorden, comment-hotspots.

## Stage 10 — Clip-optimalisatie

`optimize_boundaries()` maakt van een (zin-)span een exacte clip:

1. **Randen opschonen:** begin- en eindzinnen die puur stopwoord, begroeting, intro ("Vandaag gaan we…") of outro/CTA ("abonneer", "laat het weten in de comments") zijn, vallen af.
2. **Context-reparatie:** begint de clip met een verwijswoord ("Ze heeft…", "Hij zei…") en staat de antecedent-zin er direct voor, dan gaat die mee.
3. **Payoff-bewust einde:** komt de luidste reactie direct ná het eind, dan wordt de volgende zin toegevoegd (eventueel ten koste van opbouw vóór de hook).
4. **Te kort:** eerst vooruit uitbreiden (payoff), dan opbouw ervoor.
5. **Te lang:** de beste aaneengesloten sub-span zoeken: past in de duur, start op de hook, bevat de audio-piek/hotspot, start niet op stopwoorden of verwijswoorden, eindigt op een zinseinde.
6. **Stopwoorden eraf:** "Nou, eh, dus…" / "Zoals ik al zei…" aan het begin worden weggeknipt (intro-zinnen niet — die worden als geheel vermeden, anders begint de clip midden in een zin).
7. **Padding:** 0,12 s vóór het eerste woord (nooit in het vorige woord), en na het laatste woord 0,35 s + zolang de audio nog "opgewonden" is (lach laten landen, max 1,2 s), nooit tot in het volgende woord.
8. **Dode lucht eruit:** pauzes > 0,6 s worden ingekort tot ~0,25 s (jump cuts). Het resultaat is een lijst bron-segmenten; captions worden naar de nieuwe tijdlijn omgerekend.

Standaardduur 12–18 s (doel 15 s), per creator instelbaar; maximaal 3 s extra om een zin af te maken.

## Stage 9 — Viral Score

```
stage_k       = gewogen gemiddelde van de dimensies in stadium k
                  Stop   = Hook, Hook Strength, Curiosity
                  Hold   = Retention, Payoff, Context, Surprise, Emotion
                  Engage = Shareability, Comment Potential, Humor, Rewatch
content       = gewogen MEETKUNDIG gemiddelde van (Stop, Hold, Engage)
raw           = (1 − a)·content + a·signaalscore          (a = 20% standaard, alleen in LLM-modus)
viral_score   = raw × strafpunten(vlaggen) + crowd-bonus (≤ +6) + persoonlijke bijsturing (≤ ±12)
                begrensd door het verdict (skip ≤ 45, maybe ≤ 76), afgekapt op 0–100
```

**Waarom meetkundig?** Het gedrag op TikTok is een trechter: *P(stoppen) × P(blijven kijken) × P(reageren/delen)*. Een rekenkundig gemiddelde laat een clip met een dode eerste 2 seconden hoog scoren als hij "deelbaar" is — maar niemand ziet het deelbare deel. Het meetkundig gemiddelde laat het zwakste stadium zwaar doorwegen. Test: `tests/test_scoring.py::test_weak_hook_is_a_bottleneck`.

**Waarom een signaal-blend?** LLM's kunnen audio niet horen en neigen naar scores rond 60–80. Objectieve signalen (luidheid, comment-hotspots) corrigeren beide.

Alle gewichten (12 dimensies + 3 stadia + blend + personalisatie-invloed) zijn instelbaar op de Settings-pagina.

## Stage 8 — Geen vijf bijna-identieke clips

- **Tijd:** overlapt een clip > 35 % (van de kortste) met een gekozen clip → uitgesloten.
- **Inhoud:** cosine-similariteit van embeddings (OpenAI) of lokale TF-IDF (unigrammen + bigrammen). ≥ 0,82 → duplicaat.
- **MMR:** kies herhaaldelijk de clip met de hoogste `score − 25 × max_similariteit(gekozen)`, zodat de Top 5 verschillende momenten bevat.

Elke beoordeelde kandidaat wordt met zijn score en reden opgeslagen, zodat je op de videopagina ziet *waarom een moment niet gekozen is*.

## Het persoonlijke leersysteem

Elke clip bewaart de featurevector waarmee hij gescoord is (12 dimensies, hook-, audio-, lexicon- en crowd-features, duur, categorie). Trainingsdoel per clip:

- feedback: 🔥 = 1,0 · 👍 = 0,7 · 👎 = 0,2 · ❌ = 0,0,
- en/of performance: views (log, genormaliseerd per creator), completion rate en engagement-ratio.

Vanaf 12 clips wordt een ridge-regressie getraind (automatisch elke 10 nieuwe beoordelingen, of via Analytics → *Leermodel trainen*). De bijsturing op nieuwe clips is begrensd (±12 punten) en schaalt met de **cross-gevalideerde** betrouwbaarheid en het aantal voorbeelden — een model dat niets voorspelt, stuurt niets bij. De Analytics-pagina toont inzichten zoals *"Clips met de interessante info direct in de eerste seconde presteren gemiddeld 34% beter"* en een kalibratiegrafiek (scoregroep → aandeel positief beoordeeld).

## Kosten per stap

| Stap | Kosten | Gemeten tokens |
|---|---|---|
| Stage 0 + signaal-vensters + Pass 2 + boundaries + dedupe (TF-IDF) + rendering | gratis (lokaal) | — |
| Pass 1 | goedkoop model over het hele transcript | ~460 in + ~170 uit per videominuut |
| Pass 3 | slim model, alleen over ~30 kandidaten (onafhankelijk van de videolengte) | ~350 in + ~150 uit per kandidaat |
| Transcriptie | Whisper API $0,006/min, of lokaal/SRT gratis | — |
| Vision | alleen top N, standaard uit | — |

Plus verborgen reasoning-tokens (als output gefactureerd). De schatter staat in `backend/app/ai/costs.py`; de tabel voor jouw instellingen staat in Settings → AI & modellen. Een fatale AI-fout (ongeldige key, tegoed op, API onbereikbaar) stopt de AI-passes direct — geen tientallen mislukte aanroepen — en de analyse gaat heuristisch verder, met een melding op het dashboard.

## Zelf verbeteren

- **Prompts:** `backend/app/ai/prompts.py` (statisch, dus cache-vriendelijk).
- **Lexicon NL/EN:** `backend/app/ai/signals.py` (`LEXICON`) en `backend/app/ai/transcript.py` (stopwoorden, intro/outro-zinnen).
- **Heuristische scores:** `heuristic_dimension_scores()` in `signals.py`.
- **Funnel & strafpunten:** `backend/app/ai/scoring.py`.
- **Een eval bouwen:** beoordeel 50–100 clips in het dashboard; de kalibratiegrafiek op Analytics laat zien of hogere scores ook echt betere clips zijn. Gebruik dat als meetlat voordat je prompts of gewichten aanpast.
