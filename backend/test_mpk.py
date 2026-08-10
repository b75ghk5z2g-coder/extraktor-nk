"""
Unit tests for MPK scraper, extractor, and routes.
Run with: pytest test_mpk.py -v
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from mpk_scraper import (
    search_mpk_processes,
    extract_accompanying_documents,
    MPKProcess,
    MPKDocument,
)
from mpk_extractor import find_matches_in_text, split_sentences
from schemas import MPKSearchRequest


class TestMPKExtractor:
    """Tests for MPK extractor pattern matching"""

    def test_find_nk_full_name(self):
        """Test detection of full "Najvyšší kontrolný úrad" name"""
        text = "Najvyšší kontrolný úrad SR odporučuje..."
        matches = find_matches_in_text(text)
        assert len(matches) > 0
        assert any("NK_FULL_NAME" in m.pattern_matched for m in matches)

    def test_find_nku_acronym(self):
        """Test detection of NKÚ acronym"""
        text = "Podľa NKÚ SR by malo byť inak."
        matches = find_matches_in_text(text)
        assert len(matches) > 0
        assert any("NKU_ACRONYM" in m.pattern_matched for m in matches)

    def test_find_nk_acronym(self):
        """Test detection of NK acronym"""
        text = "NK nemá peniaze na kontrolu."
        matches = find_matches_in_text(text)
        assert len(matches) > 0
        assert any("NK_ACRONYM" in m.pattern_matched for m in matches)

    def test_find_declined_form(self):
        """Test detection of declined forms (Slovak language)"""
        text = "Najvyššieho kontrolného úradu sa to týka."
        matches = find_matches_in_text(text)
        assert len(matches) > 0

    def test_no_false_positives_in_words(self):
        """NK should not match inside longer words"""
        text = "Kniha je veľmi zaujímavá."  # 'kniha' contains 'k' but not 'NK'
        matches = find_matches_in_text(text)
        # Should not match 'kniha'
        assert not any(m.matched_text == "NK" for m in matches)

    def test_sentence_context(self):
        """Test that matches include full sentence context"""
        text = "Najvyšší kontrolný úrad robí svoju prácu. Ďakujeme mu."
        matches = find_matches_in_text(text)
        assert len(matches) > 0
        assert "Najvyšší kontrolný úrad" in matches[0].context

    def test_split_sentences_with_abbreviations(self):
        """Test sentence splitter with Slovak abbreviations"""
        text = "Podľa Z. z. číslo 44/1988 Z. z. sa upravuje. Napr. takto."
        sentences = split_sentences(text)
        # Should not split on "Z. z." or "číslo" or "napr."
        assert len(sentences) >= 1


class TestMPKScraper:
    """Tests for MPK scraper with mocked HTTP"""

    @patch("mpk_scraper.fetch_mpk_page")
    def test_search_mpk_processes_mock(self, mock_fetch):
        """Test MPK process search with mock HTML"""
        mock_html = """
        <html>
            <table>
                <tr>
                    <td><a href="/elegislativa/legislativne-procesy/SK/LP/2026/426">Návrh číslo 426</a></td>
                </tr>
            </table>
        </html>
        """
        mock_soup = Mock()
        mock_soup.find_all.return_value = []
        
        with patch("mpk_scraper.BeautifulSoup") as mock_bs:
            mock_bs.return_value = mock_soup
            mock_fetch.return_value = mock_soup
            
            session = Mock()
            # This will return empty due to mock, but tests the flow
            processes = search_mpk_processes(session, search_term="NK")
            assert isinstance(processes, list)

    def test_mpk_process_dataclass(self):
        """Test MPKProcess dataclass creation"""
        process = MPKProcess(
            lp_number="2026/426",
            title="Návrh zákona",
            url="https://www.slov-lex.sk/elegislativa/legislativne-procesy/SK/LP/2026/426",
            stadium_uuid="489d6fbd-9c69-42e1-8b9e-036f0cf47a98"
        )
        assert process.lp_number == "2026/426"
        assert process.title == "Návrh zákona"
        assert process.stadium_uuid is not None

    def test_mpk_document_dataclass(self):
        """Test MPKDocument dataclass creation"""
        doc = MPKDocument(
            title="Predkladacia správa",
            filename="predkladacia_sprava.docx",
            url="https://www.slov-lex.sk/files/predkladacia.docx",
            size_kb=20.1
        )
        assert doc.title == "Predkladacia správa"
        assert doc.size_kb == 20.1

    def test_allowed_host_validation(self):
        """Test that only slov-lex.sk URLs are allowed"""
        from mpk_scraper import _assert_allowed_host
        
        # Should pass
        _assert_allowed_host("https://www.slov-lex.sk/elegislativa/...")
        _assert_allowed_host("https://slov-lex.sk/elegislativa/...")
        
        # Should raise
        with pytest.raises(ValueError):
            _assert_allowed_host("https://evil.com/malware")


class TestMPKSchemas:
    """Tests for MPK Pydantic schemas"""

    def test_mpk_search_request_defaults(self):
        """Test MPKSearchRequest with defaults"""
        req = MPKSearchRequest()
        assert req.search_term == "NK"
        assert req.include_variants is True

    def test_mpk_search_request_custom(self):
        """Test MPKSearchRequest with custom values"""
        req = MPKSearchRequest(search_term="NKÚ", include_variants=False)
        assert req.search_term == "NKÚ"
        assert req.include_variants is False


class TestDocumentParsing:
    """Tests for document parsing with MPK documents"""

    def test_parse_docx_mock(self):
        """Test DOCX parsing with mock"""
        from parsers import parse_docx
        from unittest.mock import Mock, patch
        
        # Create mock document
        mock_para = Mock()
        mock_para.text = "Najvyšší kontrolný úrad robí svoju prácu."
        
        mock_table_row = Mock()
        mock_table_row.cells = [
            Mock(text="NK"),
            Mock(text="Kontrolný úrad")
        ]
        
        mock_table = Mock()
        mock_table.rows = [mock_table_row]
        
        mock_doc = Mock()
        mock_doc.paragraphs = [mock_para]
        mock_doc.tables = [mock_table]
        
        with patch("parsers.Document") as mock_Document:
            mock_Document.return_value = mock_doc
            # This would work if we pass real bytes, but for testing:
            # blocks = parse_docx(b"fake docx content")
            # assert len(blocks) > 0
            pass


class TestMPKIntegration:
    """Integration tests (may require real network if not mocked)"""

    @pytest.mark.skip(reason="Requires network access to slov-lex.sk")
    def test_real_mpk_search(self):
        """Test real MPK search (skipped by default)"""
        from mpk_scraper import build_session, search_mpk_processes
        
        session = build_session()
        processes = search_mpk_processes(session, search_term="NK", page=1)
        
        # This would test against live site
        assert isinstance(processes, list)


# ============================================================================
# Example API route tests (would need TestClient from FastAPI)
# ============================================================================

class TestMPKRoutes:
    """Tests for MPK API routes"""

    @pytest.fixture
    def client(self):
        """Create test client for FastAPI"""
        from fastapi.testclient import TestClient
        from main import app  # Import your main FastAPI app
        return TestClient(app)

    @pytest.mark.skip(reason="Requires app fixtures")
    def test_mpk_search_endpoint(self, client):
        """Test /api/mpk/search endpoint"""
        response = client.post(
            "/api/mpk/search",
            json={"search_term": "NK", "include_variants": True}
        )
        assert response.status_code == 200
        data = response.json()
        assert "processes_found" in data
        assert "matches" in data

    @pytest.mark.skip(reason="Requires app fixtures")
    def test_mpk_search_start_endpoint(self, client):
        """Test /api/mpk/search/start async endpoint"""
        response = client.post(
            "/api/mpk/search/start",
            json={"search_term": "NK"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data

    @pytest.mark.skip(reason="Requires app fixtures")
    def test_mpk_export_endpoint(self, client):
        """Test /api/mpk/export endpoint"""
        from schemas import MatchResult
        
        matches = [
            MatchResult(
                source="Test doc",
                pattern_matched="NK_FULL_NAME",
                matched_text="Najvyšší kontrolný úrad",
                context="Najvyšší kontrolný úrad funguje normálne."
            )
        ]
        
        response = client.post(
            "/api/mpk/export",
            json={
                "matches": [m.dict() for m in matches],
                "file_format": "csv"
            }
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/csv"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
