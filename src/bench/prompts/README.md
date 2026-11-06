# Prompts

DRAFT: to be read and approved by the legal expert before the study (see docs/paper.md §4).

- These files only seed the database. After the first start, prompts are edited on the
  page "Prompts"; every change creates a new version, and every run stores the version id
  and the full text sent.
- File name `<task>.p<N>.md`, where N is the phrasing (1–3). The part before `---user---`
  is the system message, the part after it the user message.
- Tasks: `decide` (condition A), `decide_b` (condition B: plus the approved norm texts
  in `{normen}`), `decide_c` (condition C: the same task with the RIS
  tools; the Stichtag is set by the app and never named in the prompt). Helper tasks
  have one phrasing: `extract_reasons` (reference reasons from the considerations) and
  `judge` (scores a decide run).
- Placeholders: `{input}` (sections I–III of the decision, redacted; for
  `extract_reasons` the reference part); `{normen}` only in `decide_b` (the norm texts
  approved for the case); `{checkliste}`, `{antwort}` and `{zitate}` only in `judge` (the
  confirmed reference reasons, the answer judged, its citations with the RIS check).
- No prompt may hint at the actual outcome.
