"""
API routes for the Slov-Lex "Predbežné informácie" scanner -- a separate
feature from the NRSR scraper in main.py, added later, targeting a
different site with a different API shape (see slovlex_scraper.py's module
docstring for how it works and what's verified vs. assumed).

Kept as its own router + its own history file (slovlex_history.json,
alongside history.json) rather than folded into main.py's existing
/api/scrape* + history.json, since the two features have unrelated
request/response shapes (a scan here checks N materials in a date range,
not "download and parse the documents linked from one URL") and mixing
them into one history file/schema would make both harder to reason about.
"""

import gc
import logging
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException

from appdata import user_data_dir
from extractor import find_matches_in_text
from parsers import parse_document
from schemas import MatchResult, SlovLexScanRequest, SlovLexScanResponse
from slovlex_scraper import (
    build_session,
    download_dokument,
    fetch_dokumenty,
    iterate_recent_materials,
)

logger = logging.getLogger("nku_extractor.slovlex_routes")
router = APIRouter(prefix="/api/slovlex")

MAX_DAYS_BACK = 3660  # ~10 years -- generous ceiling, not a real recommendation;
                       # see NAVOD text about this being a full-history crawl above ~365


# --- Run history (separate file from the NRSR one -- see module docstring) ---
HISTORY_FILE = user_data_dir() / "slovlex_history.json"
HISTORY_LOCK = threading.Lock()
MAX_HISTORY_ENTRIES = 200


def _load_history() -> List[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        import json
        with HISTORY_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001 - corrupt/unreadable file shouldn't crash the app
        logger.error("Could not read history file %s: %s", HISTORY_FILE, exc)
        return []


def _save_history(entries: List[dict]) -> None:
    import json
    tmp_path = HISTORY_FILE.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    tmp_path.replace(HISTORY_FILE)


def _append_history(entry: dict) -> None:
    with HISTORY_LOCK:
        entries = _load_history()
        entries.insert(0, entry)
        entries = entries[:MAX_HISTORY_ENTRIES]
        _save_history(entries)


def _history_entry(
    job_id: str,
    days_back: int,
    started_at: str,
    status: str,
    result: Optional[SlovLexScanResponse] = None,
    error: Optional[str] = None,
) -> dict:
    entry = {
        "job_id": job_id,
        "days_back": days_back,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
    }
    if result is not None:
        entry.update(
            {
                "materials_checked": result.materials_checked,
                "materials_with_matches": result.materials_with_matches,
                "matches_count": len(result.matches),
                "matches": [m.dict() for m in result.matches],
            }
        )
    if error is not None:
        entry["error"] = error
    return entry
# -----------------------------------------------------------------------------


class SlovLexJob:
    """In-memory progress tracker, same shape/spirit as ScrapeJob in main.py."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self._lock = threading.Lock()
        self.status = "running"  # "running" | "done" | "error"
        self.current = 0
        self.total = 0
        self.current_label = ""
        self.start_time = time.time()
        self.result: Optional[SlovLexScanResponse] = None
        self.error: Optional[str] = None

    def update(self, current: int = None, total: int = None, label: str = None):
        with self._lock:
            if current is not None:
                self.current = current
            if total is not None:
                self.total = total
            if label is not None:
                self.current_label = label

    def finish(self, result: SlovLexScanResponse):
        with self._lock:
            self.status = "done"
            self.result = result

    def fail(self, error: str):
        with self._lock:
            self.status = "error"
            self.error = error

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = time.time() - self.start_time
            estimated_remaining = None
            if self.status == "running" and self.total and self.current > 0:
                avg_seconds_per_item = elapsed / self.current
                estimated_remaining = avg_seconds_per_item * (self.total - self.current)

            data = {
                "job_id": self.job_id,
                "status": self.status,
                "current": self.current,
                "total": self.total,
                "location": self.current_label,
                "elapsed_seconds": round(elapsed),
                "estimated_seconds_remaining": (
                    round(estimated_remaining) if estimated_remaining is not None else None
                ),
                "error": self.error,
            }
            if self.status == "done" and self.result is not None:
                data["result"] = self.result.dict()
            return data


JOBS: Dict[str, SlovLexJob] = {}
JOBS_LOCK = threading.Lock()


def _run_extraction(blocks, source_label: str, source_url: str = None) -> List[MatchResult]:
    """Same logic as main.py's _run_extraction -- duplicated (not imported)
    to avoid a circular import between this module and main.py."""
    results: List[MatchResult] = []
    for block in blocks:
        for match in find_matches_in_text(block.text):
            results.append(
                MatchResult(
                    source=source_label,
                    source_url=source_url,
                    pattern_matched=match.pattern_matched,
                    matched_text=match.matched_text,
                    context=match.context,
                    page_hint=getattr(block, "page", None),
                )
            )
    return results


def _run_slovlex_scan(days_back: int, job: Optional[SlovLexJob] = None) -> SlovLexScanResponse:
    def report(current=None, total=None, label=None):
        if job is not None:
            job.update(current=current, total=total, label=label)

    session = build_session()
    cutoff = date.today() - timedelta(days=days_back)

    report(current=0, total=0, label="Hľadám záznamy od " + cutoff.isoformat())
    materials = list(iterate_recent_materials(session, cutoff))

    all_matches: List[MatchResult] = []
    processed: List[str] = []
    failed: List[str] = []
    materials_with_matches = 0

    report(total=len(materials))
    for idx, material in enumerate(materials, start=1):
        report(current=idx, label=f"{material.cislo}: {material.nazov[:60]}")

        material_had_match = False

        # 1. Check the title itself -- cheap, no download needed.
        for m in find_matches_in_text(material.nazov):
            all_matches.append(
                MatchResult(
                    source=f"{material.cislo}: {material.nazov}",
                    source_url=material.url,
                    pattern_matched=m.pattern_matched,
                    matched_text=m.matched_text,
                    context=m.context,
                    page_hint=None,
                )
            )
            material_had_match = True

        # 2. Check accompanying documents.
        documents = fetch_dokumenty(session, material.uuid)
        for doc in documents:
            doc_label = f"{material.cislo}: {doc.nazov}"
            content = download_dokument(session, doc.uuid)
            if content is None:
                failed.append(doc_label)
                continue
            try:
                blocks = parse_document(content, doc.nazov)
            except Exception as exc:  # noqa: BLE001 - surface as a failed doc, keep the run going
                logger.error("Parse failure for %s: %s", doc_label, exc)
                failed.append(doc_label)
                continue
            if not blocks:
                failed.append(doc_label)
                continue

            processed.append(doc_label)
            doc_matches = _run_extraction(blocks, source_label=doc_label, source_url=material.url)
            if doc_matches:
                material_had_match = True
            all_matches.extend(doc_matches)

            # A full scan can walk through thousands of documents in one run
            # (see MAX_DAYS_BACK). `content`/`blocks` for a single doc can be
            # sizeable (see the size cap in slovlex_scraper.py), and on a
            # memory-constrained host (e.g. Render's free 512 MB plan) that
            # can add up across a long run if left to Python's normal garbage
            # collection timing. Drop the references and collect explicitly
            # right after each document is done with, so memory is returned
            # promptly instead of only when the collector next feels like it.
            del content, blocks, doc_matches
            gc.collect()

        if material_had_match:
            materials_with_matches += 1

    return SlovLexScanResponse(
        materials_checked=len(materials),
        materials_with_matches=materials_with_matches,
        documents_processed=processed,
        documents_failed=failed,
        matches=all_matches,
    )


@router.post("/scan/start")
def start_slovlex_scan(payload: SlovLexScanRequest):
    if payload.days_back <= 0:
        raise HTTPException(status_code=400, detail="days_back musí byť kladné číslo.")
    if payload.days_back > MAX_DAYS_BACK:
        raise HTTPException(
            status_code=400,
            detail=f"days_back je príliš veľké (max {MAX_DAYS_BACK}).",
        )

    job_id = str(uuid.uuid4())
    job = SlovLexJob(job_id)
    with JOBS_LOCK:
        JOBS[job_id] = job

    def worker():
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            result = _run_slovlex_scan(payload.days_back, job=job)
            job.finish(result)
            _append_history(_history_entry(job_id, payload.days_back, started_at, "done", result=result))
        except Exception as exc:  # noqa: BLE001 - surface any unexpected failure to the UI
            logger.exception("Unhandled error in Slov-Lex scan job %s", job_id)
            job.fail(f"Neočakávaná chyba: {exc}")
            _append_history(
                _history_entry(job_id, payload.days_back, started_at, "error", error=str(exc))
            )

    threading.Thread(target=worker, daemon=True).start()
    return {"job_id": job_id}


@router.get("/scan/status/{job_id}")
def slovlex_scan_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Neznáme job_id (možno už bol backend reštartovaný).")
    return job.snapshot()


@router.get("/history")
def get_slovlex_history():
    return _load_history()


@router.delete("/history")
def clear_slovlex_history():
    with HISTORY_LOCK:
        _save_history([])
    return {"status": "ok"}
