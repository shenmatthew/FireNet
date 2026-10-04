# Firenet

Firenet is a prototype wildfire and fire detection system that uses networked IP cameras, 
computer vision, and a centralized web dashboard to detect fire events and alert operators 
in real time.

The project is designed to scale from a small proof-of-concept — a couple of cameras on 
a local network — toward a distributed community alert network capable of covering multiple 
locations across a geographic area.

## What It Does

- Pulls live video streams from IP cameras over RTSP
- Runs a YOLOv8 fire detection model on each stream in real time using GPU acceleration
- Logs fire detection events to a persistent SQLite database with snapshot images
- Displays alerts on a web dashboard with camera identification, confidence scores, 
  and timestamps
- Generates combined priority alerts when multiple cameras detect fire within a 
  configurable time window, scored by proximity and time correlation
- Shows camera locations on an interactive map with real-time status indicators
- Streams live annotated video feeds to the dashboard

## Hardware

- **Camera:** Annke I81HS (4MP, IP67, PoE, RTSP over H.265)
- **Processing:** Any machine with a CUDA-capable NVIDIA GPU recommended
- **Network:** Cameras and server on the same local network for prototype deployment

## Tech Stack

- **Python** — core language
- **YOLOv8 (Ultralytics)** — fire detection model
- **OpenCV** — RTSP stream capture and frame processing
- **Flask** — web server and dashboard
- **SQLite** — persistent alert and camera storage
- **Leaflet.js + OpenStreetMap** — geolocation map
- **MJPEG** — live stream delivery to browser

## Project Status

This is an active prototype. The core pipeline is functional:

- [x] RTSP stream pull and frame capture
- [x] Real-time YOLOv8 inference on GPU
- [x] Alert logging with snapshot saves
- [x] Web dashboard with camera tabs and auto-refresh
- [x] Live video view in dashboard
- [x] Persistent SQLite storage
- [x] Camera geolocation and map view
- [x] Multi-camera support via threaded stream management
- [x] Combined priority alert system
- [ ] Dedicated server deployment
- [ ] Remote access via VPN
- [ ] HTTPS and production authentication
- [ ] Community notification system
- [ ] Custom trained fire detection model

## Roadmap

The long term vision is a community-deployable wildfire early warning network. Key 
future milestones include migration to a dedicated cloud server, secure remote camera 
access, a public-facing community alert interface, and a custom trained detection model 
built on data from real deployment environments.

## Setup

See `ARCHITECTURE.md` for system design. A setup guide for new deployments is 
in progress.

## Disclaimer

Firenet is a student prototype and should not be relied upon as a primary fire detection 
or life safety system.
