# Ramen Bench

Ramen Bench is a static creative-coding benchmark viewer. Every model receives the same prompt: create a polished, interactive bowl of vegan ramen in one dependency-free HTML file.

The viewer follows the Fedora Showdown format: browse entries in the sidebar, view one result at a time, or compare up to four in a grid.

Ramen Bench also ships a Harbor task with a blind vision judge and an effort-connected Pareto viewer at `scores.html`. See [the Harbor workflow](harbor/README.md) to generate new bowls, rescore the existing artifacts, or regrade a recorded Harbor job.

## Run locally

Serve the public repository files with the development server, which excludes local dot-directories such as `.harbor/`:

```sh
python3 scripts/serve.py
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
python3 scripts/import_grok.py --session <session-directory> --artifact <index.html> --harness-version <version>
```

Grok Build sessions live under `~/.grok/sessions/`. Its importer checks the HTML
against successful recorded writes, reads persisted accounting with `grok usage`,
and excludes structured hidden reasoning. Add `--check-sources` to verify that
the artifact and regenerated public JSON match an existing import. An exported
`grok usage <session-id>` JSON file can be supplied with `--usage-file`.

The importers recover recorded transcripts, timing, and token usage from the corresponding local harness stores. Provider-recorded costs take precedence. When Codex records exact token categories, the scripts apply the pinned LiteLLM catalog directly. For terminal captures that contain only an exact total, the cost is marked estimated and uses the same-effort GPT 6 Astra run's recorded input, cache, and output proportions with that model's own pinned rates; existing recorded costs are never replaced.

Every entry point finishes by running `scripts/censor_transcripts.py`. This shared pass redacts personal paths, usernames, email and LAN addresses, secret-like values, and signed asset URLs, and replaces encrypted strings and large embedded media/base64 payloads with factual host, encoding, and size markers. It leaves ordinary recorded messages, source code, and tool activity intact. Run `python3 scripts/censor_transcripts.py` by itself for a read-only audit, or add `--write` to normalize existing transcript JSON. Run `python3 scripts/validate_registry.py` to check the complete public registry, schemas, artifact paths, transcript provenance, and censoring state.

## Shared ratings API

Community votes are stored in Cloudflare D1 through the Worker in `api/`. The browser keeps a per-run local vote marker, following Fedora Showdown's one-vote UI behavior. A salted hash of the connecting IP and user agent also prevents duplicate D1 rows without retaining either raw value; repeat submissions keep the visitor's first rating.

To provision it:

1. From `api/`, run `npx wrangler@latest login`.
2. Run `npx wrangler@latest d1 create ramen-bench-ratings`.
3. Copy `wrangler.jsonc.example` to `wrangler.jsonc` and replace `REPLACE_WITH_D1_DATABASE_ID` with the returned ID.
4. Set a long random secret with `npx wrangler@latest secret put VOTER_HASH_SECRET`.
5. Run `npm run db:migrate:remote`, then `npm run deploy`.
6. Put the deployed Worker URL in `site.config.js` as `ratingsApiUrl`.

Keep `ALLOWED_ORIGINS` in `wrangler.jsonc` restricted to the GitHub Pages production origin and any intentional local development origins. Do not commit `wrangler.jsonc`, `.dev.vars`, or API tokens.
