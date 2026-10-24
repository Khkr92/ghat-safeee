"""GHAT-SAFE CCTV module (prototype).

Counts vehicles in three image regions of an existing camera at the bend
(Approach A, Approach B, and the blind bend itself) and posts the counts to the
GHAT-SAFE server once per second. Only counts leave this machine; no video is
uploaded or stored.

    pip install ultralytics opencv-python requests
    python detector.py --source bend07.mp4 --server http://localhost:8000 --bend bend-07 --config regions.json

regions.json holds three pixel polygons drawn on one frame of the camera view:
    {"approach_a": [[x,y],...], "approach_b": [[x,y],...], "bend": [[x,y],...]}

A vehicle seen in the bend is attributed to the side it came from (the approach
region where its track was first seen). The region polygons must be calibrated
for each camera on execution day.
"""
from __future__ import annotations

import argparse
import json
import time

import cv2
import numpy as np
import requests
from ultralytics import YOLO

VEHICLE_CLASSES = {2, 3, 5, 7}  # COCO: car, motorcycle, bus, truck


def inside(poly: np.ndarray, x: float, y: float) -> bool:
    return cv2.pointPolygonTest(poly, (float(x), float(y)), False) >= 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="video file, RTSP URL or camera index")
    ap.add_argument("--server", default="http://localhost:8000")
    ap.add_argument("--bend", default="bend-07")
    ap.add_argument("--config", default="regions.json")
    ap.add_argument("--model", default="yolov8n.pt")
    ap.add_argument("--show", action="store_true", help="display annotated frames")
    args = ap.parse_args()

    regions = {k: np.array(v, dtype=np.int32) for k, v in json.load(open(args.config)).items()}
    model = YOLO(args.model)
    origin: dict[int, str] = {}  # track id -> 'A' or 'B'
    last_post = 0.0
    source = int(args.source) if args.source.isdigit() else args.source

    for result in model.track(source=source, stream=True, persist=True, classes=list(VEHICLE_CLASSES), verbose=False):
        counts = {"approach_a": 0, "approach_b": 0, "in_bend_from_a": 0, "in_bend_from_b": 0}
        boxes = result.boxes
        if boxes is not None and boxes.id is not None:
            for (x1, y1, x2, y2), tid in zip(boxes.xyxy.tolist(), boxes.id.int().tolist()):
                cx, cy = (x1 + x2) / 2, y2  # bottom-centre = road contact point
                if inside(regions["approach_a"], cx, cy):
                    origin.setdefault(tid, "A")
                    counts["approach_a"] += 1
                elif inside(regions["approach_b"], cx, cy):
                    origin.setdefault(tid, "B")
                    counts["approach_b"] += 1
                elif inside(regions["bend"], cx, cy):
                    side = origin.get(tid)
                    if side == "A":
                        counts["in_bend_from_a"] += 1
                    elif side == "B":
                        counts["in_bend_from_b"] += 1
                    else:  # direction unknown: count on both sides, which errs towards warning
                        counts["in_bend_from_a"] += 1
                        counts["in_bend_from_b"] += 1

        now = time.time()
        if now - last_post >= 1.0:
            try:
                requests.post(f"{args.server}/api/cctv/{args.bend}", json=counts, timeout=2)
            except requests.RequestException as exc:
                print("post failed:", exc)
            last_post = now

        if args.show:
            frame = result.plot()
            for name, poly in regions.items():
                cv2.polylines(frame, [poly], True, (0, 200, 255) if name != "bend" else (0, 0, 255), 2)
            cv2.putText(frame, json.dumps(counts), (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            cv2.imshow("GHAT-SAFE CCTV", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break


if __name__ == "__main__":
    main()
