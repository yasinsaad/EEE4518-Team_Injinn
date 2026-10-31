"""
INJINN edge unit (Raspberry Pi 4).

Tracks the driver's face with MediaPipe Face Landmarker, detects microsleep,
yawning and distraction, keeps a per-shift safety score, tilts the camera to
keep the face in frame, and syncs state to Firebase. Talks to the Arduino
(servo, buzzer, OLED, SOS button) over USB serial.

Usage:
    python main.py                 # headless, as run on the Pi
    python main.py --preview       # also show the annotated camera window
    python main.py --port COM5     # different serial port

Serial protocol (one ASCII line per message, 9600 baud):
    Pi -> Arduino   1..180      servo angle in degrees
                    881/882/883 alert beep: microsleep / distraction / yawn
                    999         owner ping beep
                    1000+score  update OLED score
    Arduino -> Pi   SOS_TRIGGERED
"""

import argparse
import math
import os
import threading
import time
import urllib.request
from datetime import datetime

import cv2
import mediapipe as mp
import requests
import serial

# ============================================================================
# Configuration
# ============================================================================

FIREBASE_DB_URL = "https://injinn-5dab3-default-rtdb.firebaseio.com"
CONFIG_FILE = "bus_id.txt"            # stores this unit's fleet ID
MODEL_PATH = "face_landmarker.task"
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
             "face_landmarker/float16/1/face_landmarker.task")
BAUD_RATE = 9600

# Detection thresholds: (condition, seconds it must hold, score penalty)
EAR_THRESHOLD, EYES_HOLD_S, EYES_PENALTY = 0.25, 2.0, 10        # microsleep
MAR_THRESHOLD, YAWN_HOLD_S, YAWN_PENALTY = 0.5, 2.5, 2          # yawn
PITCH_LIMIT, YAW_LIMIT, DISTRACT_HOLD_S, DISTRACT_PENALTY = 15, 25, 3.0, 3
ALERT_COOLDOWN_S = 5.0          # extra wait before the same detector re-fires
SPEED_LIMIT_KMH = 60.0
SOS_DEBOUNCE_S = 3.0
SYNC_INTERVAL_S = 0.5           # Firebase status + OLED score update period

# Camera tilt (vertical only). Positions are normalised: 0 = top of frame.
TILT_UPPER, TILT_LOWER = 0.35, 0.65   # deadband for the tracked landmark
TILT_STEP_DEG = 2.5
TILT_MIN_DEG, TILT_MAX_DEG = 30, 110
TILT_MOVE_INTERVAL_S = 0.1
SCAN_AFTER_S = 1.0              # start sweeping if no face for this long

# MediaPipe Face Mesh landmark indices
LEFT_EYE = [33, 160, 158, 133, 153, 144]
RIGHT_EYE = [362, 385, 387, 263, 373, 380]
INNER_LIP = [13, 14, 78, 308]
NOSE_BRIDGE = 1

# Arduino alert codes
CODE_MICROSLEEP, CODE_DISTRACT, CODE_YAWN, CODE_PING = 881, 882, 883, 999

# ============================================================================
# Setup
# ============================================================================

parser = argparse.ArgumentParser(description="INJINN edge unit")
parser.add_argument("--preview", action="store_true", help="show camera window")
parser.add_argument("--port", default="/dev/ttyACM0", help="Arduino serial port")
args = parser.parse_args()

if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE) as f:
        BUS_ID = f.read().strip()
else:
    BUS_ID = input("Enter Fleet ID (e.g., bus_1): ").strip()
    with open(CONFIG_FILE, "w") as f:
        f.write(BUS_ID)

BUS_URL = f"{FIREBASE_DB_URL}/fleet/{BUS_ID}"
STATUS_URL = f"{BUS_URL}/current_status.json"
HISTORY_URL = f"{BUS_URL}/history.json"
COMMAND_URL = f"{BUS_URL}/command.json"


def get_current_session():
    """Day shift 06:00-18:00, Night shift otherwise."""
    return "Day" if 6 <= datetime.now().hour < 18 else "Night"


def get_cloud_score(session):
    """Resume the score from Firebase if the unit restarts mid-shift."""
    try:
        res = requests.get(STATUS_URL, timeout=3).json()
        if res and res.get("session") == session and "score" in res:
            return int(res["score"])
    except (requests.RequestException, ValueError):
        pass
    return 100


current_session = get_current_session()
driver_score = get_cloud_score(current_session)
print(f"{BUS_ID}: starting {current_session} shift at score {driver_score}")

current_db_state = "active"
current_lat, current_lng, current_speed_kmh = 0.0, 0.0, 0.0
last_loc_time = time.time()
owner_ping_active_until = 0.0
last_sos_time = 0.0
alert_text, alert_clear_time = "", 0.0

# --- Arduino link -----------------------------------------------------------
# The unit keeps running without the Arduino; commands are then dropped.
arduino = None
serial_lock = threading.Lock()
try:
    arduino = serial.Serial(args.port, BAUD_RATE, timeout=1)
    time.sleep(2)  # the Uno resets when the port opens
except serial.SerialException:
    print(f"Arduino not found on {args.port}; running without peripherals.")


def send_to_arduino(value):
    """Send one integer command line to the Arduino, if connected."""
    if arduino is None:
        return
    try:
        with serial_lock:
            arduino.write(f"{int(value)}\n".encode())
    except serial.SerialException:
        pass

# ============================================================================
# Background threads: location, history, owner commands, SOS button
# ============================================================================


def haversine_km(lat1, lng1, lat2, lng2):
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def fetch_location():
    """Approximate position and speed from IP geolocation every 10 s.

    This is a stand-in for GPS: accuracy is city-level at best.
    """
    global current_lat, current_lng, current_speed_kmh, last_loc_time
    while True:
        try:
            res = requests.get("http://ip-api.com/json/", timeout=5).json()
            if res.get("status") == "success":
                new_lat, new_lng, now = res["lat"], res["lon"], time.time()
                if current_lat != 0.0:
                    hours = (now - last_loc_time) / 3600.0
                    speed = haversine_km(current_lat, current_lng, new_lat, new_lng) / hours if hours > 0 else 0
                    current_speed_kmh = speed if speed < 200 else 0.0  # reject jumps
                current_lat, current_lng, last_loc_time = new_lat, new_lng, now
        except (requests.RequestException, ValueError, KeyError):
            pass
        time.sleep(10)


def log_event(event_type, value):
    """Append an incident to this unit's history in Firebase (non-blocking)."""
    def _log():
        now = datetime.now()
        payload = {"event": event_type, "penalty": str(value),
                   "time": now.strftime("%I:%M:%S %p"), "date": now.strftime("%Y-%m-%d")}
        try:
            requests.post(HISTORY_URL, json=payload, timeout=5)
        except requests.RequestException:
            pass
    threading.Thread(target=_log, daemon=True).start()


def end_of_shift_reset(old_session, final_score):
    """Replace the shift's incident log with one summary line.

    Earlier shift summaries are kept; the portal's leaderboard uses them.
    """
    def _reset():
        try:
            res = requests.get(HISTORY_URL, timeout=5).json() or {}
            kept = {k: v for k, v in res.items() if "Shift Summary" in v.get("event", "")}
            now = datetime.now()
            summary = {"event": f"Shift Summary ({old_session})", "penalty": f"Score: {final_score}",
                       "time": now.strftime("%I:%M:%S %p"), "date": now.strftime("%Y-%m-%d")}
            requests.delete(HISTORY_URL, timeout=5)
            if kept:
                requests.put(HISTORY_URL, json=kept, timeout=5)
            requests.post(HISTORY_URL, json=summary, timeout=5)
        except (requests.RequestException, ValueError):
            pass
    threading.Thread(target=_reset, daemon=True).start()


def listen_for_commands():
    """Poll for an owner ping from the portal and sound the buzzer."""
    global owner_ping_active_until
    while True:
        try:
            res = requests.get(COMMAND_URL, timeout=5).json()
            if res and res.get("trigger"):
                owner_ping_active_until = time.time() + 4.0
                send_to_arduino(CODE_PING)
                requests.put(COMMAND_URL, json={"trigger": False}, timeout=5)
        except (requests.RequestException, ValueError):
            pass
        time.sleep(1.5)


def listen_for_sos():
    """Read SOS_TRIGGERED lines from the Arduino and raise an emergency."""
    global current_db_state, alert_text, alert_clear_time, last_sos_time
    while arduino is not None:
        try:
            with serial_lock:
                line = arduino.readline().decode("utf-8", errors="ignore").strip() if arduino.in_waiting else ""
            if line == "SOS_TRIGGERED" and time.time() - last_sos_time > SOS_DEBOUNCE_S:
                print("SOS pressed")
                current_db_state = "EMERGENCY"
                alert_text, alert_clear_time = "DRIVER SOS SENT!", time.time() + 10.0
                log_event("DRIVER SOS PRESSED", "CRITICAL")
                last_sos_time = time.time()
        except serial.SerialException:
            pass
        time.sleep(0.1)


for target in (fetch_location, listen_for_commands, listen_for_sos):
    threading.Thread(target=target, daemon=True).start()

# ============================================================================
# Vision
# ============================================================================

if not os.path.exists(MODEL_PATH):
    urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)

landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(
    mp.tasks.vision.FaceLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=MODEL_PATH),
        running_mode=mp.tasks.vision.RunningMode.VIDEO,
        num_faces=1,
        output_facial_transformation_matrixes=True))


def aspect_ratio(lm, idx):
    """EAR for 6 eye points, MAR for 4 lip points (vertical / horizontal)."""
    p = [lm[i] for i in idx]
    if len(idx) == 6:
        vertical = math.dist((p[1].x, p[1].y), (p[5].x, p[5].y)) + math.dist((p[2].x, p[2].y), (p[4].x, p[4].y))
        return vertical / (2.0 * math.dist((p[0].x, p[0].y), (p[3].x, p[3].y)))
    return math.dist((p[0].x, p[0].y), (p[1].x, p[1].y)) / math.dist((p[2].x, p[2].y), (p[3].x, p[3].y))


def head_pose(matrix):
    """Pitch and yaw in degrees from MediaPipe's facial transformation matrix."""
    pitch = math.degrees(math.atan2(matrix[2, 1], matrix[2, 2]))
    yaw = math.degrees(math.atan2(-matrix[2, 0], math.hypot(matrix[2, 1], matrix[2, 2])))
    return pitch, yaw


# Per-detector state: when the condition started, and whether it is ongoing
state_times = {"eyes": 0.0, "yawn": 0.0, "distract": 0.0}
is_active = {"eyes": False, "yawn": False, "distract": False}


def check(name, condition, hold_s, now):
    """True once `condition` has held for `hold_s`; then arms the cooldown."""
    if not condition:
        is_active[name] = False
        return False
    if not is_active[name]:
        is_active[name], state_times[name] = True, now
    if now - state_times[name] > hold_s:
        state_times[name] = now + ALERT_COOLDOWN_S
        return True
    return False


def raise_alert(text, duration, state, penalty, event, code, now):
    global alert_text, alert_clear_time, current_db_state, driver_score
    send_to_arduino(code)
    alert_text, alert_clear_time, current_db_state = text, now + duration, state
    driver_score -= penalty
    log_event(event, f"-{penalty}")


# Camera tilt state
tilt_angle = 90
last_move_time = 0.0
last_face_time = time.time()
scan_direction = 1


def update_tilt(face_y, now):
    """Step the servo to keep `face_y` inside the deadband, or sweep if no face."""
    global tilt_angle, last_move_time, last_face_time, scan_direction
    if now - last_move_time <= TILT_MOVE_INTERVAL_S:
        if face_y is not None:
            last_face_time = now
        return
    if face_y is not None:
        last_face_time = now
        if face_y < TILT_UPPER:
            tilt_angle = max(TILT_MIN_DEG, tilt_angle - TILT_STEP_DEG)
        elif face_y > TILT_LOWER:
            tilt_angle = min(TILT_MAX_DEG, tilt_angle + TILT_STEP_DEG)
        else:
            return
    elif now - last_face_time > SCAN_AFTER_S:
        tilt_angle += 2 * TILT_STEP_DEG * scan_direction
        if tilt_angle >= TILT_MAX_DEG:
            tilt_angle, scan_direction = TILT_MAX_DEG, -1
        elif tilt_angle <= TILT_MIN_DEG:
            tilt_angle, scan_direction = TILT_MIN_DEG, 1
    else:
        return
    send_to_arduino(tilt_angle)
    last_move_time = now


def sync_status():
    """Push current status to Firebase and the score to the OLED."""
    score = max(0, driver_score)
    try:
        requests.put(STATUS_URL, json={"state": current_db_state, "speed_kmh": current_speed_kmh,
                                       "location": {"lat": current_lat, "lng": current_lng},
                                       "score": score, "session": current_session}, timeout=2)
    except requests.RequestException:
        pass
    send_to_arduino(1000 + score)

# ============================================================================
# Main loop
# ============================================================================


cap = cv2.VideoCapture(1)
if not cap.isOpened() or not cap.read()[0]:
    cap.release()
    cap = cv2.VideoCapture(0)

last_sync = time.time()
font = cv2.FONT_HERSHEY_SIMPLEX

try:
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        now = time.time()
        h, w, _ = frame.shape

        # Shift change: archive the finished shift and start again at 100
        if get_current_session() != current_session:
            end_of_shift_reset(current_session, driver_score)
            current_session, driver_score = get_current_session(), 100
            alert_text, alert_clear_time = f"NEW SHIFT: {current_session.upper()}", now + 5.0

        if now > alert_clear_time:
            alert_text, current_db_state = "", "active"

        # Speeding: about -1 per 2 s while over the limit
        if current_speed_kmh > SPEED_LIMIT_KMH and now % 2 < 0.1:
            driver_score -= 1
            if current_db_state != "speeding":
                current_db_state, alert_text, alert_clear_time = "speeding", "OVERSPEEDING!", now + 1.0
                log_event("Speeding Violation", "-1")

        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        res = landmarker.detect_for_video(image, int(now * 1000))
        lm = res.face_landmarks[0] if res.face_landmarks else None

        update_tilt(lm[NOSE_BRIDGE].y if lm else None, now)

        if lm:
            ear = (aspect_ratio(lm, LEFT_EYE) + aspect_ratio(lm, RIGHT_EYE)) / 2.0
            mar = aspect_ratio(lm, INNER_LIP)

            if check("eyes", ear < EAR_THRESHOLD, EYES_HOLD_S, now):
                raise_alert("MICROSLEEP (>2s)!", 4, "drowsy", EYES_PENALTY,
                            "Microsleep Detected", CODE_MICROSLEEP, now)
            if check("yawn", mar > MAR_THRESHOLD, YAWN_HOLD_S, now):
                raise_alert("YAWN / FATIGUE!", 3, "drowsy", YAWN_PENALTY,
                            "Severe Yawning", CODE_YAWN, now)

            pitch = yaw = 0.0
            if res.facial_transformation_matrixes:
                pitch, yaw = head_pose(res.facial_transformation_matrixes[0])
                if check("distract", pitch > PITCH_LIMIT or abs(yaw) > YAW_LIMIT, DISTRACT_HOLD_S, now):
                    raise_alert("DISTRACTED DRIVING!", 3, "distracted", DISTRACT_PENALTY,
                                "Distraction (>3s)", CODE_DISTRACT, now)

            if args.preview:
                for i in LEFT_EYE + RIGHT_EYE:
                    cv2.circle(frame, (int(lm[i].x * w), int(lm[i].y * h)), 2, (0, 255, 0), -1)
                for i in INNER_LIP:
                    cv2.circle(frame, (int(lm[i].x * w), int(lm[i].y * h)), 2, (255, 0, 0), -1)
                cv2.putText(frame, f"EAR: {ear:.2f} | MAR: {mar:.2f}", (10, 30), font, 0.6, (0, 255, 0), 2)
                cv2.putText(frame, f"Score: {max(0, driver_score)}", (10, 60), font, 0.6, (0, 255, 255), 2)
                cv2.putText(frame, f"Pitch: {int(pitch)} | Yaw: {int(yaw)}", (10, 90), font, 0.6, (255, 0, 255), 2)

        if args.preview:
            if alert_text:
                cv2.putText(frame, alert_text, (50, h // 2), font, 1.0, (0, 0, 255), 3)
            if now < owner_ping_active_until:
                cv2.putText(frame, "!!! OWNER PING !!!", (w // 2 - 200, h // 2 - 50), font, 1.5, (0, 0, 255), 4)
            cv2.imshow(f"INJINN Fleet Edge ({BUS_ID})", frame)
            if cv2.waitKey(5) & 0xFF == ord("q"):
                break

        if now - last_sync > SYNC_INTERVAL_S:
            threading.Thread(target=sync_status, daemon=True).start()
            last_sync = now

except KeyboardInterrupt:
    pass
finally:
    try:
        requests.put(STATUS_URL, json={"state": "offline", "speed_kmh": 0,
                                       "location": {"lat": current_lat, "lng": current_lng},
                                       "score": max(0, driver_score), "session": current_session}, timeout=3)
    except requests.RequestException:
        pass
    landmarker.close()
    cap.release()
    if arduino is not None:
        arduino.close()
    cv2.destroyAllWindows()
