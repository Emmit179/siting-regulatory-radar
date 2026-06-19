TEXAS COUNTY REGULATORY RADAR — CHATGPT PRO HISTORICAL BACKFILL
================================================================

WHAT THIS DOES
--------------
This is a one-time, quality-first rebuild of the existing historical signal layer.
It reuses the cached SQLite text and does not recrawl or re-OCR the 120,000 pages.

The workflow has two model phases:

  Phase 1: document-by-document classification from curated exact-source excerpts
  Phase 2: county-level event deduplication and narrative consolidation

Every imported event is then checked locally. A displayed signal cannot survive unless:

  * its revision and input hash still match the current SQLite record;
  * its passage ID exists;
  * its quote is an exact or whitespace-equivalent substring of cached source text;
  * the same quote contains both the target technology and material action;
  * it does not match known noise such as solar speed signs, SaaS data centers,
    NOAA data centers, museum windmills, or generic unrelated land-use language;
  * its topic, stage, mechanism, posture, and outcome pass deterministic constraints;
  * its source URL comes from the crawler database, not from model output.

The existing dashboard is not replaced until all document and county checkpoints pass.
A timestamped SQLite backup is created immediately before publication.

IMPORTANT
---------
Do not run update-now.bat or deep-rebuild-intelligence.bat in the middle of this workflow.
If the underlying database changes, the importer refuses stale ChatGPT output and asks you
to regenerate the batches. Closing a batch/import window after it finishes is safe.

STEP 1 — CREATE DOCUMENT BATCHES
--------------------------------
Double-click:

  1-prepare-chatgpt-document-batches.bat

Then double-click:

  open-chatgpt-backfill-folder.bat

Open these folders/files:

  phase1-input
  phase1-output
  PROMPT-PHASE-1.txt

STEP 2 — REVIEW EACH PHASE 1 BATCH IN CHATGPT PRO
-------------------------------------------------
Create a new ChatGPT Project named something like:

  Texas County Backfill

Use the strongest Pro / extended-reasoning model available.
For each file in phase1-input:

  1. Start a NEW chat inside that Project.
  2. Upload exactly one phase1-batch-###.jsonl file.
  3. Open PROMPT-PHASE-1.txt, copy all of it, and paste it into the chat.
  4. Add one final sentence naming the attached batch, for example:

       The attached input is phase1-batch-001.jsonl. Create phase1-result-001.jsonl.

  5. Let ChatGPT finish and download the generated JSONL file.
  6. Move that downloaded file into:

       var\chatgpt_backfill\current\phase1-output

Use one fresh chat per batch. Do not combine batches. Do not ask it to browse the links.

STEP 3 — IMPORT AND VERIFY PHASE 1
----------------------------------
After downloading all Phase 1 result files, double-click:

  2-import-chatgpt-document-results.bat

The importer accepts JSON, JSONL, or TXT output and does not care if Windows adds “(1)”
to a filename. It reports exactly which input batches still need a result.

You may rerun this importer as often as needed. Verified work is checkpointed.

STEP 4 — CREATE COUNTY CONSOLIDATION BATCHES
---------------------------------------------
When Phase 1 reports complete, double-click:

  3-prepare-chatgpt-county-batches.bat

Open:

  phase2-input
  phase2-output
  PROMPT-PHASE-2.txt

Counties with zero or one verified event are finalized locally and require no Phase 2 call.

STEP 5 — REVIEW EACH PHASE 2 BATCH IN CHATGPT PRO
-------------------------------------------------
For each phase2-batch-###.jsonl file:

  1. Start a NEW chat in the same ChatGPT Project.
  2. Upload exactly one Phase 2 batch.
  3. Paste all of PROMPT-PHASE-2.txt.
  4. Add the input/output filename sentence.
  5. Download the generated result file.
  6. Put it in:

       var\chatgpt_backfill\current\phase2-output

STEP 6 — VERIFY AND PUBLISH
---------------------------
Double-click:

  4-import-and-publish-chatgpt-backfill.bat

If a result is missing or malformed, the existing dashboard stays untouched and the window
names the missing batch. Once complete, start the dashboard normally:

  start-dashboard.bat

STATUS
------
At any point, double-click:

  check-chatgpt-backfill.bat

DAILY UPDATES AFTER THE BACKFILL
--------------------------------
Continue using update-now.bat. Existing imported document checkpoints are retained. Only new
or changed candidate documents need model review. The included deep_rebuild.py also contains
the provider stability fixes for oversized Groq requests, structured JSON fallback, rolling
TPM waits, Gemini schema fallback, and accurate ChatGPT-backfill model labels.

The free API quotas remain a throughput constraint, but they are now used only for incremental
new/changed material rather than the entire historical corpus.
