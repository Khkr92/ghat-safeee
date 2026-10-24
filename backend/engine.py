"""GHAT-SAFE bend-state engine.

Pure Python, no web framework, so it can be unit-tested and reused.

Each bend is a road centreline (list of lat/lon points) running from Approach A
to Approach B. A GPS fix is projected onto the centreline to get its chainage s
(metres from the A end). The blind part of the hairpin lies between
bend_start and bend_end. Warning zones extend one geofence length before the
bend on each side.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

G = 9.81
T_REACT = 2.5          # perception-reaction time, s
LAG_S = 2.0            # GPS update + mobile network delay allowance, s
STALE_S = 5.0          # drop a phone that has not reported for this long
CCTV_FRESH_S = 3.0     # CCTV snapshot is trusted for this long
OVERSPEED_MARGIN = 5   # km/h above advisory before an overspeed alert

WEATHER = {
    "clear": {"advisory": 25, "f": 0.40},
    "rain": {"advisory": 20, "f": 0.30},
    "fog": {"advisory": 20, "f": 0.35},
}

MESSAGES = {
    "clear": ("HAIRPIN AHEAD", "No oncoming vehicle. Keep below {adv} km/h.",
              "Hairpin bend ahead. Keep below {adv}."),
    "oncoming": ("VEHICLE AHEAD - SLOW", "Oncoming vehicle near the blind bend. Slow below {adv} km/h.",
                 "Vehicle approaching from the other side. Slow down."),
    "risk": ("HEAD-ON RISK - HOLD LEFT", "Oncoming vehicle is inside the bend. Hold left, 15 km/h.",
             "Danger. Oncoming vehicle in the bend. Keep left and slow to fifteen."),
    "overspeed": ("SLOW DOWN", "{spd} km/h in a {adv} km/h hairpin zone.",
                  "You are over the safe speed for this bend."),
    "cruise": ("CRUISING", "No bend within warning range.", ""),
    "off_route": ("NOT ON THIS ROAD", "Position is away from the bend centreline.", ""),
}


def ssd(v_kmh: float, f: float, t: float = T_REACT) -> float:
    """Stopping sight distance in metres: v*t + v^2 / (2 g f)."""
    v = v_kmh / 3.6
    return v * t + v * v / (2 * G * f)


def zone_length(approach_kmh: float, f: float) -> float:
    """Geofence length = SSD + distance travelled during GPS/network lag."""
    return ssd(approach_kmh, f) + approach_kmh / 3.6 * LAG_S


class Centreline:
    """Polyline in a local metric frame, with chainage and projection."""

    def __init__(self, points: list[list[float]]):
        if len(points) < 2:
            raise ValueError("centreline needs at least two points")
        self.lat0, self.lon0 = points[0]
        self.kx = 111_320 * math.cos(math.radians(self.lat0))
        self.ky = 110_540
        self.xy = [self._to_xy(lat, lon) for lat, lon in points]
        self.ch = [0.0]
        for (x1, y1), (x2, y2) in zip(self.xy, self.xy[1:]):
            self.ch.append(self.ch[-1] + math.hypot(x2 - x1, y2 - y1))

    @property
    def length(self) -> float:
        return self.ch[-1]

    def _to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

    def project(self, lat: float, lon: float) -> tuple[float, float]:
        """Return (chainage_m, perpendicular_offset_m) of the nearest point."""
        px, py = self._to_xy(lat, lon)
        best = (0.0, float("inf"))
        for i, ((x1, y1), (x2, y2)) in enumerate(zip(self.xy, self.xy[1:])):
            dx, dy = x2 - x1, y2 - y1
            seg2 = dx * dx + dy * dy
            u = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg2))
            qx, qy = x1 + u * dx, y1 + u * dy
            d = math.hypot(px - qx, py - qy)
            if d < best[1]:
                best = (self.ch[i] + u * math.sqrt(seg2), d)
        return best

    def point_at(self, s: float) -> tuple[float, float]:
        """Lat/lon at chainage s (used by demo drives and tests)."""
        s = max(0.0, min(self.length, s))
        for i in range(len(self.ch) - 1):
            if self.ch[i + 1] >= s:
                seg = self.ch[i + 1] - self.ch[i]
                u = 0 if seg == 0 else (s - self.ch[i]) / seg
                x = self.xy[i][0] + u * (self.xy[i + 1][0] - self.xy[i][0])
                y = self.xy[i][1] + u * (self.xy[i + 1][1] - self.xy[i][1])
                return (self.lat0 + y / self.ky, self.lon0 + x / self.kx)
        return (self.lat0 + self.xy[-1][1] / self.ky, self.lon0 + self.xy[-1][0] / self.kx)


@dataclass
class Bend:
    id: str
    name: str
    points: list[list[float]]
    bend_start: float          # chainage where the blind curve begins (m)
    bend_end: float            # chainage where it ends (m)
    approach_kmh: float = 40
    weather: str = "clear"
    max_offset: float = 30     # fixes farther than this from the centreline are ignored
    line: Centreline = field(init=False, repr=False)

    def __post_init__(self):
        self.line = Centreline(self.points)

    @property
    def advisory(self) -> int:
        return WEATHER[self.weather]["advisory"]

    @property
    def zone(self) -> float:
        return zone_length(self.approach_kmh, WEATHER[self.weather]["f"])


@dataclass
class Vehicle:
    id: str
    s: float
    speed: float
    ts: float
    direction: int = 0         # +1 travelling A->B, -1 travelling B->A, 0 unknown
    state: str = "cruise"
    over_logged: bool = False


class Engine:
    def __init__(self):
        self.bends: dict[str, Bend] = {}
        self.vehicles: dict[str, dict[str, Vehicle]] = {}
        self.cctv: dict[str, dict] = {}
        self.events: list[dict] = []

    # ---------- setup ----------
    def add_bend(self, bend: Bend) -> None:
        self.bends[bend.id] = bend
        self.vehicles[bend.id] = {}

    def set_weather(self, bend_id: str, weather: str) -> None:
        if weather not in WEATHER:
            raise ValueError(f"weather must be one of {list(WEATHER)}")
        self.bends[bend_id].weather = weather

    # ---------- inputs ----------
    def report(self, bend_id: str, vehicle_id: str, lat: float, lon: float,
               speed_kmh: float, ts: float | None = None) -> dict:
        """Phone GPS fix. Returns the alert this driver should see."""
        b = self.bends[bend_id]
        now = time.time() if ts is None else ts
        s, off = b.line.project(lat, lon)
        fleet = self.vehicles[bend_id]
        if off > b.max_offset:
            fleet.pop(vehicle_id, None)
            return self._alert(b, None, "off_route")
        prev = fleet.get(vehicle_id)
        direction = prev.direction if prev else 0
        if prev and abs(s - prev.s) > 1.0:
            direction = 1 if s > prev.s else -1
        elif direction == 0:
            # first fix: assume the driver is heading into the bend
            direction = 1 if s < b.bend_start else (-1 if s > b.bend_end else 0)
        v = prev or Vehicle(vehicle_id, s, speed_kmh, now)
        v.s, v.speed, v.ts, v.direction = s, speed_kmh, now, direction
        fleet[vehicle_id] = v
        return self._evaluate_vehicle(b, v, now)

    def cctv_update(self, bend_id: str, approach_a: int, approach_b: int,
                    in_bend_from_a: int, in_bend_from_b: int, ts: float | None = None) -> None:
        """Vehicle counts from a camera at the bend (vehicles without the app)."""
        self.cctv[bend_id] = {
            "approach_a": approach_a, "approach_b": approach_b,
            "in_bend_from_a": in_bend_from_a, "in_bend_from_b": in_bend_from_b,
            "ts": time.time() if ts is None else ts,
        }

    # ---------- logic ----------
    def occupancy(self, bend_id: str, now: float | None = None) -> dict:
        b = self.bends[bend_id]
        now = time.time() if now is None else now
        fleet = self.vehicles[bend_id]
        for vid in [k for k, v in fleet.items() if now - v.ts > STALE_S]:
            del fleet[vid]
        z, bs, be = b.zone, b.bend_start, b.bend_end
        occ = {"a_zone": 0, "a_bend": 0, "b_zone": 0, "b_bend": 0}
        for v in fleet.values():
            if v.direction > 0 and bs - z <= v.s <= be:
                occ["a_zone"] += 1
                occ["a_bend"] += bs <= v.s <= be
            elif v.direction < 0 and bs <= v.s <= be + z:
                occ["b_zone"] += 1
                occ["b_bend"] += bs <= v.s <= be
        cam = self.cctv.get(bend_id)
        if cam and now - cam["ts"] <= CCTV_FRESH_S:
            occ["a_zone"] = max(occ["a_zone"], cam["approach_a"] + cam["in_bend_from_a"])
            occ["b_zone"] = max(occ["b_zone"], cam["approach_b"] + cam["in_bend_from_b"])
            occ["a_bend"] = max(occ["a_bend"], cam["in_bend_from_a"])
            occ["b_bend"] = max(occ["b_bend"], cam["in_bend_from_b"])
        return occ

    def signs(self, bend_id: str, now: float | None = None) -> dict:
        """What each approach should be told."""
        o = self.occupancy(bend_id, now)
        sign_a = "risk" if o["b_bend"] else ("oncoming" if o["b_zone"] else "clear")
        sign_b = "risk" if o["a_bend"] else ("oncoming" if o["a_zone"] else "clear")
        return {"A": sign_a, "B": sign_b, "occupancy": o}

    def _evaluate_vehicle(self, b: Bend, v: Vehicle, now: float) -> dict:
        z, bs, be = b.zone, b.bend_start, b.bend_end
        in_zone = (v.direction > 0 and bs - z <= v.s <= be) or (v.direction < 0 and bs <= v.s <= be + z)
        state = "cruise"
        if in_zone:
            sg = self.signs(b.id, now)
            state = sg["A"] if v.direction > 0 else sg["B"]
            if v.speed > b.advisory + OVERSPEED_MARGIN and state != "risk":
                state = "overspeed"
        if state != v.state:
            if state in ("oncoming", "risk") or (state == "overspeed" and not v.over_logged):
                self._log(b.id, state, v)
                if state == "overspeed":
                    v.over_logged = True
            v.state = state
        return self._alert(b, v, state)

    def _alert(self, b: Bend, v: Vehicle | None, state: str) -> dict:
        title, detail, voice = MESSAGES[state]
        spd = round(v.speed) if v else 0
        dist = None
        if v:
            dist = (b.bend_start - v.s) if v.direction > 0 else (v.s - b.bend_end)
        return {
            "bend_id": b.id, "state": state, "title": title,
            "detail": detail.format(adv=b.advisory, spd=spd), "voice": voice.format(adv=b.advisory),
            "advisory_kmh": b.advisory, "distance_to_bend_m": None if dist is None else round(dist, 1),
            "direction": None if not v else ("A->B" if v.direction > 0 else "B->A" if v.direction < 0 else "unknown"),
        }

    def _log(self, bend_id: str, kind: str, v: Vehicle) -> None:
        self.events.append({"ts": time.time(), "bend_id": bend_id, "type": kind,
                            "vehicle": v.id, "speed_kmh": round(v.speed, 1), "chainage_m": round(v.s, 1)})
        del self.events[:-1000]

    def state(self, bend_id: str) -> dict:
        b = self.bends[bend_id]
        sg = self.signs(bend_id)
        return {
            "bend_id": b.id, "name": b.name, "weather": b.weather, "advisory_kmh": b.advisory,
            "zone_length_m": round(b.zone, 1), "ssd_m": round(ssd(b.approach_kmh, WEATHER[b.weather]["f"]), 1),
            "sign_A": sg["A"], "sign_B": sg["B"], "occupancy": sg["occupancy"],
            "vehicles": len(self.vehicles[bend_id]),
        }
