# webcam-streamer

Combine multiple local IP camera feeds into a single, uninterrupted live stream and push it to the cloud. Point webcam-streamer at your RTSP cameras, define a rotation schedule, and it will cycle through them while delivering a continuous stream to any RTMP-compatible service (Restreamer, YouTube Live, Twitch, etc.) — without tearing down the upstream connection.

## How it works

Camera switching normally means tearing down and re-establishing the RTMP connection, which causes errors on most RTMP servers. To avoid this, webcam-streamer uses a two-stage ffmpeg pipeline:

1. **Camera ffmpeg** – pulls RTSP from the active camera, re-encodes to H.264 with low latency, and sends mpegts over UDP to localhost.
2. **Upstream ffmpeg** – reads from the UDP socket and pushes a continuous FLV stream to the RTMP server. This process runs permanently and never restarts during camera switches.

When the switcher rotates to the next camera, only the camera-side ffmpeg is restarted. The upstream connection stays alive.

## Features

- **Multiple RTSP cameras** – define any number of cameras in `config.yml`
- **Schedule-based rotation** – configure different camera sets and intervals per schedule (e.g. working hours vs. off hours)
- **Day and time filters** – restrict schedules to specific weekdays and time windows with timezone support
- **Drop-free RTMP streaming** – UDP relay on localhost keeps the upstream connection alive across camera switches
- **Auto-restart** – both camera and upstream ffmpeg processes are monitored and restarted if they crash
- **Token authentication** – optional `UPSTREAM_TOKEN` env var appended to the RTMP URL
- **Docker-ready** – ships with a Dockerfile and docker-compose.yml using host networking

## Configuration

```bash
cp config.example.yml config.yml
```

Edit `config.yml` with your camera URLs, upstream RTMP endpoint, and schedules:

```yaml
cameras:
  cam1:
    name: Front door
    url: rtsp://192.168.1.10:7447/stream1
  cam2:
    name: Backyard
    url: rtsp://192.168.1.11:7447/stream2

upstream:
  url: rtmp://your-server.example.com/live/stream

udp:
  dest: udp://127.0.0.1:1234
  listen: "udp://@127.0.0.1:1234?overrun_nonfatal=1&fifo_size=50000000"
  bitrate: 600k

schedules:
  - name: Daytime
    days: [mon, tue, wed, thu, fri]
    start: "08:00"
    end: "18:00"
    timezone: America/Lima
    cameras: [cam1, cam2]
    interval: 15

  - name: Fallback
    cameras: [cam1]
    interval: 30
```

Schedules are evaluated top-to-bottom; the first match wins. The last entry acts as the fallback.

Set `UPSTREAM_TOKEN` in a `.env` file (or the environment) if your RTMP server requires a token.

## Running

```bash
docker compose up -d
```
