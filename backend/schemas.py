from typing import List, Optional
from pydantic import BaseModel, HttpUrl


# === Original schemas (kept for compatibility) ===
class ScrapeRequest(BaseModel):
    url: HttpUrl


class MatchResult(BaseModel):
    source: str
    source_url: Optional[str] = None
    pattern_matched: str
    matched_text: str
    context: str
    page_hint: Optional[int] = None


class ScrapeResponse(BaseModel):
    document_count: int
    documents_processed: List[str]
    documents_failed: List[str]
    matches: List[MatchResult]


class ExportRequest(BaseModel):
    matches: List[MatchResult]
    file_format: str = "xlsx"


class SlovLexScanRequest(BaseModel):
    days_back: int = 31


class SlovLexScanResponse(BaseModel):
    materials_checked: int
    materials_with_matches: int
    documents_processed: List[str]
    documents_failed: List[str]
    matches: List[MatchResult]


# === MPK-specific schemas ===
class MPKProcessResult(BaseModel):
    """Single MPK legislative process"""
    lp_number: str  # e.g., "2026/426"
    title: str
    url: str
    stadium_uuid: Optional[str] = None
    has_nk_mention: bool = False


class MPKDocumentInfo(BaseModel):
    """Document in MPK process"""
    title: str
    filename: str
    url: str
    size_kb: Optional[float] = None


class MPKSearchRequest(BaseModel):
    search_term: str = "NK"  # Default search term
    include_variants: bool = True  # Include NKÚ, Najvyšší kontrolný úrad


class MPKSearchResponse(BaseModel):
    """Response from MPK search"""
    processes_found: int
    processes_with_matches: int
    documents_processed: List[str]
    documents_failed: List[str]
    matches: List[MatchResult]


class MPKJobStatus(BaseModel):
    """Status of async MPK scrape job"""
    job_id: str
    status: str  # "running" | "done" | "error"
    current: int
    total: int
    location: str
    elapsed_seconds: int
    estimated_seconds_remaining: Optional[int] = None
    error: Optional[str] = None
    result: Optional[MPKSearchResponse] = None
