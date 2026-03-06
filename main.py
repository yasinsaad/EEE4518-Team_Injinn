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
import serial  # Swapped gpiozero for serial

# === 1. Identity & Config ===
CONFIG_FILE = "bus_id.txt"

if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "r") as f:
        BUS_ID = f.read().strip()
else:
    BUS_ID = input("Enter Fleet ID (e.g., bus_1): ").strip()
    with open(CONFIG_FILE, "w") as f:
        f.write(BUS_ID)

# Function to fetch the actual score from the website
def get_cloud_score():
    try:
        res = requests.get(STATUS_URL, timeout=3).json()
        if res and "score" in res:
            return int(res["score"])
    except:
        return 100 # Fallback if internet is down

# Initialize the score from the website
driver_score = get_cloud_score()
print(f"System started. Current Cloud Score: {driver_score}")

FIREBASE_BASE_URL = f"https://injinn-5dab3-default-rtdb.firebaseio.com/fleet/{BUS_ID}"
STATUS_URL = f"{FIREBASE_BASE_URL}/current_status.json"
HISTORY_URL = f"{FIREBASE_BASE_URL}/history.json"
COMMAND_URL = f"{FIREBASE_BASE_URL}/command.json"

SPEED_LIMIT_KMH = 60.0

def get_current_session():
    return "Day" if 6 <= datetime.now().hour < 18 else "Night"

current_session = get_current_session()
current_db_state, driver_score = "active", 100
current_lat, current_lng, current_speed_kmh = 0.0, 0.0, 0.0
last_loc_time = time.time()
owner_ping_active_until = 0.0 
last_sos_time = 0

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
        except requests.RequestException:
            pass
        time.sleep(10)

def log_event(event_type, value):
    def _log():
        now = datetime.now()
        payload = {"event": event_type, "penalty": str(value), "time": now.strftime("%I:%M:%S %p"),
                   "date": now.strftime("%Y-%m-%d")}
        try:
            requests.post(HISTORY_URL, json=payload, timeout=5)
        except requests.RequestException:
            pass
    threading.Thread(target=_log).start()

def end_of_shift_reset(old_session, final_score):
    def _reset():
        try:
            res = requests.get(HISTORY_URL, timeout=5).json()
            retained_history = {}
            if res:
                for key, val in res.items():
                    if "Shift Summary" in val.get("event", ""):
                        retained_history[key] = val

            now = datetime.now()
            new_summary = {
                "event": f"Shift Summary ({old_session})",
                "penalty": f"Score: {final_score}",
                "time": now.strftime("%I:%M:%S %p"),
                "date": now.strftime("%Y-%m-%d")
            }

            requests.delete(HISTORY_URL, timeout=5)  
            if retained_history: requests.put(HISTORY_URL, json=retained_history, timeout=5)  
            requests.post(HISTORY_URL, json=new_summary, timeout=5)  
        except Exception:
            pass
    threading.Thread(target=_reset).start()

def listen_for_commands():
    global owner_ping_active_until
    while True:
        try:
            res = requests.get(COMMAND_URL, timeout=5).json()
            if res and res.get('trigger'):
                owner_ping_active_until = time.time() + 4.0
                
                # --- TO TRIGGER ARDUINO BUZZER & OLED ---
                try: arduino.write(b"999\n") 
                except: pass  
                
                try:
                    import winsound
                    winsound.Beep(2500, 1500)  
                except Exception:
                    print('\a')  
                requests.put(COMMAND_URL, json={"trigger": False}, timeout=5)
        except requests.RequestException:
            pass
        time.sleep(1.5)

def listen_for_arduino_sos():
    global current_db_state, alert_text, alert_clear_time, last_sos_time
    while True:
        try:
            if 'arduino' in globals() and arduino.in_waiting > 0:
                line = arduino.readline().decode('utf-8', errors='ignore').strip()
                
                if line == "SOS_TRIGGERED":
                    curr_time = time.time()
                    # Only trigger if 30 seconds have passed since the last one
                    if curr_time - last_sos_time > 3:
                        print("!!! EMERGENCY: SOS LOGGED !!!")
                        current_db_state = "EMERGENCY"
                        alert_text = "DRIVER SOS SENT!"
                        alert_clear_time = curr_time + 10.0
                        
                        # Log to history
                        log_event("DRIVER SOS PRESSED", "CRITICAL")
                        last_sos_time = curr_time
        except: 
            pass
        time.sleep(0.1)

threading.Thread(target=fetch_real_location, daemon=True).start()
threading.Thread(target=listen_for_commands, daemon=True).start()
threading.Thread(target=listen_for_arduino_sos, daemon=True).start()

# === 3. MediaPipe & Arduino Setup ===
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

# Initialize Arduino Serial instead of Local Servo
try:
    arduino = serial.Serial('/dev/ttyACM0', 9600, timeout=1)
    time.sleep(2)
except:
    print("Arduino not found on ACM0, check connection!")

current_servo_pos = 0.0 # Logic range: -1.0 to 1.0

def send_to_arduino(pos_value):
    """Converts -1.0/1.0 range to 0-180 degrees for Arduino"""
    if pos_value is None: return
    # Map -1.0 -> 0 deg, 0.0 -> 90 deg, 1.0 -> 180 deg
    angle = int((pos_value + 1) * 90)
    arduino.write(f"{angle}\n".encode())

# --- Tracking Settings ---
current_angle = 90      # Start at center (0-180 scale)
STEP_SIZE = 2.5           # Degrees to move
MOVE_DELAY = 0.1       # Speed of reaction
UPPER_THRESHOLD = 0.35 
LOWER_THRESHOLD = 0.65
SCAN_DELAY = 1        
MAX_LIMIT = 110         
MIN_LIMIT = 30          

last_move_time = 0
last_face_time = time.time()
search_direction = 1

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

        now_session = get_current_session()
        if now_session != current_session:
            end_of_shift_reset(current_session, driver_score)
            current_session, driver_score = now_session, 100
            alert_text, alert_clear_time = f"NEW SHIFT: {now_session.upper()}", curr_time + 5.0

        if curr_time > alert_clear_time: alert_text, current_db_state = "", "active"

        if current_speed_kmh > SPEED_LIMIT_KMH and curr_time % 2 < 0.1:
            driver_score -= 1
            if current_db_state != "speeding":
                current_db_state, alert_text, alert_clear_time = "speeding", "OVERSPEEDING!", curr_time + 1.0
                log_event("Speeding Violation", "-1")

        mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        res = landmarker.detect_for_video(mp_img, int(curr_time * 1000))
        h, w, _ = frame.shape
        can_move = (curr_time - last_move_time) > MOVE_DELAY

# --- ARDUINO SERVO LOGIC START ---
        if res.face_landmarks:
            lm = res.face_landmarks[0]
            last_face_time = curr_time
            eye_y = lm[1].y # Vertical position of eye/nose bridge

            if can_move:
                # If eye is above the top 25% threshold, move servo UP
                if eye_y < UPPER_THRESHOLD:
                    current_angle = max(MIN_LIMIT, current_angle - STEP_SIZE)
                    arduino.write(f"{int(current_angle)}\n".encode())
                    last_move_time = curr_time
                
                # If eye is below the bottom 75% threshold, move servo DOWN
                elif eye_y > LOWER_THRESHOLD:
                    current_angle = min(MAX_LIMIT, current_angle + STEP_SIZE)
                    arduino.write(f"{int(current_angle)}\n".encode())
                    last_move_time = curr_time
                
                # If eye is between 0.25 and 0.75, logic "seizes" (no code runs)
        else:
            # NO FACE DETECTED: Search mode (Up/Down scan)
            if curr_time - last_face_time > SCAN_DELAY:
                if can_move:
                    current_angle += (STEP_SIZE * 2 * search_direction)
                    if current_angle >= MAX_LIMIT:
                        current_angle = MAX_LIMIT
                        search_direction = -1
                    elif current_angle <= MIN_LIMIT:
                        current_angle = MIN_LIMIT
                        search_direction = 1
                    
                    arduino.write(f"{int(current_angle)}\n".encode())
                    last_move_time = curr_time
        # --- ARDUINO SERVO LOGIC END ---

        if res.face_landmarks:
            lm = res.face_landmarks[0]
            # --- VISUAL MARKINGS ---
            for eye in [LEFT_EYE, RIGHT_EYE]:
                for i in eye: cv2.circle(frame, (int(lm[i].x * w), int(lm[i].y * h)), 2, (0, 255, 0), -1)
            for i in INNER_LIP: cv2.circle(frame, (int(lm[i].x * w), int(lm[i].y * h)), 2, (255, 0, 0), -1)

            # --- AI LOGIC ---
            ear = (aspect_ratio(lm, LEFT_EYE) + aspect_ratio(lm, RIGHT_EYE)) / 2.0
            if ear < 0.25:
                if not is_active["eyes"]: is_active["eyes"], state_times["eyes"] = True, curr_time
                if curr_time - state_times["eyes"] > 2.0:
                    arduino.write(b"881\n")
                    alert_text, alert_clear_time, current_db_state = "MICROSLEEP (>2s)!", curr_time + 4, "drowsy"
                    driver_score -= 10
                    log_event("Microsleep Detected", "-10")
                    state_times["eyes"] = curr_time + 5
            else:
                is_active["eyes"] = False

            mar = aspect_ratio(lm, INNER_LIP)
            if mar > 0.5:
                if not is_active["yawn"]: is_active["yawn"], state_times["yawn"] = True, curr_time
                if curr_time - state_times["yawn"] > 2.5:
                    arduino.write(b"883\n")
                    alert_text, alert_clear_time, current_db_state = "YAWN / FATIGUE!", curr_time + 3, "drowsy"
                    driver_score -= 2
                    log_event("Severe Yawning", "-2")
                    state_times["yawn"] = curr_time + 5
            else:
                is_active["yawn"] = False

            if res.facial_transformation_matrixes:
                matrix = res.facial_transformation_matrixes[0]
                pitch = math.degrees(math.atan2(matrix[2, 1], matrix[2, 2]))
                yaw = math.degrees(math.atan2(-matrix[2, 0], math.sqrt(matrix[2, 1] ** 2 + matrix[2, 2] ** 2)))

                cv2.putText(frame, f"Pitch: {int(pitch)} | Yaw: {int(yaw)}", (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                            (255, 0, 255), 2)

                if pitch > 15 or abs(yaw) > 25:
                    if not is_active["distract"]: is_active["distract"], state_times["distract"] = True, curr_time
                    if curr_time - state_times["distract"] > 3.0:
                        arduino.write(b"882\n")
                        alert_text, alert_clear_time, current_db_state = "DISTRACTED DRIVING!", curr_time + 3, "distracted"
                        driver_score -= 3
                        log_event("Distraction (>3s)", "-3")
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
                    score_cmd = f"{1000 + max(0, driver_score)}\n"
                    arduino.write(score_cmd.encode())
                    
                except requests.RequestException:
                    pass
            threading.Thread(target=_s).start()
            last_sync = curr_time

finally:
    requests.put(STATUS_URL,
                 json={"state": "offline", "speed_kmh": 0, "location": {"lat": current_lat, "lng": current_lng},
                       "score": max(0, driver_score), "session": current_session})
    landmarker.close()
    cap.release()
    try: arduino.close()
    except: pass
    cv2.destroyAllWindows()