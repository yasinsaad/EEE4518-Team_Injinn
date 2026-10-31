/*
 * INJINN peripheral controller (Arduino Uno)
 *
 * Receives commands from the Raspberry Pi over USB serial and drives the
 * camera tilt servo, alert buzzer and OLED score display. Reports SOS button
 * presses back to the Pi.
 *
 * Wiring
 *   D9        servo signal
 *   D8        buzzer (active-low: LOW = sound)
 *   D7        SOS push button to GND (internal pull-up)
 *   A4 / A5   OLED SDA / SCL (1.3" SH1106, I2C)
 *
 * Serial protocol, 9600 baud, one integer per line
 *   1..180      set servo angle (degrees)
 *   881/882/883 alert beep (microsleep / distraction / yawn)
 *   999         owner ping beep
 *   1000+N      display score N
 *   -> Pi: "SOS_TRIGGERED" on button press
 *
 * Library: U8g2 (Arduino Library Manager)
 */

#include <U8g2lib.h>
#include <Wire.h>
#include <Servo.h>

U8G2_SH1106_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, /* reset=*/ U8X8_PIN_NONE);
Servo tiltServo;

const int SERVO_PIN = 9;
const int BUZZER_PIN = 8;
const int SOS_PIN = 7;

const unsigned long BEEP_MS = 200;
const unsigned long DEBOUNCE_MS = 50;
const unsigned long DISPLAY_PERIOD_MS = 50;   // 20 fps

int currentScore = 100;
bool lastButtonState = HIGH;
unsigned long lastButtonChange = 0;
unsigned long beepEndTime = 0;
unsigned long lastDisplayUpdate = 0;

void startBeep() {
  digitalWrite(BUZZER_PIN, LOW);
  beepEndTime = millis() + BEEP_MS;
}

void drawCentered(const char *text, int y) {
  u8g2.drawStr((128 - u8g2.getStrWidth(text)) / 2, y, text);
}

void setup() {
  Serial.begin(9600);
  Serial.setTimeout(10);   // short timeout so readStringUntil never stalls the loop

  tiltServo.attach(SERVO_PIN);
  tiltServo.write(90);

  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, HIGH);
  pinMode(SOS_PIN, INPUT_PULLUP);

  // Start-up animation: large logo slides up into the header position
  u8g2.begin();
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_logisoso24_tr);
  drawCentered("INJINN", 45);
  u8g2.sendBuffer();
  delay(1500);
  u8g2.setFont(u8g2_font_ncenB10_tr);
  for (int y = 45; y >= 12; y -= 3) {
    u8g2.clearBuffer();
    drawCentered("INJINN", y);
    u8g2.sendBuffer();
  }
}

void loop() {
  unsigned long now = millis();

  // 1. SOS button: report each press once (falling edge, debounced)
  bool buttonState = digitalRead(SOS_PIN);
  if (buttonState != lastButtonState && now - lastButtonChange > DEBOUNCE_MS) {
    if (buttonState == LOW) Serial.println("SOS_TRIGGERED");
    lastButtonState = buttonState;
    lastButtonChange = now;
  }

  // 2. Commands from the Pi
  while (Serial.available() > 0) {
    int val = Serial.readStringUntil('\n').toInt();
    if (val >= 1000) {
      currentScore = val - 1000;
    } else if (val == 999 || val == 881 || val == 882 || val == 883) {
      startBeep();
    } else if (val >= 1 && val <= 180) {
      tiltServo.write(val);
    }
  }

  // 3. End the beep without blocking the loop
  if (beepEndTime != 0 && now >= beepEndTime) {
    digitalWrite(BUZZER_PIN, HIGH);
    beepEndTime = 0;
  }

  // 4. OLED: header + live score
  if (now - lastDisplayUpdate > DISPLAY_PERIOD_MS) {
    u8g2.clearBuffer();
    u8g2.setFont(u8g2_font_ncenB10_tr);
    drawCentered("INJINN", 12);
    u8g2.drawLine(0, 15, 128, 15);
    u8g2.setFont(u8g2_font_logisoso32_tr);
    char scoreStr[6];
    itoa(currentScore, scoreStr, 10);
    drawCentered(scoreStr, 58);
    u8g2.sendBuffer();
    lastDisplayUpdate = now;
  }
}
