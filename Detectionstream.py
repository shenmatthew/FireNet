import cv2
from ultralytics import YOLO
import time
import requests
import base64
import json
from datetime import datetime

CAMERA_IP = "10.0.1.160"
USERNAME = "admin"
PASSWORD = "85eC232@45"
WEIGHTS = "C:/Users/shenp/OneDrive/Desktop/Python Scripts/Firenet/fire_detection_v1-3/weights/best.pt"
CONFIDENCE_THRESHOLD = 0.20
SERVER_URL = "http://127.0.0.1:5000/alert"
COOLDOWN_SECONDS = 10

model = YOLO(WEIGHTS)
print("Model loaded")

stream_url = f"rtsp://{USERNAME}:{PASSWORD}@{CAMERA_IP}:554/stream1"
cap = cv2.VideoCapture(stream_url)

if not cap.isOpened():
    print("Failed to connect to stream")
    exit()

print("Stream connected, starting detection...")

last_alert_time = 0

while True:
    ret, frame = cap.read()
    if not ret:
        print("Failed to read frame")
        break

    results = model(frame, conf=CONFIDENCE_THRESHOLD, device=0)

    for result in results:
        boxes = result.boxes
        if len(boxes) > 0:
            current_time = time.time()
            if current_time - last_alert_time >= COOLDOWN_SECONDS:
                for box in boxes:
                    confidence = float(box.conf)
                    label = result.names[int(box.cls)]
                    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                    # Encode annotated frame as base64
                    annotated_frame = results[0].plot()
                    _, buffer = cv2.imencode('.jpg', annotated_frame)
                    frame_b64 = base64.b64encode(buffer).decode('utf-8')

                    # Send alert to server
                    payload = {
                        'timestamp': timestamp,
                        'confidence': round(confidence, 2),
                        'label': label,
                        'frame': frame_b64,
                        'camera_id': CAMERA_IP
                    }

                    try:
                        r = requests.post(SERVER_URL, json=payload, timeout=5)
                        print(f"Alert sent | {label} | {confidence:.2f} | {timestamp}")
                    except Exception as e:
                        print(f"Failed to send alert: {e}")

                    last_alert_time = current_time
                    break  # One alert per detection event

    annotated_frame = results[0].plot()
    annotated_frame = cv2.resize(annotated_frame, (1280, 720))
    cv2.imshow("Fire Detection", annotated_frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()