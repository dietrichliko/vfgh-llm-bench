# Architecture

As of 2026-10-29. This describes how the study in [paper.md](paper.md) is implemented. The predecessor project is summarised in [prior-work.md](prior-work.md), and publication in [publication.md](publication.md).

## 1 Decisions

| Topic | Decision |
| --- | --- |
| Users | People who don't program (legal expert, project lead) run the study, so it is a **web application** |
| Server | **FastAPI** holds all logic; the browser only displays and submits |
| LLM access | **Pydantic AI**, only its **OpenAI-compatible** model class. A provider is a base URL + key from a config file |
| Database | **SQLModel** on SQLAlchemy, **SQLite** by default (`BENCH_DATABASE_URL` can point elsewhere), Alembic migrations |
| UI | **Jinja + htmx + Alpine.js**, server-rendered, no JS build, all static files vendored |
| Hosting | Any container host (laptop, institute server). fly.io only temporarily |
| Secrets | Passed into the container (secrets directory or env); never in config, logs or DB |
| Code | A new app in this repository; proven parts are ported from `../vfgh-llm-begruendung` |
| Conference scope | Test **open-weight models via OpenRouter**; u:ai frontier models as **judges and helpers** |
| Tool calling | Condition C only with models that support tools **natively**; the effect of tool emulation must not enter the results |

## 2 Components

```
                 browser (admin, legal expert, viewer)
                              │  HTML / htmx
┌─────────────────────────────▼──────────────────────────────┐
│ web: FastAPI + Jinja                                        │
│   auth · cases · orders · ratings · dashboard · export      │
└───────────────┬────────────────────────────────────────────┘
                │ SQLite (/data/bench.sqlite)
┌───────────────▼────────────────────────────────────────────┐
│ worker: same image, `bench worker`                          │
│   claims jobs → Pydantic AI agents → stores results         │
└──────┬───────────────────┬──────────────────────┬──────────┘
       │ OpenAI API         │ OpenAI API           │ MCP (Streamable HTTP)
┌──────▼──────┐   ┌────────▼────────┐   ┌─────────▼─────────┐
│ OpenRouter  │   │ aai-bridge      │   │ ris-mcp           │
│ open models │   │ (u:ai, emulated │   │ suche_normen,     │
│ native tools│   │  tools)         │   │ hole_norm         │
└─────────────┘   └─────────────────┘   └─────────┬─────────┘
                                                   │
                                         RIS OGD API v2.6
```

| Service | Image | Purpose |
| --- | --- | --- |
| `web` | `bench` | UI, API, writes jobs, reads results |
| `worker` | `bench` (`bench worker`) | Runs LLM jobs; can be restarted independently |
| `bridge-univie` | `registry.cern.ch/mbi-vienna/aai-bridge:0.4.0` | OpenAI-compatible access to u:ai, provenance headers |
| `ris-mcp` | `bench` (`bench ris-mcp`) | Legislation only, with a reference-date filter; separate judge endpoint for case-law lookup (stage 5) |

### Code layout

```
src/bench/
  settings.py      pydantic-settings: env + /run/secrets
  providers.py     config/providers.yaml → Pydantic AI models
  llm/             http client (retries, raw capture), agents, output schemas
  db.py            SQLModel engine/session
  models/          SQLModel tables
  ris/             ported: client, decision, split, leak, tenor (+ outcome per provision)
  documents.py     DocumentStore (source documents, extracted text)
  jobs.py          DB job queue, worker loop
  citations.py     ported citation check
  analysis/        κ, Fleiss' κ, Wilson, AUC, McNemar, PPI
  protocol.py      freeze / publish to the study repository
  auth.py          generic OIDC + dev login
  web/             routers, Jinja templates, htmx fragments
  static/          vendored htmx, Alpine.js, CSS
  cli.py           bench serve | worker | migrate | backup | check-providers
migrations/        Alembic
config/providers.example.yaml
deploy/            Containerfile, compose.yaml, fly/ (temporary)
tests/
```

## 3 Providers and models

All providers are OpenAI-compatible endpoints, described in `config/providers.yaml`. The file is mounted read-only into the container (`BENCH_CONFIG`, default `/config/providers.yaml`).

```yaml
providers:
  openrouter:
    base_url: https://openrouter.ai/api/v1
    api_key_secret: OPENROUTER_API_KEY        # name of the secret, never its value
    output_mode: native                        # JSON schema supported
    tool_mode: native
    max_concurrency: 8
    requests_per_minute: 60
    extra_body: {usage: {include: true}}       # OpenRouter reports the billed cost
  uai:
    base_url: http://bridge-univie:8000/v1
    api_key_secret: UAI_API_KEY
    headers: {X-Bridge-Raw: "1"}
    output_mode: prompted                      # JSON requested in the prompt
    json_object: false                         # also send response_format json_object
    tool_mode: emulated                        # never offered for condition C
    max_concurrency: 4

models:
  glm-5.3:
    provider: openrouter
    model: z-ai/glm-5.3
    family: zhipu
    cutoff: 2026-03-01                         # from the model card; required for "tested"
    roles: [tested]
    price: {in: 0.07, out: 1.4}                # USD per 1M tokens, for estimates
    expected_output_tokens: 8000               # incl. thinking, for estimates (default 6000)
    extra_body:
      provider: {order: [z-ai], allow_fallbacks: false, quantizations: [bf16]}
  claude-opus:
    provider: uai
    model: claude-opus-4-8
    family: anthropic
    roles: [judge]
    price: {in: 5, out: 25}
  gemini-pro:
    provider: uai
    model: gemini-3.1-pro-preview
    family: google
    roles: [extractor, writer]
    price: {in: 2, out: 12}
```

- **Roles:**
  - `tested`: the models under study.
  - `judge`: scores outputs.
  - `extractor`: builds the reference checklist.
  - `writer`: writes opposing reasonings.
- **Validation (Pydantic):**
  - unknown providers, duplicate keys and missing fields are rejected;
  - a `tested` model needs a `cutoff`;
  - a judge must not share a family with any model it scores.
- **Pydantic AI:** each entry becomes `OpenAIChatModel(model, provider=OpenAIProvider(openai_client=AsyncOpenAI(base_url, api_key, max_retries=0, http_client=…)))`. `output_mode` selects `NativeOutput` or `PromptedOutput`. The model profile (JSON schema / JSON object support) is set from our config, not guessed from the model name.
- **Provenance:** each run stores a hash of its resolved provider and model entry.
- **Admin page:**
  - shows every model with its status (available, or secret missing), cutoff, roles and the number of cases that pass its cutoff rule;
  - a **Test** button sends a short prompt and shows the answer, latency, usage, the answering provider and the bridge version.

## 4 Secrets

- `pydantic-settings` reads from a secrets directory (`/run/secrets`, for Podman/Docker/Kubernetes secrets) and falls back to environment variables (fly.io secrets).
- `api_key_secret: UAI_API_KEY` resolves from `/run/secrets/UAI_API_KEY` or `$UAI_API_KEY`. The OIDC client secret, the session secret and the study-repository deploy key work the same way.
- A provider whose secret is missing is shown as unavailable; the app still starts.
- Secrets are never logged, stored or exported.

## 5 Rules for LLM calls

Kept from the old project:

- **No silent retries:**
  - The SDK's own retries are off (`max_retries=0`) and `output_retries=0`.
  - Our httpx transport retries only on 429/502/503/504 or a connection error, with backoff, and never retries a timeout, because the request may already be billed.
  - An unparseable answer is stored as **not evaluable**.
- **Raw capture:** httpx event hooks store the exact request body, the raw response, all response headers (`X-Bridge-Version`, OpenRouter provider) and the duration. Pydantic AI's message history and usage are stored alongside.
- **No fallbacks:**
  - The same endpoint and model every time.
  - OpenRouter is pinned to a provider with `allow_fallbacks: false` and a quantization.
  - If the response's `model` differs from the requested id, the run is flagged.
- **Blindness by code:**
  - Inputs never contain the reference part, the operative part, the headnote, the case number or the date.
  - RIS metadata never enters a prompt.
  - Tested by asserting on the exact request body.
- **Tool calling (condition C):**
  - Pydantic AI `MCPToolset` to `ris-mcp`; the server must offer exactly `suche_normen` and `hole_norm`, any other tool set fails the run. The sha256 of the tool definitions is stored with each run.
  - The **Stichtag is fixed by the app**: the worker opens the connection with the header `X-Stichtag` (the case's reference date from the order snapshot). The tools take no date, and their answers never print it.
  - `UsageLimits(tool_calls_limit=15)`; exceeding it makes the run *not evaluable*. Tool errors (e.g. a citation not found) go back to the model as failed results.
  - Every tool call is stored with its arguments and result (`Run.tool_calls`), as is the usage of every round.
  - **Only providers with `tool_mode: native` are offered for condition C.** u:ai models (emulated tools) can be tested in conditions A and B, which use no tools; the dashboard shows their C cell as "not run: no native tool calling".
- **Judges need no tools.** Citation existence is checked by the app beforehand (`citations.py` + ris-mcp judge endpoint), and the judges receive the results.

## 6 Data

### Source documents

Checked against the RIS on 2026-10-29: there are 89 VfGH decisions under Art 139 B-VG dated from 2025-07-01.
- All of them are available as XML, HTML, PDF and RTF.
- 7 contain embedded JPG/PNG images (maps, plan excerpts, photos), including the pilot case V 251/2025.
- PDF-only documents occur among the regulations: municipal announcements (`GrA`) and district regulations (`Bvb`).
- StVO regulations announced by traffic signs are not in the RIS at all.

| Table | Content |
| --- | --- |
| `SourceDocument` | case or norm, kind (`decision`, `attachment`, `norm`), format/MIME, URL, fetched at, sha256, size, content (bytes). Decisions: XML (parsed) and PDF (for people and the archive); embedded images as attachments. Norms: whatever the RIS has, including PDF only |
| `ExtractedText` | text per source document, with method (`xml`, `html`, `pypdf`) and extractor version. **Moved to stage 4**: in stage 2 only the decision XML is parsed, and that result is `CaseParse` |
| `CaseParse` | sections, input, reference, outcome per challenged provision, what the application asked for, leak warnings; parser version and sha256 of the XML it came from |
| `Case` | RIS document number, case number, date, area, procedure type, status, flags |

- Everything lives in the database, so one SQLite backup holds the whole study. Access goes through a small `DocumentStore` interface so content can move to `/data/blobs/<sha256>` later.
- A run references the `CaseParse` it was given. Improving the parser creates a new parse without downloading again.
- The norm texts given in condition B and the ris-mcp responses in condition C are stored as received, because the RIS changes over time.
- **Case flags,** usable as analysis filters:
  - `has_images_in_input`;
  - `regulation_not_in_ris`;
  - `norm_text_from_pdf`;
  - `ex_officio`.
- Cases stay **text-only** for all models. Cases with images in the input are flagged and reported separately or excluded.
- **Ex-officio reviews** leak the court's preliminary view through the review decision, so only application proceedings are used. Procedural rulings (*Beschlüsse*) are filtered out.
- **Import of 2026-10-29** (all decisions under Art 139 B-VG from 2025-07-01):
  - 89 hits: 58 judgments (*Erkenntnisse*) and 31 procedural rulings.
  - Of the 58 judgments: **49 ready for review**, 7 ex-officio reviews and 1 individual complaint (E 1411/2025) excluded automatically, 1 with an unrecognised structure (V 220/2025: first heading "1. Anträge" instead of "I. Antrag").
  - Proposed outcomes of the 49: 22 unlawful, 7 partly unlawful, 20 dismissed. The majority class is therefore much weaker than the 63 % of the StVO cases since 2018.
  - Every application asks for annulment or for a declaration that the regulation was unlawful. At case level, "follow the application" is therefore the same as "always unlawful"; it differs only per provision and in the form of the outcome (annulled vs. was unlawful).
  - Embedded images: 8 of the 49 have images in the input.
  - 127 stored documents (XML, PDF, images) take 21 MB.
- **Condition B norms:** stage 2 stores the norms from the RIS metadata (*Normen*) as the proposal. Resolving them to texts valid on the reference date moves to stage 4, together with the ris-mcp port that provides the same RIS norm lookup.

### Study tables (overview)

| Area | Tables |
| --- | --- |
| Configuration | `ModelConfigVersion`, `PromptVersion` (editable in the UI, frozen per study) |
| Orders and runs | `Order` (with config snapshot), `Job`, `Run` (raw request/response, usage, cost, status, tool calls) |
| Evaluation | `ReferenceChecklist`, `ChecklistRating` (expert), `OpposingReasoning`, `Assessment`, `JudgeScore`, `CitationCheck`, `CalibrationRating`, `ContaminationProbe` |
| Protocol | `ProtocolSnapshot` (version, sha256, tag), `Amendment` |
| System | `User`, `Session`, `AuditLog`, `ProviderCheck` |

Completed runs and ratings are **immutable**. This is enforced in the service layer and by SQLite triggers, as in the old project; corrections create new versions.

## 7 Long-running orders

A study order can run for hours or days. The browser is only used to configure, start and watch it.

- **Configure:** the admin chooses cases × models × conditions × repetitions. The form shows the number of calls, the cost estimate, and which models are excluded from which cases by the cutoff rule. On start, one **job per LLM call** is written, and the order stores a snapshot: config hash, prompt versions, parse versions, bridge info.
- **Worker:** a separate process (`bench worker`).
  - It claims a job atomically with a lease and heartbeat.
  - When a lease expires, the job goes back to the queue. The exception: if its request was already sent, it becomes "outcome unknown" for the admin to decide, so nothing is paid twice unnoticed.
  - Results are saved per job.
- **Throttling** per provider (`max_concurrency`, `requests_per_minute`), with backoff on 429. A provider that keeps failing pauses its jobs.
- **Interleaved order:** case by case across models, so a partial run already gives comparable, preliminary results.
- **Controls:** pause, resume, cancel, "retry failed" (earlier attempts are kept), and cost caps per order and in total, checked before each job.
- **Progress page,** refreshed by htmx polling every 3 s: done/total per model and status, time left, cost, recent errors.

### As built in stage 3

- **Prompts** (`bench/prompts.py`): `decide.p1–p3` are seeded from `bench/prompts/*.md` at start-up (web and worker) when the key has no version yet; afterwards the database wins. A prompt holds the system message, the line `---user---`, and the user message with `{input}`, the only placeholder. Every edit on the prompts page is a new `PromptVersion`; the legal expert can read the prompts.
- **Job status:** `done` (answer fits the schema), `not_evaluable` (answer arrived but does not fit; never re-asked), `failed` (no answer), `unknown` (worker stopped after sending), `cancelled`. Every call is stored as an immutable `Run` with the messages, raw HTTP exchanges (without keys), parsed output, outcome label, usage and cost (price list, and the provider's own figure where reported).
- **Planning** (`bench/orders.py`): jobs are ordered repetition → phrasing → condition → case → model. A case is used for a model only if it was decided at least `cutoff_months` (default 3) after the model's training cutoff. The estimate uses 3.5 characters per input token and `expected_output_tokens`.
- **Worker** (`bench/worker.py`): one process; jobs claimed by compare-and-set on the status in `seq` order; lease 2 min, heartbeat 30 s. Model settings come from the order's snapshot, checked against its hash; a mismatch pauses the order. An order pauses at its cost cap or the total cap (`BENCH_COST_CAP_TOTAL_USD`), and after 5 failures in a row at one provider. `BENCH_WORKER_CONCURRENCY` (default 8) limits calls in flight over all providers.
- **Not yet:** the in-app notification banner; freezing the protocol.

### As built in stage 4a (condition C)

- **ris-mcp** (`bench/rismcp/`): ported from the old repository. `norms.py` is the RIS client for legislation (consolidated federal and state law at the Stichtag, municipal and district law announced before it; case-law applications and documents are refused before any request). `server.py` is the MCP server (`bench ris-mcp`, Streamable HTTP at `/mcp`, stateless, bearer key `RIS_MCP_API_KEY`, host check via `BENCH_RIS_MCP_ALLOWED_HOSTS`). Changes from the old server: the Stichtag comes from the `X-Stichtag` header instead of a tool argument; answers say "zum maßgeblichen Zeitpunkt" instead of the date; the MCP resources are gone (they bypassed the Stichtag check).
- **Prompts** `decide_c.p1–p3`: the A prompts plus a paragraph on the tools; no date.
- **Planning:** a case without a reference date is left out of B and C (listed in the preview and on the order page); the reference date can be set on the page of a released case. The order snapshot stores the reference dates, so a later change affects only new orders. The estimate for C assumes 6 rounds, each resending the context plus about 3,000 tokens of tool results.
- **Known limit:** `hole_norm` resolves abbreviated state-law citations (e.g. "Tir JagdG 2004") only if the RIS title matches; the model can use `suche_normen` instead.

### As built in stage 4b (condition B)

- **Norm sets** (`bench/normsets.py`, table `norm_sets`): per case and Stichtag, the norm texts given to the models. Each RIS norm string of the case (e.g. "Tir JagdG 2004 §2, §36") becomes one proposal per section, fetched with the ris-mcp RIS client in the version in force on the Stichtag (same blindness rules: no expiry dates, no later versions).
  - VfGH short forms are spelled out for the title search ("JagdG" → "Jagdgesetz", "RaumOG" → "Raumordnungsgesetz", "GdO" → "Gemeindeordnung"); state prefixes (Tir, Stmk, Oö, Nö, …) select the state.
  - If a title also matches implementing regulations, the statute is preferred (exact abbreviation, then no "Verordnung" in the title); if several statutes remain, the admin chooses.
  - Procedural norms (VfGG, B-VG Art 139/140) are proposed but unticked. EU law, regulations and statutes without a section are listed as "not automatic"; the admin adds norms by citation or RIS document number.
  - Measured 2026-10-29 on the 39 distinct statutes in the imported cases (at 2025-03-01): 27 resolved automatically, no wrong matches; most misses were statutes no longer in force on that date or titles that differ more ("Bauordnung für Wien").
- **Review:** on the case page (released case with Stichtag), or for all such cases at once from the case list. A set is a draft until approved; approval fixes the text block (texts joined by `---`) and its sha256, sets `Case.norm_set_id`, and flags `norm_text_from_pdf` if a text came from a PDF. Approved sets are immutable (SQLite trigger). A set applies only while the case keeps its Stichtag.
- **Runs:** prompts `decide_b.p1–p3` with the placeholder `{normen}` (allowed only there; `{input}` and `{normen}` are replaced in one pass). Cases without an approved set are left out of B and listed; the order snapshot stores the set per case, and each run its `norm_set_id`.
- **Notification:** in the app only (banner on the start page). No email.

### As built in stage 5a (reference checklists)

- **Helper tasks** (`bench/tasks.py`): orders, jobs and runs carry a `task` (`decide` or a helper task). A helper task has one prompt (`<task>.p1`), an output schema, the model role it needs, the text it reads, and a hook for a valid result. Helper orders use the same queue, worker, snapshot, cost caps and pages; there is no cutoff rule for helpers.
- **`extract_reasons`:** the extractor model (role `extractor`) reads the reference part (headnote, operative part, considerations) and returns at most eight decisive reasons with margin numbers (`Referenzgruende`). A valid answer becomes version 1 of the case's `ReferenceChecklist`.
- **Review** (`bench/checklists.py`, page "Referenzgründe", legal expert and admin): the expert rates version 1 correct, incomplete or wrong. Correct confirms it; otherwise the expert's corrected list (one reason per line) becomes a new confirmed version. The rating of the model's list is kept for the paper. `Case.checklist_id` points at the confirmed version, which the judges use. Rated and confirmed versions are immutable (SQLite trigger); a later revision is a new version.

## 8 Users and screens

| Role | Screens |
| --- | --- |
| Admin | Cases (RIS search, batch import, split/leak view, flags, release); providers and models (status, test, cutoff coverage); prompts (versions, freeze); orders (setup, cost estimate, progress, controls); protocol (freeze, amendments, publish); costs; users |
| Legal expert | Reference checklists (rate correct / incomplete / wrong); calibration queue (~40 outputs, blind, same criteria as the judges); optionally ~10 opposing-reasoning pairs, blind |
| Viewer | Dashboard (leaderboard, model profiles, case × model grid, the four analyses from `paper.md` §6), case pages, exports |

- **Login:** generic OIDC (issuer, client id and secret via config/secrets; CERN SSO works) plus a dev login for local use. Admins are listed in the settings.
- **Blindness in the UI:** before resolution, rating pages show no model name, provider or outcome assignment, not even in the HTML source, URLs or file names. Tests check the rendered HTML.
- **Style:** serif body text, print styles, keyboard use, sufficient contrast, colour never the only carrier of meaning.

## 9 Conference scope and cost

The conference shows the principle. A large study with frontier models as tested models is supported by configuration but needs separate funding.

| Part | Conference setting |
| --- | --- |
| Cases | ~20 judgments, application proceedings, after the models' cutoffs |
| Tested | 3–5 open models via OpenRouter, e.g. GLM-5.3, DeepSeek V4 Pro, a Qwen3.8 model, optionally a small one (Gemma 4 31B or Qwen-27B); final choice by training cutoff |
| Optional | u:ai models (e.g. Gemini 3.1 Pro, Mistral Large 3) tested in A and B only, at no extra cost |
| Judges | Claude Opus and GPT-5.5 (u:ai); neither family is tested |
| Helpers | Gemini 3.1 Pro: reference checklist, opposing reasonings. If Gemma is tested, Gemini is not used as a judge |
| Conditions | A with 5 runs, C with 1 run, B optional |
| Expert | 20 reference checklists, ~40 calibration ratings |
| Cost | ~10–25 USD on OpenRouter; judges and helpers on the u:ai quota (~60–80 USD at list prices) |

**Basis of the estimate** (measured 2026-10-29 on 23 RIS hits):
- The input part has a median of about 24,000 characters (about 7,000 tokens) and a maximum of about 107,000.
- Output: about 6,000 tokens per call including thinking.
- Condition C: about 6 tool rounds, about 80,000 input tokens.
- Uncertainty is about ×2.

For comparison, with frontier models as tested models and 85 cases: about 6,900 USD for the full design (15 runs in every condition, every output judged twice), or about 1,000 USD with 15 runs in A, 2 in B and C, and judges on one run per cell. The judges dominate the cost. Outcome and stability need no judge.

**Statistics caveat:** with 20 cases an accuracy of 80 % is known only to about ±18 points. Conference results are descriptive and worked cases, not a ranking.

## 10 Study protocol and publication

The app freezes the study setup into files, commits them to a separate study repository with a tag, and publishes results the same way. Zenodo archives each release with a DOI, and an OSF pre-registration links to the protocol tag. Details are in [publication.md](publication.md).

## 11 Containers and operation

- **`deploy/Containerfile`:** a uv build stage, then a slim runtime with a non-root user, the git CLI (for the protocol push), a `/data` volume and `/config` read-only.
- **`deploy/compose.yaml`:** `web`, `worker`, `bridge-univie` and later `ris-mcp`, with secrets from files and the config bind mount. Runs with Podman or Docker on a laptop or a server.
- **`deploy/fly/`:** temporary deployment. Secrets go in as env; the worker machine must not auto-stop while jobs are queued.
- **Backup:** `bench backup` makes a gzipped SQLite online backup to a directory; scheduling is the host's job.
- **Development:**
  - uv, Python 3.13, ruff (same rules as the old project), mypy in strict mode, pytest with `httpx2.MockTransport` (the openai SDK uses httpx2, which respx cannot mock);
  - pre-commit and Conventional Commits;
  - the Silverblue toolbox from the old repository.

## 12 Roadmap

Each stage runs and is tested on its own.

| Stage | Content |
| --- | --- |
| 1 Skeleton | Settings and secrets, `providers.yaml` loader, httpx client with retries and raw capture, SQLModel and Alembic, OIDC and dev login, providers page with Test button, Containerfile, compose |
| 2 Cases ✓ | Port `ris/` with tests; `SourceDocument`, `CaseParse`; batch import with XML, PDF and attachments (web page and `bench import-cases`); outcome per provision; flags and automatic exclusions; review page; release with confirmed outcome; condition B norm proposals from RIS metadata |
| 3 Runs A ✓ | Prompts in the DB, orders with snapshot and cost estimate, worker and job queue, progress page, cutoff filter, cost caps, Freeze protocol |
| 4 Conditions C ✓ and B ✓ | 4a: port `ris-mcp`, MCP client via Pydantic AI, tool limit, tool log, native-only rule, Stichtag per case. 4b: condition B norm texts at the Stichtag, reviewed per case |
| 5 Evaluation | Reference checklists, opposing reasonings and assessment, judges, citation check, calibration queue, contamination probe |
| 6 Analysis | Metrics (κ, Fleiss' κ, Wilson, AUC, McNemar, PPI), dashboard with figures 1–4, export, Publish results |

## 13 Open points

- Final list of tested open models and their training cutoffs (from model cards).
- Whether to add the further ideas:
  - the expert also judges ~10 opposing-reasoning pairs;
  - models state their confidence;
  - an "application only" control run;
  - a majority vote over the 5 runs;
  - error categories from the judges;
  - a printable page per case for the talk.
- Running open models on MBI GPUs via vLLM as another OpenAI-compatible provider (free and fully reproducible).
- Name and owner of the study repository; whether to publish raw responses; the OSF embargo (see [publication.md](publication.md)).
- Paper text to adjust:
  - the case estimate (49 usable judgments since mid-2025 rather than 80–90);
  - the baseline (majority class about 45–60 %, not 63 %; "follow the application" equals "always unlawful" at case level);
  - the pilot size;
  - "a proxy emulates tool calling for all models" → condition C only with native tool calling.
