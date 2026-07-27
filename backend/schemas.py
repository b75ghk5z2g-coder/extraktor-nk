from typing import List, Optional
from pydantic import BaseModel, HttpUrl


class ScrapeRequest(BaseModel):
    url: HttpUrl


class MatchResult(BaseModel):
    source: str            # filename or document URL
    source_url: Optional[str] = None
    pattern_matched: str    # which regex category fired
    matched_text: str       # the literal matched span
    context: str            # full sentence/paragraph containing the match
    page_hint: Optional[int] = None  # page number for PDFs, None otherwise


class ScrapeResponse(BaseModel):
    document_count: int
    documents_processed: List[str]
    documents_failed: List[str]
    matches: List[MatchResult]


class ExportRequest(BaseModel):
    matches: List[MatchResult]
    file_format: str = "xlsx"  # "xlsx" or "csv"


class SlovLexScanRequest(BaseModel):
    days_back: int = 31


class SlovLexScanResponse(BaseModel):
    materials_checked: int
    materials_with_matches: int
    documents_processed: List[str]
    documents_failed: List[str]
    matches: List[MatchResult]
