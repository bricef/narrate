# The anchor experiment (#87)

Can a generated news anchor stay recognisably the same person across many clips? This experiment
finds out which services do it best. One generated portrait, plus narration made by narrate,
goes to each talking-avatar service. The clips are then compared by eye on a blind review page,
and by a face-identity score.

It's a separate uv project, outside narrate itself, so its dependencies and keys don't touch the
tool. Nothing here is used by `narrate build`.

## The phases

| Phase | What it makes | Calls | Estimate |
|---|---|---|---|
| 0 | Four lines (L1–L3 about 10 s each, D30 about 30 s), each in a male and a female voice, with narrate and ElevenLabs `eleven_v4` | 8 | about 1,730 ElevenLabs characters |
| 1 | Portrait candidates: 4 image models × 2 genders × 3 candidates, then you pick the best of each by eye | 24 | $2.22 |
| 2 | The matrix: each picked portrait (8) × each avatar service (4), speaking L1 | 32 | $27.56 |
| 3 | The two best services from phase 2, with the best portrait per gender, on L2, L3 and D30: does the face hold across clips and over 30 s? | 12 | $22.46 at most |
| 4 | Optional, off by default: movement shots with no speech, from the same portrait (Gemini Omni Flash, Kling 3.0) | 4 | $3.55 |

Phase 3 is costed with the two dearest services until you choose them, so the plan never
under-estimates. `python -m anchor plan` prints every call, what it sends and its cost.

## Running it

```sh
cd experiments/anchor
uv sync
uv run python -m anchor inputs        # the lines, prompts and every service's settings, to review
uv run python -m anchor plan          # every call and its estimated cost (no network)
uv run python -m anchor keys          # which services have their keys set
```

Then run phase by phase. Each command first lists what it would make, and spends only with
`--spend`:

```sh
uv run python -m anchor audio --spend                 # phase 0 (ElevenLabs credits)
uv run python -m anchor portraits --spend             # phase 1; --only flux for one model
uv run python -m anchor pick flux male 2              # choose a candidate, for each model and gender
uv run python -m anchor clips --phase 2 --spend       # phase 2; --only hedra for one service
uv run python -m anchor review                        # runs/<run>/review/index.html, blind
# choose phase 3's services and portraits in plan.yaml (or pass --avatars hedra,kling)
uv run python -m anchor clips --phase 3 --spend
uv run --extra score python -m anchor score           # face similarity; then review again
uv run python -m anchor review --reveal               # the same page with the names shown
uv run python -m anchor ledger                        # what's been spent, call by call
```

## Keys

Put them in `experiments/anchor/.env`, which git ignores, as `NAME=value` lines. Variables
already set in the shell take precedence.

| Service | Variables |
|---|---|
| Nano Banana 2.1, Gemini Omni Flash | `GEMINI_API_KEY` (a paid-tier project) |
| GPT Image 2.5 Flare | `OPENAI_API_KEY` |
| FLUX 3 Image | `BFL_API_KEY` |
| Seedream 5.0 Pro (BytePlus ModelArk) | `ARK_API_KEY` |
| OmniHuman 1.5 (BytePlus Vision) | `BYTEPLUS_CV_API_KEY`, or `BYTEPLUS_AK` and `BYTEPLUS_SK` |
| Hedra | `HEDRA_API_KEY`, as `key_id:secret` |
| Kling Avatar, Kling 3.0 | `KLING_API_KEY`, or `KLING_ACCESS_KEY` and `KLING_SECRET_KEY` |
| HeyGen | `HEYGEN_API_KEY` |

The audio uses narrate's own `.env` (`ELEVENLABS_API_KEY`), because it runs `narrate audio` in
the repository.

## Spending safely

- **Nothing paid runs without `--spend`.** Without it, each command says what it would make and
  what that would cost.
- **The budget is enforced.** `budget_usd` in plan.yaml is a hard limit. Before each call, the
  ledger (`runs/<run>/ledger.jsonl`) adds what's been spent to what's still running. It refuses
  any call that would take that total past the budget.
- **Clips are never paid for twice.** Each clip's job is recorded as soon as it's submitted
  (`runs/<run>/jobs.json`). A run that stops or crashes collects those jobs next time instead
  of submitting them again. Hedra and HeyGen also get an idempotency key, so a resubmitted
  request isn't charged twice.
- **Outputs are never remade.** An output already in `runs/<run>/` is never asked for again. To
  remake one, delete it.

The ledger records each service's own report of a call's cost, where the service gives one.
Otherwise it records the estimate:

- **Reported by the service:** BFL and Kling (in credits or units).
- **Quoted before each clip:** Hedra, through its free estimate call.
- **Worked out from token counts:** OpenAI and Gemini.

## What's sent

- **The portrait.** Every avatar service gets exactly the same picture of a portrait: a
  1920×1080 JPEG, made from the picked PNG (`runs/<run>/upload/`).
- **The audio.** Every clip of a voice uses exactly the same WAV for its line.
- **Public links, for OmniHuman only.** OmniHuman accepts only URLs, so its portrait and audio
  are published on Litterbox for one hour (`uploads: litterbox` in plan.yaml). Set
  `uploads: none` to refuse instead.
- **Uploads to the other services.** The other services take uploads (Hedra, HeyGen) or base64
  (Kling).

## Judging the results

- **The review page** (`python -m anchor review`) shows every portrait candidate and every clip.
  Its letters stand for the image models and services, shuffled per run, and the files are linked
  under those letters too, so no name gives a service away. The key is in `review/key.json`.
- **The score** (`python -m anchor score`) samples 8 frames per clip and embeds the face in each
  with ArcFace (insightface buffalo_l, on the CPU). It then reports:
  - how similar each clip's face is to its portrait;
  - how similar a service's clips of the same anchor are to each other;
  - how many sampled frames had no face it could find.

  As a rough guide for ArcFace, about 0.4–0.5 and above is the same person, and 0.7 and above is
  very close.

  **Caveat:** the score measures identity, not quality. A service that barely moves the face
  scores high. Lip sync, expression and artefacts are for your eyes.

## Caveats, as of 2026-10

- **Kling:** the official API needs a prepaid resource pack, and the smallest is far beyond this
  budget. A trial pack may be available.
- **OmniHuman:** needs an enterprise-verified BytePlus account. It runs one job at a time.
- **Hedra:** its acceptable-use policy bars "fake personas", so check with Hedra that a generated
  presenter is acceptable.
- **HeyGen:** moderation refuses political content. The price per second via the API isn't
  confirmed; the ledger records the estimate.
- **Seedream:** its "AI-generated" watermark is on by default, and the harness turns it off.
- **Nano Banana and Omni:** add SynthID watermarks, which are invisible.
- **Veo 3.1 Fast:** its Gemini API model ends on 2026-10-22, so phase 4 uses Gemini Omni Flash
  instead.
- **Unverified until the first real call:** OmniHuman's request signing (when using AK/SK), and
  Gemini Omni's response time on a synchronous request.

## Tests

```sh
uv run --group dev pytest
```

The tests check:

- the plan's expansion and costs;
- the budget and the `--spend` gate;
- resuming without resubmitting a job;
- a complete run with fake services;
- the blind review page;
- each adapter's request, against canned responses shaped like its API's documentation.

They make no network calls.
