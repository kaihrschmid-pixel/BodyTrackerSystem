# body-tracker — Cowork Instructions

## Was dieses Projekt ist

Open-source System zur täglichen Körperanalyse. Waage und Kamera werden
kombiniert; Daten landen lokal in SQLite; KI analysiert Trends.

Ziel: modular, hardware-agnostisch, vollständig lokal betreibbar.

---

## Repo-Struktur

```
body-tracker/
├── adapters/
│   ├── base.py              # Abstrakte Basisklassen + Datenklassen  ← IMMER lesen
│   ├── registry.py          # Plugin-System (String → Klasse)
│   ├── scales/
│   │   ├── manual.py        # ✅ Fertig — Referenz für neue Scale-Adapter
│   │   └── ble.py           # ✅ Fertig — BLE/Bluetooth Adapter
│   ├── cameras/
│   │   ├── mock.py          # ✅ Fertig — Referenz für neue Camera-Adapter
│   │   └── v4l2.py          # ✅ Fertig — USB-Webcam
│   └── wearables/
│       └── oura.py          # ✅ Fertig — Referenz für neue Wearable-Adapter
├── core/
│   ├── database.py          # ✅ Fertig — SQLite CRUD, 7 Tabellen
│   ├── scheduler.py         # ✅ Fertig — Täglicher Orchestrator
│   ├── analysis.py          # ✅ Fertig — Claude API Prompt-Builder
│   └── cli.py               # ✅ Fertig — CLI-Einstiegspunkt
├── ui/
│   └── api.py               # ✅ Fertig — FastAPI REST + WebSocket
├── tests/
│   └── adapters/
│       └── test_base_and_manual.py  # ✅ 18 Tests
├── kits/
│   └── starter/config.yaml  # ✅ Fertig
└── pyproject.toml           # ✅ Fertig
```

---

## Was noch fehlt (offene Aufgaben)

### Scale-Adapter
- `adapters/scales/withings.py` — Withings Health API v2, OAuth 2.0
  Klasse: `WithingsScaleAdapter(ScaleAdapter)`
  Registriert als: `"withings"` in `_SCALE_REGISTRY`

### Camera-Adapter
- `adapters/cameras/picamera2.py` — Raspberry Pi CSI-Kamera
  Klasse: `PiCamera2Adapter(CameraAdapter)`
  Registriert als: `"picamera2"` in `_CAMERA_REGISTRY`
- `adapters/cameras/rtsp.py` — Smartphone als IP-Kamera
  Klasse: `RTSPCameraAdapter(CameraAdapter)`
  Registriert als: `"rtsp"` in `_CAMERA_REGISTRY`

### Wearable-Adapter
- `adapters/wearables/garmin.py` — Garmin Health API
  Klasse: `GarminAdapter(WearableAdapter)`
  Registriert als: `"garmin"` in `_WEARABLE_REGISTRY`
- `adapters/wearables/fitbit.py` — Fitbit Web API
  Klasse: `FitbitAdapter(WearableAdapter)`
  Registriert als: `"fitbit"` in `_WEARABLE_REGISTRY`
- `adapters/wearables/whoop.py` — WHOOP API
  Klasse: `WhoopAdapter(WearableAdapter)`
  Registriert als: `"whoop"` in `_WEARABLE_REGISTRY`

### Core-Module
- `core/imaging.py` — Silhouetten-Extraktion mit MediaPipe/OpenCV
  Funktion: `extract_silhouette(image_path: Path) -> Path`
  Aufgerufen von: `core/scheduler.py` nach `db.save_photo()`

### Kits
- `kits/standard/config.yaml` — Withings Body Scan + Pi Camera
- `kits/pro/config.yaml` — Withings Body Scan 2 + Intel RealSense

---

## Wie ein neuer Adapter aussehen muss

### Scale-Adapter — Pflichtstruktur

```python
# adapters/scales/MEIN_ADAPTER.py
from adapters.base import ScaleAdapter, ScaleReading, utcnow

class MeinAdapter(ScaleAdapter):
    ADAPTER_NAME = "mein_adapter"          # muss eindeutig sein

    def __init__(self, config: dict):
        super().__init__(config)
        # config kommt aus config.yaml → hardware.scale

    async def connect(self) -> None: ...   # Verbindung aufbauen
    async def read(self) -> ScaleReading: ...  # Blockiert bis stabile Messung
    async def disconnect(self) -> None: ...
    def is_available(self) -> bool: ...
```

`ScaleReading` hat nur `recorded_at` und `weight_kg` als Pflichtfelder.
Alle anderen Felder (body_fat_pct, phase_angle, etc.) sind Optional[float].
Den Adapter **niemals** direkt in core/ importieren — immer über registry.py.

### Wearable-Adapter — Pflichtstruktur

```python
# adapters/wearables/MEIN_ADAPTER.py
from adapters.base import WearableAdapter, ContextReading, utcnow

class MeinWearableAdapter(WearableAdapter):
    ADAPTER_NAME = "mein_wearable"

    async def authenticate(self) -> None: ...  # Token prüfen/refreshen
    async def fetch_context(self, date: datetime) -> ContextReading: ...
    def is_available(self) -> bool: ...
```

`ContextReading` hat Felder für Schlaf, HRV, Schritte etc. — alles Optional.
Extra-Daten die nicht in ContextReading passen → `reading.extras["key"] = value`

### Registry-Eintrag nach jeder neuen Datei

Nach Erstellung jeder Adapter-Datei **zwingend** in `adapters/registry.py` eintragen:

```python
# Beispiel für Scale:
_SCALE_REGISTRY["withings"] = "adapters.scales.withings:WithingsScaleAdapter"

# Beispiel für Wearable:
_WEARABLE_REGISTRY["garmin"] = "adapters.wearables.garmin:GarminAdapter"
```

---

## Konventionen

**Sprache:** Python 3.11+, `from __future__ import annotations`

**Async:** Alle I/O-Methoden sind `async`. Keine `time.sleep()`, immer `asyncio.sleep()`.

**Fehlerbehandlung:**
- Adapter-Fehler nie still schlucken
- Im Scheduler: Wearable-Fehler werden geloggt aber crashen nicht (`logger.warning`)
- Verbindungsfehler klar benennen mit hilfreicher Fehlermeldung für den Nutzer

**Imports:** Schwere Deps (bleak, cv2, httpx) lazy importieren — erst innerhalb
der Methode, nicht auf Modulebene. So bleibt das Projekt installierbar ohne alle
optionalen Abhängigkeiten.

**Docstring-Format:** Kurze Beschreibung + Konfigurationsbeispiel im Modulheader,
wie in `adapters/wearables/oura.py` gezeigt.

**Timestamps:** Immer UTC, immer timezone-aware. Nutze `utcnow()` aus `adapters/base.py`.

---

## Tests

Jeder neue Adapter braucht eine Testdatei unter `tests/adapters/test_NAME.py`.

Muster aus `tests/adapters/test_base_and_manual.py`:
- HTTP-Calls mit `respx` oder manuellem Mock mocken
- BLE-Calls mit `unittest.mock.AsyncMock` mocken
- Mindestens testen: `is_available()`, Parsing realer API-Responses, Fehlerfall

Tests ausführen: `pytest tests/ -v`

---

## Wichtige Designentscheidungen — nicht ändern

1. **Das Core-System ist hardware-blind.** `core/scheduler.py` importiert
   niemals einen konkreten Adapter direkt — immer über `adapters/registry.py`.

2. **Datenfelder sind optional.** Eine Waage die nur Gewicht liefert ist
   genauso gültig wie eine mit 60 Metriken. Niemals auf optionale Felder
   prüfen ohne `if value is not None`.

3. **Alles lokal.** Keine Cloud-Pflicht. `core/database.py` schreibt immer
   in die lokale SQLite. Die KI-Analyse in `core/analysis.py` ist optional.

4. **`adapters/registry.py` ist das einzige Plugin-Interface.**
   Neuen Adapter hinzufügen = eine Zeile in registry.py. Sonst nichts.

---

## Referenz-Implementierungen zum Anlesen

| Aufgabe | Datei lesen |
|---------|-------------|
| Neuer Wearable-Adapter | `adapters/wearables/oura.py` |
| Neuer Scale-Adapter | `adapters/scales/manual.py` |
| Neuer Camera-Adapter | `adapters/cameras/mock.py` |
| DB-Queries erweitern | `core/database.py` |
| Scheduler-Flow verstehen | `core/scheduler.py` |
