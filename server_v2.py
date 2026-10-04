from flask import Flask, request, render_template_string, jsonify, Response, session, redirect
from functools import wraps
import os
import base64
import sqlite3
import cv2
import threading
import time
import math
from datetime import datetime

from ultralytics import YOLO

app = Flask(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────

app.secret_key = 'changethis_to_something_random'
USERNAME = "admin"
PASSWORD = "85eC232@45"

SNAPSHOT_DIR = "C:/Users/shenp/OneDrive/Desktop/Python Scripts/Firenet/snapshots"
DB_PATH = "C:/Users/shenp/OneDrive/Desktop/Python Scripts/Firenet/alerts.db"
WEIGHTS = "C:/Users/shenp/OneDrive/Desktop/Python Scripts/Firenet/fire_detection_v1-3/weights/best.pt"


CONFIDENCE_THRESHOLD = 0.5
COOLDOWN_SECONDS = 10
MAX_DISTANCE_METERS = 3219  # 2 miles
COMBINED_ALERT_WINDOW_SECONDS = 60

os.makedirs(SNAPSHOT_DIR, exist_ok=True)

# ─── Model ────────────────────────────────────────────────────────────────────

print("Loading model...")
model = YOLO(WEIGHTS)
model_lock = threading.Lock()
print("Model loaded")

# ─── Camera Stream State ──────────────────────────────────────────────────────

camera_streams = {}
camera_streams_lock = threading.Lock()

# ─── Database ─────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            confidence REAL,
            label TEXT,
            camera_id TEXT,
            snapshot_path TEXT
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cameras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id TEXT UNIQUE,
            name TEXT,
            latitude REAL,
            longitude REAL,
            rtsp_url TEXT
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS combined_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            camera_id_1 TEXT,
            camera_id_2 TEXT,
            time_gap_seconds REAL,
            distance_meters REAL,
            priority_score REAL,
            priority_level TEXT,
            alert_id_1 INTEGER,
            alert_id_2 INTEGER
        )
    ''')
    conn.commit()
    conn.close()

def migrate_db():
    conn = get_db()
    cursor = conn.execute("PRAGMA table_info(cameras)")
    columns = [row[1] for row in cursor.fetchall()]
    if 'latitude' not in columns:
        conn.execute("ALTER TABLE cameras ADD COLUMN latitude REAL")
    if 'longitude' not in columns:
        conn.execute("ALTER TABLE cameras ADD COLUMN longitude REAL")
    if 'rtsp_url' not in columns:
        conn.execute("ALTER TABLE cameras ADD COLUMN rtsp_url TEXT")
    conn.commit()
    conn.close()

def register_camera(camera_id):
    conn = get_db()
    conn.execute('''
        INSERT OR IGNORE INTO cameras (camera_id, name, latitude, longitude, rtsp_url)
        VALUES (?, ?, NULL, NULL, NULL)
    ''', (camera_id, camera_id))
    conn.commit()
    conn.close()

def get_cameras():
    conn = get_db()
    rows = conn.execute('SELECT * FROM cameras ORDER BY name').fetchall()
    conn.close()
    return rows

def update_camera(camera_id, name, latitude, longitude, rtsp_url):
    conn = get_db()
    conn.execute('''
        UPDATE cameras SET name=?, latitude=?, longitude=?, rtsp_url=?
        WHERE camera_id=?
    ''', (name, latitude, longitude, rtsp_url, camera_id))
    conn.commit()
    conn.close()

def delete_camera(camera_id):
    conn = get_db()
    conn.execute('DELETE FROM cameras WHERE camera_id=?', (camera_id,))
    conn.commit()
    conn.close()

def get_alerts(camera_id=None):
    conn = get_db()
    if camera_id:
        rows = conn.execute(
            'SELECT * FROM alerts WHERE camera_id=? ORDER BY id DESC', (camera_id,)
        ).fetchall()
    else:
        rows = conn.execute('SELECT * FROM alerts ORDER BY id DESC').fetchall()
    conn.close()
    return rows

def get_camera_status():
    conn = get_db()
    rows = conn.execute('''
        SELECT c.camera_id, c.name, c.latitude, c.longitude,
               MAX(a.timestamp) as last_alert,
               MAX(a.confidence) as last_confidence
        FROM cameras c
        LEFT JOIN alerts a ON c.camera_id = a.camera_id
        GROUP BY c.camera_id
    ''').fetchall()
    conn.close()
    return rows

def get_combined_alerts():
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM combined_alerts ORDER BY id DESC'
    ).fetchall()
    conn.close()
    return rows

def get_recent_alerts_from_other_cameras(camera_id, within_seconds=60):
    conn = get_db()
    rows = conn.execute('''
        SELECT a.id, a.timestamp, a.camera_id, c.latitude, c.longitude
        FROM alerts a
        JOIN cameras c ON a.camera_id = c.camera_id
        WHERE a.camera_id != ?
        AND a.timestamp >= datetime('now', ?, 'localtime')
        AND c.latitude IS NOT NULL
        AND c.longitude IS NOT NULL
    ''', (camera_id, f'-{within_seconds} seconds')).fetchall()
    conn.close()
    return rows

def insert_combined_alert(timestamp, camera_id_1, camera_id_2, time_gap_seconds,
                           distance_meters, priority_score, priority_level,
                           alert_id_1, alert_id_2):
    conn = get_db()
    conn.execute('''
        INSERT INTO combined_alerts (timestamp, camera_id_1, camera_id_2, time_gap_seconds,
                                     distance_meters, priority_score, priority_level,
                                     alert_id_1, alert_id_2)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (timestamp, camera_id_1, camera_id_2, time_gap_seconds, distance_meters,
          priority_score, priority_level, alert_id_1, alert_id_2))
    conn.commit()
    conn.close()

def insert_alert(timestamp, confidence, label, camera_id, snapshot_path):
    conn = get_db()
    cursor = conn.execute('''
        INSERT INTO alerts (timestamp, confidence, label, camera_id, snapshot_path)
        VALUES (?, ?, ?, ?, ?)
    ''', (timestamp, confidence, label, camera_id, snapshot_path))
    new_alert_id = cursor.lastrowid
    conn.commit()
    conn.close()
    check_combined_alert(new_alert_id, camera_id, timestamp)

# ─── Priority Scoring ─────────────────────────────────────────────────────────

def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def calculate_priority(time_gap_seconds, distance_meters):
    time_score = 1 - (time_gap_seconds / COMBINED_ALERT_WINDOW_SECONDS)
    distance_score = 1 - (distance_meters / MAX_DISTANCE_METERS)
    time_score = max(0, min(1, time_score))
    distance_score = max(0, min(1, distance_score))
    priority_score = (time_score + distance_score) / 2
    if priority_score >= 0.8:
        priority_level = 'Critical'
    elif priority_score >= 0.5:
        priority_level = 'High'
    else:
        priority_level = 'Elevated'
    return priority_score, priority_level

def check_combined_alert(new_alert_id, new_camera_id, new_timestamp):
    conn = get_db()
    new_camera = conn.execute(
        'SELECT latitude, longitude FROM cameras WHERE camera_id=?', (new_camera_id,)
    ).fetchone()
    conn.close()

    if not new_camera or not new_camera['latitude']:
        return

    recent = get_recent_alerts_from_other_cameras(
        new_camera_id, within_seconds=COMBINED_ALERT_WINDOW_SECONDS
    )

    for other in recent:
        if not other['latitude']:
            continue

        distance = haversine_distance(
            new_camera['latitude'], new_camera['longitude'],
            other['latitude'], other['longitude']
        )

        new_dt = datetime.strptime(new_timestamp, '%Y-%m-%d %H:%M:%S')
        other_dt = datetime.strptime(other['timestamp'], '%Y-%m-%d %H:%M:%S')
        time_gap = abs((new_dt - other_dt).total_seconds())

        priority_score, priority_level = calculate_priority(time_gap, distance)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        insert_combined_alert(
            timestamp, new_camera_id, other['camera_id'],
            time_gap, distance, priority_score, priority_level,
            new_alert_id, other['id']
        )
        print(f"COMBINED ALERT | {priority_level} | Score: {priority_score:.2f} | "
              f"Cameras: {new_camera_id} + {other['camera_id']} | "
              f"Distance: {distance:.0f}m | Time gap: {time_gap:.1f}s")

# ─── Camera Stream ────────────────────────────────────────────────────────────

class CameraStream:
    def __init__(self, camera_id, rtsp_url, camera_name):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.camera_name = camera_name
        self.raw_frame = None
        self.annotated_frame = None
        self.frame_lock = threading.Lock()
        self.inference_lock = threading.Lock()
        self.running = False
        self.last_alert_time = 0
        self.capture_thread = None
        self.inference_thread = None
        self.inference_frame = None
        self.frame_count = 0
        self.INFERENCE_EVERY_N = 5

    def start(self):
        if not self.running:
            self.running = True
            self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
            self.inference_thread = threading.Thread(target=self._inference_loop, daemon=True)
            self.capture_thread.start()
            self.inference_thread.start()
            print(f"Stream started: {self.camera_id}")

    def stop(self):
        self.running = False
        print(f"Stream stopped: {self.camera_id}")

    def _capture_loop(self):
        cap = cv2.VideoCapture(self.rtsp_url)
        if not cap.isOpened():
            print(f"Failed to connect to stream: {self.camera_id}")
            self.running = False
            return

        while self.running:
            ret, frame = cap.read()
            if not ret:
                print(f"Lost connection: {self.camera_id}, retrying...")
                cap.release()
                time.sleep(5)
                cap = cv2.VideoCapture(self.rtsp_url)
                continue

            self.frame_count += 1

            with self.frame_lock:
                self.raw_frame = frame.copy()

            if self.frame_count % self.INFERENCE_EVERY_N == 0:
                with self.inference_lock:
                    self.inference_frame = frame.copy()

        cap.release()

    def _inference_loop(self):
        while self.running:
            frame = None
            with self.inference_lock:
                if self.inference_frame is not None:
                    frame = self.inference_frame.copy()
                    self.inference_frame = None

            if frame is None:
                time.sleep(0.01)
                continue

            with model_lock:
                results = model(frame, conf=CONFIDENCE_THRESHOLD, device=0, verbose=False)

            annotated = results[0].plot()

            with self.frame_lock:
                self.annotated_frame = annotated.copy()

            boxes = results[0].boxes
            if len(boxes) > 0:
                current_time = time.time()
                if current_time - self.last_alert_time >= COOLDOWN_SECONDS:
                    self.last_alert_time = current_time
                    box = boxes[0]
                    confidence = float(box.conf)
                    label = results[0].names[int(box.cls)]
                    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    filename = f"{SNAPSHOT_DIR}/alert_{timestamp.replace(':', '-').replace(' ', '_')}.jpg"
                    cv2.imwrite(filename, annotated)
                    insert_alert(timestamp, confidence, label, self.camera_id, filename)
                    print(f"ALERT | {label} | {confidence:.2f} | {self.camera_id} | {timestamp}")

    def get_jpeg_frame(self):
        with self.frame_lock:
            if self.annotated_frame is not None:
                frame = self.annotated_frame
            elif self.raw_frame is not None:
                frame = self.raw_frame
            else:
                return None
            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            return buffer.tobytes()

def get_or_start_stream(camera_id, rtsp_url, camera_name):
    with camera_streams_lock:
        if camera_id not in camera_streams:
            stream = CameraStream(camera_id, rtsp_url, camera_name)
            camera_streams[camera_id] = stream
    stream = camera_streams[camera_id]
    if not stream.running:
        stream.rtsp_url = rtsp_url
        stream.camera_name = camera_name
        stream.start()
    return stream

def generate_frames(camera_id):
    stream = camera_streams.get(camera_id)
    if not stream:
        return
    while True:
        frame = stream.get_jpeg_frame()
        if frame:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        else:
            time.sleep(0.033)

# ─── Auth ─────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

# ─── HTML ─────────────────────────────────────────────────────────────────────

NAV = '''
<div style="display:flex; gap:10px; margin-bottom:20px;">
    <a href="/" style="background:#2a2a2a; color:white; border:1px solid #444; padding:10px 20px;
       border-radius:4px; text-decoration:none; font-size:14px;">Dashboard</a>
    <a href="/live" style="background:#2a2a2a; color:white; border:1px solid #444; padding:10px 20px;
       border-radius:4px; text-decoration:none; font-size:14px;">Live View</a>
    <a href="/map" style="background:#2a2a2a; color:white; border:1px solid #444; padding:10px 20px;
       border-radius:4px; text-decoration:none; font-size:14px;">Map</a>
    <a href="/settings" style="background:#444; color:white; border:none; padding:10px 20px;
       border-radius:4px; text-decoration:none; font-size:14px;">Settings</a>
    <a href="/logout" style="background:#333; color:#aaa; border:1px solid #333; padding:10px 20px;
       border-radius:4px; text-decoration:none; font-size:14px; margin-left:auto;">Logout</a>
</div>
'''

LOGIN_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Login - Fire Detection</title>
    <style>
        body { font-family: Arial, sans-serif; background: #1a1a1a; color: #fff;
               display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .box { background: #2a2a2a; padding: 40px; border-radius: 8px; border: 1px solid #444; width: 300px; }
        h1 { color: #ff4444; margin-top: 0; }
        input { width: 100%; background: #1a1a1a; border: 1px solid #444; color: #fff;
                padding: 10px; border-radius: 4px; margin-bottom: 12px; box-sizing: border-box; }
        button { width: 100%; background: #ff4444; color: white; border: none;
                 padding: 10px; border-radius: 4px; cursor: pointer; font-size: 16px; }
        .error { color: #ff4444; font-size: 14px; margin-bottom: 10px; }
    </style>
</head>
<body>
    <div class="box">
        <h1>Fire Detection</h1>
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="POST">
            <input type="text" name="username" placeholder="Username" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login</button>
        </form>
    </div>
</body>
</html>
'''

DASHBOARD_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Fire Detection Dashboard</title>
    <style>
        body { font-family: Arial, sans-serif; background: #1a1a1a; color: #fff; padding: 20px; }
        h1 { color: #ff4444; }
        h2 { color: #ff4444; margin: 0; }
        .tabs { display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }
        .tab { background: #2a2a2a; border: 1px solid #444; color: #fff; padding: 8px 16px;
               border-radius: 4px; cursor: pointer; font-size: 14px; }
        .tab.active { background: #ff4444; border-color: #ff4444; }
        .alert-count { font-size: 18px; color: #aaa; }
        .status { font-size: 14px; color: #aaa; margin-bottom: 10px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 20px; }
        .card { background: #2a2a2a; border-radius: 8px; padding: 15px; border: 1px solid #444; }
        .card img { width: 100%; border-radius: 4px; }
        .timestamp { color: #aaa; font-size: 14px; margin-top: 8px; }
        .confidence { color: #ff4444; font-weight: bold; font-size: 16px; margin-top: 4px; }
        .camera { color: #4a9eff; font-size: 14px; margin-top: 4px; }
        .delete-btn { margin-top: 10px; background: #444; color: #fff; border: none;
                      padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 13px; }
        .delete-btn:hover { background: #ff4444; }
        .clear-btn { background: #333; color: #aaa; border: 1px solid #444; padding: 8px 16px;
                     border-radius: 4px; cursor: pointer; font-size: 14px; }
        .clear-btn:hover { background: #444; color: #fff; }
        .combined-card { background: #2a2a2a; border-radius: 8px; padding: 15px; border: 2px solid #ff4444; }
        .combined-card.priority-critical { border-color: #ff0000; background: #2d1a1a; }
        .combined-card.priority-high { border-color: #ff8800; background: #2d2010; }
        .combined-card.priority-elevated { border-color: #ffcc00; background: #2d2a10; }
        .priority-badge { display: inline-block; padding: 4px 10px; border-radius: 4px;
                          font-weight: bold; font-size: 13px; margin-bottom: 10px; }
        .priority-critical .priority-badge { background: #ff0000; color: white; }
        .priority-high .priority-badge { background: #ff8800; color: white; }
        .priority-elevated .priority-badge { background: #ffcc00; color: black; }
        .combined-cameras { font-size: 15px; font-weight: bold; margin-bottom: 8px; color: #fff; }
        .combined-detail { color: #aaa; font-size: 13px; margin-top: 4px; }
        .section-header { display: flex; align-items: center; gap: 20px; margin-bottom: 15px; }
        .divider { border: none; border-top: 1px solid #333; margin: 30px 0; }
    </style>
</head>
<body>
    <h1>Fire Detection Dashboard</h1>
    ''' + NAV + '''
    <div class="status">Last updated: <span id="last-updated">--</span></div>

    <!-- Combined Alerts Section -->
    <div style="margin-bottom:30px;">
        <div class="section-header">
            <h2>Combined Events: <span id="combined-count">0</span></h2>
            <button class="clear-btn" onclick="clearCombinedAlerts()">Clear Combined</button>
        </div>
        <div class="grid" id="combined-alert-grid"></div>
    </div>

    <hr class="divider">

    <!-- Regular Alerts Section -->
    <div class="tabs" id="camera-tabs">
        <button class="tab active" onclick="selectCamera(null, this)">All</button>
    </div>
    <div class="section-header">
        <div class="alert-count">Total Alerts: <span id="alert-count">0</span></div>
        <button class="clear-btn" onclick="clearAlerts()">Clear All Alerts</button>
    </div>
    <div class="grid" id="alert-grid"></div>

    <script>
        let currentCamera = null;
        let currentAlertCount = 0;
        let currentTabCount = 0;
        let currentCombinedCount = 0;

        function selectCamera(cameraId, tabEl) {
            currentCamera = cameraId;
            currentAlertCount = 0;
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            tabEl.classList.add('active');
            fetchAlerts();
        }

        function fetchCameras() {
            fetch('/api/cameras').then(r => r.json()).then(cameras => {
                if (cameras.length !== currentTabCount) {
                    currentTabCount = cameras.length;
                    const tabs = document.getElementById('camera-tabs');
                    tabs.innerHTML = '<button class="tab active" onclick="selectCamera(null, this)">All</button>';
                    cameras.forEach(camera => {
                        const tab = document.createElement('button');
                        tab.className = 'tab';
                        tab.textContent = camera.name;
                        tab.onclick = function() { selectCamera(camera.camera_id, this); };
                        tabs.appendChild(tab);
                    });
                }
            });
        }

        function fetchAlerts() {
            let url = '/api/alerts';
            if (currentCamera) url += '?camera_id=' + encodeURIComponent(currentCamera);
            fetch(url).then(r => r.json()).then(data => {
                const grid = document.getElementById('alert-grid');
                const count = document.getElementById('alert-count');
                count.textContent = data.length;
                document.getElementById('last-updated').textContent = new Date().toLocaleTimeString();
                if (data.length !== currentAlertCount) {
                    currentAlertCount = data.length;
                    grid.innerHTML = '';
                    data.forEach(alert => {
                        const card = document.createElement('div');
                        card.className = 'card';
                        card.innerHTML = `
                            <img src="/snapshot/${alert.id}" alt="Detection snapshot">
                            <div class="camera">Camera: ${alert.camera_name || alert.camera_id}</div>
                            <div class="timestamp">${alert.timestamp}</div>
                            <div class="confidence">Confidence: ${alert.confidence}</div>
                            <button class="delete-btn" onclick="deleteAlert(${alert.id})">Delete</button>
                        `;
                        grid.appendChild(card);
                    });
                }
            });
        }

        function fetchCombinedAlerts() {
            fetch('/api/combined_alerts').then(r => r.json()).then(data => {
                const grid = document.getElementById('combined-alert-grid');
                const count = document.getElementById('combined-count');
                count.textContent = data.length;
                if (data.length !== currentCombinedCount) {
                    currentCombinedCount = data.length;
                    grid.innerHTML = '';
                    if (data.length === 0) {
                        grid.innerHTML = '<div style="color:#aaa; font-size:14px;">No combined events detected</div>';
                        return;
                    }
                    data.forEach(alert => {
                        const card = document.createElement('div');
                        card.className = 'combined-card priority-' + alert.priority_level.toLowerCase();
                        card.innerHTML = `
                            <div class="priority-badge">${alert.priority_level}</div>
                            <div class="combined-cameras">${alert.camera_1} + ${alert.camera_2}</div>
                            <div class="combined-detail">Time gap: ${alert.time_gap_seconds}s</div>
                            <div class="combined-detail">Distance: ${alert.distance_meters}m</div>
                            <div class="combined-detail">Priority score: ${alert.priority_score}</div>
                            <div class="timestamp">${alert.timestamp}</div>
                            <button class="delete-btn" onclick="deleteCombinedAlert(${alert.id})">Delete</button>
                        `;
                        grid.appendChild(card);
                    });
                }
            });
        }

        function deleteAlert(alertId) {
            fetch('/api/alerts/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ alert_id: alertId })
            }).then(() => { currentAlertCount = 0; fetchAlerts(); });
        }

        function clearAlerts() {
            if (!confirm('Clear all alerts? This cannot be undone.')) return;
            fetch('/api/alerts/clear', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ camera_id: currentCamera })
            }).then(() => { currentAlertCount = 0; fetchAlerts(); });
        }

        function deleteCombinedAlert(alertId) {
            fetch('/api/combined_alerts/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ alert_id: alertId })
            }).then(() => { currentCombinedCount = 0; fetchCombinedAlerts(); });
        }

        function clearCombinedAlerts() {
            if (!confirm('Clear all combined alerts?')) return;
            fetch('/api/combined_alerts/clear', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            }).then(() => { currentCombinedCount = 0; fetchCombinedAlerts(); });
        }

        fetchCameras();
        fetchAlerts();
        fetchCombinedAlerts();
        setInterval(() => {
            fetchCameras();
            fetchAlerts();
            fetchCombinedAlerts();
        }, 10000);
    </script>
</body>
</html>
'''

LIVE_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Live View - Fire Detection</title>
    <style>
        body { font-family: Arial, sans-serif; background: #1a1a1a; color: #fff; padding: 20px; }
        h1 { color: #ff4444; }
        .selector { margin-bottom: 20px; }
        select { background: #2a2a2a; border: 1px solid #444; color: #fff;
                 padding: 10px; border-radius: 4px; font-size: 14px; width: 300px; }
        .stream-container { background: #2a2a2a; border-radius: 8px; padding: 15px;
                            border: 1px solid #444; display: inline-block; }
        .stream-container img { max-width: 100%; border-radius: 4px; display: block; }
        .no-stream { color: #aaa; padding: 40px; text-align: center; }
        .status { font-size: 14px; color: #aaa; margin-bottom: 10px; }
        .active-indicator { display: inline-block; width: 8px; height: 8px;
                            background: #44ff88; border-radius: 50%; margin-right: 6px; }
    </style>
</head>
<body>
    <h1>Live View</h1>
    ''' + NAV + '''
    <div class="selector">
        <select id="camera-select" onchange="switchCamera()">
            <option value="">Select a camera...</option>
        </select>
    </div>
    <div id="stream-area" class="no-stream">Select a camera to begin streaming</div>

    <script>
        let currentCameraId = null;

        function loadCameras() {
            fetch('/api/cameras').then(r => r.json()).then(cameras => {
                const select = document.getElementById('camera-select');
                const currentVal = select.value;
                select.innerHTML = '<option value="">Select a camera...</option>';
                cameras.forEach(camera => {
                    if (camera.rtsp_url) {
                        const opt = document.createElement('option');
                        opt.value = camera.camera_id;
                        opt.textContent = camera.name;
                        if (camera.camera_id === currentVal) opt.selected = true;
                        select.appendChild(opt);
                    }
                });
            });
        }

        function switchCamera() {
            const select = document.getElementById('camera-select');
            const cameraId = select.value;
            const streamArea = document.getElementById('stream-area');

            if (!cameraId) {
                streamArea.className = 'no-stream';
                streamArea.innerHTML = 'Select a camera to begin streaming';
                if (currentCameraId) {
                    fetch('/api/stream/stop', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ camera_id: currentCameraId })
                    });
                }
                currentCameraId = null;
                return;
            }

            fetch('/api/stream/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ camera_id: cameraId })
            }).then(r => r.json()).then(data => {
                if (data.status === 'ok') {
                    currentCameraId = cameraId;
                    streamArea.className = 'stream-container';
                    streamArea.innerHTML = `
                        <div class="status">
                            <span class="active-indicator"></span>
                            Live — detections active
                        </div>
                        <img src="/stream/${cameraId}" alt="Live stream">
                    `;
                } else {
                    streamArea.className = 'no-stream';
                    streamArea.innerHTML = 'Failed to start stream — check RTSP URL in settings';
                }
            });
        }

        loadCameras();
        setInterval(loadCameras, 15000);
    </script>
</body>
</html>
'''

MAP_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Camera Map - Fire Detection</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body { font-family: Arial, sans-serif; background: #1a1a1a; color: #fff; padding: 20px; margin: 0; }
        h1 { color: #ff4444; }
        #map { height: 600px; border-radius: 8px; margin-top: 10px; }
        .status { font-size: 14px; color: #aaa; margin-bottom: 10px; }
        .legend { background: #2a2a2a; padding: 10px 15px; border-radius: 8px;
                  border: 1px solid #444; margin-bottom: 10px; display: inline-flex; gap: 20px; }
        .legend-item { display: flex; align-items: center; gap: 8px; font-size: 14px; }
        .dot { width: 12px; height: 12px; border-radius: 50%; }
        .dot.red { background: #ff4444; }
        .dot.green { background: #44ff88; }
        .dot.grey { background: #888; }
    </style>
</head>
<body>
    <h1>Camera Map</h1>
    ''' + NAV + '''
    <div class="status">Last updated: <span id="last-updated">--</span></div>
    <div class="legend">
        <div class="legend-item"><div class="dot red"></div> Active Alert (last 10 min)</div>
        <div class="legend-item"><div class="dot green"></div> Normal</div>
        <div class="legend-item"><div class="dot grey"></div> No location set</div>
    </div>
    <div id="map"></div>
    <script>
        const map = L.map('map').setView([34.0, -118.0], 13);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '© OpenStreetMap contributors'
        }).addTo(map);
        let markers = {};

        function getMarkerColor(lastAlert) {
            if (!lastAlert) return 'grey';
            const diffMinutes = (new Date() - new Date(lastAlert)) / 60000;
            return diffMinutes <= 10 ? 'red' : 'green';
        }

        function makeIcon(color) {
            const colors = { red: '#ff4444', green: '#44ff88', grey: '#888888' };
            const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">
                <circle cx="12" cy="12" r="10" fill="${colors[color]}" stroke="white" stroke-width="2"/>
            </svg>`;
            return L.divIcon({ html: svg, className: '', iconSize: [24, 24],
                               iconAnchor: [12, 12], popupAnchor: [0, -14] });
        }

        function fetchCameraStatus() {
            fetch('/api/camera_status').then(r => r.json()).then(cameras => {
                document.getElementById('last-updated').textContent = new Date().toLocaleTimeString();
                cameras.forEach(camera => {
                    if (!camera.latitude || !camera.longitude) return;
                    const color = getMarkerColor(camera.last_alert);
                    const icon = makeIcon(color);
                    const popup = `<b>${camera.name}</b><br>IP: ${camera.camera_id}<br>
                                   Last Alert: ${camera.last_alert || 'None'}<br>
                                   Confidence: ${camera.last_confidence ?
                                   Number(camera.last_confidence).toFixed(2) : 'N/A'}`;
                    if (markers[camera.camera_id]) {
                        markers[camera.camera_id].setIcon(icon);
                        markers[camera.camera_id].setPopupContent(popup);
                    } else {
                        markers[camera.camera_id] = L.marker(
                            [camera.latitude, camera.longitude], { icon }
                        ).addTo(map).bindPopup(popup);
                    }
                });
                const withCoords = cameras.filter(c => c.latitude && c.longitude);
                if (withCoords.length > 0 && Object.keys(markers).length > 0) {
                    map.fitBounds(L.featureGroup(Object.values(markers)).getBounds().pad(0.2));
                }
            });
        }

        fetchCameraStatus();
        setInterval(fetchCameraStatus, 10000);
    </script>
</body>
</html>
'''

SETTINGS_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Settings - Fire Detection</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body { font-family: Arial, sans-serif; background: #1a1a1a; color: #fff; padding: 20px; }
        h1 { color: #ff4444; }
        .camera-block { background: #2a2a2a; border: 1px solid #444; border-radius: 8px;
                        padding: 20px; margin-bottom: 20px; }
        .camera-block h3 { margin: 0 0 15px 0; color: #4a9eff; }
        .field { margin-bottom: 12px; }
        .field label { display: block; color: #aaa; font-size: 13px; margin-bottom: 4px; }
        input[type="text"] { background: #1a1a1a; border: 1px solid #444; color: #fff;
                             padding: 8px; border-radius: 4px; width: 300px; }
        .mini-map { height: 250px; border-radius: 4px; border: 1px solid #444; margin-top: 6px; }
        .coords { color: #aaa; font-size: 12px; margin-top: 6px; }
        .btn-row { display: flex; gap: 10px; margin-top: 15px; }
        .save-btn { background: #ff4444; color: white; border: none; padding: 8px 16px;
                    border-radius: 4px; cursor: pointer; }
        .delete-btn { background: #444; color: white; border: none; padding: 8px 16px;
                      border-radius: 4px; cursor: pointer; }
        .message { color: #44ff88; font-size: 14px; margin-top: 8px; height: 20px; }
        .no-cameras { color: #aaa; }
    </style>
</head>
<body>
    <h1>Camera Settings</h1>
    ''' + NAV + '''
    <div id="camera-list"></div>
    <script>
        let miniMaps = {};
        let selectedCoords = {};

        function loadCameras() {
            fetch('/api/cameras').then(r => r.json()).then(cameras => {
                const container = document.getElementById('camera-list');
                container.innerHTML = '';
                if (cameras.length === 0) {
                    container.innerHTML = '<p class="no-cameras">No cameras registered yet.</p>';
                    return;
                }
                cameras.forEach(camera => {
                    const block = document.createElement('div');
                    block.className = 'camera-block';
                    block.innerHTML = `
                        <h3>${camera.name}</h3>
                        <div class="field">
                            <label>Camera IP</label>
                            <span>${camera.camera_id}</span>
                        </div>
                        <div class="field">
                            <label>Display Name</label>
                            <input type="text" id="name-${camera.camera_id}" value="${camera.name}">
                        </div>
                        <div class="field">
                            <label>RTSP URL</label>
                            <input type="text" id="rtsp-${camera.camera_id}"
                                   value="${camera.rtsp_url || ''}"
                                   placeholder="rtsp://admin:password@${camera.camera_id}:554/stream1">
                        </div>
                        <div class="field">
                            <label>Location — click map to place camera</label>
                            <div id="map-${camera.camera_id}" class="mini-map"></div>
                            <div class="coords" id="coords-${camera.camera_id}">
                                ${camera.latitude && camera.longitude
                                    ? 'Current: ' + Number(camera.latitude).toFixed(5) + ', ' +
                                      Number(camera.longitude).toFixed(5)
                                    : 'No location set'}
                            </div>
                        </div>
                        <div class="btn-row">
                            <button class="save-btn" onclick="saveCamera('${camera.camera_id}')">Save</button>
                            <button class="delete-btn" onclick="deleteCamera('${camera.camera_id}')">Delete</button>
                        </div>
                        <div class="message" id="msg-${camera.camera_id}"></div>
                    `;
                    container.appendChild(block);

                    const lat = camera.latitude || 34.0;
                    const lng = camera.longitude || -118.0;
                    const zoom = camera.latitude ? 15 : 10;
                    const miniMap = L.map('map-' + camera.camera_id).setView([lat, lng], zoom);
                    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                        attribution: '© OpenStreetMap contributors'
                    }).addTo(miniMap);

                    let marker = null;
                    if (camera.latitude && camera.longitude) {
                        marker = L.marker([camera.latitude, camera.longitude]).addTo(miniMap);
                        selectedCoords[camera.camera_id] = {
                            lat: camera.latitude, lng: camera.longitude
                        };
                    }

                    miniMap.on('click', function(e) {
                        const { lat, lng } = e.latlng;
                        selectedCoords[camera.camera_id] = { lat, lng };
                        document.getElementById('coords-' + camera.camera_id).textContent =
                            'Selected: ' + lat.toFixed(5) + ', ' + lng.toFixed(5);
                        if (marker) miniMap.removeLayer(marker);
                        marker = L.marker([lat, lng]).addTo(miniMap);
                    });

                    miniMaps[camera.camera_id] = miniMap;
                });
            });
        }

        function showMessage(cameraId, text) {
            const msg = document.getElementById('msg-' + cameraId);
            msg.textContent = text;
            setTimeout(() => msg.textContent = '', 3000);
        }

        function saveCamera(cameraId) {
            const name = document.getElementById('name-' + cameraId).value;
            const rtsp = document.getElementById('rtsp-' + cameraId).value;
            const coords = selectedCoords[cameraId];
            fetch('/api/cameras/update', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    camera_id: cameraId,
                    name: name,
                    rtsp_url: rtsp,
                    latitude: coords ? coords.lat : null,
                    longitude: coords ? coords.lng : null
                })
            }).then(r => r.json()).then(() => showMessage(cameraId, 'Saved successfully'));
        }

        function deleteCamera(cameraId) {
            if (!confirm('Delete this camera?')) return;
            fetch('/api/cameras/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ camera_id: cameraId })
            }).then(r => r.json()).then(() => loadCameras());
        }

        loadCameras();
    </script>
</body>
</html>
'''

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form['username'] == USERNAME and request.form['password'] == PASSWORD:
            session['logged_in'] = True
            return redirect('/')
        error = 'Invalid credentials'
    return render_template_string(LOGIN_HTML, error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/')
@login_required
def dashboard():
    return render_template_string(DASHBOARD_HTML)

@app.route('/live')
@login_required
def live_view():
    return render_template_string(LIVE_HTML)

@app.route('/map')
@login_required
def map_page():
    return render_template_string(MAP_HTML)

@app.route('/settings')
@login_required
def settings():
    return render_template_string(SETTINGS_HTML)

@app.route('/stream/<camera_id>')
@login_required
def stream(camera_id):
    stream_obj = camera_streams.get(camera_id)
    if not stream_obj:
        return 'Stream not found', 404
    return Response(
        generate_frames(camera_id),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )

@app.route('/api/stream/start', methods=['POST'])
@login_required
def api_stream_start():
    data = request.json
    camera_id = data.get('camera_id')
    conn = get_db()
    camera = conn.execute(
        'SELECT camera_id, name, rtsp_url FROM cameras WHERE camera_id=?', (camera_id,)
    ).fetchone()
    conn.close()
    if not camera or not camera['rtsp_url']:
        return jsonify({'status': 'error', 'message': 'No RTSP URL configured'})
    get_or_start_stream(camera['camera_id'], camera['rtsp_url'], camera['name'])
    return jsonify({'status': 'ok'})

@app.route('/api/stream/stop', methods=['POST'])
@login_required
def api_stream_stop():
    data = request.json
    camera_id = data.get('camera_id')
    with camera_streams_lock:
        stream_obj = camera_streams.get(camera_id)
        if stream_obj:
            stream_obj.stop()
            del camera_streams[camera_id]
    return jsonify({'status': 'ok'})

@app.route('/api/cameras')
@login_required
def api_cameras():
    return jsonify([dict(row) for row in get_cameras()])

@app.route('/api/camera_status')
@login_required
def api_camera_status():
    return jsonify([dict(row) for row in get_camera_status()])

@app.route('/api/cameras/update', methods=['POST'])
@login_required
def api_update_camera():
    data = request.json
    update_camera(
        data.get('camera_id'),
        data.get('name'),
        data.get('latitude'),
        data.get('longitude'),
        data.get('rtsp_url')
    )
    return jsonify({'status': 'ok'})

@app.route('/api/cameras/delete', methods=['POST'])
@login_required
def api_delete_camera():
    delete_camera(request.json.get('camera_id'))
    return jsonify({'status': 'ok'})

@app.route('/api/alerts')
@login_required
def api_alerts():
    camera_id = request.args.get('camera_id')
    alerts = get_alerts(camera_id)
    cameras = {row['camera_id']: row['name'] for row in get_cameras()}
    return jsonify([{
        'id': row['id'],
        'timestamp': row['timestamp'],
        'confidence': row['confidence'],
        'label': row['label'],
        'camera_id': row['camera_id'],
        'camera_name': cameras.get(row['camera_id'], row['camera_id']),
        'snapshot_path': row['snapshot_path']
    } for row in alerts])

@app.route('/api/alerts/delete', methods=['POST'])
@login_required
def api_delete_alert():
    conn = get_db()
    conn.execute('DELETE FROM alerts WHERE id=?', (request.json.get('alert_id'),))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/alerts/clear', methods=['POST'])
@login_required
def api_clear_alerts():
    camera_id = request.json.get('camera_id')
    conn = get_db()
    if camera_id:
        conn.execute('DELETE FROM alerts WHERE camera_id=?', (camera_id,))
    else:
        conn.execute('DELETE FROM alerts')
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/combined_alerts')
@login_required
def api_combined_alerts():
    rows = get_combined_alerts()
    cameras = {row['camera_id']: row['name'] for row in get_cameras()}
    return jsonify([{
        'id': row['id'],
        'timestamp': row['timestamp'],
        'camera_1': cameras.get(row['camera_id_1'], row['camera_id_1']),
        'camera_2': cameras.get(row['camera_id_2'], row['camera_id_2']),
        'time_gap_seconds': round(row['time_gap_seconds'], 1),
        'distance_meters': round(row['distance_meters'], 0),
        'priority_score': round(row['priority_score'], 2),
        'priority_level': row['priority_level']
    } for row in rows])

@app.route('/api/combined_alerts/delete', methods=['POST'])
@login_required
def api_delete_combined_alert():
    conn = get_db()
    conn.execute('DELETE FROM combined_alerts WHERE id=?', (request.json.get('alert_id'),))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/combined_alerts/clear', methods=['POST'])
@login_required
def api_clear_combined_alerts():
    conn = get_db()
    conn.execute('DELETE FROM combined_alerts')
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/snapshot/<int:alert_id>')
@login_required
def serve_snapshot(alert_id):
    conn = get_db()
    row = conn.execute('SELECT snapshot_path FROM alerts WHERE id=?', (alert_id,)).fetchone()
    conn.close()
    if row and os.path.exists(row['snapshot_path']):
        with open(row['snapshot_path'], 'rb') as f:
            return f.read(), 200, {'Content-Type': 'image/jpeg'}
    return 'Snapshot not found', 404

@app.route('/alert', methods=['POST'])
def receive_alert():
    data = request.json
    timestamp = data.get('timestamp')
    confidence = data.get('confidence')
    label = data.get('label')
    camera_id = data.get('camera_id')
    frame_b64 = data.get('frame')

    register_camera(camera_id)

    snapshot_path = None
    if frame_b64:
        frame_bytes = base64.b64decode(frame_b64)
        filename = f"{SNAPSHOT_DIR}/alert_{timestamp.replace(':', '-').replace(' ', '_')}.jpg"
        with open(filename, 'wb') as f:
            f.write(frame_bytes)
        snapshot_path = filename

    insert_alert(timestamp, confidence, label, camera_id, snapshot_path)
    print(f"ALERT RECEIVED | {label} | {confidence} | {camera_id} | {timestamp}")
    return {'status': 'ok'}, 200

# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    migrate_db()
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)