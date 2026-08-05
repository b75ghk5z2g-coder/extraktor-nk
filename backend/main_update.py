# main.py (Úprava logiky pre fallback na názov bodu programu)
import re

NKU_TITLE_REGEX = re.compile(
    r"\bNKÚ\b|Najvyšší\s+kontrolný\s+úrad|Najvyššieho\s+kontrolného\s+úradu|kontrolnej\s+činnosti\s+NKÚ",
    re.IGNORECASE,
)

def process_agenda_item(item: dict) -> dict:
    """Spracuje bod programu. Ak chýba dokument, hľadá zhodu v názve (napr. tlač 1204)."""
    title = item.get("title", "")
    url = item.get("url", "")
    documents = item.get("documents", [])

    # 1. Pokus o spracovanie priložených dokumentov (ak existujú)
    if documents:
        # Pôvodná logika spracovania dokumentov (nahradiť vašou funkciou)
        # doc_matches = process_documents(documents)
        doc_matches = [] # Placeholder
        if doc_matches:
            return {
                "status": "SUCCESS",
                "source": "DOCUMENT",
                "title": title,
                "url": url,
                "details": doc_matches,
            }

    # 2. Fallback: Ak dokumenty neexistujú, skontroluje sa názov bodu programu
    if NKU_TITLE_REGEX.search(title):
        return {
            "status": "SUCCESS",
            "source": "PAGE_TITLE",
            "title": title,
            "url": url,
            "note": "Bod programu nemá samostatnú dôvodovú správu, zhoda zachytená z názvu na NRSR.",
        }

    # 3. Ak nie sú dokumenty ani zmienka v názve -> Korektný stav "Bez zhody" (nie zlyhanie)
    return {"status": "NO_MATCH", "source": "NONE", "title": title, "url": url}
