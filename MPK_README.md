# MPK Extraktor - Medzi-rezortné pripomienkové konanie

Extraktor údajov o spomienkach na **Najvyšší kontrolný úrad SR (NK/NKÚ)** z **MPK portálu** na **slov-lex.sk**.

## 📋 Popis

Tento projekt automatizuje:
- 🔍 **Vyhľadávanie** legislatívnych procesov v MPK podľa kľúčových slov (NK, NKÚ, Najvyšší kontrolný úrad)
- 📥 **Stiahnutie** sprievodných dokumentov (DOCX, PDF, RTF)
- 🔎 **Extrahovanie** textov spomínajúcich NK v súboroch
- 📊 **Štruktúrovanie** výsledkov v JSON/XLSX/CSV formáte

## 🚀 Rýchly start

### Inštalácia

```bash
# Klonuj repo a prepni sa na MPK branch
git clone https://github.com/b75ghk5z2g-coder/extraktor-nk.git
cd extraktor-nk
git checkout mpk-adapter

# Inštaluj závisnosti
pip install -r backend/requirements.txt

# Pridaj pytest pre testy
pip install pytest pytest-mock
```

### Spustenie

```bash
cd backend
python -m uvicorn main:app --reload
```

Aplikácia bude dostupná na: **http://localhost:8000**

## 🔗 API Endpointy

### 1. Synchronné vyhľadávanie (blokuje kým sa neukončí)

```bash
curl -X POST http://localhost:8000/api/mpk/search \
  -H "Content-Type: application/json" \
  -d '{
    "search_term": "NK",
    "include_variants": true
  }'
```

**Odpoveď:**
```json
{
  "processes_found": 5,
  "processes_with_matches": 3,
  "documents_processed": ["doc1.docx", "doc2.pdf"],
  "documents_failed": [],
  "matches": [
    {
      "source": "Predkladacia správa (LP: 2026/426)",
      "source_url": "https://...",
      "pattern_matched": "NK_FULL_NAME",
      "matched_text": "Najvyšší kontrolný úrad",
      "context": "Podľa Najvyššieho kontrolného úradu...",
      "page_hint": 1
    }
  ]
}
```

### 2. Asynchrónne vyhľadávanie (na pozadí)

Spustenie:
```bash
curl -X POST http://localhost:8000/api/mpk/search/start \
  -H "Content-Type: application/json" \
  -d '{"search_term": "NK"}'
```

Odpoveď:
```json
{
  "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

Kontrola stavu:
```bash
curl http://localhost:8000/api/mpk/search/status/a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

Odpoveď:
```json
{
  "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "running",
  "current": 3,
  "total": 10,
  "location": "Dokument: vlastny_material.docx",
  "elapsed_seconds": 45,
  "estimated_seconds_remaining": 75
}
```

### 3. Export výsledkov

```bash
# Export do CSV
curl -X POST http://localhost:8000/api/mpk/export \
  -H "Content-Type: application/json" \
  -d '{
    "matches": [...],
    "file_format": "csv"
  }' \
  > results.csv

# Export do XLSX
curl -X POST http://localhost:8000/api/mpk/export \
  -H "Content-Type: application/json" \
  -d '{
    "matches": [...],
    "file_format": "xlsx"
  }' \
  > results.xlsx
```

## 📁 Štruktúra projektu

```
backend/
├── main.py                 # FastAPI app (originál + MPK routes)
├── mpk_scraper.py         # Scrapovanie slov-lex.sk
├── mpk_extractor.py       # Pattern matching (NK/NKÚ)
├── mpk_routes.py          # API endpointy (/api/mpk/*)
├── schemas.py             # Pydantic modely (aktualizované)
├── parsers.py             # Parsovanie DOCX/PDF (originál)
├── extractor.py           # Originálne NKÚ extraktory
├── scraper.py             # Originálny NRSR scraper
├── requirements.txt       # Python dependencies
└── test_mpk.py           # Unit testy

frontend/
└── index.html            # Web UI
```

## 🔍 Ako funguje

### Workflow

```
1. Hľadaj v MPK portáli
   └─> https://www.slov-lex.sk/elegislativa/legislativne-procesy/?stadium=MPK&hladanyVyraz=NK

2. Pre každý nájdený proces
   └─> Zisti stadium_uuid
       └─> Načítaj sprievodné dokumenty
           └─> Stiahni DOCX/PDF/RTF súbory

3. Pre každý stiahnutý dokument
   └─> Sparsuj obsah (TextBlock-y)
       └─> Aplikuj regex vzory (NK, NKÚ, Najvyšší kontrolný úrad)
           └─> Vrať zápasy s kontextom (veta/odsek)

4. Štruktúruj výsledky
   └─> JSON / XLSX / CSV
```

### Regex vzory

| Vzor | Príklad | Detekuje |
|------|---------|----------|
| `NK_FULL_NAME` | "Najvyšší kontrolný úrad" | Plné názvy a skloňované formy |
| `NK_ACRONYM` | "NK robí audit" | Skratka NK (s word boundaries) |
| `NKU_ACRONYM` | "NKÚ SR" | Skratka NKÚ |

Vzory sú **case-insensitive** a zvládajú **slovenské skloňovanie** (nominatív, genitív, datív, atď.)

## 🧪 Testy

Spustenie testov:

```bash
cd backend
pytest test_mpk.py -v
```

### Pokryté testy

- ✅ Detekcia NK/NKÚ textov
- ✅ Skloňované formy (slovenčina)
- ✅ Parsovanie viet (s skratkami)
- ✅ SSRF ochrana (iba slov-lex.sk)
- ✅ Dátové modely (Pydantic)

## ⚠️ Bezpečnosť

### SSRF Ochrana
Všetky URL sa validujú - iba `slov-lex.sk` domény sú povolené:
```python
_ALLOWED_MPK_HOSTS = {"slov-lex.sk", "www.slov-lex.sk"}
```

### Pamäť
- Maximálna veľkosť dokumentu: **20 MB**
- Garbage collection po každom dokumente
- Stream download s kontrolou veľkosti

### Politeness
- Odsúpisovacia pauza: **1.5 sekúndy** medzi requestami
- Retry stratégia (MAX 3 pokusy s exponenciálnym backoffom)
- Rešpektovanie `robots.txt` slov-lex.sk

## 🔧 Konfigurácia

Upravuj v `mpk_scraper.py`:

```python
REQUEST_DELAY_SECONDS = 1.5      # Pauza medzi requestami
MAX_RETRIES = 3                   # Počet pokusov pri chybe
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024  # Max veľkosť súboru
```

## 📝 Príklady

### Python script

```python
from mpk_scraper import build_session, search_mpk_processes
from mpk_extractor import find_matches_in_text

session = build_session()
processes = search_mpk_processes(session, search_term="NK", page=1)

for process in processes:
    print(f"LP {process.lp_number}: {process.title}")
    print(f"  URL: {process.url}")
```

### cURL s parametrami

```bash
# Hľadaj "Najvyšší kontrolný úrad"
curl -X POST http://localhost:8000/api/mpk/search \
  -H "Content-Type: application/json" \
  -d '{
    "search_term": "Najvyšší kontrolný úrad",
    "include_variants": true
  }'
```

## 🐛 Troubleshooting

### "403 Forbidden"
- slov-lex.sk blokuje IP → počkaj a skús neskôr
- Alebo zmeň `User-Agent` v `mpk_scraper.py`

### "Neznáme job_id"
- Job timeout → async jobs nie sú perzistentné (stracia sa pri reštarte servera)
- Skús znovu `/api/mpk/search/start`

### "No 'awpage' divs found"
- Markup slov-lex.sk sa zmenil → uprav CSS selektory v `mpk_scraper.py`

## 📚 Rozšírenie

### Pridať ďalšie hľadané výrazy
Uprav `mpk_extractor.py`:
```python
_PATTERNS = {
    "NK_FULL_NAME": re.compile(r"(?i)najvyšš\w*...", re.IGNORECASE),
    "NK_ACRONYM": re.compile(r"(?<!\w)NK(?!\w)"),
    "CUSTOM_PATTERN": re.compile(r"(?i)tvoj_vzor"),  # <- PRIDAJ TU
}
```

### Pridať nový export formát
Uprav `mpk_routes.py`:
```python
@router.post("/export")
def export_mpk_matches(payload: ExportRequest):
    if payload.file_format == "json":
        return {"matches": [m.dict() for m in payload.matches]}
```

## 📄 Licencia

Rovnaká ako originálny `extraktor-nk` projekt.

## 👤 Autor

Upravené na MPK portál z originálneho `extraktor-nk` projektu.

---

**Branch:** `mpk-adapter`  
**Status:** ✅ Pracovné verzie s testami  
**Kompatibilita:** Python 3.10+
