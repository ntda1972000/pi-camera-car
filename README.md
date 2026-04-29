# pi-camera-car

A Raspberry Pi-powered RC car with live camera streaming and a browser-based control dashboard.

---

## Features (implemented)

### Camera & Streaming
- **Live MJPEG stream** served over HTTP — works in every browser, no plugin needed
- **WebRTC support** (optional) via `aiortc` — lower latency H.264 stream, prefers hardware codecs
- **Camera rotation** — configurable 0°/90°/180°/270° via libcamera transforms
- **Adaptive resolution & FPS** — choose from 160×120 up to 640×480, 5–30 fps
- **Camera watchdog** — background thread detects a stalled camera and auto-restarts it
- **No-camera fallback** — app starts and all routes work even when no CSI camera is connected

### Web Interface
- **Public index page** — shows the live stream, data-rate estimate, battery level
- **Login page** — password-protected access to the dashboard and settings (werkzeug hash)
- **Dashboard** (`/dashboard`) — virtual joystick for driving; I/O device toggles
- **Settings page** (`/settings`) — change resolution, FPS, stream mode, max motor speed, camera rotation, and I/O device names/states; live data-rate preview updates as you tweak sliders

### API endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | `/video_feed` | MJPEG multipart stream |
| GET | `/api/status` | JSON snapshot of all settings + live bitrate + battery |
| GET | `/api/estimate` | Data-rate estimate for ?width=&height=&fps= |
| POST | `/api/control` | Joystick x/y input (−1…1) |
| POST | `/api/update_settings` | Update any combination of settings |
| POST | `/api/io_toggle` | Toggle I/O device by index (0–3) |
| POST | `/api/webrtc/offer` | WebRTC SDP offer/answer (requires aiortc) |

### Persistence
- All settings saved to `settings.json`; survive reboots
- Merges defaults so new config keys are always present on older installs

### Battery monitoring
- Reads `/sys/class/power_supply/*/capacity` — works automatically with PiJuice, Waveshare UPS HAT, Geekworm X728, and similar HATs

---

## Hardware

| Component | Notes |
|-----------|-------|
| Raspberry Pi 4 / 5 | Tested on Pi 4 2 GB |
| Raspberry Pi Camera Module (CSI) | Any v1/v2/v3 module |
| Motor driver HAT | GPIO PWM — **wiring TBD** |
| 4× GPIO-controlled outputs | Lights, horns, etc. |
| UPS / battery HAT | Optional; enables battery % display |
| 4G / DCOM USB modem | Optional; for remote control over mobile data |

---

## Getting started

```bash
# Clone
git clone https://github.com/ntda1972000/pi-camera-car.git
cd pi-camera-car

# Download mediamtx binary (needed for HLS/RTSP mode)
bash setup.sh

# Install Python dependencies (Raspberry Pi OS)
pip install -r requirements.txt

# Run
python app.py
# Open http://<pi-ip>:5000
```

Default password: `duyanhcar` — **change `ADMIN_PASSWORD_HASH` in `app.py` before deployment**.

### Auto-start on boot (systemd)
```bash
sudo cp pi-camera-car.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable pi-camera-car
sudo systemctl start pi-camera-car
# Check status
sudo systemctl status pi-camera-car
```

### Optional: WebRTC (lower latency)
```bash
pip install aiortc
```
Restart the app; WebRTC will be listed automatically in the stream-mode selector.

---

## Project structure

```
pi-camera-car/
├── app.py                   # Flask application — camera, streaming, API, motor stub
├── setup.sh                 # Downloads mediamtx binary for HLS/RTSP mode
├── mediamtx.yml             # Auto-generated mediamtx config (do not hand-edit)
├── pi-camera-car.service    # Systemd service for auto-start on boot
├── requirements.txt         # Python dependencies
├── settings.json            # Persisted runtime settings (auto-generated)
└── templates/
    ├── index.html           # Public stream viewer
    ├── login.html           # Password prompt
    ├── dashboard.html       # Joystick + I/O control
    └── settings.html        # Settings editor
```

---

## Roadmap / planned improvements

- [ ] **Motor driver integration** — translate joystick x/y to PWM signals (L298N / MX1508 HAT)
- [ ] **GPIO I/O wiring** — map the 4 virtual device toggles to real GPIO pins
- [ ] **4G / remote control** — FastAPI layer + DCOM/USB modem for driving over the internet
- [ ] **WebRTC TURN server** — allow WebRTC to traverse NAT (Coturn or hosted TURN)
- [ ] **Secure secret key** — load `app.secret_key` from environment variable / `.env`
- [ ] **HTTPS / TLS** — terminate SSL so cookies are secure over the internet
- [ ] **Speed PID loop** — closed-loop speed control with encoder feedback
- [ ] **Headlights control** — first I/O device (GPIO pin) wired to front LEDs
- [ ] **Mobile-first UI** — improved touch joystick, swipe gestures, portrait layout
- [ ] **OTA update endpoint** — pull latest code and restart service via API
- [x] **Systemd service file** — auto-start on boot (`pi-camera-car.service`)
- [x] **HLS/RTSP streaming** — hardware H.264 via mediamtx (`rpicam-vid` → RTSP → HLS)
- [ ] **Recording / snapshot** — save frames to disk or stream to remote storage
- [ ] **Multi-camera support** — switch between CSI and USB webcams at runtime

---

## License

MIT
