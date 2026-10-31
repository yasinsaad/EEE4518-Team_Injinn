"""
Prototype 4: per-run shift numbering.

Alternative shift handling: each run is numbered "Day N" or "Night N"
(stored in session_counts.json) and the shift summary is written when the
program exits, instead of at 06:00/18:00.
"""

import cv2
import mediapipe as mp
import math
import time
import os
import json
import urllib.request
import requests
import threading
from datetime import datetime
import numpy as np

# --- Windows Audio Alarm (Fails safely on Mac/Linux) ---
try:
    import winsound
except ImportError:
    winsound = None

# === 1. Identity, Config & TIME-BASED SESSIONS ===
CONFIG_FILE = "bus_id.txt"
SESSION_FILE = "session_counts.json"  # Tracks Day and Night runs separately

# Get Bus ID
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "r") as f:
        BUS_ID = f.read().strip()
else:
    BUS_ID = input("Enter Fleet ID (e.g., bus_1): ").strip()
    with open(CONFIG_FILE, "w") as f:
        f.write(BUS_ID)

# Load previous session counts
if os.path.exists(SESSION_FILE):
    with open(SESSION_FILE, "r") as f:
        counts = json.load(f)
else:
    counts = {"day": 0, "night": 0}

# Check real-world time to determine Day (6AM-6PM) or Night (6PM-6AM)
current_hour = datetime.now().hour
if 6 <= current_hour < 18:
    counts["day"] += 1
    current_session = f"Day {counts['day']}"
else:
    counts["night"] += 1
    current_session = f"Night {counts['night']}"

# Save updated counts back to the file
with open(SESSION_FILE, "w") as f: json.dump(counts, f)

print("\n" + "=" * 40)
print(f" 🚀 STARTING SHIFT: {current_session.upper()} for {BUS_ID.upper()}")
print("=" * 40 + "\n")

FIREBASE_BASE_URL = f"https://injinn-5dab3-default-rtdb.firebaseio.com/fleet/{BUS_ID}"
STATUS_URL = f"{FIREBASE_BASE_URL}/current_status.json"
HISTORY_URL = f"{FIREBASE_BASE_URL}/history.json"
COMMAND_URL = f"{FIREBASE_BASE_URL}/command.json"

SPEED_LIMIT_KMH = 60.0

current_db_state, driver_score = "active", 100
current_lat, current_lng, current_speed_kmh = 0.0, 0.0, 0.0
last_loc_time = time.time()
owner_ping_active_until = 0.0


# === 2. Telemetry & History Syncing ===
def fetch_real_location():
    global current_lat, current_lng, current_speed_kmh, last_loc_time
    while True:
        try:
            res = requests.get("http://ip-api.com/json/", timeout=5).json()
            if res['status'] == 'success':
                new_lat, new_lng, new_time = res['lat'], res['lon'], time.time()
                if current_lat != 0.0:
                    dt = (new_time - last_loc_time) / 3600.0
                    R = 6371.0
                    a = math.sin(math.radians(new_lat - current_lat) / 2) ** 2 + math.cos(
                        math.radians(current_lat)) * math.cos(math.radians(new_lat)) * math.sin(
                        math.radians(new_lng - current_lng) / 2) ** 2
                    speed = (R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))) / dt if dt > 0 else 0
                    current_speed_kmh = speed if speed < 200 else 0.0
                current_lat, current_lng, last_loc_time = new_lat, new_lng, new_time
        except:
            pass
        time.sleep(10)


def log_event(event_type, value):
    def _log():
        now = datetime.now()
        payload = {"event": event_type, "penalty": str(value), "time": now.strftime("%I:%M:%S %p"),
                   "date": now.strftime("%Y-%m-%d")}
        try:
            requests.post(HISTORY_URL, json=payload, timeout=5)
        except:
            pass

    threading.Thread(target=_log).start()


def listen_for_commands():
    global owner_ping_active_until
    while True:
        try:
            res = requests.get(COMMAND_URL, timeout=5).json()
            if res and res.get('trigger'):
                print("\n🚨 EMERGENCY PING FROM OWNER! 🚨\n")
                owner_ping_active_until = time.time() + 4.0
                try:
                    if winsound: winsound.Beep(2500, 1500)
                except:
                    print('\a')
                requests.put(COMMAND_URL, json={"trigger": False}, timeout=5)
        except:
            pass
        time.sleep(1.5)


threading.Thread(target=fetch_real_location, daemon=True).start()
threading.Thread(target=listen_for_commands, daemon=True).start()

# === 3. MediaPipe AI Setup ===
model_path = 'face_landmarker.task'
if not os.path.exists(model_path): urllib.request.urlretrieve(
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
    model_path)

opts = mp.tasks.vision.FaceLandmarkerOptions(
    base_options=mp.tasks.BaseOptions(model_asset_path=model_path), running_mode=mp.tasks.vision.RunningMode.VIDEO,
    num_faces=1, output_facial_transformation_matrixes=True
)
landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(opts)

LEFT_EYE, RIGHT_EYE = [33, 160, 158, 133, 153, 144], [362, 385, 387, 263, 373, 380]
INNER_LIP = [13, 14, 78, 308]


def aspect_ratio(lm, indices):
    p = [lm[i] for i in indices]
    if len(indices) == 6:
        return (math.hypot(p[1].x - p[5].x, p[1].y - p[5].y) + math.hypot(p[2].x - p[4].x, p[2].y - p[4].y)) / (
                    2.0 * math.hypot(p[0].x - p[3].x, p[0].y - p[3].y))
    elif len(indices) == 4:
        return math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) / math.hypot(p[2].x - p[3].x, p[2].y - p[3].y)


# === 4. Main Tracking Loop ===
state_times = {"eyes": 0.0, "yawn": 0.0, "distract": 0.0}
is_active = {"eyes": False, "yawn": False, "distract": False}
alert_text, alert_clear_time, last_sync = "", 0.0, time.time()

cap = cv2.VideoCapture(1)
if not cap.isOpened() or not cap.read()[0]: cap.release(); cap = cv2.VideoCapture(0)

try:
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret: break
        curr_time = time.time()

        if curr_time > alert_clear_time: alert_text, current_db_state = "", "active"

        # Speeding Logic
        if current_speed_kmh > SPEED_LIMIT_KMH and curr_time % 2 < 0.1:
            driver_score -= 1
            if current_db_state != "speeding":
                current_db_state, alert_text, alert_clear_time = "speeding", "OVERSPEEDING!", curr_time + 1.0
                log_event("Speeding Violation", "-1")

        mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        res = landmarker.detect_for_video(mp_img, int(curr_time * 1000))
        h, w, _ = frame.shape

        if res.face_landmarks:
            lm = res.face_landmarks[0]

            # --- VISUAL MARKINGS (Always On) ---
            for eye in [LEFT_EYE, RIGHT_EYE]:
                for i in eye: cv2.circle(frame, (int(lm[i].x * w), int(lm[i].y * h)), 2, (0, 255, 0), -1)
            for i in INNER_LIP: cv2.circle(frame, (int(lm[i].x * w), int(lm[i].y * h)), 2, (255, 0, 0), -1)

            # 1. Microsleep
            ear = (aspect_ratio(lm, LEFT_EYE) + aspect_ratio(lm, RIGHT_EYE)) / 2.0
            if ear < 0.25:
                if not is_active["eyes"]: is_active["eyes"], state_times["eyes"] = True, curr_time
                if curr_time - state_times["eyes"] > 2.0:
                    alert_text, alert_clear_time, current_db_state = "MICROSLEEP (>2s)!", curr_time + 4, "drowsy"
                    driver_score -= 10;
                    log_event("Microsleep Detected", "-10");
                    state_times["eyes"] = curr_time + 5
            else:
                is_active["eyes"] = False

            # 2. Yawning
            mar = aspect_ratio(lm, INNER_LIP)
            if mar > 0.5:
                if not is_active["yawn"]: is_active["yawn"], state_times["yawn"] = True, curr_time
                if curr_time - state_times["yawn"] > 2.5:
                    alert_text, alert_clear_time, current_db_state = "YAWN / FATIGUE!", curr_time + 3, "drowsy"
                    driver_score -= 2;
                    log_event("Severe Yawning", "-2");
                    state_times["yawn"] = curr_time + 5
            else:
                is_active["yawn"] = False

            # 3. Distraction (Head Pose)
            pitch, yaw = 0, 0
            if res.facial_transformation_matrixes:
                matrix = res.facial_transformation_matrixes[0]
                pitch = math.degrees(math.atan2(matrix[2, 1], matrix[2, 2]))
                yaw = math.degrees(math.atan2(-matrix[2, 0], math.sqrt(matrix[2, 1] ** 2 + matrix[2, 2] ** 2)))
                cv2.putText(frame, f"Pitch: {int(pitch)} | Yaw: {int(yaw)}", (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                            (255, 0, 255), 2)

                if pitch > 15 or abs(yaw) > 25:
                    if not is_active["distract"]: is_active["distract"], state_times["distract"] = True, curr_time
                    if curr_time - state_times["distract"] > 3.0:
                        alert_text, alert_clear_time, current_db_state = "DISTRACTED DRIVING!", curr_time + 3, "distracted"
                        driver_score -= 3;
                        log_event("Distraction (>3s)", "-3");
                        state_times["distract"] = curr_time + 5
                else:
                    is_active["distract"] = False

            cv2.putText(frame, f"EAR: {ear:.2f} | MAR: {mar:.2f}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0),
                        2)
            cv2.putText(frame, f"Score: {max(0, driver_score)}", (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255),
                        2)
            if alert_text: cv2.putText(frame, alert_text, (50, h // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 3)

        if curr_time < owner_ping_active_until:
            cv2.putText(frame, "!!! OWNER PING !!!", (w // 2 - 200, h // 2 - 50), cv2.FONT_HERSHEY_SIMPLEX, 1.5,
                        (0, 0, 255), 4)

        if curr_time - last_sync > 0.5:
            def _s():
                try:
                    requests.put(STATUS_URL, json={"state": current_db_state, "speed_kmh": current_speed_kmh,
                                                   "location": {"lat": current_lat, "lng": current_lng},
                                                   "score": max(0, driver_score), "session": current_session},
                                 timeout=2)
                except:
                    pass


            threading.Thread(target=_s).start()
            last_sync = curr_time

        cv2.imshow(f'INJINN Fleet Edge ({BUS_ID})', frame)

        # When user presses 'q', the loop breaks, and it drops into the "finally" block below to save the shift!
        if cv2.waitKey(5) & 0xFF == ord('q'):
            break

finally:
    print(f"\n🛑 ENDING SHIFT: {current_session}. Saving final score ({max(0, driver_score)}) to Firebase...")

    now = datetime.now()
    new_summary = {
        "event": f"Shift Summary ({current_session})",
        "penalty": f"Score: {max(0, driver_score)}",
        "time": now.strftime("%I:%M:%S %p"),
        "date": now.strftime("%Y-%m-%d")
    }
    try:
        requests.post(HISTORY_URL, json=new_summary, timeout=5)
    except Exception as e:
        print("Failed to save final shift score:", e)

    try:
        requests.put(STATUS_URL,
                     json={"state": "offline", "speed_kmh": 0, "location": {"lat": current_lat, "lng": current_lng},
                           "score": max(0, driver_score), "session": "Offline"})
    except:
        pass

    landmarker.close()
    cap.release()
    cv2.destroyAllWindows()
    time.sleep(1)