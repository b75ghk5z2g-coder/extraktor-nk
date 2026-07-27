import io
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import qrcode
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook

from extractor import find_matches_in_text
from parsers import parse_document, parse_document_preview_html
from scraper import (
    build_session,
    download_file,
    extract_cpt_documents,
    extract_cpt_page_links,
    extract_document_links,
    fetch_document_preview,
    fetch_session_page,
    is_cpt_detail_url,
    is_document_preview_url,
    pick_explanatory_document,
)
from appdata import user_data_dir
from auth import PinAuthMiddleware
from schemas import ExportRequest, MatchResult, ScrapeRequest, ScrapeResponse
from slovlex_routes import router as slovlex_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nku_extractor.main")

app = FastAPI(title="NKU Reference Extractor")
app.add_middleware(PinAuthMiddleware)
app.include_router(slovlex_router)

# NOTE: no CORS middleware here on purpose. The frontend is always served
# by this same backend (mounted at "/" below) -- both when opened locally
# (http://localhost:8000/) and from a tablet over Wi-Fi (http://<lan-ip>:8000/)
# -- so every legitimate request is same-origin and needs no CORS grant.
# A wildcard CORS policy here would let ANY website open in the same
# browser silently call this API (SSRF/CSRF risk) with zero benefit, since
# nothing legitimate actually needs cross-origin access.


# --- Run history -------------------------------------------------------
# Persisted to a JSON file (not just in-memory like ScrapeJob/JOBS below),
# so past runs survive a backend restart -- unlike live job progress, which
# is fine to lose on restart since a finished run's whole point is to be
# looked up again later.
HISTORY_FILE = user_data_dir() / "history.json"
HISTORY_LOCK = threading.Lock()
MAX_HISTORY_ENTRIES = 200  # keep the file bounded over months of use


def _load_history() -> List[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        with HISTORY_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read history file %s: %s", HISTORY_FILE, exc)
        return []


def _save_history(entries: List[dict]) -> None:
    # Write-to-temp-then-rename so a crash mid-write can't leave history.json
    # half-written/corrupted (a corrupted JSON file would otherwise silently
    # wipe out all past history on the next read).
    tmp_path = HISTORY_FILE.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    tmp_path.replace(HISTORY_FILE)


def _append_history(entry: dict) -> None:
    with HISTORY_LOCK:
        entries = _load_history()
        entries.insert(0, entry)  # most recent first
        entries = entries[:MAX_HISTORY_ENTRIES]
        _save_history(entries)


def _history_entry(
    job_id: str,
    url: str,
    started_at: str,
    status: str,
    result: Optional[ScrapeResponse] = None,
    error: Optional[str] = None,
) -> dict:
    entry = {
        "job_id": job_id,
        "url": url,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
    }
    if result is not None:
        entry.update(
            {
                "document_count": result.document_count,
                "processed_count": len(result.documents_processed),
                "failed_count": len(result.documents_failed),
                "matches_count": len(result.matches),
                "matches": [m.dict() for m in result.matches],
            }
        )
    if error is not None:
        entry["error"] = error
    return entry
# -------------------------------------------------------------------------


_ID_PARAM_RE = re.compile(r"[?&]ID=(\d+)", re.IGNORECASE)
_DOCID_PARAM_RE = re.compile(r"[?&]DocID=(\d+)", re.IGNORECASE)

# This tool only ever needs to fetch nrsr.sk pages. Without this check,
# /api/scrape(/start) would happily fetch ANY URL a caller supplies --
# effectively turning this local server into an open SSRF proxy (e.g. a
# malicious webpage open in the same browser, or another device on the
# same Wi-Fi, could ask it to hit internal/local addresses on the user's
# behalf). Since the whole point of this tool is nrsr.sk pages anyway,
# restricting to that domain costs nothing and closes the hole entirely.
_ALLOWED_SCRAPE_HOSTS = {"nrsr.sk", "www.nrsr.sk"}


def _assert_allowed_host(url: str) -> None:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if host not in _ALLOWED_SCRAPE_HOSTS and not host.endswith(".nrsr.sk"):
        raise HTTPException(
            status_code=400,
            detail="Táto appka funguje len s adresami na nrsr.sk.",
        )


def _short_location(url: str) -> str:
    """
    Turns a raw nrsr.sk URL into a short, human-readable "where are we"
    string for the progress display, e.g.
    ".../Default.aspx?sid=zakony/cpt&...&ID=1077" -> "tlač 1077"
    ".../Dynamic/DocumentPreview.aspx?DocID=577477" -> "dokument 577477"
    Falls back to a trailing slice of the URL if neither pattern matches,
    so something is always shown rather than nothing.
    """
    if not url:
        return ""
    match = _ID_PARAM_RE.search(url)
    if match:
        return f"tlač {match.group(1)}"
    match = _DOCID_PARAM_RE.search(url)
    if match:
        return f"dokument {match.group(1)}"
    return url if len(url) <= 60 else "…" + url[-60:]


class ScrapeError(Exception):
    """
    Raised inside _run_scrape() for the same "can't continue" situations that
    used to raise HTTPException directly. Since background-thread jobs have
    no request/response cycle to attach an HTTPException to, this is caught
    by both the synchronous /api/scrape endpoint (re-raised as HTTPException)
    and the background job worker (recorded as job.error).
    """


class ScrapeJob:
    """
    In-memory progress tracker for one long-running /api/scrape/start job.
    NOTE: state lives in process memory only (JOBS dict below) -- restarting
    the backend loses in-flight/finished job records. Fine for this
    single-user internal tool; would need a real store for multi-worker
    deployment.
    """

    def __init__(self, job_id: str):
        self.job_id = job_id
        self._lock = threading.Lock()
        self.status = "running"  # "running" | "done" | "error"
        self.current = 0
        self.total = 0
        self.current_label = ""
        self.start_time = time.time()
        self.result: Optional[ScrapeResponse] = None
        self.error: Optional[str] = None

    def update(self, current: int = None, total: int = None, label: str = None):
        with self._lock:
            if current is not None:
                self.current = current
            if total is not None:
                self.total = total
            if label is not None:
                self.current_label = label

    def finish(self, result: ScrapeResponse):
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

            # Estimate remaining time from the *actual* average pace so far
            # (not a hardcoded assumption about REQUEST_DELAY_SECONDS), so it
            # naturally accounts for slow/failed requests, retries, etc.
            # Only meaningful once at least one item has been processed and
            # we know the total; otherwise there's nothing to extrapolate from.
            estimated_remaining = None
            if self.status == "running" and self.total and self.current > 0:
                avg_seconds_per_item = elapsed / self.current
                estimated_remaining = avg_seconds_per_item * (self.total - self.current)

            data = {
                "job_id": self.job_id,
                "status": self.status,
                "current": self.current,
                "total": self.total,
                "location": _short_location(self.current_label),
                "elapsed_seconds": round(elapsed),
                "estimated_seconds_remaining": (
                    round(estimated_remaining) if estimated_remaining is not None else None
                ),
                "error": self.error,
            }
            if self.status == "done" and self.result is not None:
                data["result"] = self.result.dict()
            return data


JOBS: Dict[str, ScrapeJob] = {}
JOBS_LOCK = threading.Lock()


def _run_extraction(blocks, source_label: str, source_url: str = None) -> List[MatchResult]:
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


def _process_cpt_page(session, cpt_url: str):
    """
    Visits one tlač (ČPT) detail page, picks its explanatory document
    ("Dôvodová správa", falling back to "Spoločná správa"), fetches and
    parses it. Returns (matches, processed_url_or_None, failed_url_or_None).
    Used both for a single pasted ČPT URL and for crawling every ČPT
    linked from a session's program page.
    """
    cpt_soup = fetch_session_page(session, cpt_url)
    if cpt_soup is None:
        return [], None, cpt_url

    documents = extract_cpt_documents(cpt_soup, cpt_url)
    target = pick_explanatory_document(documents)
    if target is None:
        # Not every tlač has an explanatory document (e.g. procedural
        # agenda items) -- this isn't necessarily a failure, just nothing
        # to extract from. Still reported as "failed" so it's visible in
        # the results rather than silently disappearing.
        logger.info("No Dôvodová/Spoločná správa found on %s", cpt_url)
        return [], None, cpt_url

    html = fetch_document_preview(session, target.url)
    if html is None:
        return [], None, target.url

    blocks = parse_document_preview_html(html)
    if not blocks:
        return [], None, target.url

    label = f"{target.label} (tlač: {cpt_url})"
    matches = _run_extraction(blocks, source_label=label, source_url=target.url)
    return matches, target.url, None


def _run_scrape(url: str, session, job: Optional["ScrapeJob"] = None) -> ScrapeResponse:
    """
    Core logic shared by the synchronous /api/scrape endpoint and the
    background-thread job started by /api/scrape/start. Identical to the
    old scrape_session() body, except:
    - HTTPException -> ScrapeError (no request/response cycle in a
      background thread to attach an HTTPException to)
    - `job` (optional) gets .update(current, total, label) calls sprinkled
      through the Case 3 / fallback loops, which is the only thing a caller
      polling /api/scrape/status/{job_id} actually sees change over the
      10-20 minute run.
    """

    def report(current=None, total=None, label=None):
        if job is not None:
            job.update(current=current, total=total, label=label)

    # Case 1: the pasted URL is itself a DocumentPreview.aspx link -- i.e. a
    # single document (e.g. from "strana 58" style references), not a
    # session/listing page to crawl for attachments. Handle it directly and
    # skip the link-discovery flow below entirely.
    if is_document_preview_url(url):
        report(current=0, total=1, label=url)
        html = fetch_document_preview(session, url)
        if html is None:
            raise ScrapeError(
                "Could not fetch the document preview page (blocked, "
                "timed out, or non-200 status). Check the URL/DocID."
            )
        blocks = parse_document_preview_html(html)
        report(current=1)
        if not blocks:
            return ScrapeResponse(
                document_count=1, documents_processed=[], documents_failed=[url], matches=[]
            )
        matches = _run_extraction(blocks, source_label=url, source_url=url)
        return ScrapeResponse(
            document_count=1,
            documents_processed=[url],
            documents_failed=[],
            matches=matches,
        )

    # Case 2: the pasted URL is a single tlač (ČPT) detail page -- pick and
    # analyze its explanatory document.
    if is_cpt_detail_url(url):
        report(current=0, total=1, label=url)
        matches, processed_url, failed_url = _process_cpt_page(session, url)
        report(current=1)
        return ScrapeResponse(
            document_count=1,
            documents_processed=[processed_url] if processed_url else [],
            documents_failed=[failed_url] if failed_url else [],
            matches=matches,
        )

    # Case 3: a session/program page. Discover every ČPT linked from it and
    # crawl each one's explanatory document. NOTE: for a full session
    # (e.g. 52. schôdza had 193 distinct ČPT) this means roughly 2x that
    # many requests to nrsr.sk (one for each ČPT detail page, one for its
    # document) -- see the polite-scraping delay in scraper.py. Confirm
    # this volume of automated requests is acceptable before running this
    # on a schedule or in a loop; nrsr.sk's robots.txt disallows automated
    # crawling, so this should stay a deliberate, occasional, human-triggered
    # action, not something run unattended/repeatedly.
    report(current=0, total=0, label=url)
    soup = fetch_session_page(session, url)
    if soup is None:
        raise ScrapeError(
            "Could not fetch the NRSR page (blocked, timed out, or non-200 "
            "status). Check the URL and try again; if this persists the "
            "site may be rate-limiting this IP."
        )

    cpt_urls = extract_cpt_page_links(soup, url)

    if cpt_urls:
        all_matches: List[MatchResult] = []
        processed: List[str] = []
        failed: List[str] = []

        report(total=len(cpt_urls))
        for idx, cpt_url in enumerate(cpt_urls, start=1):
            report(current=idx, label=cpt_url)
            matches, processed_url, failed_url = _process_cpt_page(session, cpt_url)
            all_matches.extend(matches)
            if processed_url:
                processed.append(processed_url)
            if failed_url:
                failed.append(failed_url)

        return ScrapeResponse(
            document_count=len(cpt_urls),
            documents_processed=processed,
            documents_failed=failed,
            matches=all_matches,
        )

    # Fallback: no ČPT links found on this page -- maybe it's some other
    # kind of listing page with direct file attachments (the original,
    # simpler behavior this endpoint had before ČPT crawling was added).
    links = extract_document_links(soup, url)
    if not links:
        return ScrapeResponse(
            document_count=0, documents_processed=[], documents_failed=[], matches=[]
        )

    all_matches: List[MatchResult] = []
    processed: List[str] = []
    failed: List[str] = []

    report(total=len(links))
    for idx, link in enumerate(links, start=1):
        report(current=idx, label=link.url)
        if link.kind == "preview":
            html = fetch_document_preview(session, link.url)
            if html is None:
                failed.append(link.url)
                continue
            blocks = parse_document_preview_html(html)
            if not blocks:
                failed.append(link.url)
                continue
        else:
            content = download_file(session, link.url)
            if content is None:
                failed.append(link.url)
                continue
            try:
                blocks = parse_document(content, link.url)
            except Exception as exc:
                logger.error("Parse failure for %s: %s", link.url, exc)
                failed.append(link.url)
                continue

        matches = _run_extraction(blocks, source_label=link.label, source_url=link.url)
        all_matches.extend(matches)
        processed.append(link.url)

    return ScrapeResponse(
        document_count=len(links),
        documents_processed=processed,
        documents_failed=failed,
        matches=all_matches,
    )


@app.post("/api/scrape", response_model=ScrapeResponse)
def scrape_session(payload: ScrapeRequest):
    """
    Synchronous, blocking variant -- kept for scripting/testing convenience
    (e.g. curl/Postman). The frontend UI now uses /api/scrape/start +
    /api/scrape/status/{job_id} instead, since this blocks the whole
    10-20 minute run behind a single open HTTP request with no progress
    feedback.
    """
    url = str(payload.url)
    _assert_allowed_host(url)
    session = build_session()
    job_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        result = _run_scrape(url, session)
    except ScrapeError as exc:
        _append_history(_history_entry(job_id, url, started_at, "error", error=str(exc)))
        raise HTTPException(status_code=502, detail=str(exc))
    _append_history(_history_entry(job_id, url, started_at, "done", result=result))
    return result


@app.post("/api/scrape/start")
def start_scrape(payload: ScrapeRequest):
    """
    Kicks off a scrape in a background thread and immediately returns a
    job_id. Poll /api/scrape/status/{job_id} to watch progress and fetch
    the final result once status is "done".
    """
    url = str(payload.url)
    _assert_allowed_host(url)
    job_id = str(uuid.uuid4())
    job = ScrapeJob(job_id)
    with JOBS_LOCK:
        JOBS[job_id] = job

    def worker():
        session = build_session()
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            result = _run_scrape(url, session, job=job)
            job.finish(result)
            _append_history(_history_entry(job_id, url, started_at, "done", result=result))
        except ScrapeError as exc:
            job.fail(str(exc))
            _append_history(_history_entry(job_id, url, started_at, "error", error=str(exc)))
        except Exception as exc:  # noqa: BLE001 - surface any unexpected failure to the UI
            logger.exception("Unhandled error in scrape job %s", job_id)
            job.fail(f"Neočakávaná chyba: {exc}")
            _append_history(_history_entry(job_id, url, started_at, "error", error=str(exc)))

    threading.Thread(target=worker, daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/scrape/status/{job_id}")
def scrape_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Neznáme job_id (možno už bol backend reštartovaný).")
    return job.snapshot()


@app.get("/api/history")
def get_history():
    """
    Past runs (both /api/scrape and /api/scrape/start), most recent first.
    Each entry with status "done" includes its full matches list, so past
    results can be re-viewed/re-exported without re-running the scrape.
    """
    return _load_history()


@app.delete("/api/history")
def clear_history():
    with HISTORY_LOCK:
        _save_history([])
    return {"status": "ok"}


@app.post("/api/upload", response_model=ScrapeResponse)
async def upload_documents(files: List[UploadFile] = File(...)):
    all_matches: List[MatchResult] = []
    processed: List[str] = []
    failed: List[str] = []

    for f in files:
        content = await f.read()
        try:
            blocks = parse_document(content, f.filename)
        except Exception as exc:
            logger.error("Parse failure for %s: %s", f.filename, exc)
            failed.append(f.filename)
            continue

        if not blocks:
            failed.append(f.filename)
            continue

        matches = _run_extraction(blocks, source_label=f.filename)
        all_matches.extend(matches)
        processed.append(f.filename)

    return ScrapeResponse(
        document_count=len(files),
        documents_processed=processed,
        documents_failed=failed,
        matches=all_matches,
    )


@app.post("/api/export")
def export_matches(payload: ExportRequest):
    if payload.file_format not in ("xlsx", "csv"):
        raise HTTPException(status_code=400, detail="file_format must be 'xlsx' or 'csv'")

    headers = ["Source", "Source URL", "Pattern", "Matched Text", "Context", "Page"]
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

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        writer.writerows(rows)
        byte_buf = io.BytesIO(buf.getvalue().encode("utf-8-sig"))  # BOM for Excel/Slovak diacritics
        return StreamingResponse(
            byte_buf,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=nku_matches.csv"},
        )

    wb = Workbook()
    ws = wb.active
    ws.title = "NKU Matches"
    ws.append(headers)
    for row in rows:
        ws.append(row)
    for col_idx, header in enumerate(headers, start=1):
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = max(15, len(header) + 5)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=nku_matches.xlsx"},
    )


@app.get("/api/health")
def health():
    return {"status": "ok"}


def _get_lan_ip() -> Optional[str]:
    """Zisti lokalnu IP adresu tohto pocitaca vo Wi-Fi/LAN sieti.

    Trik: UDP "connect" nic realne neposiela po sieti (nevyzaduje
    internetove pripojenie), len necha operacny system vybrat, ktorym
    sietovym rozhranim by sa taka komunikacia posielala - a z toho
    zistime nasu lokalnu IP adresu.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


@app.get("/api/tablet-info")
def tablet_info(request_port: int = 8000):
    # This feature is for connecting a tablet over the SAME local Wi-Fi as
    # this computer. When hosted (NKU_PIN set), the appka isn't running on
    # anyone's local network -- everyone just uses the public URL directly
    # -- so this would otherwise show a meaningless internal server address.
    if os.environ.get("NKU_PIN", "").strip():
        return {"available": False, "url": None}
    ip = _get_lan_ip()
    if not ip:
        return {"available": False, "url": None}
    return {"available": True, "url": f"http://{ip}:{request_port}"}


@app.get("/api/tablet-qr.png")
def tablet_qr(request_port: int = 8000):
    if os.environ.get("NKU_PIN", "").strip():
        raise HTTPException(status_code=404, detail="Nedostupné v hostovanom režime.")
    ip = _get_lan_ip()
    if not ip:
        raise HTTPException(status_code=503, detail="Nepodarilo sa zistit lokalnu IP adresu.")
    url = f"http://{ip}:{request_port}"
    img = qrcode.make(url, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


# --- Servovanie frontendu -------------------------------------------------
# Umoznuje otvorit appku aj z ineho zariadenia v tej istej Wi-Fi sieti
# (napr. tablet), nielen dvojklikom na frontend/index.html na tomto PC.
# Registrovane AZ TU, na konci suboru, po vsetkych /api/... routach,
# aby "/" mount neprebral prioritu pred nimi.
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

