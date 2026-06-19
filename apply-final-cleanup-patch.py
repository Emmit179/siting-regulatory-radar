from __future__ import annotations

import py_compile
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "src" / "countywatch" / "deep_rebuild.py"
CLEANUP_MODULE = ROOT / "src" / "countywatch" / "final_cleanup.py"
BACKUP = ROOT / "src" / "countywatch" / "deep_rebuild.py.before-final-cleanup"

MARKER = "from .final_cleanup import cleanup_consolidated_signals"
ANCHOR = (
    "    signals, county_results = _consolidated_signals(db)\n"
    "    backup = _backup_database(settings, db)\n"
    "    now = utcnow()"
)
REPLACEMENT = (
    "    signals, county_results = _consolidated_signals(db)\n"
    "    backup = _backup_database(settings, db)\n"
    "    from .final_cleanup import cleanup_consolidated_signals\n\n"
    "    signals, county_results = cleanup_consolidated_signals(db, signals, county_results)\n"
    "    now = utcnow()"
)


def fail(message: str) -> int:
    print(f"ERROR: {message}")
    return 1


def main() -> int:
    if not TARGET.exists():
        return fail(f"Could not find {TARGET}. Extract this ZIP into the project folder beside update-now.bat.")
    if not CLEANUP_MODULE.exists():
        return fail(f"Could not find {CLEANUP_MODULE}. Re-extract the ZIP and try again.")

    original = TARGET.read_text(encoding="utf-8")
    if MARKER in original:
        try:
            py_compile.compile(str(TARGET), doraise=True)
            py_compile.compile(str(CLEANUP_MODULE), doraise=True)
        except py_compile.PyCompileError as exc:
            return fail(f"The already-installed patch does not compile: {exc}")
        print("The final cleanup hook is already installed. No existing file was changed.")
        return 0

    occurrences = original.count(ANCHOR)
    if occurrences != 1:
        return fail(
            "The expected surgical insertion point was not found exactly once in "
            "src\\countywatch\\deep_rebuild.py. No file was changed."
        )

    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)

    patched = original.replace(ANCHOR, REPLACEMENT, 1)
    TARGET.write_text(patched, encoding="utf-8", newline="\n")

    try:
        py_compile.compile(str(TARGET), doraise=True)
        py_compile.compile(str(CLEANUP_MODULE), doraise=True)
    except py_compile.PyCompileError as exc:
        TARGET.write_text(original, encoding="utf-8", newline="\n")
        return fail(f"Compilation failed, so the original file was restored: {exc}")

    print("Installed successfully.")
    print("Exactly one existing source file was modified:")
    print(r"  src\countywatch\deep_rebuild.py")
    print("A safety copy was saved as:")
    print(r"  src\countywatch\deep_rebuild.py.before-final-cleanup")
    print("No dashboard JavaScript, CSS, crawler, exporter, or database file was replaced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
