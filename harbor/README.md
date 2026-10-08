# Ramen Bench in Harbor

`ramen/` is a Harbor task: agents write `/app/index.html`, which Harbor records as an artifact. The verifier runs in a separate container, captures the artifact in Chromium, and asks three vision models to grade the same frames. Agent containers never receive judge credentials. Existing HTML, manifests, transcripts, and recorded generation accounting stay unchanged.

## Reward

The standalone gate uses a static dependency check plus blocked-request evidence from browser capture. External or relative assets, CSS imports, and JavaScript module imports fail the gate. Inline SVG, fragment references, and embedded data/blob assets are allowed. A missing or empty artifact also earns zero. The browser blocks external requests, WebSockets, and service workers; observation covers the capture window, not every possible future execution path.

The v3 task requires code-drawn artwork: HTML/CSS, inline SVG, canvas and WebGL are eligible; embedded raster artwork is disallowed. The verifier checks raster data URIs independently of the standalone gate. This is a stricter, explicit artifact rule than merely banning image-generation tools. Source checks cannot prove every tool call or detect every obfuscated raster encoding. Recorded image-generation use remains disqualifying evidence; Astra XHigh is a confirmed example. Original recorded HTML is never edited.

The appearance rubric uses these weights:

| Criterion | Weight | Evidence |
| --- | ---: | --- |
| Noodles | 20% | Flexible continuous strands, thickness, overlap and placement |
| Toppings | 25% | Recognizable organic ingredients, appropriate texture and scale |
| Bowl | 20% | Coherent rim, cavity, walls, base, perspective and materials |
| Broth | 10% | Liquid appearance, depth, immersion and ingredient integration |
| Steam | 15% | Translucent natural wisps across time-ordered frames |
| Composition | 10% | Proportions, palette, readable focus and coherent lighting |

Visual plausibility is judged within the chosen style: a strong procedural illustration need not be photographic. Explicit criterion ceilings apply to dominant rigid/maze noodles, plastic/card-like toppings, incompatible bowl construction and static steam. Background glow cannot substitute for convincing food.

Vegan appearance is a separate diagnostic and adds no quality points. A judge flags clear animal ingredients when a majority of its samples assign vegan compliance zero; a majority of judges must flag this before the composite is disqualified. Ambiguous ingredients alone do not cause a zero. Interaction, responsiveness, reduced motion and browser frame intervals do not add visual quality points.

```
reward = 0                                         if a compliance gate fails
reward = mean(weighted visual reward of each judge) otherwise
```

The ensemble uses GPT 6.1 Sol at Low, Claude Sonnet 5.5 at Low, and GLM 5.3 Flash at its provider default through OpenRouter. Three independent samples per judge are averaged, then judge means receive equal weight. A failed judge fails the ensemble. Strict JSON schema constrains responses; malformed samples are retried individually up to three attempts, with all attempt usage kept locally. Safe failure metadata excludes provider text and credentials.

The judge sees five desktop frames (three temporal, two pointer positions), one mobile frame and two reduced-motion frames, with actual elapsed times. It sees no candidate model identity, effort, source code, costs, transcripts or human target ratings. Captures use a fixed random seed and fresh contexts; animation timing is not bit-for-bit deterministic. Screenshots cannot establish recipes, fully measure motion smoothness or prove accessibility. Image instructions are treated as untrusted candidate content.

Top-level standard deviation measures disagreement between judges' visual rewards; per-judge spread measures sample variation. Neither establishes accuracy or uncertainty across generation runs. Changing verifier files, weights, judge models, requested efforts or samples requires a fresh evaluation; the audit rejects stale fingerprints and mixed settings. Provider-default effort is recorded as `default`, not silently replaced with an explicit effort. Routing is not pinned to a serving provider.

`calibration.json` records the user's approximate ratings and qualitative expectations separately from actual results. These examples helped design the rubric and are not an independent held-out validation set. Targets are never included in judge prompts or substituted for measured LLM scores.

The score page also supports post-scoring weights and human ratings without new API calls. `weights.json` controls the displayed LLM composite: noodles 10%, toppings 10%, bowl 20%, broth 10%, steam 25%, and composition 25%. These are view weights; the native Harbor rewards and `scores.json` retain their original weighting and provenance. Per-judge means and sample spread are recomputed for the view from the recorded criterion scores.

`human-scores.json` contains an editable `humanScore` field for every currently registered bowl. Enter a number from 0 to 1, such as `0.85`, or leave it `null` when unrated. The panel is labeled **Expert panel of ramen eaters**; its initial ratings come from the user's feedback, not additional panel members. The current human weight is `0.8`: rated bowls display `0.8 × humanScore + 0.2 × LLM composite`. Unrated bowls use their LLM composite until a human rating is supplied. Compliance failures remain zero regardless of the human rating. The human file is separate from generated scores so scoring and collection cannot overwrite later human entries. Change its `weight` to adjust human influence; these preferences are never sent to the LLM judges.

## Setup

Use a separate Python environment from the historical importers: their pinned pricing catalog has incompatible dependencies with current Harbor. Historical generation costs are never recalculated by this workflow.

```sh
uv venv .harbor/venv
uv pip install --python .harbor/venv/bin/python -r requirements-harbor.txt
```

Create `.harbor/judge.env` locally (the entire `.harbor/` directory is gitignored):

```dotenv
OPENROUTER_API_KEY=your-key
RAMEN_JUDGE_MODELS=openrouter/openai/gpt-6.1-sol,openrouter/anthropic/claude-sonnet-5.5,openrouter/z-ai/glm-5.3-flash
RAMEN_JUDGE_EFFORTS='{"openrouter/openai/gpt-6.1-sol":"low","openrouter/anthropic/claude-sonnet-5.5":"low","openrouter/z-ai/glm-5.3-flash":"default"}'
RAMEN_JUDGE_SAMPLES=3
```

Do not commit or paste a real API key. The default is the three-model ensemble above. Set `RAMEN_JUDGE_MODELS` to a comma-separated list of distinct LiteLLM vision routes supporting JSON output. `--judge-model` accepts the same list for local scoring; the singular `RAMEN_JUDGE_MODEL` remains a local-scoring fallback. Judge models run concurrently, each with three sequential samples. `RAMEN_JUDGE_EFFORTS` or local `--judge-efforts` accepts a JSON map of configured model routes to supported efforts; `default` omits the requested effort. OpenRouter model capabilities are checked before paid calls, including image support and effort compatibility. Reasoning is excluded from responses and never published. For direct providers, use `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GEMINI_API_KEY` as appropriate. The task interpolates credentials only into `[verifier.env]`. Judge explanations and usage are retained, but structured hidden reasoning and raw provider payloads are never published. Judge API failure or malformed scores produce no reward file and a failing verification, rather than a misleading zero.

## Generate new ramens

```sh
.harbor/venv/bin/harbor run \
  -p harbor/ramen -a codex -m openai/gpt-6-luna \
  --ak reasoning_effort=high \
  --env-file .harbor/judge.env \
  --jobs-dir "$PWD/.harbor/jobs"
```

The selected coding agent also needs its own credentials/configuration. The task instruction follows `PROMPT.md`, makes the output path absolute, and explicitly prohibits embedded raster artwork for v3. Harbor writes each trial's `verifier/reward.json` (numerical metrics) and `verifier/grade.json` (judge samples, criterion scores, artifact/verifier hashes, usage, capture metadata, and diagnostics). Generated trials and screenshots remain local until deliberately imported/published; the existing harness importers still govern public transcripts and accounting.

For Codex subscription authentication, use Harbor's native agent environment
`CODEX_AUTH_JSON_PATH` pointing to your local `auth.json`, for example
`/home/your-user/.codex2/auth.json`. Harbor uploads the credential into the
ephemeral agent environment; never bake it into an image or publish it. Use the
agent's `config` keyword argument to set `model_reasoning_effort`, including
`ultra` when your installed Codex client supports it. A custom image containing
Codex must include both `codex` and its companion `codex-code-mode-host` binary.

Import completed native Codex trials with the Harbor environment:

```sh
LITELLM_LOCAL_MODEL_COST_MAP=True .harbor/venv/bin/python scripts/import_codex.py \
  --harbor-job .harbor/jobs/<job-name> --dry-run
LITELLM_LOCAL_MODEL_COST_MAP=True .harbor/venv/bin/python scripts/import_codex.py \
  --harbor-job .harbor/jobs/<job-name> --publish-scores
python3 scripts/censor_transcripts.py
python3 scripts/validate_registry.py
```

The importer checks the main recorded session, model, effort, collected artifact,
and exact final file writes. It copies the HTML unchanged and preserves native
Harbor tokens, cost, and generation duration. Harbor's own LiteLLM cost estimates
are marked estimated; unknown costs remain unknown. Existing judgments and human
ratings are preserved, and new human ratings start at `null`. Repeated imports
are deterministic. Retain the private native job directory for later source
checks; `--run openai/<model>/<effort> --dry-run` can verify an imported run again.

Codex runs that spawn review subagents can expose a Harbor export bug: Harbor
selects the newest rollout rather than the main thread. The importer identifies
the main thread from Codex's recorded `thread.started` event and verifies the bowl
against that thread's final writes. When the exported trajectory belongs to a
descendant, it validates and retains Harbor's accounting separately, sums the
recorded per-session token totals once each, and leaves missing cost unknown.
The manifest records this accounting distinction; the public transcript uses
the main thread and excludes private agent communication.

If the job stops after generation and verification but before Harbor finalizes
the trial's `result.json`, add `--recover-completed`. Recovery requires a recorded
Codex task completion, a matching collected artifact, the native ATIF trajectory,
and a complete grade matching the saved verifier reward. It never creates a trial
result or an AgentContext. Recovered manifests explicitly record that the trial
result is missing, retain the trajectory's native accounting, and use the recorded
Codex session start and completion for generation timing. Incomplete generations
and incomplete judging cannot be recovered this way.

### Claude Code subscription authentication

Claude Code's native Harbor agent stores sessions under `/logs/agent/sessions`.
Mount the local Claude credentials into that directory read-only; mounting only
`~/.claude` at the container's home does not authenticate Harbor's separate
Claude configuration directory. For example:

```sh
.harbor/venv/bin/harbor run \
  -p harbor/ramen -a claude-code -m anthropic/claude-haiku-5-5 \
  --ak reasoning_effort=high --ak version=2.1.295 \
  --mounts '[{"type":"bind","source":"/home/your-user/.claude/.credentials.json","target":"/logs/agent/sessions/.credentials.json","read_only":true}]' \
  --env-file .harbor/judge.env --jobs-dir "$PWD/.harbor/jobs"
```

Haiku 5.5 supports `low`, `medium`, `high`, `xhigh`, and `max`. Set the effort
explicitly for each trial. Refresh revoked subscription credentials with
`claude auth login` before starting another job. Credentials and native jobs
remain private; the verifier receives only the separately configured judge key.

Import completed trials through the Claude entry point:

```sh
LITELLM_LOCAL_MODEL_COST_MAP=True .harbor/import-venv/bin/python scripts/import_claude_code.py \
  --harbor-job .harbor/jobs/<job-name> --dry-run
LITELLM_LOCAL_MODEL_COST_MAP=True .harbor/import-venv/bin/python scripts/import_claude_code.py \
  --harbor-job .harbor/jobs/<job-name> --publish-scores
LITELLM_LOCAL_MODEL_COST_MAP=True .harbor/import-venv/bin/python scripts/import_claude_code.py \
  --harbor-job .harbor/jobs/<job-name> --check-sources
```

Use the pinned historical importer dependencies in `requirements-backfill.txt`
for `.harbor/import-venv`, separately from Harbor's environment. The importer
requires successful native completion, checks the model and recorded effort,
matches the exact task instruction, and compares every HTML byte to recorded
file operations. It checks native main-session tokens against Harbor accounting
and retains the CLI's auxiliary accounting separately. Unknown or unverified
costs remain unknown. Source checks regenerate both public JSON documents and
require them to match the censored import. Retain the native job for these checks.

If generation succeeded but its verifier failed, regrade the saved trial with
Harbor and pass `--grade-job .harbor/jobs/<regrade-directory>` to both import and
source-check commands. The importer joins successful native regrades by their
recorded source trial identity and unchanged artifact bytes. It retains original
generation timing and accounting, records the failed verifier, and publishes only
the successful regrade's native reward.

### Pi and OpenRouter

Harbor's native Pi agent can generate bowls using OpenRouter credentials in the
ignored local environment file:

```sh
.harbor/venv/bin/harbor run \
  -p harbor/ramen -a pi -m openrouter/mistralai/mistral-large-4-0 \
  --ak version=1.0.4 --env-file .harbor/judge.env \
  --jobs-dir "$PWD/.harbor/jobs"
```

Mistral Large 4's [native API supports reasoning](https://docs.mistral.ai/studio/conversations/reasoning)
at `none` and `high`. OpenRouter's endpoint advertised neither reasoning nor
effort controls when added on October 6, 2026; a strict `high` request with
`provider.require_parameters=true` returned HTTP 404 because no endpoint could
handle those parameters. Direct requests without strict routing also returned
no separate thinking content and zero reported reasoning tokens: both `high` and `none`,
the `reasoning_effort="high"` shorthand, explicit
`reasoning={"effort":"high","enabled":true,"exclude":false}`, and streaming
were checked. Accepted requests therefore do not establish that effort controls
are honored. On a matched prompt, explicit `high`, `none`, and default requests
returned the same step-by-step explanation in ordinary answer text; that text
does not establish distinct reasoning modes. OpenRouter's [routing documentation](https://openrouter.ai/docs/guides/routing/provider-selection#requiring-providers-to-support-all-parameters)
explains that unsupported parameters can be ignored without strict routing.
Verify returned reasoning before adding a `High` run when gateway support changes.
The recorded variation is therefore `Default`, with no
requested effort override. It must not be relabeled `None` or `High`. Pi's internal
thinking setting is recorded separately and does not establish provider support.

Import the completed native job with the Pi entry point:

```sh
LITELLM_LOCAL_MODEL_COST_MAP=True .harbor/venv/bin/python scripts/import_pi.py \
  --harbor-job .harbor/jobs/<job-name> --dry-run
LITELLM_LOCAL_MODEL_COST_MAP=True .harbor/venv/bin/python scripts/import_pi.py \
  --harbor-job .harbor/jobs/<job-name> --refresh-provider-costs \
  --env-file .harbor/judge.env --publish-scores
python3 scripts/censor_transcripts.py
python3 scripts/validate_registry.py
```

The Pi importer replays recorded writes and edits, copies the HTML unchanged,
and validates session tokens against Harbor's native accounting and stdout.
Public transcripts preserve user/assistant text and tools while excluding system
messages and structured hidden reasoning. Original sessions and provider records
remain private in the native trial directory; retain them for source checks.
If the file write succeeds but Pi's final follow-up API requests fail, a finalized
Harbor trial can still supply a verifiable bowl. The importer requires the
successful recorded write and exact artifact match, explicitly records that API
failure, and preserves the original errors rather than inventing a final reply.

Pi's bundled catalog initially did not recognize this new model, so its fallback
pricing estimate is unsuitable for the chart. `--refresh-provider-costs` obtains
OpenRouter's original generation records for each recorded response ID. Cost is
the sum of their provider-recorded `total_cost` fields, with session and model
identity checked; incomplete charge records leave cost unknown. The native Pi
estimate is retained separately. Further imports use those saved records offline
and produce deterministic output, without implementing our own pricing logic.

## Score the existing collection in Harbor

```sh
python3 scripts/ramen_harbor.py export
.harbor/venv/bin/harbor run \
  -p .harbor/replay -a oracle -n 2 \
  --env-file .harbor/judge.env \
  --jobs-dir "$PWD/.harbor/jobs" --job-name legacy-ramen
python3 scripts/ramen_harbor.py collect .harbor/jobs/legacy-ramen
```

The exporter produces one task per registered run and a `replay-map.json` joining task names to original run IDs and SHA-256 hashes. The oracle only copies the recorded HTML into `/app/index.html`; it does not ask a model to regenerate it. Export is deterministic and preserves every artifact byte. These are artifact replay trials, not reconstructed historical Harbor trials: no transcript events, agent timings, costs, or provenance are fabricated. Collection verifies the grade against the source HTML and Harbor's recorded rewards, rejects duplicate attempts, and publishes only successfully scored artifacts. Coverage is visible on the score page.

For calibration, export a subset with repeatable `--run-id` arguments and a separate `--output .harbor/calibration` directory. `collect` accepts multiple job directories, so a successful pilot can be combined with additional calibration trials without repeating its API calls. All collected results must use the same verifier fingerprint, judge ensemble, and sample count. A five-bowl calibration set is a useful first review before scoring the rest.

To avoid rebuilding and importing the same browser image for every task, build it once with `docker build -t ramen-verifier harbor/ramen/tests`, then export with `--verifier-image ramen-verifier`. On a Podman host, use `podman build` directly. Rebuild that image after changing verifier files; collection checks the fingerprint actually recorded by the verifier against the current source.

## Native regrade

After changing the verifier, regenerate the replay tasks with `export`, retaining their names, then run:

```sh
.harbor/venv/bin/python scripts/ramen_harbor.py regrade .harbor/jobs/legacy-ramen \
  --job-name legacy-ramen-v2
python3 scripts/ramen_harbor.py collect .harbor/jobs/legacy-ramen-v2
```

The wrapper reads `.harbor/judge.env` and invokes native `harbor job regrade`, passing credentials through the process environment rather than command arguments. Native regrade matches task names and uses the source trial's recorded artifact manifest. It never reruns the generation agent. For newly generated Harbor bowls, use `--tasks harbor/ramen` and the corresponding job; collection into the existing registry requires a real harness importer for those new sessions.

See Harbor's [task format](https://docs.harborframework.com/core-concepts/tasks/overview), [separate verifiers](https://docs.harborframework.com/core-concepts/tasks/separate-verifier), and [regrade requirements](https://docs.harborframework.com/core-concepts/jobs/regrade).

## Local scoring and resume

For quick iteration, the exact same verifier can run on the saved artifacts outside Harbor:

```sh
PLAYWRIGHT_BROWSERS_PATH="$PWD/.harbor/browsers" \
  .harbor/venv/bin/python -m playwright install --with-deps chromium
PLAYWRIGHT_BROWSERS_PATH="$PWD/.harbor/browsers" \
  .harbor/venv/bin/python scripts/ramen_harbor.py score
```

`score` reads `.harbor/judge.env`, uses `.harbor/grades/`, resumes valid completed grades, and returns nonzero if any artifact fails. `--force` bypasses the final-grade cache; exact capture and judge checkpoints can still be reused. `--capture-only` prepares frames without calling a judge or publishing scores. `--run-id` is repeatable; use `--output .harbor/subset-scores.json` when testing a subset, since each publication replaces the score set. `--samples 1` is useful for initial calibration. The runtime must allow Chromium to create sockets. Container grading is preferred for published comparisons; local hardware, browser rendering, and viewport details can affect results.

The verifier has four CPUs and 4 GiB of memory, with a 120-second timeout for each page operation and screenshot. A transient capture failure gets at most one fresh-browser retry; the retry scheduling deadline is six minutes, and Harbor's overall verifier deadline still applies. Provider timeouts, connection errors, rate limits, server errors, and malformed judgments get at most three attempts per sample. Authentication and other permanent HTTP errors fail immediately. Safe error records identify the stage, error code, and available HTTP status without copying provider exception messages or credentials.

Capture metadata and frame hashes are checkpointed atomically, as is each successful judge sample and its recorded usage. Reusing the same output directory resumes the exact completed sample prefix. Artifact changes, renderer-policy changes, modified PNG bytes, different judge messages, efforts, or request options invalidate reuse. All configured judges must finish every requested sample before a reward is written. A failed ensemble removes any stale reward; it never silently drops a judge or assigns a failure a quality score. These checkpoints resume verifier invocations that retain `/logs/verifier`; a native Harbor regrade creates a new trial directory and does not automatically import an earlier trial's private checkpoints.

Both `score` and `collect` write `scores.json`, applying the shared censor to public explanations. They do not publish screenshots, API payloads, local paths, or native session logs. Validate before publishing:

```sh
python3 scripts/censor_transcripts.py --write
python3 scripts/censor_transcripts.py scores.json --write
python3 scripts/censor_transcripts.py
python3 scripts/censor_transcripts.py scores.json
python3 scripts/validate_registry.py
python3 -m unittest scripts/test_ramen_harbor.py scripts/test_ramen_reliability.py scripts/test_censor_transcripts.py
node --test scripts/test_scores.mjs
```

Serve the repository normally and open `scores.html`. Cost vs composite reward is the default; generation time is a toggle. Both horizontal axes descend from high values to low, so up and to the right is better. Each model's efforts connect from Low to Ultra, even when cost is non-monotonic. Model names label their own lines, each effort is shown beside its solid dot, and dots do not encode Pareto dominance. Unknown cost stays unknown; zero values can be viewed on the linear scale. Estimated costs are labeled and can be excluded. Unscored artifacts have no plot point and are never assigned zero. The table links back to each unchanged bowl and exposes the individual judge explanations.
