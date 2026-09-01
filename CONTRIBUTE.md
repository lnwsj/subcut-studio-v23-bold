# Contributing to SubCut Studio v2.3 Bold

Welcome! This is the public codebase for **SubCut Studio v2.3 Bold** — a Thai subtitle + silence-cut web app. The repo is the working code (no secrets) of the production deployment at [https://subfree.cutdee.com](https://subfree.cutdee.com).

## Quick start (local dev)

```bash
# 1. Install deps (Python 3.11-3.12, FFmpeg, NVIDIA/CUDA recommended)
./scripts/install.sh

# 2. Copy + fill in env
cp app/.env.example app/.env
# Edit app/.env: set APP_AUTH_SECRET, APP_BROWSER_IDENTITY_SECRET

# 3. Run
./scripts/start.sh
# → http://localhost:18800
```

## Project structure

```
app/
├── main.py              # FastAPI entry
├── subcut_main.py       # SubCut routes
├── worker_main.py       # Worker process
├── backend/
│   ├── api/routes/      # 13 route files
│   └── services/        # business logic
└── frontend/static/     # HTML/CSS/JS (no build step)
scripts/                  # install/start/smoke_test
deploy/                   # systemd + docker
docs/                     # API.md, ARCHITECTURE.md
```

## Tech stack

- **Backend**: FastAPI + SQLite (file DB) + uvloop
- **Worker**: separate process, polls jobs with lease/heartbeat
- **ASR**: OpenAI Whisper / faster-whisper (CUDA)
- **Subtitle**: libass (FFmpeg) for ASS burn-in
- **Frontend**: vanilla JS (no React/Vue/build) — mobile-first
- **Storage**: local FS (userspaces per user)

## 26 subtitle templates (v2.3 has 21 in code; UI exposes 7)

Backend catalog (`autosu_settings.py`) has 21 templates. The UI dropdown currently shows 7 — see "Adding 19 more" in the [roadmap](#roadmap).

| ID | Visual |
|---|---|
| default | white + black outline |
| yellow_pop | TikTok yellow |
| box | dark backdrop |
| karaoke_highlight | word-by-word fill |
| luxury_gold | gold italic |
| comic_burst | bold comic |
| random_mix | randomize per render |
| outline | thicker outline |
| neon | cyan glow |
| blue_glow | blue glow |
| sticker | pink-edged sticker |
| green_pop | green gaming |
| red_alert | red urgent |
| purple_punch | purple creator |
| cyan_ice | cool cyan-white |
| orange_sale | orange promo |
| white_shadow | minimal white |
| black_label | high-contrast label |
| retro_pixel | retro headline |
| soft_pastel | soft pastel |
| lime_pop | bright lime |

## Roadmap

- [ ] **Expose all 21 templates in UI** (currently only 7 in dropdown)
- [ ] **Add 5 more templates** (news_ticker, bold_thunder, sticker_pop, instagram, subtitle_heavy) → 26 total
- [ ] **Thai word grouping** (PyThaiNLP) — merge whisper 1-2 char fragments into real Thai words
- [ ] **large-v3 Whisper** support (currently medium)
- [ ] **Versioned subtitle editor** with diff/restore
- [ ] **Lease fencing token** to prevent stale worker overwrites
- [ ] **AI Subtitle QC** (cue overlap, CPS>20, low confidence)

## Coding style

- Python: type hints everywhere, snake_case, docstrings
- Frontend: vanilla JS, no build step, semantic HTML
- No frontend framework — keep it portable
- All Thai copy in templates, not hardcoded
- Tests: `pytest` for backend, `python scripts/smoke_test.py` for E2E

## Security & secrets

- **NEVER commit `.env`** — it is in `.gitignore`
- Use `.env.example` for templates (placeholders like `__REDACTED_SET_VIA_LOCAL_ENV__`)
- Generate secrets with: `python -c "import secrets; print(secrets.token_urlsafe(48))"`
- Production runs on [https://subfree.cutdee.com](https://subfree.cutdee.com) behind nginx + Cloudflare

## License

Proprietary. Internal codebase made public for educational/portfolio purposes. Please contact the owner before commercial use.

## Contact

- Live demo: https://subfree.cutdee.com
- CaptionCut Studio (sister app): https://aisub.cutdee.com
