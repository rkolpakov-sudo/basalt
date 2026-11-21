# AGENTS.md

Basalt is a local inference engine built for NVIDIA Blackwell (sm_120) on Linux and Windows x64. A model runs on one Blackwell GPU, optionally with more GPUs as peer expert tiers, and, for the mixture-of-experts models, system RAM holding the rest (a dense model runs fully on one GPU). The C++/CUDA engine is in `src/` and `include/`, each model's own code under `src/models/<model>`, and the Python server with an OpenAI- and Anthropic-compatible API is in `serve/`.

## If you are here to install this fork

<!-- local fork: keep this block near the top when merging upstream -->
The `local` branch carries local changes: a reworked web UI (`serve/web/index.html`, `chat.js`, `settings.js`, `extra.js`, `extra.css`),
`serve/api/config_api.py` and the routes it adds in `serve/handler.py` (`/api/config` GET and POST, `/api/model/caps`, `/api/vram`,
`/api/restart`), the launcher scripts (`Basalt.bat`, `serve_ctl.ps1`) and a tuned `basalt_config.json`. `README.md` and
`CONTRIBUTING.md` describe the upstream project and do not cover installing this fork.

- **Install by [INSTALL-LOCAL.md](INSTALL-LOCAL.md)**, in the repo root. Do not build from source, do not use the upstream release archives.
- `bin/` and `models/` are not in git and are copied in by hand; `data/` is tracked and comes with the clone.
- **Keep `basalt_config.json` as it is** (context 131072, the VRAM tuning): tune on a copy and measure before replacing it.
- The rules below are about changing the engine, not about installing it.

## Rules

[CONTRIBUTING.md](CONTRIBUTING.md) is the project's rulebook and applies to agents in full: scope, platform and model layering, one fast path, what every change must include, benchmark hygiene, commits and docs. The rules below are only for agents.

- **Design the end state first.** Before writing code, design the architecture properly and read similar code. If a model already implements the feature, reuse that logic or make it shared. Do not propose features or improvements before testing them and checking the architecture.
- **Flag odd numbers.** Sanity-check every result before reporting it (size against speed, run-to-run spread, earlier numbers) and report anything odd with its suspected cause.
- **Read before answering.** Check the code, docs and existing results before stating a fact about Basalt.
- **No amends, no force-pushes.** Always make new commits.
- **Don't push or publish anywhere** (GitHub, HF, any remote) without the user's approval.
- **Don't write tools nobody asked for.**
