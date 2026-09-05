# Ramen Bench contributor instructions

## Onboard benchmark runs from recorded sessions

- Treat each model's `index.html` as an immutable benchmark artifact. Verify it against the harness's final recorded write; do not clean up, reformat, or otherwise edit the model output.
- Generate `run.json` and `transcript.json` with the repository importer for that harness. Do not write or fill transcript events, token counts, timing, costs, or provenance by hand.
- Use the harness entry points in `scripts/`: `import_claude_code.py`, `import_codex.py`, `import_antigravity.py`, or `import_muse.py`. Extend the appropriate importer when adding another run so the result is reproducible.
- Prefer provider-recorded costs. Otherwise calculate costs with the pinned LiteLLM catalog only when the recorded token categories are sufficient. Keep cost unknown when calculating it would require guessing.
- Add the run manifest to `registry.json` under Vendor → Model → Effort. Order models newest to oldest and efforts highest to lowest: Ultra, Max, XHigh, High, Medium, Low.

## Censor every public transcript with the shared tool

- Run `python3 scripts/censor_transcripts.py --write` after importing or changing public transcript data. Never rely on one-off manual redaction.
- Run `python3 scripts/censor_transcripts.py` again in read-only mode and require zero files needing changes before publishing.
- Preserve ordinary user and assistant messages, including plain-text reasoning that the model intentionally exposed. Preserve readable source code and tool activity.
- Exclude structured hidden reasoning, system and developer prompts, encrypted or otherwise opaque payloads, private agent-mail activity, credentials, secret-like values, signed asset URLs, personal paths, usernames, email addresses, and private or LAN IP addresses.
- Replace large embedded images, base64, binary, and opaque encrypted strings with the shared censor's factual encoding-and-size marker. Do not publish undecodable payload chunks.
- Never fabricate, summarize, or reconstruct missing transcript content. A factual omission marker is allowed; invented model text is not.

## Validate before committing

- Run `python3 scripts/validate_registry.py`. It must validate every registered manifest, artifact, transcript schema, result statistic, and censor state.
- Run the relevant importer source check and confirm regenerated output is deterministic for the run being added.
- Run `python3 -m unittest scripts/test_censor_transcripts.py`, Python compilation and Ruff for changed importers, the API tests when API code changes, and the LeakTK pre-commit scan.
- Keep unrelated working-tree changes out of the commit.
