# Help topics

Material for the in-app help (planned after the fly.io deployment, together with the
English UI). Collected while testing; each topic names where it belongs.

Decisions (user, 2026-11-07): help in three layers — a "?" next to fields that opens a
short explanation in place (not hover tooltips: they fail on touch screens and with the
keyboard), help pages (`/help`) with the study in plain language, a glossary and
step-by-step guides, and links from each page to its help section. The whole UI and the
help in German and English (gettext/Babel, switch per user, browser language first).
Prompts, case texts, model answers and stored error messages stay German: they are
study data.

## Help pages

- **The study in plain terms:** [plain-language.md](plain-language.md), also in German.
- **Glossary:** conditions A/B/C, Stichtag, norm set, phrasing (Formulierung),
  repetition, cutoff rule, checklist, judge, calibration, contamination probe, opposing
  reasoning, deviation (Abweichung), protocol freeze.
- **Guides:** review and release a case; set the Stichtag; approve the norms for B;
  create an order; what to do with jobs that are unknown, cut off or timed out.

## Field help ("?")

### Case page

- **Release form (Ergebnis bestätigen):** one outcome for the whole case, pre-selected
  from the operative part; check it against the decision.
- **Exclusion reason (Grund):** only for excluding a case, never for releasing it. Every
  exclusion needs a reason because the case selection must be traceable (how many cases
  the RIS returned, how many were left out and why). Stored on the case and in the audit
  log; "Wieder aufnehmen" undoes it. Automatic exclusions use the same field (e.g.
  reviews opened by the court itself: the input shows the court's preliminary view).
  Typical manual reasons: the input gives away the result; input and reference are split
  wrongly; images needed to understand the case; the same case twice.
- **Stichtag:** the date whose legal situation the models get in B (norm texts) and C
  (RIS lookup). It must be a day on which all versions the court quotes as applicable
  ("idF …" in section II, Rechtslage) were in force.
  - Where to find it: section "II. Rechtslage" names the versions ("idF LGBl 17/2026");
    the RIS shows each section's "Inkrafttretensdatum" and "Außerkrafttretensdatum", and
    the search field "Fassung vom" shows the law as it stood on any date.
  - Example V8/2026 (Tyrolean hunting regulation): the court quotes the regulation idF
    LGBl 14/2024 and the act idF LGBl 17/2026, in force together only from 1.4.2026 to
    21.10.2026. The date of the underlying administrative decision (24.4.2025) would be
    wrong: it gives an older § 52a. The decision date (29.9.2026) is right.
  - Example V29/2026 ua (speed limits, Schladming): four regulations from 1996, 2003 and
    2011, each judged by the StVO in force when it was issued. No single date fits:
    leave the Stichtag empty, the case runs in condition A only.
  - Emptying a browser date field needs day, month and year deleted one by one; the
    button "Stichtag entfernen" does it at once.
  - The rule for the study is for the legal expert to set (open point).

### Norms for condition B

- **Fassung line:** each fetched norm shows its version ("zuletzt geändert durch LGBl …
  · in Kraft seit …"); compare it with the "idF" in the decision. Sections the latest
  amendment did not touch keep an older last amendment: that is correct.
- **Not fetched automatically:** regulations (also the challenged one), annexes to an
  act, whole acts, EU law. Add regulations and annexes by RIS document number. EU law is
  not in the RIS; the decision's Rechtslage usually quotes the relevant articles.
- **Norm ergänzen:** a RIS document number (e.g. LTI40050640) identifies one document;
  the scope field (Geltungsbereich) is then ignored. For a citation ("§ 1 2. DVO TJG
  2004") the scope tells the app where to search (Tirol for Tyrolean law).
- **Procedural law** (VfGG, Art 139 B-VG) is proposed but not ticked, by design.

### Orders

- **Phrasings (Formulierungen):** the same task in three wordings (p1 "Du bist der
  Verfassungsgerichtshof…", p2 "Sie erhalten…", p3 "Entscheide den folgenden Fall…").
  Repetitions measure chance with the same prompt, phrasings measure dependence on the
  wording (level 4, stability). The result per model is the majority over all runs.
  1 = p1 only (enough for a technical test), 3 = all.
- **Cases without Stichtag** run in A only; the preview lists them.
- **Cutoff rule:** a case is used for a model only if it was decided at least
  `cutoff_months` after the model's training cutoff.

### Jobs and runs

- **unbekannt (unknown):** the worker stopped while a request was out; the model may have
  answered and been billed. Retry from the order page. Deploy only while no order runs.
- **nicht auswertbar (not evaluable):** an answer arrived but does not fit the schema, or
  the app could not process it. It counts as wrong and is never re-asked: "Aufrufe
  wiederholen" re-sends only failed and unknown jobs.
- **abgeschnitten (cut off):** the answer reached `max_output_tokens`; typical for a model
  caught in a loop. Not evaluable — itself a finding about the model.
- **Zeitlimit (time limit):** the run exceeded `max_run_seconds`. Needed because
  OpenRouter keeps a connection alive while the model writes, so a plain read timeout
  never fires. Counts as not evaluable (wrong), like an answer cut off at the cap.
- **Provider shown on a run:** OpenRouter picks a provider per call unless it is pinned
  in `providers.yaml`; for the study every tested model must be pinned (provider and
  quantization), otherwise one series mixes hosting setups.

## Setup (README, admin help)

- Rootless Podman with SELinux: secret files need `chmod 644` and the label
  `container_file_t`; the directory `deploy/secrets` stays `700`.
- No compose provider installed: `uvx podman-compose`. After a rebuild run `down`
  first, or the containers keep the old image.
- `UAI_API_KEY` has the same value as the bridge's `AAI_BRIDGE_API_KEY`.
- OpenRouter: a separate key with a credit limit for the study.
