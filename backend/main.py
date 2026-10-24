"""GHAT-SAFE API server.

Run:  uvicorn main:app --reload --port 8000   (from the backend folder)
Then open:
  http://localhost:8000/            simulator + authority dashboard
  http://localhost:8000/driver/     driver app (live GPS or demo drive)
  http://localhost:8000/docs        interactive API documentation
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine import Bend, Engine, WEATHER

ROOT = Path(__file__).resolve().parent
engine = Engine()
for spec in json.loads((ROOT / "bends.json").read_text())["bends"]:
    engine.add_bend(Bend(**spec))

app = FastAPI(title="GHAT-SAFE", version="0.1.0",
              description="Software blind-curve warning for ghat-road hairpin bends — Team CE Endeavours")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class Report(BaseModel):
    bend_id: str
    vehicle_id: str = Field(min_length=1, max_length=64)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    speed_kmh: float = Field(ge=0, le=200)


class CctvCounts(BaseModel):
    approach_a: int = Field(0, ge=0)
    approach_b: int = Field(0, ge=0)
    in_bend_from_a: int = Field(0, ge=0)
    in_bend_from_b: int = Field(0, ge=0)


class WeatherIn(BaseModel):
    weather: str


def _bend(bend_id: str) -> Bend:
    if bend_id not in engine.bends:
        raise HTTPException(404, f"Unknown bend '{bend_id}'")
    return engine.bends[bend_id]


@app.get("/api/bends")
def list_bends():
    return [{"id": b.id, "name": b.name, "weather": b.weather, "advisory_kmh": b.advisory} for b in engine.bends.values()]


@app.get("/api/bends/{bend_id}")
def bend_detail(bend_id: str):
    b = _bend(bend_id)
    return {"id": b.id, "name": b.name, "points": b.points, "bend_start": b.bend_start,
            "bend_end": b.bend_end, "length_m": round(b.line.length, 1), **engine.state(bend_id)}


@app.get("/api/bends/{bend_id}/state")
def bend_state(bend_id: str):
    _bend(bend_id)
    return engine.state(bend_id)


@app.post("/api/bends/{bend_id}/weather")
def set_weather(bend_id: str, body: WeatherIn):
    _bend(bend_id)
    if body.weather not in WEATHER:
        raise HTTPException(422, f"weather must be one of {list(WEATHER)}")
    engine.set_weather(bend_id, body.weather)
    return engine.state(bend_id)


@app.post("/api/report")
def report(r: Report):
    _bend(r.bend_id)
    return engine.report(r.bend_id, r.vehicle_id, r.lat, r.lon, r.speed_kmh)


@app.post("/api/cctv/{bend_id}")
def cctv(bend_id: str, c: CctvCounts):
    _bend(bend_id)
    engine.cctv_update(bend_id, c.approach_a, c.approach_b, c.in_bend_from_a, c.in_bend_from_b)
    return engine.state(bend_id)


@app.get("/api/events")
def events(limit: int = 100):
    return list(reversed(engine.events[-limit:]))


@app.websocket("/ws/{bend_id}")
async def ws_state(ws: WebSocket, bend_id: str):
    await ws.accept()
    if bend_id not in engine.bends:
        await ws.close(code=4404)
        return
    try:
        while True:
            await ws.send_json(engine.state(bend_id))
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass


app.mount("/driver", StaticFiles(directory=ROOT.parent / "driver-app", html=True), name="driver")
app.mount("/", StaticFiles(directory=ROOT.parent / "web", html=True), name="web")
