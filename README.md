# Ramen Bench

Ramen Bench is a static creative-coding benchmark viewer. Every model receives the same prompt: create a polished, interactive bowl of vegan ramen in one dependency-free HTML file.

The viewer follows the Fedora Showdown format: browse entries in the sidebar, view one result at a time, or compare up to four in a grid.

## Run locally

Serve the repository root with any static file server, for example:

```sh
python3 -m http.server 8000
```

Then open <http://localhost:8000>.

## Publish on GitHub Pages

The Pages workflow in `.github/workflows/pages.yml` deploys the repository root on every push to `main`. In the GitHub repository, choose **Settings → Pages → Source → GitHub Actions** once; the workflow needs no repository secrets.

## Run layout

Every run lives at `<vendor>/<model>/<variation>/`:

```text
<vendor>/<model>/<variation>/
├── index.html       # model's unmodified final answer
├── run.json         # model, harness, timing, tokens, and costs
├── transcript.json  # normalized, public session transcript
└── transcript.jsonl # optional untouched harness export
```

Add the run's manifest path to `registry.json`. Both JSON documents have versioned schemas in `schemas/`.
Copyable starting documents live in `templates/`.

Example registry item:

```js
"openai/gpt-5.6-sol/high/run.json"
```

`run.json` keeps an itemized token and cost breakdown, wall/API timing, harness provenance, and artifact paths. `transcript.json` stores normalized messages and tool activity for the built-in session viewer. It preserves reasoning the model intentionally shared as an ordinary message, while excluding hidden reasoning fields, encrypted payloads, and secrets.

## Import session records

Install the pinned importer dependencies with `python3 -m pip install -r requirements-backfill.txt`. Each harness has its own repeatable entry point:

```sh
python3 scripts/import_claude_code.py
python3 scripts/import_codex.py
python3 scripts/import_antigravity.py
python3 scripts/import_muse.py <redacted-export-directory>
```

The importers recover recorded transcripts, timing, and token usage from the corresponding local harness stores. Provider-recorded costs take precedence; otherwise the scripts use the pinned LiteLLM catalog when the recorded token categories are sufficient for a calculation.

Every entry point finishes by running `scripts/censor_transcripts.py`. This shared pass redacts personal paths, usernames, email and LAN addresses, and secret-like values, and replaces encrypted strings and large embedded media/base64 payloads with factual encoding-and-size markers. It leaves ordinary recorded messages, source code, and tool activity intact. Run `python3 scripts/censor_transcripts.py` by itself for a read-only audit, or add `--write` to normalize existing transcript JSON. Run `python3 scripts/validate_registry.py` to check the complete public registry, schemas, artifact paths, transcript provenance, and censoring state.

## Shared ratings API

Community votes are stored in Cloudflare D1 through the Worker in `api/`. No ratings are stored in the browser. A salted hash of the connecting IP and user agent prevents duplicate votes without retaining either raw value; voting again updates that visitor's existing rating.

To provision it:

1. From `api/`, run `npx wrangler@latest login`.
2. Run `npx wrangler@latest d1 create ramen-bench-ratings`.
3. Copy `wrangler.jsonc.example` to `wrangler.jsonc` and replace `REPLACE_WITH_D1_DATABASE_ID` with the returned ID.
4. Set a long random secret with `npx wrangler@latest secret put VOTER_HASH_SECRET`.
5. Run `npm run db:migrate:remote`, then `npm run deploy`.
6. Put the deployed Worker URL in `site.config.js` as `ratingsApiUrl`.

Keep `ALLOWED_ORIGINS` in `wrangler.jsonc` restricted to the GitHub Pages production origin and any intentional local development origins. Do not commit `wrangler.jsonc`, `.dev.vars`, or API tokens.
