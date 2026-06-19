# Architecture

## Data flow

1. **County registry** — all 254 Texas counties are seeded by FIPS and resolved against Texas Counties Deliver, which links to each county's official website.
2. **Source discovery** — the official homepage, sitemaps, verified common paths, and linked meeting vendors are searched. Sources are stored separately from documents and can be corrected with `countywatch add-source`.
3. **Incremental retrieval** — every source listing is considered before county caps are applied; `ETag`, `Last-Modified`, canonical URLs, raw-content hashes, and text hashes prevent redundant extraction and model calls.
4. **Extraction** — HTML, PDF, DOCX, RTF, text/CSV, images, and YouTube captions are supported. Low-text PDFs can be OCRed with Tesseract.
5. **Retrieval before generation** — a high-recall, weighted topic/regulatory proximity filter selects bounded excerpts. The LLM never receives unrelated full packets.
6. **Grounded classification** — free-provider routing tries Groq, Gemini, then OpenRouter. A strict schema captures topic, posture, stage, mechanism, confidence, and an exact quote. Quotes that cannot be mapped back to source text are discarded.
7. **Second pass** — proposed high-risk/drafting/moratorium signals are independently verified when call budget permits.
8. **Rules fallback** — when no model key is configured or free limits are exhausted, a conservative exact-quote local classifier keeps the pipeline operational and clearly labels its engine.
9. **Transparent scoring** — stage, mechanism, posture, confidence, and recency produce a 0–100 research-priority score. County aggregation is bounded and never converts poor coverage into a low-risk label.
10. **Static publication** — JSON/CSV and a dependency-free HTML/CSS/JavaScript dashboard are produced in `site/` and deployed to GitHub Pages.

## State model

SQLite stores county resolution, source health, document identities, immutable revisions, compressed text for potentially relevant records, analyses keyed by prompt version, exact-quote signals, daily county snapshots, crawl runs, and model usage. Raw documents and full extracted text live under `var/` locally and are content-addressed.

The scheduled workflow persists SQLite as a machine-managed GitHub Release asset. This avoids committing a changing binary database to the main branch while retaining incremental state between ephemeral Actions runners. A bounded run stops cleanly before the platform timeout; counties not reached are ordered first on the next run.

## Risk semantics

The score is a triage metric, not probability and not a legal conclusion. Procedural stage is the strongest driver:

- mention 10
- study 25
- staff direction 42
- drafting 58
- public notice 64
- public hearing 70
- introduction 78
- adopted 94
- enforcement 98
- rescinded 18

Moratoria and prohibitions add weight; supportive incentives reduce restriction risk. Non-adopted activity decays faster than adopted/enforced rules. Every daily export recalculates decay.

## Coverage semantics

Coverage combines official-site resolution, recent discovery, substantive source count, recent successful source checks, and extracted-document history, with penalties for repeated failures. Below 35% with no signals is **coverage unknown**, not low risk.

## Boundaries

The crawler obeys `robots.txt`, paces each host, does not bypass authentication, and records failures. Some counties do not publish all records electronically, use portals that change without notice, or post scanned material with poor OCR. The coverage audit makes these gaps visible; manual official source additions do not require code changes.
