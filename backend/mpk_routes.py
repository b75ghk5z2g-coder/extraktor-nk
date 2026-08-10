"""
FastAPI routes for MPK (medzi-rezortné pripomienkové konanie) operations.
"""

import gc
import io
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from openpyxl import Workbook

from mpk_scraper import (
    build_session,
    download_file,
    extract_accompanying_documents,
    get_stadium_uuid_from_process,
    search_mpk_processes,
)
from mpk_extractor import find_matches_in_text
from parsers import parse_document
from schemas import (
    ExportRequest,
    MatchResult,
    MPKJobStatus,
    MPKSearchRequest,
    MPKSearchResponse,
)

logger = logging.getLogger("mpk_extractor.routes")
router = APIRouter(prefix="/api/mpk", tags=["MPK"])


class MPKJob:
    """In-memory progress tracker for MPK scrape jobs"""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self._lock = threading.Lock()
        self.status = "running"
        self.current = 0
        self.total = 0
        self.current_label = ""
        self.start_time = time.time()
        self.result: Optional[MPKSearchResponse] = None
        self.error: Optional[str] = None

    def update(self, current: int = None, total: int = None, label: str = None):
        with self._lock:
            if current is not None:
                self.current = current
            if total is not None:
                self.total = total
            if label is not None:
                self.current_label = label

    def finish(self, result: MPKSearchResponse):
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
                "location": self.current_label[:100],  # Truncate for display
                "elapsed_seconds": round(elapsed),
                "estimated_seconds_remaining": (
                    round(estimated_remaining) if estimated_remaining is not None else None
                ),
                "error": self.error,
            }
            if self.status == "done" and self.result is not None:
                data["result"] = self.result.dict()
            return data


MPK_JOBS: Dict[str, MPKJob] = {}
MPK_JOBS_LOCK = threading.Lock()


def _run_extraction(blocks, source_label: str, source_url: str = None) -> List[MatchResult]:
    """Run extraction on parsed document blocks"""
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


def _run_mpk_search(
    search_term: str,
    job: Optional["MPKJob"] = None
) -> MPKSearchResponse:
    """Core MPK search logic"""

    def report(current=None, total=None, label=None):
        if job is not None:
            job.update(current=current, total=total, label=label)

    session = build_session()
    all_matches: List[MatchResult] = []
    processed: List[str] = []
    failed: List[str] = []

    # Search MPK processes
    report(current=0, total=0, label=f"Vyhľadávanie: {search_term}")
    processes = search_mpk_processes(session, search_term=search_term)

    if not processes:
        return MPKSearchResponse(
            processes_found=0,
            processes_with_matches=0,
            documents_processed=[],
            documents_failed=[],
            matches=[],
        )

    report(total=len(processes))
    processes_with_matches = 0

    for idx, process in enumerate(processes, start=1):
        report(current=idx, label=f"Proces: {process.title[:60]}")

        try:
            # Get stadium UUID for this process
            stadium_uuid = get_stadium_uuid_from_process(session, process.url)
            if not stadium_uuid:
                logger.warning("Could not get stadium UUID for %s", process.url)
                failed.append(process.url)
                continue

            # Extract accompanying documents
            documents = extract_accompanying_documents(session, process.url, stadium_uuid)
            if not documents:
                failed.append(process.url)
                continue

            # Download and process each document
            process_matches = []
            for doc in documents:
                report(label=f"Dokument: {doc.filename}")
                content = download_file(session, doc.url)
                if content is None:
                    failed.append(doc.url)
                    continue

                try:
                    blocks = parse_document(content, doc.filename)
                except Exception as exc:
                    logger.error("Parse failure for %s: %s", doc.filename, exc)
                    failed.append(doc.url)
                    continue

                if not blocks:
                    failed.append(doc.url)
                    continue

                # Extract matches
                source_label = f"{doc.title} (LP: {process.lp_number})"
                matches = _run_extraction(blocks, source_label=source_label, source_url=doc.url)
                process_matches.extend(matches)
                processed.append(doc.url)

                # Memory management
                del blocks
                gc.collect()

            if process_matches:
                all_matches.extend(process_matches)
                processes_with_matches += 1

        except Exception as exc:
            logger.error("Error processing %s: %s", process.url, exc)
            failed.append(process.url)
            continue

    return MPKSearchResponse(
        processes_found=len(processes),
        processes_with_matches=processes_with_matches,
        documents_processed=processed,
        documents_failed=failed,
        matches=all_matches,
    )


@router.post("/search", response_model=MPKSearchResponse)
def search_mpk(payload: MPKSearchRequest):
    """Synchronous MPK search (blocks until complete)"""
    search_term = payload.search_term or "NK"
    logger.info("Starting synchronous MPK search for: %s", search_term)

    try:
        result = _run_mpk_search(search_term)
        return result
    except Exception as exc:
        logger.error("MPK search failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/search/start")
def start_mpk_search(payload: MPKSearchRequest):
    """Start async MPK search in background thread"""
    search_term = payload.search_term or "NK"
    job_id = str(uuid.uuid4())
    job = MPKJob(job_id)

    with MPK_JOBS_LOCK:
        MPK_JOBS[job_id] = job

    def worker():
        try:
            result = _run_mpk_search(search_term, job=job)
            job.finish(result)
            logger.info("MPK search job %s completed", job_id)
        except Exception as exc:
            logger.exception("Unhandled error in MPK search job %s", job_id)
            job.fail(str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return {"job_id": job_id}


@router.get("/search/status/{job_id}")
def mpk_search_status(job_id: str):
    """Get status of async MPK search job"""
    with MPK_JOBS_LOCK:
        job = MPK_JOBS.get(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Neznáme job_id")

    return job.snapshot()


@router.post("/export")
def export_mpk_matches(payload: ExportRequest):
    """Export MPK matches to XLSX or CSV"""
    if payload.file_format not in ("xlsx", "csv"):
        raise HTTPException(status_code=400, detail="file_format must be 'xlsx' or 'csv'")

    headers = ["Zdroj", "URL zdroja", "Vzor", "Nájdený text", "Kontext", "Strana"]
    rows = [
        [
            m.source,
            m.source_url or "",
            m.pattern_matched,
            m.matched_text,
            m.context,
            m.page_hint if m.page_hint is not None else "",
        ]
        for m in payload.matches
    ]

    if payload.file_format == "csv":
        import csv
        from fastapi.responses import StreamingResponse

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        writer.writerows(rows)
        byte_buf = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
        return StreamingResponse(
            byte_buf,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=mpk_matches.csv"},
        )

    # XLSX
    from fastapi.responses import StreamingResponse

    wb = Workbook()
    ws = wb.active
    ws.title = "MPK Náklaďy NK"
    ws.append(headers)
    for row in rows:
        ws.append(row)
    for col_idx, header in enumerate(headers, start=1):
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = max(
            15, len(header) + 5
        )

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=mpk_matches.xlsx"},
    )
