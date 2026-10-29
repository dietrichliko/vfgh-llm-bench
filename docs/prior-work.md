# Prior work: vfgh-llm-begruendung

As of 2026-10-29. This summarises the sibling repository `../vfgh-llm-begruendung` (GitHub `dietrichliko/vfgh-llm-study`, deployed at https://vfgh-llm-study.mbi-ai.dev). All 37 commits are from 2026-10-21. Its docs and code are in German; paths below are relative to that repository.

## 1 What it was

A web application for an earlier, smaller study design. The research question was narrower than in [paper.md](paper.md): *if an LLM can write a convincing reasoning for either outcome, does the duty to give reasons (§§ 58, 60 AVG) still work as a control?*

- **Scale:** pilot of about 10 hand-picked Art 139 B-VG cases, 2–3 tested models per case, 100–400 model answers in total.
- **Evaluation:** a human legal expert (doctoral candidate in law) rated answers blind in a web UI. An LLM judge was only a second opinion.
- **Built in three stages,** each stage usable on its own:
  1. Show one case.
  2. Blind rating.
  3. Condition C with RIS research, a judge model and a contamination test.
- **Stack:**
  - Backend: FastAPI with SQLite and Alembic.
  - Frontend: SvelteKit, served by the backend.
  - Login: CERN SSO / eduGAIN.
  - Hosting: fly.io, with backup to Tigris and a shutdown after idle time.
  - Development: a Silverblue toolbox, pre-commit, Conventional Commits and git-cliff releases.
- **LLM access:**
  - Main route: through the [academic-ai-bridge](https://gitlab.cern.ch/mbi-vienna/academic-ai-bridge) 0.4.0 with profile `univie`, which is u:ai.
  - The ÖAW profile and OpenRouter are optional.

## 2 What was built

About 9,000 lines including tests. Every stage was implemented and tested. Whether real study runs happened cannot be told from the repository; there is no local data directory.

| Area | Where | What it does |
| --- | --- | --- |
| RIS case search and import | `app/src/vfgh/ris/client.py`, `importer.py` | Complete, paginated search of VfGH decisions (counts per area and year). It imports the **XML** version of a decision, not the HTML. |
| Decision parsing | `ris/decision.py` | Parses the XML into headnote, operative part and paragraphs with margin numbers. It drops the `sr-only` screen-reader duplicates. |
| Input/reference split | `ris/split.py` | Splits **at the heading "Erwägungen"**, not at the number IV, because ex-officio reviews are numbered differently. It recognises the procedure type (court application, individual application, ex officio) and flags joint statute reviews (G numbers). Cases with an unexpected structure are flagged, not processed. |
| Leak check | `ris/leak.py` | Flags sentences of 12 or more words that appear in both input and reference. It redacts the case's own case number and ECLI and warns about the decision date appearing in the input. |
| Outcome label | `ris/tenor.py` | Proposes one of `aufgehoben` / `teilweise_aufgehoben` / `abgewiesen` / `zurueckgewiesen` from the operative part; the admin confirms it. |
| LLM client | `app/src/vfgh/llm.py` | Plain httpx client for OpenAI-compatible APIs, with no SDK, so exactly the stored JSON is sent and the raw response is stored. It retries only on 429/502/503/504, with backoff, always on the same endpoint and model. It never retries a timeout, because the request may already be running and incurring cost. |
| Run manager | `app/src/vfgh/runs.py` | Plans runs with a cost estimate. It applies cost caps per order, per case and in total, and keeps a cache keyed on the hash of model, parameters, prompt, input and repetition number, so nothing is paid twice. It also enforces the family rule for judges and marks a run when the answering model differs from the requested one. Runs that cannot be evaluated are marked, not retried silently. Runs resume after a restart and are immutable once complete. |
| Tool loop (condition C) | `runs.py` (`_execute_with_tools`), `mcp_tools.py` | The app is the MCP client and runs the loop itself. On connecting, it checks that the server offers **exactly** `suche_normen` and `hole_norm`. It sends them as OpenAI tools to the bridge, which emulates tool calling for u:ai. At most 15 tool calls; every call is logged. |
| RIS MCP server | `ris-mcp/src/ris_mcp/` | A separate service with Streamable HTTP and a bearer key. It has two tools: `suche_normen` (federal law, state law `LrKons`, municipal law `Gr`/`GrA`, district regulations `Bvb`) and `hole_norm` (understands natural citations such as "§ 24 StVO"). Only versions in force on the reference date are returned, with no expiry date and no annotations that would reveal a later annulment. Long texts come in parts of at most 12,000 characters. A separate judge endpoint `/richter/mcp` adds `pruefe_entscheidung` (case-law lookup). |
| Models | `app/src/vfgh/studie/modelle.yaml` | Models are configuration, not code. The file lists endpoints, families, roles (tested model, judge, reference proposal), prices and expected output tokens, and versions each config change. |
| Prompts | `app/src/vfgh/studie/prompts/*.v1.md` | German prompts: free decision, forced reasoning for annulment and for dismissal (each also as a `_c` variant for condition C), reference proposal, judge, contamination. Each file splits into system and user parts with placeholders. Prompts may change during development; from the release tag on they are frozen. A test asserts that no tested-model prompt hints at the outcome. |
| Citation check | `app/src/vfgh/citations.py` | Classifies each citation (norm, decision, literature) and checks automatically whether it exists in the RIS. The result goes to the judge, who checks whether the citation fits the claim. |
| Pairs | `app/src/vfgh/selection.py` | Builds the forced-reasoning pair per model and case, with the A/B order taken from a stored seed. |
| Reference checklist | `app/src/vfgh/api_checklist.py` | An LLM proposes the reasons; the expert classifies each as `tragend` (decisive), `verstärkend` (supporting), `nebenbei` (incidental) or `offen_gelassen` (left open), edits them and releases a versioned checklist. |
| Blind rating | `app/src/vfgh/api_rating.py`, `frontend/` | Rating queue in random order, rating forms for level 2 (free runs) and level 5 (pairs), resolution of the pair only after rating, scheduled re-rating after 14 days, and versioned, immutable ratings. |
| Stage 3 | `app/src/vfgh/api_stage3.py` | Condition C orders, the judge model with the family rule, and the contamination test (case number and date only). |
| Analysis and export | `app/src/vfgh/analysis.py`, `export.py` | Cohen's κ, Wilson intervals, judge–human agreement, a filter for contaminated cases and an effort table (tokens and time per run and model). Export as CSV, JSON and JSON Lines. |
| Case page | `app/src/vfgh/casepage.py`, `templates/casepage.html.j2` | A standalone, printable HTML page per case, generated from the log at no cost. |
| Bridge requirements | `docs/bridge-anforderungen.md` | What the study needed from academic-ai-bridge: no invented default parameters, upstream profiles, provenance via `X-Bridge-Raw` / `x_upstream`, a versioned tool-emulation prompt, upstream schema validation, and an info endpoint. Implemented by bridge 0.4.0. |

**Tests**, in `app/tests` and `ris-mcp/tests`, cover:
- blindness of the input,
- the split on real decisions (V 251/2025, V 228, V 81),
- caching, cost caps, model mismatch and immutability,
- the tool limit and rejection of an unexpected tool set,
- the judge family rule,
- prompts free of outcome hints,
- the reference-date filter in ris-mcp and the judge-only case-law tool.

## 3 Findings worth keeping

From `docs/06-abnahme-und-offene-punkte.md` (checked 2026-10-21):

- **Historical law versions:**
  - RIS OGD API v2.6 returns the version in force on a reference date with `Applikation=LrKons`, `Bundesland.SucheIn<Land>=true` (the flat form is ignored) and `Fassung.FassungVom`. This works for state law and the same way for federal law.
  - How far back each state goes must be checked per case.
- **Screen-reader duplicates:** RIS full texts contain `class="sr-only"` duplicates, for example "Absatz eins". They must be dropped.
- **Structure:**
  - Applications follow I. application / II. applicable law / III. submissions / IV. considerations / V. outcome.
  - Ex-officio reviews follow I. initiating proceedings and review decision / II. law / III. considerations / IV. outcome.
  - That is why the split is anchored on the heading "Erwägungen". All 40 decisions checked from 2024/2025 split cleanly.
- **Ex-officio reviews leak.** Their section I contains the court's own preliminary concerns, which the considerations then repeat (V 218/2025: 7 leak hits). The proposal was to use only application proceedings as test cases.
- **Metadata leak:** RIS search hits already carry the headnote, so metadata must never enter an input.
- **Gaps in the RIS:** StVO regulations announced by traffic signs, which is most StVO cases, are not in the RIS. Municipal and district law coverage is patchy: `GrA` only since about 2025, mostly Tirol and Vorarlberg.
- **Case counts:** searching `Norm=B-VG Art139` with `Entscheidungsart=Erkenntnis` gave 47 judgments for 2024 and 59 for 2025 (14 of them joined with a statute review). By area: StVO about 40, spatial planning about 25.
- **Models on u:ai** (checked 2026-10-21):
  - Available: gpt-5.5, gpt-5.2, gpt-5, o3, claude-opus-4-6, claude-opus-4-8, gemini-3.1-pro-preview, gemini-3.5-flash, gemini-2.5-pro, Mistral-Large-3, sonar-pro, sonar-reasoning-pro.
  - Never use `sonar-*` as a tested model: they have built-in web search.
- **No caching on u:ai:** u:ai reports no cached tokens, so prompt caching was deferred. For a larger study, cost, caching (for example via OpenRouter) and the effort of agentic runs need replanning. The effort table provides the pilot numbers for that.
- **Reasoning tokens** are read from the usage reported by bridge 0.4.

## 4 Differences from the new design

| Aspect | vfgh-llm-begruendung | New design (paper.md) | Consequence |
| --- | --- | --- | --- |
| Core question | Can reasoning still serve as a control? (level 5 is the core) | Do LLMs reason like the VfGH, on five levels equally? | Level 5 becomes one of five, measured differently |
| Who rates | Human expert, blind, in a web UI; LLM judge as second opinion | Two LLM judges from different providers; expert rates ~100 outputs once for calibration, plus 30 by a second person; PPI correction | Most of the rating UI goes; we need a small calibration UI or a spreadsheet workflow |
| Reference reasons | Expert edits and classifies an LLM proposal | LLM extracts a checklist (with headnotes); the expert only rates it correct / incomplete / wrong | A simpler rating step |
| Level 5 | Tested model writes forced reasonings for both outcomes; the human guesses which is the VfGH's | A **different** model writes both reasonings; the **tested** model judges each one in a fresh context | New prompts and a new run type; the pair logic is mostly obsolete |
| Conditions | A and C (B was explicitly out of scope) | A, B (pre-selected norms in the applicable version) and C | B is new: someone or something must select the norms per case |
| Cases | About 10 hand-picked applications | All Art 139 decisions since mid-2025 (about 80–90), filtered per model by training cutoff + 3 months | Batch import and an automatic split with no manual release per case; cutoff metadata per model |
| Ex-officio cases | Proposed to exclude (leak via the review decision) | Not mentioned | **Open:** include, exclude or report separately |
| Outcome labels | 4 labels for the whole decision | 5 labels **per challenged provision** (annulled, was unlawful, dismissed, rejected, discontinued) plus main vs. alternative application | New output schema and outcome extraction from the operative part |
| Repetitions | 1–5 free runs | 5 runs × 3 phrasings | 15 runs per case, model and condition; cost and time scale up |
| Baselines | None | Majority class and "follow the application" | We need a label for what the application asked for |
| Statistics | κ and Wilson intervals | Plus Fleiss' κ, AUC, McNemar and PPI | New analysis code |
| Output | Web app plus printable case page | Interactive dashboard (leaderboard, model profiles, case × model grid, four analyses) | A new front end; probably static |
| Tool emulation | academic-ai-bridge emulates tool calling; the app runs the loop | "A proxy emulates it for all models and logs every call" | Probably the same bridge; to confirm |
| Operations | Multi-user web app on fly.io with SSO and backup | Batch pipeline; only the expert calibration needs a UI | Auth, backup and idle shutdown are likely not needed |

## 5 Reuse candidates

| Component | Proposal | Note |
| --- | --- | --- |
| `ris/` (client, decision, split, leak, tenor) | **Reuse, adapt** | The core of the data step and well tested. The outcome labels must become per provision. |
| `ris-mcp/` | **Reuse as is** | Already matches the paper (legislation only, reference-date filter, no case law). The judge endpoint serves the citation check on level 3. |
| `llm.py` | **Reuse as is** | Small and correct. |
| Tool loop and MCP client from `runs.py` / `mcp_tools.py` | **Extract** | Today it is tied to the SQLAlchemy models and the RunManager. |
| Cache key, cost caps and provenance from `runs.py` | **Reuse the ideas** | Re-implement more simply for a batch pipeline. |
| `citations.py` | **Reuse, adapt** | Feeds level 3. |
| `analysis.py` (κ, Wilson) | **Reuse parts** | Extend with Fleiss' κ, AUC, McNemar and PPI. |
| Prompts | **Starting point** | Adapt to the new output schema, the 3 phrasings, condition B and the opposing-reasoning judgment. Decide on the language. |
| `modelle.yaml` | **Reuse the format** | Add a training cutoff per model. |
| Auth, backup, lifecycle, rating UI, case page, SvelteKit front end | **Drop** | Unless we decide the calibration rating needs a web UI. |
| Toolbox, pre-commit, git-cliff, CI | **Reuse** | Same developer setup. |
