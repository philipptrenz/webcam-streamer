#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

DAY_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


def _parse_time(t):
    h, m = map(int, str(t).split(":"))
    return h * 60 + m


def matches_schedule(schedule):
    tz = ZoneInfo(schedule.get("timezone", "UTC"))
    now = datetime.now(tz)

    if "days" in schedule:
        allowed = {DAY_MAP[d.lower()] for d in schedule["days"]}
        if now.weekday() not in allowed:
            return False

    if "start" in schedule and "end" in schedule:
        current = now.hour * 60 + now.minute
        if not (_parse_time(schedule["start"]) <= current < _parse_time(schedule["end"])):
            return False

    return True


def get_active_schedule(config):
    for schedule in config["schedules"]:
        if matches_schedule(schedule):
            return schedule
    return config["schedules"][-1]


def build_upstream_url(config):
    url = config["upstream"]["url"]
    token = os.environ.get("UPSTREAM_TOKEN", "")
    if token:
        url += f"?token={token}"
    return url


def start_upstream(udp_url, upstream_url):
    """Permanent ffmpeg: reads UDP on localhost, pushes to RTMP."""
    return subprocess.Popen([
        "ffmpeg",
        "-hide_banner", "-loglevel", "warning",
        "-fflags", "+genpts+discardcorrupt+nobuffer",
        "-err_detect", "ignore_err",
        "-i", udp_url,
        "-vcodec", "copy", "-an",
        "-f", "flv",
        "-flvflags", "no_duration_filesize",
        upstream_url,
    ])


def start_camera(camera_url, udp_dest, bitrate):
    """Per-camera ffmpeg: RTSP → re-encode → UDP localhost."""
    return subprocess.Popen([
        "ffmpeg",
        "-hide_banner", "-loglevel", "warning",
        "-rtsp_transport", "tcp",
        "-i", camera_url,
        "-preset", "ultrafast",
        "-vcodec", "libx264",
        "-tune", "zerolatency",
        "-g", "50",
        "-an",
        "-b:v", bitrate,
        "-f", "mpegts",
        "-flush_packets", "1",
        udp_dest,
    ])


def stop_ffmpeg(proc):
    if proc is None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def main():
    config_path = os.environ.get("CONFIG_PATH", "/config.yml")
    config = load_config(config_path)
    upstream_url = build_upstream_url(config)
    cameras = config["cameras"]

    udp_cfg = config.get("udp", {})
    udp_dest = udp_cfg.get("dest", "udp://127.0.0.1:1234")
    udp_listen = udp_cfg.get("listen", "udp://@127.0.0.1:1234?overrun_nonfatal=1&fifo_size=50000000")
    bitrate = udp_cfg.get("bitrate", "600k")

    cam_proc = None
    upstream_proc = None

    def shutdown(sig, _):
        stop_ffmpeg(cam_proc)
        stop_ffmpeg(upstream_proc)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    cam_index = 0
    prev_schedule = None
    current_cam_id = None

    while True:
        # (Re)start upstream if it died
        if upstream_proc is None or upstream_proc.poll() is not None:
            if upstream_proc is not None:
                print(f"[upstream] ffmpeg died (code {upstream_proc.returncode}), restarting", flush=True)
            print(f"[upstream] starting: {udp_listen} -> RTMP", flush=True)
            upstream_proc = start_upstream(udp_listen, upstream_url)

        schedule = get_active_schedule(config)
        cam_ids = schedule["cameras"]
        interval = schedule.get("interval", 15)

        if schedule.get("name") != prev_schedule:
            print(f"[switcher] schedule: {schedule.get('name', 'default')}, "
                  f"cameras: {cam_ids}, interval: {interval}s", flush=True)
            prev_schedule = schedule.get("name")
            cam_index = 0
            current_cam_id = None

        cam_index %= len(cam_ids)
        cam_id = cam_ids[cam_index]
        cam = cameras[cam_id]

        # Skip restart if same camera and ffmpeg still running
        if cam_id == current_cam_id and cam_proc and cam_proc.poll() is None:
            time.sleep(interval)
            continue

        # Restart if same camera but ffmpeg died
        if cam_id == current_cam_id and cam_proc and cam_proc.poll() is not None:
            print(f"[camera] ffmpeg died (code {cam_proc.returncode}), restarting", flush=True)
            cam_proc = None
            time.sleep(2)

        stop_ffmpeg(cam_proc)
        # print(f"[camera] -> {cam_id} ({cam.get('name', '')})", flush=True)
        cam_proc = start_camera(cam["url"], udp_dest, bitrate)
        current_cam_id = cam_id

        cam_index += 1
        elapsed = 0
        while elapsed < interval:
            if cam_proc.poll() is not None:
                print(f"[camera] ffmpeg exited early (code {cam_proc.returncode}), skipping", flush=True)
                cam_proc = None
                current_cam_id = None
                time.sleep(2)
                break
            time.sleep(1)
            elapsed += 1


if __name__ == "__main__":
    main()
