"""
Scraper module for slov-lex.sk MPK (medzi-rezortné pripomienkové konanie) pages.

Workflow:
1. Search MPK processes: https://www.slov-lex.sk/elegislativa/legislativne-procesy/?stadium=MPK
2. Filter by keyword (NK, NKÚ, etc.)
3. Get process detail: /SK/LP/YYYY/NNN
4. Extract accompanying documents: /sprievodne-dokumenty?stadiumUuid=...
5. Download DOCX/PDF files
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("mpk_extractor.scraper")

# Supported document formats in MPK
DOC_EXTENSIONS = (".pdf", ".docx", ".doc", ".rtf")

# MPK allowed hosts
_ALLOWED_MPK_HOSTS = {"slov-lex.sk", "www.slov-lex.sk"}

# Polite scraping delays
REQUEST_DELAY_SECONDS = 1.5
MAX_RETRIES = 3
BACKOFF_FACTOR = 1.0

# Memory cap for large documents
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024  # 20 MB

# Default headers
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


@dataclass
class MPKProcess:
    """Represents one legislative process in MPK"""
    lp_number: str  # e.g., "2026/426"
    title: str
    url: str
    stadium_uuid: Optional[str] = None


@dataclass
class MPKDocument:
    """Represents one accompanying document"""
    title: str
    filename: str
    url: str
    size_kb: Optional[float] = None
    kind: str = "file"  # "file" or "preview"


def _assert_allowed_host(url: str) -> None:
    """Ensure URL is from slov-lex.sk to prevent SSRF attacks"""
    from urllib.parse import urlparse
    
    host = (urlparse(url).hostname or "").lower()
    if host not in _ALLOWED_MPK_HOSTS and not host.endswith(".slov-lex.sk"):
        raise ValueError(
            "Táto appka funguje len s adresami na slov-lex.sk."
        )


def build_session() -> requests.Session:
    """Build a requests session with retry strategy"""
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


def fetch_mpk_page(session: requests.Session, url: str, timeout: int = 20) -> Optional[BeautifulSoup]:
    """Fetch and parse MPK page. Returns None on failure."""
    _assert_allowed_host(url)
    time.sleep(REQUEST_DELAY_SECONDS)
    
    try:
        resp = session.get(url, timeout=timeout)
    except requests.RequestException as exc:
        logger.error("Network error fetching %s: %s", url, exc)
        return None

    if resp.status_code == 403:
        logger.error("403 Forbidden fetching %s", url)
        return None

    if not resp.ok:
        logger.error("Unexpected status %s fetching %s", resp.status_code, url)
        return None

    resp.encoding = resp.apparent_encoding or "utf-8"
    return BeautifulSoup(resp.text, "html.parser")


def search_mpk_processes(
    session: requests.Session,
    search_term: Optional[str] = None,
    page: int = 1
) -> List[MPKProcess]:
    """
    Search MPK processes. Returns list of processes.
    
    Args:
        session: requests.Session
        search_term: search term (e.g., "NK", "NKÚ")
        page: page number (1-based)
    
    Returns:
        List of MPKProcess objects
    """
    base_url = "https://www.slov-lex.sk/elegislativa/legislativne-procesy/"
    params = {
        "stadium": "MPK",
        "page": page,
    }
    if search_term:
        params["hladanyVyraz"] = search_term

    url = base_url + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    logger.info("Searching MPK: %s", url)
    
    soup = fetch_mpk_page(session, url)
    if soup is None:
        return []

    processes = []
    # Find process rows in the table
    # Note: selector may need adjustment based on actual HTML structure
    for row in soup.find_all("tr", class_=re.compile("process|item")):
        try:
            # Extract LP number and title
            link = row.find("a", href=re.compile(r"/elegislativa/legislativne-procesy/SK/LP/"))
            if not link:
                continue
            
            href = link.get("href", "")
            title = link.get_text(strip=True)
            
            # Extract LP number from URL (e.g., SK/LP/2026/426)
            match = re.search(r"/LP/(\d+/\d+)", href)
            lp_number = match.group(1) if match else ""
            
            full_url = urljoin(base_url, href)
            
            processes.append(MPKProcess(
                lp_number=lp_number,
                title=title,
                url=full_url
            ))
        except Exception as exc:
            logger.warning("Failed to parse MPK row: %s", exc)
            continue

    return processes


def get_stadium_uuid_from_process(session: requests.Session, process_url: str) -> Optional[str]:
    """Extract stadium UUID from process detail page"""
    soup = fetch_mpk_page(session, process_url)
    if soup is None:
        return None

    # Look for stadium UUID in data attributes or links
    for link in soup.find_all("a", href=re.compile(r"stadiumUuid=")):
        match = re.search(r"stadiumUuid=([a-f0-9\-]+)", link.get("href", ""))
        if match:
            return match.group(1)
    
    return None


def extract_accompanying_documents(
    session: requests.Session,
    process_url: str,
    stadium_uuid: str
) -> List[MPKDocument]:
    """
    Extract accompanying documents from process detail page.
    
    Args:
        session: requests.Session
        process_url: URL of the process detail page
        stadium_uuid: stadium UUID for the specific stage
    
    Returns:
        List of MPKDocument objects
    """
    # Build URL to accompanying documents section
    doc_url = process_url.replace("/legislativne-procesy/", "/legislativne-procesy/") + \
              f"/sprievodne-dokumenty?stadiumUuid={stadium_uuid}"
    
    logger.info("Extracting documents from: %s", doc_url)
    soup = fetch_mpk_page(session, doc_url)
    if soup is None:
        return []

    documents = []
    seen_urls = set()

    # Find download links for documents
    for row in soup.find_all("tr"):
        try:
            # Extract document info from row
            title_cell = row.find("td")
            if not title_cell:
                continue
            
            title = title_cell.get_text(strip=True)
            
            # Find download link
            link = row.find("a", href=re.compile(r"\.(pdf|docx|doc|rtf)$", re.IGNORECASE))
            if not link:
                continue
            
            href = link.get("href", "")
            full_url = urljoin(doc_url, href)
            
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            
            # Extract filename
            filename = href.rsplit("/", 1)[-1] if "/" in href else href
            
            # Try to extract file size
            size_text = row.get_text()
            size_match = re.search(r"([\d.]+)\s*([KMG]B)", size_text, re.IGNORECASE)
            size_kb = None
            if size_match:
                size_val = float(size_match.group(1))
                unit = size_match.group(2).upper()
                if unit == "KB":
                    size_kb = size_val
                elif unit == "MB":
                    size_kb = size_val * 1024
                elif unit == "GB":
                    size_kb = size_val * 1024 * 1024
            
            documents.append(MPKDocument(
                title=title,
                filename=filename,
                url=full_url,
                size_kb=size_kb,
                kind="file"
            ))
        except Exception as exc:
            logger.warning("Failed to parse document row: %s", exc)
            continue

    return documents


def download_file(
    session: requests.Session,
    url: str,
    timeout: int = 30,
    max_bytes: int = MAX_DOCUMENT_BYTES
) -> Optional[bytes]:
    """
    Download a single document. Returns raw bytes, or None on failure.
    """
    _assert_allowed_host(url)
    time.sleep(REQUEST_DELAY_SECONDS)
    
    try:
        with session.get(url, timeout=timeout, stream=True) as resp:
            if resp.status_code == 403:
                logger.error("403 Forbidden downloading %s", url)
                return None
            if not resp.ok:
                logger.error("Unexpected status %s downloading %s", resp.status_code, url)
                return None

            # Check content length
            content_length = resp.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > max_bytes:
                        logger.warning(
                            "Skipping %s: reports %.1f MB, over the %.0f MB cap",
                            url,
                            int(content_length) / 1_000_000,
                            max_bytes / 1_000_000,
                        )
                        return None
                except ValueError:
                    pass

            # Stream download with size cap
            chunks: List[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    logger.warning(
                        "Skipping %s: exceeded the %.0f MB cap while downloading",
                        url,
                        max_bytes / 1_000_000,
                    )
                    return None
                chunks.append(chunk)
            
            return b"".join(chunks)
    except requests.RequestException as exc:
        logger.error("Network error downloading %s: %s", url, exc)
        return None
