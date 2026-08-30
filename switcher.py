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
        "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+discardcorrupt+nobuffer",
        "-err_detect", "ignore_err",
        "-i", udp_url,
        "-vcodec", "copy", "-an",
        "-f", "flv",
        "-flvflags", "no_duration_filesize",
        upstream_url,
    ])


def start_camera(camera_url, udp_dest, bitrate, resolution):
    """Per-camera ffmpeg: RTSP → re-encode → UDP localhost."""
    w, h = resolution.split("x")
    return subprocess.Popen([
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", camera_url,
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
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
    resolution = udp_cfg.get("resolution", "1280x720")

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
    # Per-camera failure tracking: cam_id -> (fail_count, next_retry_time)
    cam_failures = {}

    def record_failure(cid):
        count = cam_failures.get(cid, (0, 0))[0] + 1
        backoff = min(2 ** count, 300)
        cam_failures[cid] = (count, time.monotonic() + backoff)
        return count, backoff

    def is_available(cid):
        if cid not in cam_failures:
            return True
        return time.monotonic() >= cam_failures[cid][1]

    def clear_failure(cid):
        cam_failures.pop(cid, None)

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

        if not cam_ids:
            print("[switcher] no cameras in schedule, waiting 10s", flush=True)
            time.sleep(10)
            continue

        if schedule.get("name") != prev_schedule:
            print(f"[switcher] schedule: {schedule.get('name', 'default')}, "
                  f"cameras: {cam_ids}, interval: {interval}s", flush=True)
            prev_schedule = schedule.get("name")
            cam_index = 0
            current_cam_id = None

        # Pick the next available camera, skipping ones still in backoff
        cam_index %= len(cam_ids)
        cam_id = None
        for i in range(len(cam_ids)):
            candidate = cam_ids[(cam_index + i) % len(cam_ids)]
            if candidate not in cameras:
                print(f"[switcher] unknown camera '{candidate}', skipping", flush=True)
                record_failure(candidate)
                continue
            if is_available(candidate):
                cam_id = candidate
                cam_index = (cam_index + i) % len(cam_ids)
                break

        if cam_id is None:
            # All cameras in backoff — wait for the soonest one
            retry_times = [cam_failures[c][1] for c in cam_ids if c in cam_failures]
            if retry_times:
                wait = max(min(retry_times) - time.monotonic(), 1)
            else:
                wait = 5
            print(f"[switcher] all cameras unavailable, waiting {wait:.0f}s", flush=True)
            time.sleep(wait)
            continue

        cam = cameras[cam_id]

        # Same camera still running — poll frequently so we notice if it dies
        if cam_id == current_cam_id and cam_proc and cam_proc.poll() is None:
            elapsed = 0
            while elapsed < interval:
                if cam_proc.poll() is not None:
                    break
                time.sleep(1)
                elapsed += 1
            continue

        # Same camera but ffmpeg died
        if cam_id == current_cam_id and cam_proc and cam_proc.poll() is not None:
            count, backoff = record_failure(cam_id)
            print(f"[camera] {cam_id} died (code {cam_proc.returncode}), backoff {backoff}s (attempt {count})", flush=True)
            cam_proc = None
            current_cam_id = None
            continue

        stop_ffmpeg(cam_proc)
        cam_proc = start_camera(cam["url"], udp_dest, bitrate, resolution)
        current_cam_id = cam_id

        cam_index += 1
        elapsed = 0
        while elapsed < interval:
            if cam_proc.poll() is not None:
                count, backoff = record_failure(cam_id)
                print(f"[camera] {cam_id} exited early (code {cam_proc.returncode}), backoff {backoff}s (attempt {count})", flush=True)
                cam_proc = None
                current_cam_id = None
                break
            time.sleep(1)
            elapsed += 1
        else:
            clear_failure(cam_id)


if __name__ == "__main__":
    main()
