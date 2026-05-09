# body-tracker

Open-source daily body tracking system.  
Modular hardware adapters · local SQLite database · AI-powered insights.

---

## Quickstart (5 minutes, no hardware needed)

```bash
git clone https://github.com/your-org/body-tracker
cd body-tracker

# Install Python deps
pip install -e ".[dev]"

# Create a config file
body-tracker init

# Run your first manual measurement
body-tracker measure

# Start the dashboard server
uvicorn ui.api:app --host 127.0.0.1 --port 8000
# Open http://localhost:8000
```

---

## Hardware support

### Scales

| Adapter | Hardware | Features |
|---------|----------|---------|
| `manual` | Any scale | Manual entry or CSV import |
| `ble` | Xiaomi, Renpho, any BLE scale | Weight + BIA via Bluetooth |
| `withings` | Withings Body Scan / Body Scan 2 | 60+ biomarkers, segmental, phase angle, EKG |

### Cameras

| Adapter | Hardware |
|---------|----------|
| `v4l2` | Any USB webcam (Linux/Windows/macOS via OpenCV) |
| `picamera2` | Raspberry Pi Camera (CSI) |
| `rtsp` | Smartphone as IP camera (DroidCam, EpocCam) |
| `realsense` | Intel RealSense depth camera |
| `mock` | No hardware — generates placeholder images |

### Wearables (context data)

| Adapter | Data |
|---------|------|
| `oura` | Sleep, HRV, readiness, activity |
| `garmin` | Training, body composition, HRV (OAuth 1.0a) |
| `fitbit` | Steps, sleep, heart rate (OAuth 2.0 PKCE) |
| `withings_health` | Withings activity + sleep (shares Withings tokens) |
| `whoop` | Strain, recovery (OAuth 2.0) |
| `apple_health` | Offline XML export — no account required |
| `csv` | Universal CSV import — any source |

---

## Configuration

Copy a kit config and edit it:

```bash
cp kits/starter/config.yaml config.yaml
# Edit: set your hardware, height, timezone
```

Key settings:

```yaml
profile:
  height_cm: 180
  sex: male
  timezone: Europe/Berlin

hardware:
  scale:
    adapter: ble          # manual | ble | withings
    device_name: MI Scale

  camera:
    adapter: v4l2         # v4l2 | picamera2 | rtsp | mock
    device: 0

wearables:
  oura:
    enabled: true
    personal_access_token: YOUR_TOKEN

ai:
  enabled: true
  api_key: YOUR_ANTHROPIC_KEY
```

---

## CLI

```bash
body-tracker measure           # one measurement session
body-tracker daemon            # persistent daemon — waits for scale
body-tracker import data.csv   # bulk import historical data
body-tracker history           # print recent sessions
body-tracker adapters          # list available adapters
body-tracker auth <service>    # authorise a cloud service (see below)
```

---

## Authorising cloud services

Withings, Fitbit, and WHOOP require a one-time OAuth 2.0 authorisation flow.
Run it once per device — tokens are persisted to a local JSON file and refreshed
automatically on each run.

### Prerequisites

Register a developer application at the provider's portal and configure the
**redirect URI** to `http://localhost:9753/callback`.

| Service | Portal |
|---------|--------|
| Withings | [developer.withings.com](https://developer.withings.com) |
| Fitbit | [dev.fitbit.com](https://dev.fitbit.com) |
| WHOOP | [developer.whoop.com](https://developer.whoop.com) |

Add your `client_id` and `client_secret` to `config.yaml`:

```yaml
# Withings scale
hardware:
  scale:
    adapter: withings
    client_id: "YOUR_WITHINGS_CLIENT_ID"
    client_secret: "YOUR_WITHINGS_CLIENT_SECRET"
    token_path: data/withings_token.json

# Fitbit wearable
wearables:
  fitbit:
    enabled: true
    client_id: "YOUR_FITBIT_CLIENT_ID"
    client_secret: "YOUR_FITBIT_CLIENT_SECRET"
    token_path: data/fitbit_token.json

# WHOOP wearable
wearables:
  whoop:
    enabled: true
    client_id: "YOUR_WHOOP_CLIENT_ID"
    client_secret: "YOUR_WHOOP_CLIENT_SECRET"
    token_path: data/whoop_token.json
```

### Running the flow

```bash
body-tracker auth withings    # opens browser, exchanges code, saves tokens
body-tracker auth fitbit      # uses PKCE (S256) — no client_secret required
body-tracker auth whoop
```

The command:
1. Opens your browser at the provider's authorisation page
2. Starts a temporary HTTP server on `localhost:9753` to catch the redirect
3. Exchanges the authorisation code for access + refresh tokens
4. Writes the tokens to `token_path` as JSON

After a successful auth you can run `body-tracker measure` normally — tokens
are refreshed automatically when they expire.

### Token file format

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "updated_at": "2025-06-01T08:30:00+00:00"
}
```

### Garmin (OAuth 1.0a)

Garmin uses OAuth 1.0a with HMAC-SHA1 request signing rather than a browser
redirect flow. Configure the four tokens directly in `config.yaml` — no
interactive auth step is needed:

```yaml
wearables:
  garmin:
    enabled: true
    consumer_key: "YOUR_CONSUMER_KEY"
    consumer_secret: "YOUR_CONSUMER_SECRET"
    access_token: "YOUR_ACCESS_TOKEN"
    token_secret: "YOUR_TOKEN_SECRET"
```

Obtain these credentials from [developer.garmin.com](https://developer.garmin.com)
after registering your application.

---

## CSV import format

```csv
date,weight_kg,body_fat_pct,muscle_mass_kg,bone_mass_kg,water_pct,visceral_fat_index,bmr_kcal,phase_angle
2025-01-01,83.2,19.4,62.1,3.3,57.2,9.0,1820,5.9
2025-01-02,82.9,19.2,62.3,3.3,57.4,8.9,1822,6.0
```

All fields except `date` and `weight_kg` are optional.

---

## Adding an adapter

Create a file in `adapters/scales/`, `adapters/cameras/`, or `adapters/wearables/`
that extends the abstract base class from `adapters/base.py`.
Register it in `adapters/registry.py` with a one-line entry.

That's it — no other changes needed.

---

## Kit variants

| Kit | Scale | Camera | Cost (approx.) |
|-----|-------|--------|----------------|
| **Starter** | Any BLE scale | USB webcam | €50–80 |
| **Standard** | Withings Body Scan | Pi Camera | €450 |
| **Pro** | Withings Body Scan 2 | Intel RealSense | €700+ |

---

## Running tests

```bash
pytest tests/ -v
```

---

## License

MIT — use freely, contribute back.
