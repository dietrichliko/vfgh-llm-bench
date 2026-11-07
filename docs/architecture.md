# Architecture

As of 2026-11-06. This describes how the study in [paper.md](paper.md) is implemented. The predecessor project is summarised in [prior-work.md](prior-work.md), and publication in [publication.md](publication.md).

## 1 Decisions

| Topic | Decision |
| --- | --- |
| Users | People who don't program (legal expert, project lead) run the study, so it is a **web application** |
| Server | **FastAPI** holds all logic; the browser only displays and submits |
| LLM access | **Pydantic AI**, only its **OpenAI-compatible** model class. A provider is a base URL + key from a config file |
| Database | **SQLModel** on SQLAlchemy, **SQLite** by default (`BENCH_DATABASE_URL` can point elsewhere), Alembic migrations |
| UI | **Jinja + htmx + Alpine.js**, server-rendered, no JS build, all static files vendored |
| Hosting | Locally with compose (images built from the working tree), or fly.io with the released image |
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
| `ris-mcp` | `bench` (`bench ris-mcp`) | Legislation only, with a reference-date filter. Case law is never served; the citation check looks decisions up from the worker |

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
  cli.py           bench serve | worker | all | migrate | backup | restore | check-providers
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
- **Pydantic AI:** each entry becomes `OpenAIChatModel(model, provider=OpenAIProvider(openai_client=AsyncOpenAI(base_url, api_key, max_retries=0, http_client=…)))`. `output_mode` selects `NativeOutput` or `PromptedOutput`. The model profile (JSON schema / JSON object support) is set from our config, not guessed from the model name. Limits per provider: `timeout_seconds` (no data from the endpoint), `max_run_seconds` (wall clock for a whole run incl. tool rounds, default 900; needed because OpenRouter keeps a connection alive while the model writes, so the read timeout never fires; exceeded → `failed`), `max_output_tokens` (cap incl. thinking, a model may override it; sent as `max_tokens_field`: `max_tokens` for OpenRouter, `max_completion_tokens` for the u:ai bridge, which refuses a cap for models whose upstream cannot apply one, so u:ai has none). An answer cut off at the cap ends `not_evaluable` with "abgeschnitten" in the error. Found in the local test 2026-11-07: Gemma 4 31B wrote 140,000 characters in condition C and did not finish in B after 13 minutes. Pydantic AI's own cost estimate (`RunUsage.cost`, a Decimal) is stored as float in `usage`; the app's cost uses the `providers.yaml` prices.
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
- **Judges need no tools.** Citation existence is checked by the app beforehand (`citations.py`, from the worker straight to the RIS), and the judges receive the results with the text found.

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
- **Not yet:** the in-app notification banner. (Freezing the protocol came in stage 6d.)

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

### As built in stage 5b (judges and citation check)

- **Judge orders** (`bench/judges.py`): on the page of a decide order, the admin picks judge models (role `judge`) and sees per judge the number of judgments, the estimate and what is left out. One job per evaluable decide run and judge. Left out: runs whose case has no confirmed checklist; judges of the tested model's family (**family rule**); pairs already judged or planned (a failed judgment is repeated with "retry failed" on the judge order). The snapshot fixes the checklist version per case.
- **Citation check** (`bench/citations.py`): before the first judge call on a run, the worker checks each of its citations in the RIS, without a model, and stores one `CitationCheck` per citation (immutable): `found`, `not_found`, `unresolvable`, `not_checkable` (literature) or `error`.
  - Norms: the version in force on the case's Stichtag (decision date if the case has none), resolved with the condition B title rules (state prefixes, VfGH short forms); the excerpt is the start of the norm text.
  - Decisions: VfSlg number or case number (VfGH, VwGH, OGH) via the case-law client (`bench/ris/judikatur.py`, ported from the old ris-mcp judge endpoint). There is no judge endpoint on ris-mcp: judges have no tools, and ris-mcp stays legislation only.
  - A citation already checked for another run (same text, kind and Stichtag) is reused, not asked again. Two judges of one run wait for one check.
  - If the RIS is unreachable (network, 429, 5xx), the job goes back to the queue unsent and the order pauses; resuming continues the check where it stopped.
- **Judge prompt** `judge.p1` with the placeholders `{input}` (the case as the model saw it), `{checkliste}`, `{antwort}` (operative part, decisive reasons, reasoning) and `{zitate}` (citations with check result and RIS excerpt). The judge never sees the tested model's name, provider or condition; a test asserts this on the request body.
- **Criteria** (schema `Bewertung`): per reference reason *ja / teilweise / nein* with evidence from the answer (level 2); per citation whether the source carries the statement, *ja / teilweise / nein / nicht_beurteilbar*, judged only on the RIS excerpt (level 3, accuracy; existence comes from the check). An answer that does not score every reason and every citation exactly once is *not evaluable*. A valid one becomes a `JudgeScore` (immutable).
- **Run page:** the decide run shows the citation check and each judge's scores, with the share of reference reasons given (ja = 1, teilweise = ½) for orientation; metrics follow in stage 6.
- Left out on purpose: an overall grade and additional reasons beyond the checklist (not among the paper's metrics; each extra criterion would need its own calibration).

### As built in stage 5c (calibration queue)

- **Draw** (`bench/calibration.py`, page "Kalibrierung", admin): the admin draws *n* answers (default 40) from the decide runs that at least one judge has scored and that are not drawn yet. Sampling goes round robin over the strata condition × model, and within a stratum takes the least used case first, so every condition gets its share (the paper's threshold must hold in every condition). The seed is stored per draw; further draws add to the sample, never the same run twice. A run whose judges used different checklist versions is left out; the item keeps the version the judges used.
- **Second rating:** the first *k* picks of a draw (default 10), spread over the strata, are also rated by a second person, as the upper bound of agreement. Which person rates first does not matter: an item needs one rating, a double item two from different people.
- **Queue and rating page** (legal expert and admin): each person sees the items they can still rate, in item order. The page shows the case input as the models saw it, the reference reasons, the answer (operative part, decisive reasons, reasoning) and each citation with the RIS check and excerpt, and asks the judges' criteria: per reference reason *ja / teilweise / nein* with an optional passage, per citation *ja / teilweise / nein / nicht beurteilbar* with an optional note. Items are stored in random order, so the item id says nothing about the stratum.
- **Blind:** the rating page shows no model, provider, condition, run id or judge score (a test checks the HTML). The stratum is shown to the admin only, on the overview.
- `CalibrationItem` and `CalibrationRating` are immutable (SQLite trigger, migration 0008). Judge–human and human–human agreement (κ per criterion and condition) and the PPI correction follow in stage 6.

### As built in stage 5d (opposing reasonings)

- **Writing** (`bench/opposing.py`, helper task `write_opposing`, role `writer`, page "Gegenbegründungen", admin): per released case without a pair, the writer model reads the **case input only** (as the tested models do, never the decision) and writes two reasonings in the style of the VfGH's considerations: one concluding that the challenged provisions are unlawful, one that they are not. An answer whose shorter side has fewer than 80 % of the longer side's words, or fewer than 150 words, is *not evaluable*. A valid answer becomes two `OpposingReasoning` rows (side `unlawful` / `lawful`, word count, writer model and family); the latest pair of a case is the one assessed.
- **Assessment** (task `assess`, prompt `assess.p1` with `{input}` and `{begruendung}`): the tested models, **condition A only**. One job per reasoning, model and repetition, each a fresh call with the case input and one reasoning; the model never sees the other side, the writer or the outcome, and is never asked for an outcome. It rates the reasoning on four levels: *tragfähig / eher tragfähig / eher nicht tragfähig / nicht tragfähig* (sound = the first two; the ordinal scale gives the AUC). Cutoff rule as for deciding; **family rule**: no tested model assesses reasonings written by its own family. Reasonings already assessed or planned per model and repetition are not planned again. A valid answer becomes an `Assessment` (immutable), storing the side.
- **Partly unlawful cases** are written and assessed like the others; which side counts as the VfGH's is decided in the analysis (stage 6).
- Pages: the overview lists per case the word counts and the number of assessments per side; the case page shows both texts and every assessment (admin only, not blind). Migration 0009 adds the tables and `reasoning_id` on jobs and runs.
- Not built: the optional expert rating of ~10 pairs (open point in §13).

### As built in stage 5e (contamination probe)

- **Probe** (`bench/contamination.py`, task `probe`, prompt `probe.p1` with `{datum}` and `{geschaeftszahl}`; `{input}` is refused there; page "Kontamination", admin): each tested model gets, in a fresh call, only "VfGH <date>, <case number>" and says whether it knows the decision (*ja / unsicher / nein*), its outcome and the reasons it recalls. **Every released case** is probed, including cases that fail the model's cutoff rule: those are the **positive control** (`before_cutoff`) and never scored in the study. A pair of case and model is probed once.
- **Outcome** is compared with the VfGH automatically (`outcome_match`; none if the model gives no outcome). A valid answer becomes a `ContaminationProbe` (immutable, migration 0010).
- **Reasons** (task `probe_judge`, prompt `probe_judge.p1` with `{checkliste}` and `{antwort}`, no case input): for each probe that claims knowledge (ja or unsicher), names reasons, and whose case has a confirmed checklist, each chosen judge scores the recalled reasons against the reference reasons (*ja / teilweise / nein*, every reason exactly once, otherwise not evaluable). Family rule as for judging; the judge never sees the tested model. The result is a `JudgeScore` with the probe run as target and no citations; calibration only draws decide runs.
- **Recalled:** the outcome matches and at least one judge finds at least one reference reason ("ja"). The page shows per model, after the cutoff (study) and before it (control): probed, knowledge claimed, outcome right, recalled; and the list per case and model. How recalled cases enter the results is decided in stage 6.

### As built in stage 6a (analysis plan and metrics)

- **Analysis plan:** [analysis-plan.md](analysis-plan.md) fixes every computation (unit, interval, test, threshold, exclusions) and is frozen with the protocol.
- **Code** (`bench/analysis/`): `stats.py` holds the statistics in plain Python, without numpy: Wilson interval, exact McNemar, Cohen's κ, linearly weighted κ, Fleiss' κ, AUC (Mann–Whitney), case-cluster bootstrap with a fixed seed, and the PPI mean. `metrics.py` computes the five levels, calibration agreement and the threshold, PPI per condition, and the contamination sensitivity tables from the stored runs; `analyse(db)` returns all of it as one report for the dashboard and the export.
- **Decisions** (user, 2026-11-06): majority label per case for level 1 (a tie is wrong); the judges' mean, with criteria below κ 0.7 in any condition shown but marked *not calibrated*; recalled cases only in a sensitivity table; partly unlawful cases reported separately in level 5.

### As built in stage 6b (dashboard)

- **Page "Ergebnisse"** (`/dashboard`, `bench/web/dashboard.py`, viewer, expert and admin): computed live from `analyse(db)` over all stored decide runs; a condition switch (A/B/C) above the figures. Until the protocol is frozen (6d), the page says that the numbers are provisional.
- **Figure 1, leaderboard:** per model the cases, accuracy with its interval as a bar and the "always unlawful" baseline as a tick, share of correct runs, κ, reasons, fabricated and inaccurate citations; in A also unstable, both sound and AUC. Judge-based columns carry "vorl." while the criterion is not calibrated.
- **Figure 2, profiles:** all metrics on 0–100 (higher is better) in a blue heatmap with the values in the cells.
- **Figure 3, case × model:** majority label right / wrong per cell with a glyph (✓ ✗ ·, ~ for disagreeing runs), hits per model and models right per case; admins can open the case.
- **Figure 4:** source access (accuracy per condition, one line per model, with the McNemar results in the table); prior knowledge (share recalled before vs after the cutoff, from the contamination probe, instead of accuracy by distance to the cutoff, because pre-cutoff cases are never run); sham reasoning (accuracy against both judged sound, with AUC and the partly unlawful row in the table); error direction (confusion matrix for a chosen model, diagonal outlined).
- **Details:** calibration κ per criterion, condition and rater with the threshold; PPI; the sensitivity table without recalled cases; stability with κ per phrasing; citation counts.
- **Charts:** inline SVG and HTML tables, no JavaScript library; the reference palette of the dataviz method (categorical slots in fixed order, one blue ramp); colour is never alone (glyphs, values, labels, legends); every chart has its numbers in a table; hover tooltips via SVG titles. Light mode only, like the rest of the app.

### As built in stage 6c (export)

- **One ZIP** (`bench/export.py`): `README.md`, `manifest.json` (time, app version, protocol, rows and sha256 per file), `data/*.jsonl.gz` (cases with their released parse, orders with snapshots, prompt versions, runs, checklists, citation checks, judge scores, calibration items and ratings, opposing reasonings, assessments, contamination probes, norm sets), `metrics/*.csv` (every table of the analysis report; intervals as `_low` / `_high`) and `dashboard/<condition>.html` with its stylesheet, readable offline. Same data and time give the same bytes.
- **Privacy:** user ids become "Person n"; names, e-mail addresses, users, sessions, the audit log and provider checks are never exported. Keys were never stored.
- **Raw HTTP exchanges** are left out by default (publishing them is an open point, §13); admins can add them (`data/raw_exchanges.jsonl.gz`).
- **Where:** button on the dashboard (`/export.zip`, viewer and up; `?raw=1` admin only; each download is in the audit log) and `bench export <file> [--raw]`. Publish results (6d) will commit the same files under `results/`.

### As built in stage 6d (freeze protocol and publish results)

- **Study page** (`/study`, `bench/web/study.py`, admin): create the study (slug, name), then edit the draft setup: cases, models per role (tested, judge, writer, extractor), conditions with repetitions and phrasings, the cutoff rule. The page lists what blocks freezing (e.g. no cases) and hints (cases without confirmed checklist, Stichtag or norms).
- **Freeze** (`bench/protocol.py`): resolves the draft into a `ProtocolVersion` (immutable): cases with released parse and input hash, models with config hash, the latest version of every prompt with sha256, the analysis plan's sha256, and the sha256 of all of it. It writes the protocol files of publication.md §4 (`protocol.yaml`, `cases.csv`, `models.yaml` with secret names only, `prompts/*.md`, `analysis-plan.md`, `environment.yaml`, `AMENDMENTS.md`). Version 1 needs no reason; every later version is an **amendment** with a reason and a change.
- **Conformance** is computed from each order's snapshot against the latest protocol version: created after the first freeze; cases and parses, models of the right role with the same config hash, prompt versions, conditions with at most the frozen repetitions and phrasings, the cutoff rule, Stichtage, norm sets and checklists. Orders that do not match may run but are **deviations**: the order page says why, and they are left out of the dashboard, the export and published results (the dashboard can show all orders on request). Judge, assessment and probe runs count only if their own order conforms.
- **Analysis plan** now lives in the package (`src/bench/analysis/analysis-plan.md`, so the container can freeze it); `docs/analysis-plan.md` is a symlink.
- **Publish results:** the export over the conforming orders, with the protocol tag and hash in the manifest; raw exchanges per publish by checkbox (default off). Each release is a `ResultsRelease` (immutable); its ZIP is kept in `<data_dir>/releases/`.
- **Study repository** (`bench/studyrepo.py`): `BENCH_STUDY_REPO_URL`, branch `BENCH_STUDY_REPO_BRANCH`, SSH deploy key from the secret `STUDY_REPO_DEPLOY_KEY`. Each push works in a fresh clone in a temporary directory, writes below `studies/<slug>/` (`results/` replaced as a whole), commits, sets an annotated tag (`<slug>-protocol-vN`, `<slug>-results-vN`) and pushes; an existing tag is never moved. Each attempt is recorded (`RepoPush`); a failed push can be repeated. Without a repository, protocol and results are offered as ZIP files to commit by hand. The GitHub release (and with it the Zenodo DOI) is still created by hand from the tag.
- `environment.yaml` records `BENCH_GIT_COMMIT` and `BENCH_IMAGE_DIGEST` if set at image build.

## 8 Users and screens

| Role | Screens |
| --- | --- |
| Admin | Cases (RIS search, batch import, split/leak view, flags, release); providers and models (status, test, cutoff coverage); prompts (versions, freeze); orders (setup, cost estimate, progress, controls); protocol (freeze, amendments, publish); costs; users |
| Legal expert | Reference checklists (rate correct / incomplete / wrong); calibration queue (~40 outputs, ~10 of them also rated by a second person; blind, same criteria as the judges); optionally ~10 opposing-reasoning pairs, blind |
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
- **`deploy/fly/`:** `web.toml` runs the released ghcr.io image (deployed by digest with `deploy.sh`, which sets `BENCH_IMAGE_DIGEST`) as one machine with the volume; `bridge.toml` the u:ai bridge, private only. Secrets go in as env.
- **`bench all`** (`bench/supervisor.py`): web, worker and ris-mcp (on `localhost:8001`) as child processes of one machine, because a fly.io volume belongs to one machine. Exits 1 if a child dies (fly.io restarts the machine). Stops with exit 0 after `BENCH_IDLE_STOP_MINUTES` (15) without requests and without open jobs (a running job, or a queued one in a running order); the restart policy `on-failure` leaves it stopped and the proxy starts it at the next request. fly.io's auto-stop is off because it sees only requests, not the worker's LLM calls. Activity: the web process touches `BENCH_ACTIVITY_FILE` on every request except `/health` and while its background tasks run (`bench/activity.py`).
- **u:ai bridge on fly.io** (`bench/flymachines.py`): reached at `vfgh-bench-bridge.internal:8000` over the private network, because fly.io's proxy closes connections silent for 60 s and u:ai calls are not streamed (streaming would change the raw exchanges the study records). `bench all` starts web and ris-mcp first (so the request that woke the machine is answered), then the bridge's machines via the Machines API (`FLY_API_TOKEN`, a deploy token of the bridge app only; a machine still stopping is retried), waits for the bridge's `/health`, then starts the worker; it stops the bridge after the children, also when it fails itself. Failures are logged, not raised. The bridge runs with `uvicorn --host ::` because its own command listens on IPv4 only.
- **Backup:** `bench backup` makes a gzipped SQLite online backup to a directory; locally, scheduling is the host's job. On fly.io, `bench all` uploads one to an S3 bucket (Tigris: `BUCKET_NAME`, `AWS_*`) before a pending migration, every `BENCH_BACKUP_INTERVAL_HOURS` (6) and when it stops, only if the database changed: unchanged size and mtime of the file and its WAL skip the copy, otherwise the sha256 of the uncompressed copy decides (an unchanged database gives a byte-identical copy). A missing bucket key only switches backups off. Deploys stop the worker without draining: deploy while no order runs. Pruning keeps all of the last `BENCH_BACKUP_KEEP_DAYS` (14) days, before that the newest of each ISO week. Volume snapshots are kept 30 days in addition. `bench restore KEY|FILE` checks the backup (integrity, `alembic_version`) under a temporary name and only then stages it (run as root, e.g. via `fly ssh console`, it gives the file to the owner of the data directory); the next start of `bench all` swaps it in and keeps the old file beside it (`--now` swaps at once when nothing runs). Decisions (user, 2026-11-07): local runs build their own containers, fly.io the released image; the bridge is part of the fly.io deployment; machines stop when unused; backups to Tigris (EOS via a CERNBox upload-only link considered, postponed).
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
| 5 Evaluation ✓ | 5a reference checklists ✓, 5b judges and citation check ✓, 5c calibration queue ✓, 5d opposing reasonings and assessment ✓, 5e contamination probe ✓ |
| 6 Analysis ✓ | 6a analysis plan and metrics ✓; 6b dashboard ✓; 6c export ✓; 6d freeze protocol and publish results ✓. Metrics (κ, Fleiss' κ, Wilson, AUC, McNemar, PPI), dashboard with figures 1–4, export, Publish results |

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
