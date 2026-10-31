"""
Prototype 1: blink counter.

First test of the MediaPipe Tasks API. Counts blinks (EAR < 0.25 for at least
2 frames) and shows the EAR. No alerts, scoring or cloud sync.
"""

import cv2
import mediapipe as mp
import math
import time
import os
import urllib.request

# === 0. Download Model (Required for Tasks API) ===
model_path = 'face_landmarker.task'
if not os.path.exists(model_path):
    print(f"Downloading {model_path}...")
    url = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
    urllib.request.urlretrieve(url, model_path)
    print("Download complete.")

# === 1. Define Landmarks (Same Indices) ===
LEFT_EYE = [33, 160, 158, 133, 153, 144]
RIGHT_EYE = [362, 385, 387, 263, 373, 380]

def calculate_ear(landmarks, indices):
    # Note: landmarks is now a list of NormalizedLandmark objects
    p = [landmarks[i] for i in indices]

    # Euclidean distance
    v1 = math.hypot(p[1].x - p[5].x, p[1].y - p[5].y)
    v2 = math.hypot(p[2].x - p[4].x, p[2].y - p[4].y)
    h = math.hypot(p[0].x - p[3].x, p[0].y - p[3].y)

    ear = (v1 + v2) / (2.0 * h)
    return ear


# === 2. Setup MediaPipe Tasks ===
BaseOptions = mp.tasks.BaseOptions
FaceLandmarker = mp.tasks.vision.FaceLandmarker
FaceLandmarkerOptions = mp.tasks.vision.FaceLandmarkerOptions
VisionRunningMode = mp.tasks.vision.RunningMode

options = FaceLandmarkerOptions(
    base_options=BaseOptions(model_asset_path=model_path),
    running_mode=VisionRunningMode.VIDEO, # optimized for video streams
    num_faces=1,
    min_face_detection_confidence=0.5,
    min_face_presence_confidence=0.5,
    min_tracking_confidence=0.5,
    output_face_blendshapes=False,
    output_facial_transformation_matrixes=False
)

# Initialize Landmarker
landmarker = FaceLandmarker.create_from_options(options)

# === 3. Parameters ===
EAR_THRESHOLD = 0.25
CONSEC_FRAMES = 2

# === 4. Camera Setup ===
cap = cv2.VideoCapture(1)
if not cap.isOpened() or not cap.read()[0]:
    print("Camera 1 unavailable/dark. Switching to Camera 0...")
    cap.release()
    cap = cv2.VideoCapture(0)

blink_count = 0
frame_counter = 0 # For EAR logic
global_timestamp = 0 # For MediaPipe timing

print("Window opening... (Check taskbar if hidden)")

try:
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            print("Failed to grab frame.")
            break

        # Convert to MediaPipe Image
        # MediaPipe Tasks requires RGB input as an mp.Image object
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)

        # Detect
        # We must provide a timestamp in ms that increases monotonically
        current_timestamp_ms = int(time.time() * 1000)
        detection_result = landmarker.detect_for_video(mp_image, current_timestamp_ms)

        h, w, _ = frame.shape

        # Check if landmarks were found
        if detection_result.face_landmarks:
            # face_landmarks is a list of lists (one list per face)
            # We only asked for num_faces=1, so we take index 0
            lm = detection_result.face_landmarks[0]

            # --- VISUALIZATION ---
            for eye_indices in [LEFT_EYE, RIGHT_EYE]:
                for i in eye_indices:
                    point = lm[i]
                    cx, cy = int(point.x * w), int(point.y * h)
                    cv2.circle(frame, (cx, cy), 2, (0, 255, 0), -1)

            # --- EAR CALCULATION ---
            left_ear = calculate_ear(lm, LEFT_EYE)
            right_ear = calculate_ear(lm, RIGHT_EYE)
            avg_ear = (left_ear + right_ear) / 2.0

            # Logic
            if avg_ear < EAR_THRESHOLD:
                frame_counter += 1
                cv2.putText(frame, "EYES CLOSED", (250, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
            else:
                if frame_counter >= CONSEC_FRAMES:
                    blink_count += 1
                frame_counter = 0

            # Stats
            cv2.putText(frame, f"Blinks: {blink_count}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            cv2.putText(frame, f"EAR: {avg_ear:.2f}", (10, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 0), 2)

        cv2.imshow('MediaPipe Blink Tracker (Tasks API)', frame)

        if cv2.waitKey(5) & 0xFF == ord('q'):
            break

finally:
    # Cleanup
    landmarker.close()
    cap.release()
    cv2.destroyAllWindows()