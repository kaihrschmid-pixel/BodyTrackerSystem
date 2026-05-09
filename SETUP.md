# Hardware Setup & Configuration Guide

This guide walks you through setting up body-tracker from zero — choosing hardware, wiring everything together, and running your first measurement session.

---

## Table of Contents

1. [Choose your kit](#1-choose-your-kit)
2. [Install the software](#2-install-the-software)
3. [Starter Kit — BLE scale + USB webcam](#3-starter-kit--ble-scale--usb-webcam)
4. [Standard Kit — Withings Body Scan + Raspberry Pi Camera](#4-standard-kit--withings-body-scan--raspberry-pi-camera)
5. [Pro Kit — Withings Body Scan 2 + Intel RealSense](#5-pro-kit--withings-body-scan-2--intel-realsense)
6. [Wearable integrations](#6-wearable-integrations)
7. [AI analysis](#7-ai-analysis)
8. [Daily use](#8-daily-use)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Choose your kit

| Kit | Scale | Camera | Cost (approx.) | Best for |
|-----|-------|--------|----------------|----------|
| **Starter** | Any BLE scale (or manual entry) | USB webcam | €0–80 | Getting started quickly |
| **Standard** | Withings Body Scan | Raspberry Pi Camera Module 3 | €450–550 | Clinical-grade BIA + automated photo |
| **Pro** | Withings Body Scan 2 | Intel RealSense D435i | €1 200–1 600 | Full 3D depth + circumference estimates |

You can always start with the Starter Kit and upgrade later — each kit uses the same database and config format.

---

## 2. Install the software

**Requirements:** Python 3.11 or newer.

```bash
# Clone the repo
git clone https://github.com/kaihrschmid-pixel/BodyTrackerSystem
cd BodyTrackerSystem

# Starter Kit (minimal dependencies)
pip install -e "."

# Standard Kit (adds Pi Camera + MediaPipe silhouette extraction)
pip install -e ".[picamera2]"

# Pro Kit (adds Intel RealSense SDK bindings)
pip install -e ".[realsense]"

# Everything (USB webcam + BLE + vision)
pip install -e ".[full]"

# Development tools (tests, linting)
pip install -e ".[dev]"
```

Create the data directories:

```bash
mkdir -p data/photos data/exports data/silhouettes
```

---

## 3. Starter Kit — BLE scale + USB webcam

### 3.1 Hardware

| Item | Example model | Price |
|------|---------------|-------|
| BLE body composition scale | Xiaomi Mi Body Composition Scale 2 | ~€25 |
| USB webcam | Logitech C270 / C920 | ~€30–60 |
| Host machine | Any Linux/macOS/Windows PC or Raspberry Pi 4 | — |

**Camera placement:** Mount the webcam on a tripod or shelf bracket at **~1 m height**, **2–3 m in front of the scale**, pointing at the spot where you stand. This gives a full-body silhouette.

### 3.2 Config

```bash
cp kits/starter/config.yaml config.yaml
```

Open `config.yaml` and edit:

```yaml
profile:
  height_cm: 180          # your height
  sex: "male"             # "male" | "female" | "other"
  birthdate: "1990-01-01"
  timezone: "Europe/Berlin"

hardware:
  scale:
    adapter: "ble"
    device_name: "MI Scale"   # Bluetooth device name from your scale's manual
                               # Common alternatives: "MIBCS", "Mi Scale"

  camera:
    adapter: "v4l2"
    device: "/dev/video0"     # check: ls /dev/video*
    resolution: [1920, 1080]
    stabilisation_delay_s: 3  # seconds between stepping off scale and photo capture
```

**Not sure about your scale's Bluetooth name?** Run this to scan nearby BLE devices:

```bash
python - <<'EOF'
import asyncio
from bleak import BleakScanner
async def scan():
    devices = await BleakScanner.discover(timeout=10)
    for d in devices:
        print(d.name, d.address)
asyncio.run(scan())
EOF
```

**Not sure which video device?** List available cameras:

```bash
ls /dev/video*          # Linux
# macOS: system_profiler SPCameraDataType
```

### 3.3 First run

```bash
# Test without any hardware first
body-tracker measure

# Full session: waits for BLE scale event, then triggers camera
body-tracker measure --config config.yaml
```

---

## 4. Standard Kit — Withings Body Scan + Raspberry Pi Camera

### 4.1 Hardware

| Item | Price |
|------|-------|
| Withings Body Scan | ~€400 |
| Raspberry Pi 4 Model B (4 GB) | ~€60 |
| Raspberry Pi Camera Module 3 NoIR | ~€25 |
| MicroSD card (32 GB+) | ~€10 |
| Phone/tablet tripod mount | ~€20 |

**Camera placement:** CSI ribbon cable connects directly to the Pi. Mount it at **~1 m height**, **2–3 m in front of the scale**. The Camera Module 3 NoIR works well in low bathroom lighting.

### 4.2 Raspberry Pi setup

```bash
# Enable the camera interface
sudo raspi-config
# → Interface Options → Camera → Enable

# Verify the camera is detected
libcamera-hello

# Install body-tracker with picamera2 support
pip install -e ".[picamera2]"
```

### 4.3 Withings OAuth setup

Withings requires a developer app for API access. This is free and takes ~5 minutes.

1. Go to [developer.withings.com](https://developer.withings.com) and create an account
2. Create a new application:
   - **Callback / Redirect URI:** `http://localhost:9753/callback`
   - **Scope:** `user.metrics,user.activity`
3. Copy your **Client ID** and **Client Secret**

Add them to `config.yaml`:

```yaml
hardware:
  scale:
    adapter: "withings"
    client_id: "YOUR_CLIENT_ID"
    client_secret: "YOUR_CLIENT_SECRET"
    token_path: "data/withings_token.json"
```

Run the one-time authorisation flow:

```bash
body-tracker auth withings
# Opens your browser → log in to Withings → tokens saved automatically
```

### 4.4 Config

```bash
cp kits/standard/config.yaml config.yaml
```

Key settings to edit:

```yaml
profile:
  height_cm: 180
  sex: "male"
  birthdate: "1990-01-01"
  timezone: "Europe/Berlin"

hardware:
  scale:
    adapter: "withings"
    client_id: "..."
    client_secret: "..."
    token_path: "data/withings_token.json"

  camera:
    adapter: "picamera2"
    resolution: [2304, 1296]   # Camera Module 3 native 16:9
    stabilisation_delay_s: 3
    hflip: false               # set true if image is mirrored
    vflip: false

imaging:
  enabled: true
  segmentation_model: 1
  min_detection_confidence: 0.5
  output_dir: "data/silhouettes"
```

### 4.5 Run on boot (systemd)

To start body-tracker automatically when the Pi boots:

```bash
sudo nano /etc/systemd/system/body-tracker.service
```

```ini
[Unit]
Description=body-tracker daemon
After=network.target bluetooth.target

[Service]
User=pi
WorkingDirectory=/home/pi/BodyTrackerSystem
ExecStart=/home/pi/.venv/bin/body-tracker daemon --config config.yaml
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable body-tracker
sudo systemctl start body-tracker
```

---

## 5. Pro Kit — Withings Body Scan 2 + Intel RealSense

### 5.1 Hardware

| Item | Price |
|------|-------|
| Withings Body Scan 2 (or Body Scan 1) | ~€600 (or ~€400) |
| Intel RealSense D435i | ~€200 |
| Intel NUC or x86 mini-PC (4+ cores, USB 3.0) | ~€300 |
| Heavy-duty tripod + RealSense mount | ~€50 |

> **Note:** The Raspberry Pi 4 has USB 3.0 bandwidth limitations that affect RealSense depth streaming. A Pi 5 works, but a mini-PC is recommended for reliable depth capture.

**Camera placement:** Mount the RealSense at **1.2 m height**, **2.5–3 m in front of the scale**. The D435i's IR emitter reaches up to 10 m; `depth_clip_m: 4.0` in config filters everything beyond 4 m.

### 5.2 RealSense SDK installation

```bash
# macOS
brew install librealsense

# Ubuntu / Debian
sudo apt-key adv --keyserver keyserver.ubuntu.com \
  --recv-key F6E65AC044F831AC80A06380C8B3A55A6F3EFCD
sudo add-apt-repository \
  "deb https://librealsense.intel.com/Debian/apt-repo $(lsb_release -cs) main"
sudo apt update && sudo apt install librealsense2-dkms librealsense2-utils

# Install Python bindings
pip install -e ".[realsense]"

# Verify camera is detected
realsense-viewer
```

### 5.3 Config

```bash
cp kits/pro/config.yaml config.yaml
```

Withings OAuth setup is identical to the Standard Kit (see [section 4.3](#43-withings-oauth-setup)).

Key camera settings:

```yaml
hardware:
  camera:
    adapter: "realsense"
    resolution: [1280, 720]
    fps: 30
    stabilisation_delay_s: 4
    depth_preset: "HIGH_ACCURACY"   # DEFAULT | HIGH_ACCURACY | HIGH_DENSITY | MEDIUM_DENSITY
    save_depth_map: true            # saves 16-bit depth PNG alongside photo
    depth_clip_m: 4.0
    emitter_enabled: true

imaging:
  enabled: true
  use_depth_map: true
  depth_foreground_clip_m: 3.0      # pixels beyond this → treated as background
  min_detection_confidence: 0.6
```

---

## 6. Wearable integrations

Wearables provide sleep, HRV, and activity context for the AI analysis. All are optional.

### Oura Ring

No developer app needed — just a personal access token.

1. Go to [cloud.ouraring.com/personal-access-tokens](https://cloud.ouraring.com/personal-access-tokens)
2. Create a token and paste it into `config.yaml`:

```yaml
wearables:
  oura:
    enabled: true
    personal_access_token: "YOUR_TOKEN"
```

### Fitbit

1. Register an app at [dev.fitbit.com](https://dev.fitbit.com)
   - **OAuth 2.0 Application Type:** Personal
   - **Callback URL:** `http://localhost:9753/callback`
2. Add credentials to `config.yaml`:

```yaml
wearables:
  fitbit:
    enabled: true
    client_id: "YOUR_CLIENT_ID"
    client_secret: "YOUR_CLIENT_SECRET"
    token_path: "data/fitbit_token.json"
```

3. Authorise:

```bash
body-tracker auth fitbit
```

### WHOOP

1. Register an app at [developer.whoop.com](https://developer.whoop.com)
   - **Redirect URI:** `http://localhost:9753/callback`
2. Add credentials and authorise:

```yaml
wearables:
  whoop:
    enabled: true
    client_id: "YOUR_CLIENT_ID"
    client_secret: "YOUR_CLIENT_SECRET"
    token_path: "data/whoop_token.json"
```

```bash
body-tracker auth whoop
```

### Garmin

Garmin uses OAuth 1.0a — no browser flow needed, just four tokens directly from [developer.garmin.com](https://developer.garmin.com):

```yaml
wearables:
  garmin:
    enabled: true
    consumer_key: "YOUR_CONSUMER_KEY"
    consumer_secret: "YOUR_CONSUMER_SECRET"
    access_token: "YOUR_ACCESS_TOKEN"
    token_secret: "YOUR_TOKEN_SECRET"
```

### Apple Health (offline)

Export your Health data from the iPhone Health app (Profile → Export All Health Data), unzip the archive, and import:

```bash
body-tracker import apple_health_export/export.xml
```

No API key or internet connection required.

---

## 7. AI analysis

body-tracker uses Claude (Anthropic) to analyse trends and optionally describe silhouette photos.

1. Get an API key at [console.anthropic.com](https://console.anthropic.com)
2. Enable AI in `config.yaml`:

```yaml
ai:
  enabled: true
  provider: "anthropic"
  model: "claude-opus-4-5"
  api_key: "YOUR_API_KEY"
  analyse_photo: true      # include silhouette image in the prompt
  analyse_trends: true     # weekly/monthly trend summaries
  context_days: 30         # how many past days to include
```

The AI analysis runs automatically at the end of each `body-tracker measure` session and is also available on demand:

```bash
body-tracker analyse          # analyse latest session
body-tracker analyse --days 7 # weekly summary
```

---

## 8. Daily use

```bash
# One measurement session (interactive)
body-tracker measure

# Persistent daemon — waits for scale event, triggers camera automatically
body-tracker daemon

# Start the web dashboard
uvicorn ui.api:app --host 127.0.0.1 --port 8000
# Open http://localhost:8000

# View recent history in terminal
body-tracker history

# Bulk import historical data from CSV
body-tracker import data.csv

# List all detected adapters
body-tracker adapters
```

### CSV import format

```csv
date,weight_kg,body_fat_pct,muscle_mass_kg,bone_mass_kg,water_pct,visceral_fat_index,bmr_kcal,phase_angle
2025-01-01,83.2,19.4,62.1,3.3,57.2,9.0,1820,5.9
```

Only `date` and `weight_kg` are required — all other columns are optional.

---

## 9. Troubleshooting

### BLE scale not detected

```bash
# Check Bluetooth is on
bluetoothctl show

# Scan for nearby devices
bluetoothctl scan on    # wait 10 seconds, then look for your scale name
```

Make sure the scale is actively transmitting (step on it briefly).

### Camera not found (v4l2)

```bash
ls /dev/video*          # list all video devices
v4l2-ctl --list-devices # detailed list (install: sudo apt install v4l-utils)
```

Change `device: "/dev/video0"` to the correct path.

### Picamera2 error: "No cameras available"

```bash
# Check camera is enabled
sudo raspi-config   # Interface Options → Camera

# Check cable connection
vcgencmd get_camera   # should return: supported=1 detected=1
```

### Withings / Fitbit / WHOOP token expired

Tokens are refreshed automatically. If refresh fails (e.g. revoked access), re-run:

```bash
body-tracker auth withings    # or fitbit / whoop
```

### RealSense not detected

```bash
rs-enumerate-devices   # lists all connected RealSense cameras
```

Make sure the camera is connected to a **USB 3.0 port** (blue port). USB 2.0 is too slow for depth streaming.

### "Module not found" errors

Install the correct extras group for your kit:

```bash
pip install -e ".[picamera2]"   # Standard Kit
pip install -e ".[realsense]"   # Pro Kit
pip install -e ".[ble]"         # BLE scale
pip install -e ".[full]"        # everything
```
