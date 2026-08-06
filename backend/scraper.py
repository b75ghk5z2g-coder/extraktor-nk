"""
Scraper module for nrsr.sk parliamentary session pages.

IMPORTANT / UNVERIFIED: this environment has no network access to nrsr.sk,
so the link-discovery heuristics below are generic (any <a href> pointing at
a document-like extension). Inspect a live session page's HTML and adjust
DOC_EXTENSIONS / the CSS selector in `extract_document_links` if the real
markup differs (e.g. links wrapped in onclick postbacks instead of plain
hrefs -- see module docstring note on ASP.NET WebForms below).
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("nku_extractor.scraper")

DOC_EXTENSIONS = (".pdf", ".docx", ".doc", ".rtf")

# nrsr.sk's server-rendered, page-by-page document preview. VERIFIED
# 2026-07-22 (live response, DocID=577477): static HTML, not JS-rendered;
# whole document (all pages) in a single response; no separate downloadable
# file/Download.aspx link behind it as far as we've confirmed. Treated as a
# distinct link "kind" because it needs parse_document_preview_html(), not
# the byte-based parse_document() used for actual file attachments.
DOCUMENT_PREVIEW_RE = re.compile(r"/Dynamic/DocumentPreview\.aspx", re.IGNORECASE)

# Individual tlač (parliamentary print / ČPT) detail page, linked from a
# session's program page. VERIFIED 2026-07-22 (live response, 52. schôdza,
# ID=567): 193 distinct links of the form
# Default.aspx?sid=zakony/cpt&ZakZborID=..&CisObdobia=..&ID=<tlac_number>
CPT_LINK_RE = re.compile(r"sid=zakony/cpt", re.IGNORECASE)

# Realistic modern browser UA. Rotate/adjust if the target starts
# fingerprinting beyond simple UA checks (out of scope here).
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

REQUEST_DELAY_SECONDS = 1.5  # polite delay between sequential downloads
MAX_RETRIES = 3
BACKOFF_FACTOR = 1.0  # 0s, 1s, 2s, 4s... between retries

# Free-tier hosting (e.g. Render's free plan) caps the whole process at
# 512 MB of RAM. A schôdza can link ~200 tlače, each fetching a document
# preview that "can be several MB" (see fetch_document_preview's docstring)
# plus BeautifulSoup's own parse-tree overhead on top of that -- with no
# cap, that adds up fast over a long crawl. Skip outlier-sized responses
# (logged, counted as failed) rather than risk an OOM crash mid-run.
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024  # 20 MB


@dataclass
class DiscoveredLink:
    url: str
    label: str
    kind: str = "file"  # "file" = direct downloadable attachment (existing
                         # behavior); "preview" = DocumentPreview.aspx HTML
                         # render, needs different fetch+parse handling.


def is_document_preview_url(url: str) -> bool:
    """True if `url` points at nrsr.sk's DocumentPreview.aspx endpoint."""
    return bool(DOCUMENT_PREVIEW_RE.search(urlparse(url).path))


def is_cpt_detail_url(url: str) -> bool:
    """True if `url` points at a single tlač (ČPT) detail page."""
    return bool(CPT_LINK_RE.search(url))


def extract_cpt_page_links(soup: BeautifulSoup, base_url: str) -> List[str]:
    """
    Finds links to individual tlač (ČPT) detail pages on a schôdza program
    page (e.g. the 193 agenda items found on 52. schôdza, ID=567). Each
    such link needs to be visited separately to find its explanatory
    document -- the program page itself only lists "Spoločná správa" for
    items in second reading, not "Dôvodová správa".
    """
    urls: List[str] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not CPT_LINK_RE.search(href):
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        urls.append(absolute)
    return urls


def extract_cpt_agenda_items(soup: BeautifulSoup, base_url: str) -> List[DiscoveredLink]:
    """
    Same link discovery as extract_cpt_page_links, but also keeps the
    anchor's visible text -- the agenda item's title as printed on the
    program page (e.g. "Správa o výsledkoch kontrolnej činnosti Najvyššieho
    kontrolného úradu Slovenskej republiky za rok 2025 (tlač 1204)").

    This exists for a specific gap: some agenda items (reports FROM NKÚ
    itself, procedural items, etc.) have no "Dôvodová správa"/"Spoločná
    správa" attachment at all -- there is nothing for pick_explanatory_document
    to pick. Previously such items were just marked "failed" even when their
    own title plainly names NKÚ. Callers should pass `.label` through to
    _process_cpt_page's `agenda_title` so it can be checked as a fallback
    when no explanatory document is found.
    """
    items: List[DiscoveredLink] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not CPT_LINK_RE.search(href):
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        label = a.get_text(strip=True)
        items.append(DiscoveredLink(url=absolute, label=label, kind="agenda"))
    return items


# UNVERIFIED: no live access to a tlač detail page's markup to confirm which
# element actually holds the agenda item's own title text (as opposed to the
# title of whichever document happens to be open). Tries the most likely
# candidates in order and falls back to <title>. Adjust the selector list if
# this misses on a real page.
_CPT_TITLE_SELECTORS = ("h1", "h2", ".detail_h1", "#markContent h1")


def extract_cpt_title(soup: BeautifulSoup) -> Optional[str]:
    """
    Best-effort extraction of a tlač (ČPT) detail page's own title/heading,
    for when a ČPT URL is visited directly (not discovered via a program
    page, so there's no agenda item label already in hand -- see
    extract_cpt_agenda_items). Returns None if nothing usable is found.
    """
    for selector in _CPT_TITLE_SELECTORS:
        el = soup.select_one(selector)
        if el:
            text = el.get_text(strip=True)
            if text:
                return text

    if soup.title and soup.title.string:
        text = soup.title.string.strip()
        if text:
            return text

    return None


def extract_cpt_documents(soup: BeautifulSoup, base_url: str) -> List[DiscoveredLink]:
    """
    Parses a tlač (ČPT) detail page's "Dokumenty" section into a list of
    labeled documents. VERIFIED 2026-07-22 (live response, tlač 1077):
    each document appears as a Download.aspx link (raw file, paired with an
    <img alt="Label (Label)">) immediately followed by a DocumentPreview.aspx
    link whose visible text is "Label (size KB)", e.g. "Dôvodová správa (216 KB)".

    We use the DocumentPreview.aspx link (kind="preview") rather than
    Download.aspx, since parse_document_preview_html() is already verified
    working for text extraction and needs no binary file parsing.
    """
    documents: List[DiscoveredLink] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        absolute = urljoin(base_url, href)
        if not DOCUMENT_PREVIEW_RE.search(urlparse(absolute).path):
            continue
        label = a.get_text(strip=True)
        if not label:
            continue
        documents.append(DiscoveredLink(url=absolute, label=label, kind="preview"))
    return documents


def pick_explanatory_document(
    documents: List[DiscoveredLink],
    preferred_labels=("dôvodová správa", "spoločná správa"),
) -> Optional[DiscoveredLink]:
    """
    From a ČPT page's document list, choose which one to analyze.
    Different tlače carry different document sets depending on their
    legislative stage: a first-reading tlač usually has "Dôvodová správa";
    a second-reading tlač usually has "Spoločná správa" instead (and may not
    have "Dôvodová správa" at all). "Dôvodová správa" is preferred when
    present; "Spoločná správa" is used as a fallback. Returns None if
    neither is found on this tlač (not every tlač has an explanatory
    document -- e.g. procedural/organizational agenda items).
    """
    for wanted in preferred_labels:
        for doc in documents:
            if doc.label.lower().startswith(wanted):
                return doc
    return None


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=BACKOFF_FACTOR,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_session_page(session: requests.Session, url: str, timeout: int = 20) -> Optional[BeautifulSoup]:
    """Fetch and parse the given NRSR page. Returns None on hard failure (e.g. 403 after retries)."""
    time.sleep(REQUEST_DELAY_SECONDS)
    try:
        resp = session.get(url, timeout=timeout)
    except requests.RequestException as exc:
        logger.error("Network error fetching %s: %s", url, exc)
        return None

    if resp.status_code == 403:
        logger.error(
            "403 Forbidden fetching %s after %d retries. "
            "The site may require a warmed-up session (visit the homepage first) "
            "or is blocking this IP/UA combination.",
            url, MAX_RETRIES,
        )
        return None

    if not resp.ok:
        logger.error("Unexpected status %s fetching %s", resp.status_code, url)
        return None

    resp.encoding = resp.apparent_encoding or "utf-8"
    return BeautifulSoup(resp.text, "html.parser")


def extract_document_links(soup: BeautifulSoup, base_url: str) -> List[DiscoveredLink]:
    """
    Find all hyperlinks on the page pointing to parliamentary material files.

    NOTE: some NRSR list views paginate/filter via ASP.NET postback
    (__doPostBack calls on <a> tags with javascript: hrefs) rather than
    static hrefs. Those links will NOT be picked up here -- this only
    handles plain <a href="...file.ext"> links, which is the common case
    for individual document attachments on a single session's detail page.
    """
    links: List[DiscoveredLink] = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("javascript:"):
            continue  # postback link we can't follow without a browser engine

        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        path_lower = parsed.path.lower()

        if path_lower.endswith(DOC_EXTENSIONS):
            if absolute in seen:
                continue
            seen.add(absolute)
            label = a.get_text(strip=True) or path_lower.rsplit("/", 1)[-1]
            links.append(DiscoveredLink(url=absolute, label=label, kind="file"))
        elif DOCUMENT_PREVIEW_RE.search(path_lower):
            if absolute in seen:
                continue
            seen.add(absolute)
            label = a.get_text(strip=True) or "document preview"
            links.append(DiscoveredLink(url=absolute, label=label, kind="preview"))

    return links


def download_file(
    session: requests.Session, url: str, timeout: int = 30, max_bytes: int = MAX_DOCUMENT_BYTES
) -> Optional[bytes]:
    """
    Download a single document with a polite delay. Returns raw bytes, or
    None on failure (including "too large" -- streamed and capped at
    max_bytes, see MAX_DOCUMENT_BYTES above for why).
    """
    time.sleep(REQUEST_DELAY_SECONDS)
    try:
        with session.get(url, timeout=timeout, stream=True) as resp:
            if resp.status_code == 403:
                logger.error("403 Forbidden downloading %s -- skipping.", url)
                return None
            if not resp.ok:
                logger.error("Unexpected status %s downloading %s", resp.status_code, url)
                return None

            content_length = resp.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > max_bytes:
                        logger.warning(
                            "Skipping %s: reports %.1f MB, over the %.0f MB cap",
                            url, int(content_length) / 1_000_000, max_bytes / 1_000_000,
                        )
                        return None
                except ValueError:
                    pass

            chunks: List[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    logger.warning(
                        "Skipping %s: exceeded the %.0f MB cap while downloading",
                        url, max_bytes / 1_000_000,
                    )
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except requests.RequestException as exc:
        logger.error("Network error downloading %s: %s", url, exc)
        return None


def fetch_document_preview(
    session: requests.Session, url: str, timeout: int = 60, max_bytes: int = MAX_DOCUMENT_BYTES
) -> Optional[str]:
    """
    Fetch a nrsr.sk DocumentPreview.aspx page. Returns decoded HTML text, or
    None on failure (including "too large" -- streamed and capped at
    max_bytes, see MAX_DOCUMENT_BYTES above). Uses a longer timeout than
    fetch_session_page/download_file because a single response can be
    several MB (a full multi-page document rendered as HTML in one request
    -- see DOCUMENT_PREVIEW_RE docstring).
    """
    time.sleep(REQUEST_DELAY_SECONDS)
    try:
        with session.get(url, timeout=timeout, stream=True) as resp:
            if resp.status_code == 403:
                logger.error("403 Forbidden fetching document preview %s -- skipping.", url)
                return None
            if not resp.ok:
                logger.error(
                    "Unexpected status %s fetching document preview %s", resp.status_code, url
                )
                return None

            content_length = resp.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > max_bytes:
                        logger.warning(
                            "Skipping document preview %s: reports %.1f MB, over the %.0f MB cap",
                            url, int(content_length) / 1_000_000, max_bytes / 1_000_000,
                        )
                        return None
                except ValueError:
                    pass

            chunks: List[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    logger.warning(
                        "Skipping document preview %s: exceeded the %.0f MB cap while downloading",
                        url, max_bytes / 1_000_000,
                    )
                    return None
                chunks.append(chunk)

            raw = b"".join(chunks)
            encoding = resp.encoding or resp.apparent_encoding or "utf-8"
            return raw.decode(encoding, errors="replace")
    except requests.RequestException as exc:
        logger.error("Network error fetching document preview %s: %s", url, exc)
        return None
