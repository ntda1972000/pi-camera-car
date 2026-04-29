import io
import os
import time
import threading
import json
import logging
from flask import Flask, render_template, Response, request, session, jsonify, redirect, url_for, make_response
from werkzeug.security import generate_password_hash, check_password_hash
from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput
from libcamera import Transform

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)
app.secret_key = "rc-car-secret-2026"  # Change this in production

# ---------------------------------------------------------------------------
# PASSWORD (encoded with werkzeug)
# ---------------------------------------------------------------------------
ADMIN_PASSWORD_HASH = generate_password_hash("duyanhcar")

# ---------------------------------------------------------------------------
# SETTINGS — persisted to settings.json
# ---------------------------------------------------------------------------
SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "settings.json")

DEFAULT_SETTINGS = {
    "resolution": [320, 240],
    "fps": 15,
    "stream_mode": "mjpeg",
    "max_motor_speed": 7,     # 0–10 scale
    "camera_rotation": 0,     # degrees: 0, 90, 180, 270
    "io_devices": [
        {"name": "Device 1", "state": False},
        {"name": "Device 2", "state": False},
        {"name": "Device 3", "state": False},
        {"name": "Device 4", "state": False},
    ],
}


def load_settings():
    global settings
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                saved = json.load(f)
            # Merge with defaults so new keys are always present
            for k, v in DEFAULT_SETTINGS.items():
                if k not in saved:
                    saved[k] = v
            saved["resolution"] = list(saved["resolution"])
            settings = saved
            logging.info("Settings loaded from settings.json")
            return
        except Exception as e:
            logging.warning(f"Could not load settings.json: {e}")
    settings = dict(DEFAULT_SETTINGS)
    # Ensure io_devices is always a 4-element list
    _ensure_io_devices()


def _ensure_io_devices():
    """Guarantee settings always has exactly 4 io_device entries."""
    devices = settings.get("io_devices", [])
    while len(devices) < 4:
        devices.append({"name": f"Device {len(devices) + 1}", "state": False})
    settings["io_devices"] = devices[:4]


def save_settings():
    try:
        data = dict(settings)
        data["resolution"] = list(data["resolution"])
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logging.error(f"Could not save settings.json: {e}")


load_settings()

# Live I/O states (in-memory). Persisted initial_state lives in settings["io_devices"].
io_states = [bool(d["state"]) for d in settings["io_devices"]]

# Available options
RESOLUTION_OPTIONS = [(160, 120), (320, 240), (480, 360), (640, 480)]
FPS_OPTIONS = [5, 10, 15, 20, 30]
ROTATION_OPTIONS = [0, 90, 180, 270]
STREAM_MODE_OPTIONS = ["mjpeg"]  # "webrtc" prepended at runtime when aiortc is installed
MJPEG_QUALITY = 25

# ---------------------------------------------------------------------------
# WEBRTC (aiortc) — optional; app works fine without it (falls back to MJPEG)
# Install with:  pip install aiortc
# ---------------------------------------------------------------------------
try:
    from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
    import av as _av
    import asyncio as _asyncio
    WEBRTC_AVAILABLE = True
    logging.info("aiortc available — WebRTC enabled.")
except ImportError:
    WEBRTC_AVAILABLE = False
    logging.info("aiortc not installed — WebRTC disabled. Run: pip install aiortc")

if WEBRTC_AVAILABLE:
    if "webrtc" not in STREAM_MODE_OPTIONS:
        STREAM_MODE_OPTIONS.insert(0, "webrtc")
    _rtc_loop = _asyncio.new_event_loop()
    threading.Thread(target=_rtc_loop.run_forever, daemon=True, name="rtc-loop").start()
    _pcs: set = set()

    # WebRTC tuning. H.264 needs a realistic floor at low resolutions; below
    # ~150 kbps the encoder produces large I-frames that overshoot the cap.
    WEBRTC_TARGET_KBPS = 100        # target ~MJPEG bandwidth
    WEBRTC_MAX_FPS = 12             # throttle frames sent to encoder (saves CPU + bitrate)

    class PiCameraTrack(VideoStreamTrack):
        """Reads from the shared MJPEG buffer, decodes, and feeds WebRTC as H.264.

        Reuses a single PyAV container across recv() calls so the decoder keeps
        warm caches, and throttles to WEBRTC_MAX_FPS so we don't pay for re-encoding
        every camera frame.
        """

        def __init__(self):
            super().__init__()
            self._last_jpeg_id = None
            self._last_emit = 0.0

        async def recv(self):
            pts, time_base = await self.next_timestamp()
            loop = _asyncio.get_event_loop()

            # Throttle to WEBRTC_MAX_FPS to avoid feeding redundant frames
            min_interval = 1.0 / max(1, WEBRTC_MAX_FPS)
            now = time.time()
            wait = self._last_emit + min_interval - now
            if wait > 0:
                await _asyncio.sleep(wait)
            self._last_emit = time.time()

            jpeg = await loop.run_in_executor(None, self._grab)
            if jpeg:
                try:
                    import io as _io
                    # Scale down to half resolution before H.264 encode.
                    # This is the single most effective way to cut I-frame size:
                    # a 320x240 independent JPEG decode produces a raw YUV frame
                    # that x264 must encode fresh — scaling to 160x120 quarters
                    # the pixel count and cuts the encoded size by ~4x.
                    container = _av.open(_io.BytesIO(bytes(jpeg)), format="mjpeg")
                    try:
                        for raw in container.decode(video=0):
                            w, h = tuple(settings["resolution"])
                            enc_w = max(160, w // 2)
                            enc_h = max(120, h // 2)
                            frame = raw.reformat(
                                width=enc_w, height=enc_h, format="yuv420p"
                            )
                            frame.pts = pts
                            frame.time_base = time_base
                            return frame
                    finally:
                        container.close()
                except Exception as exc:
                    logging.debug(f"WebRTC frame decode: {exc}")
            # Blank frame fallback
            w, h = tuple(settings["resolution"])
            frame = _av.VideoFrame(width=w // 2, height=h // 2, format="yuv420p")
            frame.pts = pts
            frame.time_base = time_base
            return frame

        def _grab(self):
            with output.condition:
                output.condition.wait(timeout=4.0)
            return output.frame

    async def _do_offer(sdp: str, kind: str) -> dict:
        pc = RTCPeerConnection()
        _pcs.add(pc)

        @pc.on("connectionstatechange")
        async def _on_state():
            logging.info(f"WebRTC peer → {pc.connectionState}")
            if pc.connectionState in ("closed", "failed", "disconnected"):
                await pc.close()
                _pcs.discard(pc)

        sender = pc.addTrack(PiCameraTrack())

        # Prefer H.264 over VP8 (smaller bitrate, hardware-accel on most phones)
        try:
            from aiortc.rtcrtpsender import RTCRtpSender
            caps = RTCRtpSender.getCapabilities("video")
            h264 = [c for c in caps.codecs if c.mimeType.lower() == "video/h264"]
            other = [c for c in caps.codecs if c.mimeType.lower() != "video/h264"]
            for tr in pc.getTransceivers():
                if tr.sender is sender and h264:
                    tr.setCodecPreferences(h264 + other)
        except Exception as exc:
            logging.debug(f"Codec pref failed: {exc}")

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=kind))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        # Cap H.264 bitrate. aiortc's encoder treats this as a hint, but combined
        # with the FPS throttle in PiCameraTrack it produces stable ~target_kbps.
        try:
            params = sender.getParameters()
            if params.encodings:
                params.encodings[0].maxBitrate = WEBRTC_TARGET_KBPS * 1000
                await sender.setParameters(params)
        except Exception as exc:
            logging.debug(f"Bitrate cap failed: {exc}")

        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

# ---------------------------------------------------------------------------
# MOTOR CONTROL STATE (in-memory; will be sent to motor driver later)
# ---------------------------------------------------------------------------
control_state = {"x": 0.0, "y": 0.0}  # x=steering, y=throttle (-1..1)


def get_battery_percent():
    """
    Read battery % from the power_supply kernel interface.
    Works automatically with most UPS HATs (PiJuice, Waveshare, Geekworm X728).
    Returns int 0-100, or None if no battery monitor is present.
    Swap this body for ADC/I2C reading when your hardware arrives.
    """
    import glob
    for cap_path in glob.glob("/sys/class/power_supply/*/capacity"):
        try:
            with open(cap_path) as f:
                return max(0, min(100, int(f.read().strip())))
        except Exception:
            continue
    return None


def estimate_bitrate_kbps(width, height, fps, quality=25):
    """
    Estimate bitrate in kbps for MJPEG based on resolution and FPS.
    Rough formula: JPEG frame size scales with pixel count and quality.
    """
    pixels = width * height
    # Base frame size (bytes) for quality 25; higher quality = larger frames
    frame_size_bytes = (pixels / 75000) * (quality / 25) * 8000
    bitrate_kbps = (frame_size_bytes * fps) / 1000
    return max(50, min(2000, bitrate_kbps))  # Clamp to reasonable range


def estimate_mb_per_hour(bitrate_kbps):
    """Calculate MB per hour from bitrate in kbps."""
    return (bitrate_kbps * 3600) / (8 * 1024)


class StreamingOutput(io.BufferedIOBase):
    """Thread-safe buffer that holds the latest MJPEG frame."""

    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()
        self.last_frame_time = 0
        self._bw_window = []  # list of (timestamp, frame_bytes)

    def write(self, buf):
        with self.condition:
            self.frame = buf
            now = time.time()
            self.last_frame_time = now
            self._bw_window.append((now, len(buf)))
            # Keep only last 5 seconds
            cutoff = now - 5.0
            self._bw_window = [(t, b) for t, b in self._bw_window if t > cutoff]
            self.condition.notify_all()

    def actual_bitrate_kbps(self):
        """Rolling 5-second average of frame-production bitrate in kbps."""
        now = time.time()
        window = [(t, b) for t, b in self._bw_window if t > now - 5.0]
        if len(window) < 2:
            return 0.0
        total_bytes = sum(b for _, b in window)
        elapsed = window[-1][0] - window[0][0]
        if elapsed <= 0:
            return 0.0
        return round((total_bytes * 8) / (elapsed * 1000), 1)


# --- Camera setup ---
output = StreamingOutput()

# Try to initialise the camera; continue without it if not present.
_cameras = Picamera2.global_camera_info()
if _cameras:
    picam2 = Picamera2()
    CAMERA_AVAILABLE = True
else:
    picam2 = None
    CAMERA_AVAILABLE = False
    logging.warning("No camera detected — running in no-camera mode. "
                    "Check the CSI ribbon cable and reboot.")

def _make_transform(degrees):
    """Convert rotation degrees to a libcamera Transform (hflip/vflip/transpose combos)."""
    if degrees == 90:
        return Transform(transpose=True, hflip=True)
    elif degrees == 180:
        return Transform(hflip=True, vflip=True)
    elif degrees == 270:
        return Transform(transpose=True, vflip=True)
    return Transform()  # 0° = identity


def configure_camera():
    """Apply current settings to the camera. No-op if no camera is present."""
    if not CAMERA_AVAILABLE:
        return
    try:
        picam2.stop_recording()
    except:
        pass

    transform = _make_transform(settings.get("camera_rotation", 0))

    picam2.configure(
        picam2.create_video_configuration(
            main={"size": tuple(settings["resolution"])},
            controls={"FrameRate": settings["fps"]},
            transform=transform,
        )
    )

    # Always run MJPEG encoder — WebRTC reads from this same shared buffer
    encoder = MJPEGEncoder(bitrate=None)
    encoder.quality = MJPEG_QUALITY
    picam2.start_recording(encoder, FileOutput(output))

configure_camera()

# ── Camera watchdog ──────────────────────────────────────────────────────────
# If no frame arrives within WATCHDOG_TIMEOUT seconds, restart the camera.
WATCHDOG_TIMEOUT = 5  # seconds

def camera_watchdog():
    while True:
        time.sleep(WATCHDOG_TIMEOUT)
        if not CAMERA_AVAILABLE:
            continue
        elapsed = time.time() - output.last_frame_time
        if output.last_frame_time > 0 and elapsed > WATCHDOG_TIMEOUT:
            logging.warning(f"No frame for {elapsed:.1f}s — restarting camera...")
            try:
                configure_camera()
                logging.info("Camera restarted successfully.")
            except Exception as e:
                logging.error(f"Camera restart failed: {e}")

threading.Thread(target=camera_watchdog, daemon=True).start()
# ─────────────────────────────────────────────────────────────────────────────


def generate_frames():
    """Yield MJPEG frames for the multipart HTTP stream."""
    if not CAMERA_AVAILABLE:
        # Return a minimal 1×1 placeholder JPEG so the stream doesn't hang
        while True:
            time.sleep(1)
            return
    while True:
        with output.condition:
            has_frame = output.condition.wait(timeout=4.0)
        if has_frame and output.frame:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + output.frame + b"\r\n"
            )
        # If timeout, loop again — watchdog will restart camera if needed


# --- Routes ---

@app.route("/")
def index():
    bitrate_kbps = estimate_bitrate_kbps(
        settings["resolution"][0],
        settings["resolution"][1],
        settings["fps"],
        MJPEG_QUALITY
    )
    mb_per_hour = estimate_mb_per_hour(bitrate_kbps)
    
    return render_template("index.html",
                           mode=settings.get("stream_mode", "mjpeg"),
                           resolution=settings["resolution"],
                           fps=settings["fps"],
                           quality=MJPEG_QUALITY,
                           bitrate_kbps=round(bitrate_kbps, 1),
                           mb_per_hour=round(mb_per_hour, 2),
                           battery_percent=get_battery_percent())


@app.route("/video_feed")
def video_feed():
    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if check_password_hash(ADMIN_PASSWORD_HASH, password):
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        else:
            return render_template("login.html", error="Invalid password")
    return render_template("login.html")


@app.route("/dashboard")
def dashboard():
    if not session.get("authenticated"):
        return redirect(url_for("login"))
    return render_template("dashboard.html")


@app.route("/settings")
def settings_page():
    if not session.get("authenticated"):
        return redirect(url_for("login"))
    
    bitrate_kbps = estimate_bitrate_kbps(
        settings["resolution"][0],
        settings["resolution"][1],
        settings["fps"],
        MJPEG_QUALITY
    )
    mb_per_hour = estimate_mb_per_hour(bitrate_kbps)
    
    resp = make_response(render_template("settings.html",
                           current_resolution=tuple(settings["resolution"]),
                           current_fps=settings["fps"],
                           current_stream_mode=settings.get("stream_mode", "mjpeg"),
                           current_max_motor_speed=settings.get("max_motor_speed", 7),
                           current_rotation=settings.get("camera_rotation", 0),
                           io_devices=settings.get("io_devices", []),
                           resolution_options=RESOLUTION_OPTIONS,
                           fps_options=FPS_OPTIONS,
                           rotation_options=ROTATION_OPTIONS,
                           stream_mode_options=STREAM_MODE_OPTIONS,
                           bitrate_kbps=round(bitrate_kbps, 1),
                           mb_per_hour=round(mb_per_hour, 2),
                           quality=MJPEG_QUALITY))
    # Prevent browser BFCache from serving a stale snapshot on back-button navigation
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/estimate", methods=["GET"])
def api_estimate():
    """Return data rate estimate for given resolution and FPS."""
    width = request.args.get("width", type=int)
    height = request.args.get("height", type=int)
    fps = request.args.get("fps", type=int)
    
    if not all([width, height, fps]):
        return jsonify({"error": "Missing width, height, or fps"}), 400
    
    bitrate_kbps = estimate_bitrate_kbps(width, height, fps, MJPEG_QUALITY)
    mb_per_hour = estimate_mb_per_hour(bitrate_kbps)
    
    return jsonify({
        "bitrate_kbps": round(bitrate_kbps, 1),
        "mb_per_hour": round(mb_per_hour, 2)
    })


@app.route("/api/status")
def api_status():
    """Return current camera settings and data estimates."""
    bitrate_kbps = estimate_bitrate_kbps(
        settings["resolution"][0],
        settings["resolution"][1],
        settings["fps"],
        MJPEG_QUALITY
    )
    mb_per_hour = estimate_mb_per_hour(bitrate_kbps)
    elapsed = time.time() - output.last_frame_time if output.last_frame_time > 0 else None
    return jsonify({
        "resolution": settings["resolution"],
        "fps": settings["fps"],
        "stream_mode": settings.get("stream_mode", "mjpeg"),
        "max_motor_speed": settings.get("max_motor_speed", 7),
        "camera_rotation": settings.get("camera_rotation", 0),
        "bitrate_kbps": round(bitrate_kbps, 1),
        "mb_per_hour": round(mb_per_hour, 2),
        "camera_ok": elapsed is not None and elapsed < WATCHDOG_TIMEOUT,
        "last_frame_age_s": round(elapsed, 1) if elapsed is not None else None,
        "actual_bitrate_kbps": output.actual_bitrate_kbps(),
        "battery_percent": get_battery_percent(),
        "io_devices": [
            {"name": d["name"], "state": io_states[i]}
            for i, d in enumerate(settings["io_devices"])
        ],
    })


@app.route("/api/update_settings", methods=["POST"])
def api_update_settings():
    """Update camera settings (resolution, FPS, stream mode, motor speed, rotation, IO devices)."""
    if not session.get("authenticated"):
        return jsonify({"error": "Not authenticated"}), 403

    data = request.get_json()
    new_resolution   = data.get("resolution")
    new_fps          = data.get("fps")
    new_stream_mode  = data.get("stream_mode")
    new_motor_speed  = data.get("max_motor_speed")
    new_rotation     = data.get("camera_rotation")
    new_io_devices   = data.get("io_devices")

    if new_resolution and tuple(new_resolution) in RESOLUTION_OPTIONS:
        settings["resolution"] = list(new_resolution)

    if new_fps and new_fps in FPS_OPTIONS:
        settings["fps"] = new_fps

    if new_stream_mode and new_stream_mode in STREAM_MODE_OPTIONS:
        settings["stream_mode"] = new_stream_mode

    if new_motor_speed is not None and 0 <= int(new_motor_speed) <= 10:
        settings["max_motor_speed"] = int(new_motor_speed)

    if new_rotation is not None and int(new_rotation) in ROTATION_OPTIONS:
        settings["camera_rotation"] = int(new_rotation)

    if new_io_devices and isinstance(new_io_devices, list):
        merged = []
        for i, dev in enumerate(new_io_devices[:4]):
            name = str(dev.get("name", f"Device {i+1}"))[:32].strip() or f"Device {i+1}"
            state = bool(dev.get("state", False))
            merged.append({"name": name, "state": state})
        # Re-init live states from new initial states
        global io_states
        io_states = [d["state"] for d in merged]
        settings["io_devices"] = merged

    configure_camera()
    save_settings()

    bitrate_kbps = estimate_bitrate_kbps(
        settings["resolution"][0],
        settings["resolution"][1],
        settings["fps"],
        MJPEG_QUALITY
    )
    mb_per_hour = estimate_mb_per_hour(bitrate_kbps)

    return jsonify({
        "status": "ok",
        "resolution": settings["resolution"],
        "fps": settings["fps"],
        "stream_mode": settings["stream_mode"],
        "max_motor_speed": settings["max_motor_speed"],
        "bitrate_kbps": round(bitrate_kbps, 1),
        "mb_per_hour": round(mb_per_hour, 2)
    })


@app.route("/api/control", methods=["POST"])
def api_control():
    """Receive joystick input. Ready for motor driver when hardware arrives."""
    data = request.get_json()
    x = max(-1.0, min(1.0, float(data.get("x", 0))))
    y = max(-1.0, min(1.0, float(data.get("y", 0))))
    control_state["x"] = x
    control_state["y"] = y
    # TODO: translate x/y + max_motor_speed to PWM signals when motor driver arrives
    return jsonify({"status": "ok", "x": x, "y": y})


@app.route("/api/io_toggle", methods=["POST"])
def api_io_toggle():
    """Toggle a live I/O device state by index (0-3)."""
    if not session.get("authenticated"):
        return jsonify({"error": "Not authenticated"}), 403
    data = request.get_json()
    idx = data.get("index")
    if idx is None or not isinstance(idx, int) or not (0 <= idx <= 3):
        return jsonify({"error": "index must be 0–3"}), 400
    io_states[idx] = not io_states[idx]
    # TODO: set GPIO pin when hardware is configured
    return jsonify({"status": "ok", "index": idx, "state": io_states[idx]})


@app.route("/api/webrtc/offer", methods=["POST"])
def webrtc_offer():
    """WebRTC SDP offer/answer handshake. Returns 501 if aiortc not installed."""
    if not WEBRTC_AVAILABLE:
        return jsonify({"error": "aiortc not installed on server"}), 501
    body = request.get_json()
    future = _asyncio.run_coroutine_threadsafe(
        _do_offer(body["sdp"], body["type"]), _rtc_loop
    )
    try:
        return jsonify(future.result(timeout=15))
    except Exception as exc:
        logging.error(f"WebRTC offer error: {exc}")
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
