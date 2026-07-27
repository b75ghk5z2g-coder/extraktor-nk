"""
Vstupny bod pre zabaleny (.exe) server.

Toto NIE JE potrebne spustat priamo pri bezhnom vyvoji (na to sluzi
"python -m uvicorn main:app --reload"). Tento subor sluzi ako cielovy
skript pre PyInstaller, aby sa dal server zabalit do jedneho .exe
suboru, ktory uz nepotrebuje Python nainstalovany na cielovom pocitaci.
"""
import uvicorn

if __name__ == "__main__":
    print("=" * 50)
    print(" NKU Extraktor - server sa spusta...")
    print(" Nezatvarajte toto okno, kym appku pouzivate.")
    print(" Appku najdete v subore frontend/index.html")
    print("=" * 50)
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
