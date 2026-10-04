# Firenet — System Architecture

## Overview

Firenet is structured as a single combined Python server that manages all camera streams, 
runs ML inference, stores alert data, and serves the web dashboard. At prototype scale 
everything runs on one machine. The architecture is designed so components can be separated 
onto dedicated infrastructure as the system scales.

## Component Diagram

```
Cameras (Annke I81HS)
        │ RTSP
        ▼
Flask Server
├── CameraStream (one per camera)
│   ├── Capture Thread — pulls RTSP frames, updates frame buffer
│   └── Inference Thread — runs YOLOv8 on GPU, triggers alerts
│
├── Alert Pipeline
│   ├── Write alert to SQLite
│   ├── Save snapshot to disk
│   └── Check combined alert conditions
│
├── MJPEG Stream — serves annotated frames to browser
│
├── SQLite Database
│   ├── alerts
│   ├── cameras
│   └── combined_alerts
│
└── Web Dashboard
    ├── /           — Alert dashboard
    ├── /live       — Live stream viewer
    ├── /map        — Geolocation map
    ├── /settings   — Camera configuration
    └── /api/*      — JSON endpoints
        │
        ▼
   Browser Client
```

## Key Design Decisions

**Separated capture and inference threads**
Each camera runs two threads — one dedicated to pulling frames from the RTSP stream 
at full speed, and one dedicated to running YOLOv8 inference. This prevents slow 
inference calls from stalling the video feed. Inference runs on every Nth frame 
(configurable) to balance detection responsiveness against GPU load.

**Camera-agnostic pipeline**
The server pulls standard RTSP streams and runs inference server-side. Any camera 
that speaks RTSP can be added by configuring its URL in the settings page. The 
detection model is a swappable `.pt` file — changing models requires no code changes.

**Combined alert scoring**
When a new alert is logged, the server queries for recent alerts from other cameras 
within a configurable time window (default 60 seconds). For each match, a priority 
score is calculated using a normalized formula combining time gap and geographic 
distance via the Haversine formula. Scores map to three priority levels: 
Elevated, High, and Critical.

**SQLite for prototype storage**
All alert data, camera registrations, and combined events are stored in a local 
SQLite database. This is appropriate for prototype scale. Migration to PostgreSQL 
is planned for production deployment.

## Database Schema

**alerts**
| Field | Type | Description |
|---|---|---|
| id | INTEGER | Primary key |
| timestamp | TEXT | Detection time |
| confidence | REAL | Model confidence 0–1 |
| label | TEXT | Detected class |
| camera_id | TEXT | Camera IP address |
| snapshot_path | TEXT | Path to saved frame |

**cameras**
| Field | Type | Description |
|---|---|---|
| id | INTEGER | Primary key |
| camera_id | TEXT | Camera IP (unique) |
| name | TEXT | Human readable name |
| latitude | REAL | GPS latitude |
| longitude | REAL | GPS longitude |
| rtsp_url | TEXT | Full RTSP stream URL |

**combined_alerts**
| Field | Type | Description |
|---|---|---|
| id | INTEGER | Primary key |
| timestamp | TEXT | When combined event was detected |
| camera_id_1 | TEXT | First camera |
| camera_id_2 | TEXT | Second camera |
| time_gap_seconds | REAL | Time between individual alerts |
| distance_meters | REAL | Distance between cameras |
| priority_score | REAL | Combined score 0–1 |
| priority_level | TEXT | Elevated / High / Critical |
| alert_id_1 | INTEGER | Foreign key to alerts |
| alert_id_2 | INTEGER | Foreign key to alerts |

## Known Limitations

- Alerts are only as good as the detection model. The current weights were trained 
  on a small community dataset and will miss fires outside that distribution.
- The Annke I81HS uses a visible light sensor, not a thermal imager. Detection 
  depends on visual fire characteristics rather than heat signatures.
- MJPEG streaming introduces latency and is not suitable for production use at scale. 
  WebRTC or HLS would be more appropriate replacements.
- SQLite does not support concurrent writes well under heavy multi-camera load. 
  PostgreSQL is the intended replacement.
- Remote access is not yet implemented. Cameras and server must be on the same network.
