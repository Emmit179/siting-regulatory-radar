FINAL DETERMINISTIC CLEANUP

1. Extract this ZIP directly into the folder containing update-now.bat.
2. Double-click apply-final-cleanup-patch.bat.
3. Double-click final-cleanup-and-republish.bat.
4. Start the dashboard normally with start-dashboard.bat.

The cleanup uses the completed ChatGPT Pro checkpoints already stored in SQLite.
It makes no model calls and performs no new crawl.

It fixes:
- records whose actual government actor is a different county;
- facial local moratorium orders misclassified as state advocacy;
- crawl timestamps and strong source-filename date conflicts;
- signed/expiring canonical links when a durable official endpoint is available;
- canonical quotes not attached to their matching supporting-source record;
- low-value non-regulatory items such as scholarship payments;
- only exact, highly conservative residual duplicates.

The raw documents, document-level model output, and source history remain in SQLite.
Only the active signal layer and dashboard export are rebuilt. The normal atomic
cutover creates a timestamped SQLite backup before replacing active signals.

A machine-readable report is written to:
  var\final_cleanup\latest-report.json

Future deep cutovers automatically run the same deterministic QA because the cleanup
hook is installed inside deep_rebuild.py.
