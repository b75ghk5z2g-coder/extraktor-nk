# scraper.py (Úprava pre extrakciu textu odkazov)
def extract_agenda_items(soup):
    """Extrahuje body programu zo stránky schôdze vrátane názvu odkazu."""
    agenda_items = []

    for link in soup.find_all("a", href=True):
        href = link["href"]
        text = link.get_text(strip=True)

        if "cpt" in href.lower() or "det" in href.lower() or "stlac" in href.lower():
            agenda_items.append(
                {
                    "title": text,
                    "url": (
                        f"https://www.nrsr.sk/web/{href}"
                        if not href.startswith("http")
                        else href
                    ),
                }
            )
    return agenda_items
