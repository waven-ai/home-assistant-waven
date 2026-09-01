# Waven for Home Assistant

Cloud-quality speech for Home Assistant's Assist — your own **cloned voice**,
**streaming multilingual** transcription, and **per-pipeline model choice** —
running on Waven's GPUs so an HA Green or a Pi 4 doesn't need an NVIDIA box in
the closet.

> **The wedge:** say a French sentence into an English Assist pipeline and watch
> the transcript keep up; have HA read the kids a bedtime story in your own
> voice. Nabu Casa Cloud can't do either at any price.

Wake word, voice activity detection and intent matching stay **local**. Only the
post-wake utterance (transcribed) and the response text (synthesised) ever reach
Waven. See [`docs/home-assistant-integration-spec.md`](https://github.com/bobbaboui/waven.ai/blob/main/docs/home-assistant-integration-spec.md)
for the full product rationale, competitive framing, and latency budget.

## Two ways to install

| | [`custom_components/waven/`](./custom_components/waven) | [`waven-wyoming-proxy/`](https://github.com/bobbaboui/waven.ai/tree/main/homeassistant/waven-wyoming-proxy) |
|---|---|---|
| **What** | Native HACS integration (runs inside HA) | Standalone Wyoming add-on / Docker image |
| **For** | Most people — five clicks, full options | Privacy-minded, HA Core in a venv, or non-HA Wyoming clients (Rhasspy) |
| **STT** | **Streaming** WebSocket (partials decode live) | Batch (single final transcript per utterance) |
| **Voice routing** | Per-response-category hybrid policy + per-call override | Single default voice |
| **Daily cap + audit card** | Yes, in HA with notifications | Process-local cap |
| **Setup** | Settings → Devices & Services → **Waven** → paste key | Add-on Store → add our repository → install → Wyoming Protocol |

Both speak to the same Waven account and the same minute pool. Pick the
integration unless you specifically want our code out of HA's process.

### Custom integration (recommended)

1. Add this repository as a [HACS](https://hacs.xyz) custom repository (category
   *Integration*), install **Waven**, and restart Home Assistant.
2. **Settings → Devices & Services → Add Integration → Waven**, paste your
   `wvn_…` API key (from the Waven dashboard). We validate it and show your tier.
3. Open the integration's **Configure** panel to assign voices per response
   category, choose the STT model, and set the daily cap.
4. In your **Assist pipeline**, pick *Waven* for speech-to-text and
   text-to-speech.

> **One-time prerequisite:** make sure the Waven account has accepted the
> current Terms of Service and Privacy Policy in the dashboard. Speech-to-text
> works regardless, but **text-to-speech is blocked (HTTP 428) until the terms
> are accepted** — the integration posts a Home Assistant notification telling
> you so the first time it happens.

### Wyoming add-on

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add
   `https://github.com/waven-ai/waven-wyoming-proxy` — a Home Assistant add-on
   repository — then install **Waven Wyoming Proxy** from the store.
2. Paste your `wvn_…` API key in the add-on's **Configuration** tab, pick a
   default voice, and start it.
3. **Settings → Devices & Services → Add Integration → Wyoming Protocol**,
   host = the add-on, port = `10300`; select the STT and TTS services it
   advertises in your Assist pipeline.

Every option, the plain-Docker path, and the manual `/addons`-copy fallback are
in [`waven-wyoming-proxy/README.md`](https://github.com/bobbaboui/waven.ai/blob/main/homeassistant/waven-wyoming-proxy/README.md).

## Voice cloning — the hybrid policy

Cloning (OmniVoice) is heavier than the stock Kokoro voices, so by default the
integration routes by **response category** to keep first impressions snappy:

| Response type | Default voice | Why |
|---|---|---|
| Quick acks ("OK", "Done") | Kokoro stock | Must feel instant. |
| Confirmations ("Turning off the kitchen lights") | Kokoro stock | Sub-second matters here. |
| Long announcements (morning briefing, bedtime story) | Your cloned voice | The ~600 ms clone overhead amortises over a long utterance — this is the magic moment. |
| Errors | Kokoro stock | Errors should be instant. |

Assign each slot in the **Configure** panel. You can also override per call with
the standard `voice:` option on `tts.speak` (e.g. `gallery:<your-voice-id>`), or
set every slot to your clone if you want it always.

## The daily cap (a hard safety rail)

A stuck microphone or a runaway capture automation could otherwise stream hours
of audio. The integration enforces a **hard per-household daily cap** (default
30 min combined STT + TTS, adjustable). At 80 % it posts a Home Assistant
notification; at 100 % cloud voice **pauses until local midnight** — but your
automations keep running and your lights still turn off. The cap, the usage
accounting, and the "what did Waven hear?" audit card are all enforced
**in Home Assistant** (the backend has no per-household cap), which is also where
the persistent storage and notification surface live.

## "Why not just run Whisper locally?"

- **Speed on Pi-class hardware is bad.** Whisper-tiny on a Pi 4 is several times
  slower than real time for noisy utterances; the user notices every command.
- **Quality on Pi-class hardware is bad.** Whisper-tiny WER on conversational
  speech with a mid-quality mic is well above 10 %; commands miss often enough
  that people give up.
- **Local voice cloning doesn't exist.** Local TTS is Piper-class — no
  open-weight cloning model runs comfortably on a Pi.

"Whisper-large on a 3090 in the closet" is a fine answer — for the small,
technical audience that already built it. This integration is for everyone else:
cloud-quality voice without the GPU.

## Privacy

- **What never leaves the house:** wake-word audio, VAD audio between wake
  events, intent decisions, sensor/automation state, your device list.
- **Retention:** the utterance audio Home Assistant sends to the speech-to-text
  endpoints it uses is **never stored** on Waven's servers — it is held only for
  the length of the request (in memory, or in a temporary file the server
  deletes when the request ends) and never written to Waven's output cache.
  Synthesized responses sit in Waven's standard 72-hour output cache. Turning
  off *Allow Waven's short-term audio cache* in options stamps every request
  with `X-Waven-Retain-Audio: false`, **which the backend enforces**: each
  flagged clip is deleted as soon as Home Assistant has downloaded it, and a
  clip that is never downloaded is removed by the hourly server-side cleanup
  sweep — normally within minutes of that sweep, and in the worst case a little
  over an hour after it was rendered. The trade-off rides on the same switch:
  because the clip is gone after the first download, a response whose download
  is interrupted is lost (not retried) rather than kept, and Home Assistant
  stays silent for that one utterance. Reference audio for cloned voices you
  saved to your gallery is user-managed persistent storage and is not affected
  by this toggle — delete the voice in the dashboard to remove it. (See
  `const.RETENTION_HEADER` and `backend/app/request_source.py`.)
- **Audit:** the *Recent voice requests* sensor exposes a local log (timestamp,
  model, duration) so you can answer "what did Waven hear last night?" without
  leaving HA.

## For developers

```
homeassistant/
├── custom_components/waven/    # the HACS integration (native HA stt/tts entities)
├── waven-wyoming-proxy/        # the standalone Wyoming add-on / Docker image
│   └── tests/                  # World C — HA-free Wyoming proxy tests
├── hacs.json                   # HACS metadata (this dir is the integration repo root)
├── pytest.ini                  # HA-free suite config (asyncio auto)
├── tests/                      # World A — HA-free unit tests (api client + pure modules)
└── tests_ha/                   # World B — HA-harness tests (config flow, coordinator, entities)
```

Tests run in **two environments that must stay separate** — the
`pytest-homeassistant-custom-component` plugin and the synthetic-`waven`
bootstrap in `tests/conftest.py` can't share one pytest session:

```bash
cd homeassistant

# Worlds A + C — fast, no Home Assistant install (api client incl. its
# WebSocket path, the Wyoming proxy, and the pure const/audio/quota/routing modules)
pip install -r requirements-test.txt
python3 -m pytest tests waven-wyoming-proxy/tests -q

# World B — full HA runtime (config flow, coordinator, entities, diagnostics, setup)
pip install -r requirements-test-ha.txt
python3 -m pytest tests_ha -q
```

CI gates both as the `ha-integration-test` and `ha-runtime-test` jobs.

### Endpoints used (all verified against `backend/app/routers`)

| Purpose | Call |
|---|---|
| Validate key + tier + quota | `GET /api/v1/user` |
| List cloned voices | `GET /api/v1/voice-gallery` |
| TTS | `POST /api/v1/generate` (multipart) → `GET /api/v1/audio/{uid}/{file}` |
| Streaming STT | `WS /api/v1/stt/transcribe/stream` (config frame → `pcm_f32le` frames → `{"type":"end"}`) |
| Batch STT (proxy) | `POST /api/v1/stt/transcribe` (multipart) |

Auth is `Authorization: Bearer wvn_…` on every call; every request also carries
`X-Waven-Source` — the integration sends `home-assistant` and the Wyoming proxy
sends `home-assistant-wyoming`. The backend allowlists both and folds them into a
single `source="ha"` value on the `Generation` / `Transcription` row (any other
or absent value stays `NULL`, the implicit dashboard/app/API bucket). That slice
surfaces under `usage.source_breakdown.ha` in `GET /api/v1/user` — display-only,
so it never gates admission or changes the shared STT+TTS quota pool.

### Publishing (maintainers)

The monorepo is the single source of truth; two **public GitHub** repos are
downstream mirrors that HACS / GHCR consume:

| Source (here) | GitHub repo | What runs there |
|---|---|---|
| `custom_components/waven/` + `hacs.json` + `README.md` | `waven-ai/home-assistant-waven` | `.github/workflows/validate.yml` (hassfest + HACS); HACS installs from its releases |
| `waven-wyoming-proxy/` | `waven-ai/waven-wyoming-proxy` | `.github/workflows/build-image.yml` → multi-arch `ghcr.io/waven-ai/waven-wyoming-proxy` |

Bump the version in `custom_components/waven/manifest.json` (integration) or
`waven-wyoming-proxy/config.yaml` (proxy) to cut the next release. The test-only
files (`tests/`, `tests_ha/`, `requirements-test*.txt`, `pytest.ini`) are pruned
from the integration mirror.

**Release ordering for 0.1.1 (retention opt-out).** The backend half must be
live in production **before** either client is published, because
`diagnostics.py` hardcodes `retain_audio_opt_out_enforced_by_server: True` —
a build-time assertion about `api.waven.ai` (the default host) that is only
true once the enforcing backend is deployed. Order: (1) backend change to
prod; (2) force-push the integration mirror with `manifest.json` at `0.1.1`;
(3) for the add-on, push the `v0.1.1` tag and let the GHCR image finish
building **before** any add-on repo advertises 0.1.1 — otherwise a household
updates to a manifest whose image does not exist yet. `publish.sh` enforces
step 3 by splitting `--push` (tag) from `--advertise` (main); see below.

**How to push.** `scripts/ha-mirror/publish.sh` builds both mirror trees
(prunes the test-only files, hoists the add-on's whole `.github/` to the mirror
root, rewrites monorepo-relative links to GitHub URLs), verifies the
versions/URLs agree with `--org`, and — only with `--push` — force-pushes one
squashed commit plus a `v<version>` tag (and a GitHub release for the
integration, which is what HACS lists). Without `--push` it is a dry run you
can execute anywhere. It refuses to move an existing tag to different content,
so a re-publish means a version bump, and it skips a push whose tree is already
byte-identical to what is published. `all` publishes integration then proxy,
matching the ordering above.

The **proxy** publish is deliberately two invocations, because force-pushing
that mirror's `main` *is* the act of advertising the version (Supervisor reads
the add-on repository from its default branch, then pulls `<image>:<version>`).
`--push` sends the tag, which starts the image build; `--advertise` moves
`main`, and refuses unless `ghcr.io/<org>/waven-wyoming-proxy:<version>` is
already **anonymously** pullable — the same request a household's Supervisor
makes, so it catches both "the build isn't done" and "the package is still
private".

```bash
scripts/ha-mirror/publish.sh                # dry run: build + verify + show plan
scripts/ha-mirror/publish.sh integration --push       # main + tag + GitHub release
scripts/ha-mirror/publish.sh proxy --push             # tag only -> starts the image build
scripts/ha-mirror/publish.sh proxy --push --advertise # once the image is public: move main
scripts/ha-mirror/publish.sh proxy --push --create    # also create a missing mirror repo
```

The proxy mirror `publish.sh` builds is a full Supervisor **add-on repository**:
`repository.yaml` at the root with the add-on itself in a
`waven-wyoming-proxy/` subdirectory (and the build workflow hoisted to the
mirror root, where Actions reads it) — so households add the repo URL in the
Add-on Store and install one-click. The layout is generated and verified by
`build_proxy()`/`verify_proxy()`; nothing in this monorepo moves.

**One-time after the first image push: make the GHCR package public.** A
package created by a GitHub Actions push starts **private**, and nothing in
`build-image.yml` changes that. Both documented ways to run the add-on — the
Supervisor pulling it for a household, and the plain `docker run` in
`waven-wyoming-proxy/README.md` — pull **anonymously**, so while the package is
private every install fails with an opaque registry error (a `denied` /
`manifest unknown` that names no permission problem) on the user's Pi, with the
image sitting right there in the org. Flip it once, after the first successful
`ghcr.io/waven-ai/waven-wyoming-proxy` push and **before** advertising the
version: the package page → **Package settings → Danger Zone → Change
visibility → Public**. It sticks across later pushes; only a brand-new package
name needs it again.

The mirrors are not wired into CI on purpose: nothing in the monorepo depends
on them — the integration and proxy are fully tested here
(`ha-integration-test` / `ha-runtime-test` in `.github/workflows/ci.yml`) — and
a publish is a user-facing release that should follow the ordering above, not
every merge to `main`. **As of 2026-08-24 the `waven-ai` GitHub org (and so
both mirror repos) does not exist yet**; 0.1.1 is unpublished until it does.

**Brand icon.** The Devices & Services / HACS icon comes from the
[`home-assistant/brands`](https://github.com/home-assistant/brands) repo, not
this one: open a PR adding `custom_integrations/waven/{icon.png,logo.png}` (icon
256×256 plus a 512×512 `@2x`; logo a trimmed landscape PNG). It is cosmetic for
HACS custom-repo installs — the HACS validation `ignore`s the brands check until
that PR lands — and mandatory for an eventual Home Assistant core submission.

### Backend support (implemented + optional)

`X-Waven-Source` attribution (a nullable `source` column on `Generation` /
`Transcription` plus the `source_breakdown` block in `GET /api/v1/user`) is
**wired** backend-side, and so is **`X-Waven-Retain-Audio` enforcement**: a
flagged `POST /api/v1/generate` stamps a `.noretain` sidecar next to the output
file *before* the bytes are written (a stamp that fails is a 500 with no audio
written — fail closed), the marker follows the clip through the mp3/ogg
transcode, and `GET /audio` deletes clip + sidecar right after the first
**complete** download.

A **ranged** read (HTTP `Range`) deliberately does NOT burn the clip — a
browser or media player probing the file must not be able to destroy it — so a
ranged fetch leaves the clip to the sweep instead; neither HA client sends
`Range`. A full download that dies mid-transfer *does* burn it (the server
cannot tell a finished response from a dropped one), which is exactly why an
interrupted TTS fetch costs one lost utterance rather than being retried:
privacy over retention.

Never-fetched flagged clips fall to the cleanup sweep: it removes them once
they are older than `NO_RETAIN_AUDIO_TTL_MINUTES` (default 10) instead of the
72-hour `AUDIO_TTL_HOURS`. Worst case at the defaults is **≈75 min**, not 10:
10 (TTL) + `AUDIO_CLEANUP_MIN_INTERVAL_SECONDS` (3600, the minimum gap between
sweeps) + one `CLEANUP_INTERVAL_SECONDS` (300) tick to notice that gap has
elapsed. A sweep that stops running altogether is alerted on at 2 h
(`CleanupWedged`). Orphan markers are only swept once they are older than that
same backstop, so a marker stamped mid-request cannot be pulled out from under
it. (See `backend/app/request_source.py`, `routers/audio.py`, `cleanup.py`.)

Scope: enforcement is wired into `/generate` only — `/generate-long` and the
async TTS job path are a deliberate follow-up, since neither HA client calls
them. The STT endpoints the HA clients use (sync `POST /api/v1/stt/transcribe`
and the streaming WS) never persist utterance audio — it lives only for the
length of the request, in memory or in an auto-deleted tempfile, and never
reaches the output cache — so they need no enforcement hook; the **async** job
endpoint `POST /api/v1/stt/transcribe/jobs`, which neither HA client calls,
*does* store the upload in Redis for 1 h and sits in the same follow-up bucket
as `/generate-long` and async TTS jobs. Gallery reference audio is user-managed
persistent storage, out of scope for the toggle by design. Two additions remain
optional:

1. A `GET /api/v1/usage/ha-audit?from=&to=` endpoint returning per-request rows
   (optional — the integration already keeps a local audit ring buffer).
2. A server-side per-household daily cap (optional; needs a `household_id`).
   Until then, the clients' local caps are the safety rail.
