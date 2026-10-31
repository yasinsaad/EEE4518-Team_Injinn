# INJINN: IoT-based Driving Safety Suite with Oculomotor

Course project for **EEE 4518: Electrical and Electronic Workshop**, Department of EEE, Islamic University of Technology (IUT). Submitted 13 April 2026.

A dashboard-mounted unit watches the driver through a webcam and flags microsleep, yawning and distraction. The unit sounds a buzzer, shows a safety score on an OLED, and tilts the camera to keep the driver's face in frame. Each unit pushes its state, score and incident log to Firebase, and a web portal shows the whole fleet to an owner and each vehicle's own data to its driver.

![Prototype](docs/images/prototype.jpg)

Demo video: https://youtu.be/JEI0bmbcsGw

## Contents

- [Team](#team)
- [System overview](#system-overview)
- [Hardware](#hardware)
- [Repository layout](#repository-layout)
- [Detection and scoring](#detection-and-scoring)
- [Camera tilt control](#camera-tilt-control)
- [Raspberry Pi ↔ Arduino serial protocol](#raspberry-pi--arduino-serial-protocol)
- [Cloud data model](#cloud-data-model)
- [Fleet portal](#fleet-portal)
- [Demonstration video](#demonstration-video)
- [Setup and running](#setup-and-running)
- [Known limitations](#known-limitations)
- [Project history](#project-history)

## Team

| Member | ID | Contribution |
|---|---|---|
| Md. Jubair Ali Fahim | 220021203 | Initial Python codebase, alert system, SOS feature |
| Arham Takib | 220021208 | Web portal, Firebase backend, testing |
| Mubtasim Labib | 220021242 | Hardware prototype, 3D-printed enclosure, camera tilt control |
| Tashfeea Ainur Rahman | 220021250 | OLED interface, tilt tuning, Pi–Arduino serial protocol |
| Yasin Hasan Saad | 220021252 | MediaPipe vision pipeline, migration to Raspberry Pi, system integration |

## System overview

![Block diagram](docs/images/block-diagram.png)

| Layer | Runs on | File | Role |
|---|---|---|---|
| Vision and telemetry | Raspberry Pi 4 | `main.py` | Face landmark tracking, driver-state detection, scoring, camera tilt commands, Firebase sync |
| Peripheral control | Arduino Uno | `main.ino` | Servo, buzzer, OLED score display, SOS button |
| Fleet portal | Browser (hosted on Netlify) | `INJINN.html` | Owner and driver dashboards reading from Firebase |

Data flow:

1. The webcam feeds frames to `main.py`. MediaPipe Face Landmarker returns 478 landmarks and a head-pose matrix per frame.
2. `main.py` computes eye and mouth aspect ratios and head pitch and yaw. It updates the score and sends alert codes and servo angles to the Arduino over USB serial.
3. Every 0.5 s, `main.py` writes the vehicle's status to Firebase and sends the score to the Arduino for display.
4. The portal polls Firebase every second. An owner can send a "ping" to a vehicle, which `main.py` picks up and relays to the buzzer.
5. Pressing the SOS button makes the Arduino send `SOS_TRIGGERED`. `main.py` then sets the vehicle state to `EMERGENCY` and logs the event.

## Hardware

### Bill of materials

| Item | Purpose | Cost (BDT) |
|---|---|---|
| Raspberry Pi 4 | Runs vision pipeline and telemetry | 14,000 |
| Arduino Uno | Peripheral controller | 1,000 |
| 1.3" OLED, SH1106, I²C | Score display | 350 |
| Servo motor | Camera tilt | 400 |
| USB webcam (Logitech) | Driver camera | 2,300 |
| Buzzer, active-low | Audible alert | 80 |
| Power bank | Power supply | 1,500 |
| Breadboard | Wiring | 150 |
| 3D-printed housing | Enclosure | 900 |
| Push button | SOS input | – |
| **Total** | | **20,680** |

### Arduino pin map

| Pin | Connection |
|---|---|
| D9 | Servo signal |
| D8 | Buzzer (driven LOW to sound) |
| D7 | SOS button to GND (internal pull-up) |
| A4 / A5 (SDA / SCL) | OLED |
| USB | Raspberry Pi, appears as `/dev/ttyACM0`, 9600 baud |

The webcam connects to the Raspberry Pi over USB and is mounted on the servo arm outside the enclosure. The OLED and SOS button sit on the enclosure's side panel.

![Enclosure print and early test rig](docs/images/build.jpg)

## Repository layout

```
main.py          Vision pipeline, scoring, serial and Firebase I/O (Raspberry Pi, headless)
main.ino         Arduino firmware: servo, buzzer, OLED, SOS button
INJINN.html      Single-file fleet portal (Tailwind, Leaflet, Chart.js)
pc/injinn_pc.py  Desktop version with a preview window, no Arduino (used in the demo video)
prototypes/      Earlier development versions, see "Code history"
docs/images/     Figures used in this README
```

`main.py` and `pc/injinn_pc.py` share the same detection, scoring and Firebase code. The differences:

| | `main.py` | `pc/injinn_pc.py` |
|---|---|---|
| Target | Raspberry Pi + Arduino | Windows or any PC with a webcam |
| Preview window | none | "INJINN Fleet Edge (bus_N)" |
| Quit | Ctrl+C | `q` in the window |
| Camera tilt, buzzer, OLED, SOS | yes, over serial | no |
| Owner ping | Arduino buzzer + `winsound` | `winsound` beep + on-screen text |

Files created at runtime by `main.py`:

- `bus_id.txt` stores the vehicle ID (for example `bus_1`), which `main.py` asks for on first run.
- `face_landmarker.task` is the MediaPipe model, downloaded automatically if it is missing.

## Detection and scoring

Each shift starts at a score of 100. The shift is "Day" from 06:00 to 18:00 and "Night" otherwise. At a shift change, the score resets to 100 and a shift summary is logged.

| Event | Condition | Penalty | Arduino code | Portal state |
|---|---|---|---|---|
| Microsleep | EAR < 0.25 for > 2.0 s | −10 | `881` | `drowsy` |
| Yawn / fatigue | MAR > 0.5 for > 2.5 s | −2 | `883` | `drowsy` |
| Distraction | pitch > 15° or \|yaw\| > 25° for > 3.0 s | −3 | `882` | `distracted` |
| Speeding | estimated speed > 60 km/h | −1 per ~2 s | – | `speeding` |
| SOS button | button press, 3 s debounce | none | – | `EMERGENCY` |

After an alert fires, the same detector cannot fire again until the condition has held for another 5 s plus its hold time.

Definitions:

- **EAR (eye aspect ratio)**: the average over both eyes of (sum of two vertical eyelid distances) / (2 × horizontal eye width). The landmarks are 33, 160, 158, 133, 153, 144 for the left eye and 362, 385, 387, 263, 373, 380 for the right.
- **MAR (mouth aspect ratio)**: the inner-lip vertical gap (landmarks 13–14) divided by the mouth width (78–308).
- **Pitch and yaw**: taken from MediaPipe's facial transformation matrix.

![Microsleep, distraction and yawn alerts](docs/images/detection-alerts.jpg)

## Camera tilt control

The servo tilts the camera vertically. There is no pan. The controller is a step controller with a deadband:

- The tracked point is the nose-bridge landmark (index 1). Its vertical position is normalised to the frame, 0 at the top and 1 at the bottom.
- If the point is above 0.35, the servo steps 2.5° in one direction. If it is below 0.65, the servo steps 2.5° in the other. Between those values the servo holds still.
- The servo moves at most once every 0.1 s, and its angle is limited to 30–110°.
- If no face is visible for more than 1 s, the servo sweeps between the limits in 5° steps until it finds one.

## Raspberry Pi ↔ Arduino serial protocol

The link runs at 9600 baud. Each message is one ASCII line ending in `\n`.

| Pi → Arduino | Meaning |
|---|---|
| `1`–`180` | Set servo angle in degrees |
| `881`, `882`, `883` | Alert: microsleep, distraction, yawn. Gives a 200 ms beep. |
| `999` | Owner ping. Gives a 200 ms beep. |
| `1000 + score` | Update the OLED score (`1087` displays 87) |

| Arduino → Pi | Meaning |
|---|---|
| `SOS_TRIGGERED` | SOS button pressed |

## Cloud data model

The system uses a Firebase Realtime Database, accessed through its REST API without authentication:

```
fleet/
  <bus_id>/
    current_status   { state, speed_kmh, location: {lat, lng}, score, session }
    history/         push-ID → { event, penalty, time, date }
    command          { trigger: true | false }
```

- **`state`** takes one of these values: `active`, `drowsy`, `distracted`, `speeding`, `EMERGENCY`, `offline`.
- **`history`**: at each shift change, `main.py` deletes every entry except the "Shift Summary" lines, then appends a summary for the shift that just ended.
- **`command`**: the portal sets `trigger: true` to ping a vehicle. `main.py` polls this every 1.5 s, beeps, and resets it to `false`.

## Fleet portal

Hosted at https://marvelous-dolphin-44656c.netlify.app/. You can also open `INJINN.html` directly in a browser.

![Portal screens](docs/images/portal-screens.jpg)

The portal has two logins. These demo credentials are hardcoded in the page and protect nothing:

| Role | Access ID | Passcode | View |
|---|---|---|---|
| Owner | `admin` | `admin` | All vehicles |
| Driver | `<bus_id>`, e.g. `bus_1` | `driver` | Own vehicle only |

**Owner view:**
- A fleet list showing each vehicle's state, speed and live score, with a PING button.
- A map that can follow one vehicle.
- A per-vehicle incident bar chart.
- A leaderboard.

**Driver view:**
- The vehicle's live score.
- A map.
- Shift history.
- Audible alerts in the browser.

**Leaderboard:** each logged shift summary earns points by final score:

| Final shift score | Points |
|---|---|
| ≥ 80 | 3 |
| 56–79 | 2 |
| 40–55 | 1 |

Vehicles rank by total points, and ties go to the higher sum of shift scores.

The portal marks a vehicle `offline` if its status has not changed for 5 minutes.

## Demonstration video

[YouTube, 87 s](https://youtu.be/JEI0bmbcsGw). It shows three parts:

1. **Detection, 0–42 s.** `pc/injinn_pc.py` runs on a laptop in a window titled "INJINN Fleet Edge (bus_3)" shows the camera feed with the EAR/MAR, score and pitch/yaw overlay. Next to it, a phone shows the owner portal. The driver acts out distraction, microsleep and a yawn. The on-screen score falls 100 → 97 → 87 → 85 → 82 → 72, which matches the penalties in the table above (−3, −10, −2, −3, −10). Bus 3's portal state changes to `distracted` or `drowsy` with each alert.
2. **Camera tilt, 42–72 s.** The person crouches and stands in front of the unit, and the servo tilts the webcam to follow their face. The OLED shows the live score, which changes from 70 to 60 during this segment.
3. **SOS, 75–84 s.** Pressing the yellow button changes Bus 1 to `EMERGENCY` on the owner portal, and its map marker pulses.

![Microsleep alert with owner portal](docs/images/microsleep-demo.jpg)

![SOS button and portal emergency state](docs/images/sos-demo.jpg)

## Setup and running

### Raspberry Pi

```bash
pip install -r requirements.txt
python main.py
```

On first run, enter the vehicle ID (for example `bus_1`). The script opens camera index 1 and falls back to index 0 if that fails. It expects the Arduino on `/dev/ttyACM0`, so change the port in `main.py` for other systems. Press Ctrl+C to stop. On exit, the script writes `state: offline` to Firebase.

### PC version

```bash
pip install mediapipe opencv-python numpy requests
python pc/injinn_pc.py
```

Press `q` in the window to quit.

### Arduino

1. Install the **U8g2** library from the Arduino Library Manager. `Servo` and `Wire` come with the IDE.
2. Open `main.ino`, select Arduino Uno, and upload.
3. The OLED shows the INJINN start-up animation, then the score.

### Using another Firebase project

Replace `injinn-5dab3-default-rtdb.firebaseio.com` in `main.py` (`FIREBASE_BASE_URL`) and in `INJINN.html` (`FIREBASE_URL`, `FIREBASE_BASE`). The code reads and writes without authentication, so the database rules must allow public access to `fleet/`.

## Known limitations

Treat this as a classroom prototype, not a deployable product.

- **Location and speed**: these come from IP geolocation (`ip-api.com`) polled every 10 s, not GPS. Speed is the distance between successive IP fixes, so it is unreliable, and the speeding penalty can fire falsely or not at all.
- **Map position**: the portal ignores the reported coordinates and places every vehicle at a fixed point on the IUT campus.
- **Security**: the Firebase database is open, and the portal's logins are checked in client-side JavaScript. Anyone with the URL can read the fleet data or send pings.
- **Thresholds**: the EAR, MAR and head-pose thresholds were set by hand and tested on team members only. No accuracy or false-alarm measurements were recorded.
- **Unused overlay**: `main.py` still draws the EAR/score overlay on each frame but never displays it.
- **Bugs in `main.py`**:
  - `get_cloud_score()` runs before `STATUS_URL` is defined. Its bare `except` hides the error, and the score is set to 100 right after anyway, so the cloud score is never restored after a restart.
  - `listen_for_commands` and `listen_for_arduino_sos` start before the serial port opens. Errors are swallowed, so the first ping can be lost.
  - If the Arduino is missing, the main loop crashes on the first `arduino.write`. The code has no fallback.
  - The SOS debounce comment says 30 s, but the code uses 3 s.
  - `send_to_arduino()` and `current_servo_pos` are never used.
- **Bugs in `main.ino`**:
  - `triggerAlert()` and `alertMessage` are never used, so the OLED shows only the score and no alert text.
  - The 200 ms beep uses `delay()`, which blocks serial reads and button polling while the buzzer sounds.

## Project history

| Weeks | Work |
|---|---|
| 1–7 | Python and OpenCV environment for blink tracking |
| 8 | MediaPipe vision pipeline |
| 9–13 | Attempted deployment on an Nvidia Jetson Nano, abandoned due to faulty hardware |
| 14 | Web portal and Firebase backend |
| 15 | Moved to Raspberry Pi 4 and Arduino. Circuit design and camera tilt mount. |
| 16 | OLED, buzzer, SOS, serial protocol, integration, enclosure |

### Code history

The files in `prototypes/` record how the vision code developed. None of them talk to the Arduino.

| File | Stage |
|---|---|
| `01_blink_counter.py` | MediaPipe Tasks API test. Counts blinks with EAR < 0.25 for ≥ 2 frames. No alerts or cloud. |
| `02_microsleep_firebase.py` | First Firebase version, hardcoded to `bus_1`. Microsleep (eyes closed > 2 s, −10) plus a "repeated long blinks" rule: two consecutive blinks of 1–2 s cost −5. No yawn or distraction detection. This rule was dropped in later versions. |
| `03_auto_level_sim.py` | `pc/injinn_pc.py` plus a simulated camera-levelling loop. It tracks the eye-corner midpoint against a 0.45–0.55 band and adjusts a 0–1 value, but drives no hardware. `main.py` later replaced this with the servo controller described above. |
| `04_session_counter.py` | Alternative shift handling. Each run is numbered "Day N" or "Night N" (stored in `session_counts.json`), and the shift summary is written when the program exits, not at 06:00/18:00. |

None of the versions use the thresholds stated in the course report (EAR < 0.22 for 1.5 s, MAR 0.58). Every version uses EAR < 0.25, and every version with yawn detection uses MAR > 0.5 for 2.5 s.

The project was first proposed as software-only drowsiness detection. The hardware unit, portal and scoring were added during the semester.
