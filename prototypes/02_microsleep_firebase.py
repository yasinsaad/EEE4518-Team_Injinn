"""
Prototype 2: first Firebase version.

Writes status and incidents to fleet/bus_1. Detects microsleep (eyes closed
> 2 s, -10) and repeated long blinks (two consecutive 1-2 s blinks, -5).
No yawn or distraction detection yet.
"""

import cv2
import mediapipe as mp
import math
import time
import os
import urllib.request
import requests
import threading
from datetime import datetime

# === 0. Configuration ===
# Replace YOUR-PROJECT-ID. Notice we are now writing to /fleet/bus_1
FIREBASE_BASE_URL = "https://injinn-5dab3-default-rtdb.firebaseio.com/fleet/bus_1"
STATUS_URL = f"{FIREBASE_BASE_URL}/current_status.json"
HISTORY_URL = f"{FIREBASE_BASE_URL}/history.json"

SPEED_LIMIT_KMH = 60.0

# --- State Variables ---
current_db_state = "active"
driver_score = 100
current_lat = 0.0
current_lng = 0.0
current_speed_kmh = 0.0
last_loc_time = time.time()


# === 1. Background Location Fetcher ===
def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    dLat, dLon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dLat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dLon / 2) ** 2
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


def fetch_real_location():
    global current_lat, current_lng, current_speed_kmh, last_loc_time
    while True:
        try:
            res = requests.get("http://ip-api.com/json/", timeout=5).json()
            if res['status'] == 'success':
                new_lat, new_lng, new_time = res['lat'], res['lon'], time.time()
                if current_lat != 0.0:
                    time_diff = (new_time - last_loc_time) / 3600.0
                    if time_diff > 0:
                        speed = calculate_distance(current_lat, current_lng, new_lat, new_lng) / time_diff
                        current_speed_kmh = speed if speed < 200 else 0.0
                current_lat, current_lng, last_loc_time = new_lat, new_lng, new_time
        except:
            pass
        time.sleep(10)


threading.Thread(target=fetch_real_location, daemon=True).start()


# === 2. Firebase Syncing ===
def update_firebase_state(state, speed, lat, lng, score):
    def _send():
        try:
            requests.put(STATUS_URL, json={
                "state": state, "speed_kmh": round(speed, 1),
                "location": {"lat": lat, "lng": lng}, "score": max(0, score)
            }, timeout=5)
        except:
            pass

    threading.Thread(target=_send).start()


def log_event_to_history(event_type, penalty):
    def _log():
        try:
            now = datetime.now()
            payload = {
                "event": event_type,
                "penalty": penalty,
                "time": now.strftime("%I:%M:%S %p"),
                "date": now.strftime("%Y-%m-%d")
            }
            requests.post(HISTORY_URL, json=payload, timeout=5)
        except:
            pass

    threading.Thread(target=_log).start()


# === 3. MediaPipe Setup ===
model_path = 'face_landmarker.task'
if not os.path.exists(model_path): urllib.request.urlretrieve(
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
    model_path)

LEFT_EYE = [33, 160, 158, 133, 153, 144]
RIGHT_EYE = [362, 385, 387, 263, 373, 380]


def calculate_ear(lm, indices):
    p = [lm[i] for i in indices]
    v1, v2, h = math.hypot(p[1].x - p[5].x, p[1].y - p[5].y), math.hypot(p[2].x - p[4].x, p[2].y - p[4].y), math.hypot(
        p[0].x - p[3].x, p[0].y - p[3].y)
    return (v1 + v2) / (2.0 * h)


options = mp.tasks.vision.FaceLandmarkerOptions(
    base_options=mp.tasks.BaseOptions(model_asset_path=model_path),
    running_mode=mp.tasks.vision.RunningMode.VIDEO, num_faces=1, min_face_detection_confidence=0.5,
    min_face_presence_confidence=0.5, min_tracking_confidence=0.5
)
landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(options)

# === 4. Variables & Camera ===
EAR_THRESHOLD = 0.25
blink_count, is_eyes_closed, eyes_closed_start_time = 0, False, 0.0
previous_long_blink, long_blink_alert_sent = False, False
alert_text, alert_clear_time, last_sync_time = "", 0.0, time.time()

cap = cv2.VideoCapture(1)
if not cap.isOpened() or not cap.read()[0]: cap.release(); cap = cv2.VideoCapture(0)

try:
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret: break

        current_time = time.time()

        if current_time > alert_clear_time and current_db_state != "active":
            alert_text = ""
            current_db_state = "active"

        # Speeding Logic
        if current_speed_kmh > SPEED_LIMIT_KMH and current_time % 2 < 0.1:
            driver_score -= 1
            if current_db_state != "speeding":
                current_db_state = "speeding"
                alert_text = "OVERSPEEDING!"
                alert_clear_time = current_time + 1.0
                log_event_to_history("Speeding Violation", "-1")

        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        detection_result = landmarker.detect_for_video(mp_image, int(current_time * 1000))

        h, w, _ = frame.shape

        if detection_result.face_landmarks:
            lm = detection_result.face_landmarks[0]

            # --- RESTORED: Draw Eye Landmarks (Green Dots) ---
            for eye_indices in [LEFT_EYE, RIGHT_EYE]:
                for i in eye_indices:
                    cx, cy = int(lm[i].x * w), int(lm[i].y * h)
                    cv2.circle(frame, (cx, cy), 2, (0, 255, 0), -1)

            avg_ear = (calculate_ear(lm, LEFT_EYE) + calculate_ear(lm, RIGHT_EYE)) / 2.0

            if avg_ear < EAR_THRESHOLD:
                if not is_eyes_closed:
                    is_eyes_closed, eyes_closed_start_time, long_blink_alert_sent = True, current_time, False

                closed_duration = current_time - eyes_closed_start_time

                # --- RESTORED: Show Duration Timer ---
                cv2.putText(frame, f"Closed: {closed_duration:.1f}s", (250, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                            (0, 0, 255), 2)

                if closed_duration > 2.0 and not long_blink_alert_sent:
                    alert_text, alert_clear_time, current_db_state = "CRITICAL: EYES CLOSED > 2s!", current_time + 4.0, "drowsy"
                    driver_score -= 10
                    previous_long_blink, long_blink_alert_sent = False, True
                    log_event_to_history("Microsleep (> 2s)", "-10")
            else:
                if is_eyes_closed:
                    closed_duration, is_eyes_closed = current_time - eyes_closed_start_time, False
                    if closed_duration >= 0.05:
                        blink_count += 1
                        if 1.0 <= closed_duration <= 2.0:
                            if previous_long_blink:
                                alert_text, alert_clear_time, current_db_state = "CRITICAL: REPEATED LONG BLINKS!", current_time + 4.0, "drowsy"
                                driver_score -= 5
                                previous_long_blink = False
                                log_event_to_history("Consecutive Long Blinks", "-5")
                            else:
                                previous_long_blink = True
                        elif closed_duration < 1.0:
                            previous_long_blink = False

            # --- RESTORED: Local UI Feedback with Blink Counter ---
            cv2.putText(frame, f"EAR: {avg_ear:.2f} | Blinks: {blink_count}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (255, 0, 0), 2)
            cv2.putText(frame, f"Score: {max(0, driver_score)}", (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255),
                        2)
            if alert_text: cv2.putText(frame, alert_text, (50, frame.shape[0] // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                                       (0, 0, 255), 3)

        if current_time - last_sync_time > 0.5 and current_lat != 0.0:
            update_firebase_state(current_db_state, current_speed_kmh, current_lat, current_lng, driver_score)
            last_sync_time = current_time

        cv2.imshow('INJINN Fleet Tracker', frame)
        if cv2.waitKey(5) & 0xFF == ord('q'): break
finally:
    update_firebase_state("offline", 0, current_lat, current_lng, driver_score)
    landmarker.close();
    cap.release();
    cv2.destroyAllWindows()