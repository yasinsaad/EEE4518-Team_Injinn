#include <U8g2lib.h>
#include <Wire.h>
#include <Servo.h>

// 1.3" OLED Driver (SH1106)
U8G2_SH1106_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, /* reset=*/ U8X8_PIN_NONE);

Servo s;
int lastAngle = 90;
const int buzzerPin = 8;     
const int sosButtonPin = 7;  
bool lastButtonState = HIGH;

int currentScore = 100;
String alertMessage = "";
unsigned long alertEndTime = 0;
unsigned long lastDisplayUpdate = 0;

void setup() {
  Serial.begin(9600);
  s.attach(9);
  
  // Higher timeout can cause lag, lower can split numbers. 10ms is the sweet spot.
  Serial.setTimeout(10); 
  
  u8g2.begin();
  pinMode(buzzerPin, OUTPUT);
  digitalWrite(buzzerPin, HIGH); // Assuming active-low buzzer; flip if needed
  pinMode(sosButtonPin, INPUT_PULLUP); 
  s.write(lastAngle);

  // --- STARTUP ANIMATION ---
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_logisoso24_tr);
  int bigWidth = u8g2.getStrWidth("INJINN");
  u8g2.drawStr((128 - bigWidth) / 2, 45, "INJINN");
  u8g2.sendBuffer();
  delay(1500); 

  // Transition animation
  for (int y = 45; y >= 12; y -= 3) {
    u8g2.clearBuffer();
    u8g2.setFont(u8g2_font_ncenB10_tr);
    int smallWidth = u8g2.getStrWidth("INJINN");
    u8g2.drawStr((128 - smallWidth) / 2, y, "INJINN");
    u8g2.sendBuffer();
  }
}

void loop() {
  // 1. SOS DETECTION
  bool currentButtonState = digitalRead(sosButtonPin);
  if (currentButtonState == LOW && lastButtonState == HIGH) {
    Serial.println("SOS_TRIGGERED"); 
    delay(50); 
  }
  lastButtonState = currentButtonState;

  // 2. FAST SERIAL PROCESSING (No more parseInt lag)
  while (Serial.available() > 0) {
    String data = Serial.readStringUntil('\n'); // Read the whole line at once
    int val = data.toInt(); // Convert to integer instantly
    
    if (val >= 1000) {
      currentScore = val - 1000;
    } 
    else if (val == 999 || val == 881 || val == 882 || val == 883) {
      digitalWrite(buzzerPin, LOW);
      delay(200); // Shorter beep so it doesn't lag the screen
      digitalWrite(buzzerPin, HIGH);
    } 
    else if (val >= 1 && val <= 180) {
      s.write(val);
      lastAngle = val;
    }
  }

  // 3. OLED DISPLAY REFRESH
  // We keep this at 50ms (20fps) so the screen stays smooth
  if (millis() - lastDisplayUpdate > 50) {
    u8g2.clearBuffer();
    
    u8g2.setFont(u8g2_font_ncenB10_tr);
    u8g2.drawStr((128 - u8g2.getStrWidth("INJINN")) / 2, 12, "INJINN");
    u8g2.drawLine(0, 15, 128, 15);
    
    u8g2.setFont(u8g2_font_logisoso32_tr);
    String scoreStr = String(currentScore);
    u8g2.drawStr((128 - u8g2.getStrWidth(scoreStr.c_str())) / 2, 58, scoreStr.c_str());
    
    u8g2.sendBuffer();
    lastDisplayUpdate = millis();
  }
}

void triggerAlert(String msg, int duration) {
  alertMessage = msg;
  alertEndTime = millis() + duration;
}
