"""
Fetches "Predbežné informácie" (preliminary legislative info) listings and
their accompanying documents from the Slov-Lex e-Legislatíva module, via its
underlying JSON API.

IMPORTANT / HOW THIS WAS FOUND: www.slov-lex.sk/elegislativa/* is a
JavaScript (Angular) single-page app -- a plain HTTP GET to any of its pages
returns only a "please enable JavaScript" shell, no content. There is no
server-rendered fallback for this module (unlike e-Zbierka, which has one at
static.slov-lex.sk). The endpoints below were found by inspecting the
browser's own Network tab while using the real site, on 2026-07-24, and were
individually re-verified by hand (see each function's docstring) -- except
where noted as UNVERIFIED, because this environment has no network access to
api-gateway.slov-lex.sk to test POST requests directly.

All three endpoints work fully anonymously: the real frontend itself sends
literal "Authorization: Bearer null" / "OrganizationId: null" headers (not
a real token -- the site has no login for this data), so no credentials are
needed or stored here.

This is a *separate* module from scraper.py (which targets nrsr.sk) -- the
appka now has two independent scraper backends for two unrelated sites, each
with its own quirks. They share extractor.py (regex matching) and parsers.py
(PDF/DOCX/RTF text extraction) since that logic is generic to "text blocks
in, matches out" and doesn't care which site the bytes came from.

Like nrsr.sk, this should stay a deliberate, occasional, human-triggered
action -- not something scheduled to run unattended in a tight loop. The API
is undocumented and could change without notice; if requests start failing,
that's the most likely reason (re-check the Network tab for a changed
endpoint shape rather than assuming a transient network issue).
"""

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterator, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("nku_extractor.slovlex_scraper")

API_BASE = "https://api-gateway.slov-lex.sk"
FILTER_URL = f"{API_BASE}/internal/elegislativa/legislativne-materialy/filter"
DOCUMENTS_URL_TMPL = (
    f"{API_BASE}/external/evidencna-aplikacia/legislativne-materialy/{{uuid}}/sprievodne-dokumenty"
)
DOWNLOAD_URL_TMPL = f"{API_BASE}/external/evidencna-aplikacia/sprievodne-dokumenty/{{uuid}}/download"

# Public-facing page for a material, for humans clicking through from a
# match in the results table (not used for any API call).
MATERIAL_PAGE_URL_TMPL = "https://www.slov-lex.sk/elegislativa-fe/legislativne-procesy/SK/{cislo}"

# Mirrors exactly what the real Angular frontend sends (captured from a live
# browser session on 2026-07-24) -- deliberately NOT a generic/randomised UA,
# since matching the real client as closely as possible is the whole reason
# this works anonymously without a 403.
DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
    "Authorization": "Bearer null",
    "OrganizationId": "null",
    "Origin": "https://www.slov-lex.sk",
    "Referer": "https://www.slov-lex.sk/",
    "Connection": "keep-alive",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}

REQUEST_DELAY_SECONDS = 1.0  # polite delay between sequential requests
PAGE_SIZE = 50
MAX_PAGES_SAFETY_CAP = 500  # ~25000 records -- hard stop against a runaway loop
MAX_RETRIES = 3
BACKOFF_FACTOR = 1.0


@dataclass
class MaterialRef:
    uuid: str
    cislo: str  # e.g. "PI/2026/176"
    nazov: str  # title
    zaciatok_stadia: Optional[date]
    url: str  # public slov-lex.sk page, for humans


@dataclass
class DokumentRef:
    uuid: str
    nazov: str  # filename, e.g. "predbezna informacia_....docx"
    velkost: Optional[int]


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=BACKOFF_FACTOR,
        status_forcelist=[429, 500, 502, 503, 504],
        # Unlike scraper.py, this module also retries POST -- the /filter
        # call is a read-only search despite the verb, so retrying it is safe.
        allowed_methods=["GET", "HEAD", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        logger.warning("Unexpected date format from Slov-Lex API: %r", value)
        return None


def fetch_materials_page(
    session: requests.Session, page: int, size: int = PAGE_SIZE, timeout: int = 30
) -> Optional[dict]:
    """
    One page of the Predbežné informácie listing, newest first.

    VERIFIED 2026-07-24 (page=1, size=10) against a live response -- the
    request body shape below (only typKod filled in, everything else empty/
    null) was copied from the real frontend's own request and is known-good
    for page 1. Behavior for page > 1 is UNVERIFIED (assumed to follow the
    same {totalElements, number, size, legMaterialList} shape based on
    standard Spring Pageable conventions, but not confirmed against a real
    response) -- watch the logs on the first real run.
    """
    time.sleep(REQUEST_DELAY_SECONDS)
    body = {
        "stadium": None,
        "hladanyVyraz": "",
        "cisloLegislativnehoMaterialu": "",
        "rezortneCislo": "",
        "typKod": "PredbeznaInfo",
        "oblastKodList": [],
        "nazov": "",
        "rocnik": "",
        "datumZmenyOd": None,
        "datumZmenyDo": None,
    }
    params = {"page": page, "size": size, "sortBy": "VYJADRENIA", "sortDirection": "DESC"}
    try:
        resp = session.post(FILTER_URL, params=params, json=body, timeout=timeout)
    except requests.RequestException as exc:
        logger.error("Network error fetching Slov-Lex materials page %d: %s", page, exc)
        return None

    if not resp.ok:
        logger.error(
            "Unexpected status %s fetching Slov-Lex materials page %d. Body: %.500s",
            resp.status_code, page, resp.text,
        )
        return None

    try:
        return resp.json()
    except ValueError:
        logger.error("Non-JSON response fetching Slov-Lex materials page %d", page)
        return None


def iterate_recent_materials(
    session: requests.Session, cutoff: date, page_size: int = PAGE_SIZE
) -> Iterator[MaterialRef]:
    """
    Yields every Predbežná informácia material with zaciatokStadia >= cutoff,
    newest first, paginating until either an older-than-cutoff record is
    seen or the listing runs out.

    Relies on the listing being sorted newest-first (confirmed against the
    one live sample we have -- dates were non-increasing across the page).
    If Slov-Lex ever changes the default sort, this would need
    datumZmenyOd/datumZmenyDo in the request body instead (present in the
    API but UNVERIFIED -- not used here to avoid depending on unconfirmed
    behavior).
    """
    page = 1
    while page <= MAX_PAGES_SAFETY_CAP:
        data = fetch_materials_page(session, page, size=page_size)
        if data is None:
            logger.error(
                "Stopping Slov-Lex listing walk at page %d after a fetch failure -- "
                "results collected so far are still returned, this is not a full run.",
                page,
            )
            return

        items = data.get("legMaterialList") or []
        if not items:
            return

        hit_older_than_cutoff = False
        for item in items:
            zaciatok = _parse_date(item.get("zaciatokStadia"))
            if zaciatok is not None and zaciatok < cutoff:
                hit_older_than_cutoff = True
                break
            cislo = item.get("cisloLegislativnehoMaterialu", "")
            yield MaterialRef(
                uuid=item["uuid"],
                cislo=cislo,
                nazov=item.get("nazov", ""),
                zaciatok_stadia=zaciatok,
                url=MATERIAL_PAGE_URL_TMPL.format(cislo=cislo),
            )

        if hit_older_than_cutoff:
            return

        total_elements = data.get("totalElements")
        if total_elements is not None and page * page_size >= total_elements:
            return

        page += 1


def fetch_dokumenty(
    session: requests.Session, material_uuid: str, timeout: int = 30
) -> List[DokumentRef]:
    """
    List of accompanying documents for one material. VERIFIED 2026-07-24 --
    confirmed the stadiumUuid query param used by the real frontend is not
    actually required; calling with just the material uuid returns the same
    result.
    """
    time.sleep(REQUEST_DELAY_SECONDS)
    url = DOCUMENTS_URL_TMPL.format(uuid=material_uuid)
    try:
        resp = session.get(url, timeout=timeout)
    except requests.RequestException as exc:
        logger.error("Network error fetching documents for %s: %s", material_uuid, exc)
        return []

    if not resp.ok:
        logger.error(
            "Unexpected status %s fetching documents for %s", resp.status_code, material_uuid
        )
        return []

    try:
        items = resp.json()
    except ValueError:
        logger.error("Non-JSON response fetching documents for %s", material_uuid)
        return []

    return [
        DokumentRef(uuid=d["uuid"], nazov=d.get("nazov", ""), velkost=d.get("velkost"))
        for d in items
        if "uuid" in d
    ]


def download_dokument(
    session: requests.Session, doc_uuid: str, timeout: int = 30
) -> Optional[bytes]:
    """Download one accompanying document's raw bytes. VERIFIED 2026-07-24."""
    time.sleep(REQUEST_DELAY_SECONDS)
    url = DOWNLOAD_URL_TMPL.format(uuid=doc_uuid)
    try:
        resp = session.get(url, timeout=timeout)
    except requests.RequestException as exc:
        logger.error("Network error downloading document %s: %s", doc_uuid, exc)
        return None

    if not resp.ok:
        logger.error("Unexpected status %s downloading document %s", resp.status_code, doc_uuid)
        return None

    return resp.content
