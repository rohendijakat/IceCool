#!/usr/bin/env python3
"""
IceCool - Household climatic control companion for FridgAI.
Single-file app: zones, setpoints, hysteresis, schedules, temperature conversion, CLI, API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

# -----------------------------------------------------------------------------
# CONSTANTS
# -----------------------------------------------------------------------------

ICECOOL_VERSION = (1, 4)
ICECOOL_APP_NAME = "IceCool"
ICECOOL_TEMP_SCALE = 1_000_000_000_000
ICECOOL_MIN_SETPOINT_DECICELSIUS = 0
ICECOOL_MAX_SETPOINT_DECICELSIUS = 500
ICECOOL_MAX_READINGS_PER_ZONE = 60_000
ICECOOL_MAX_HYSTERESIS_BANDS = 2_500
ICECOOL_MAX_SCHEDULE_WINDOWS = 96
ICECOOL_DEFROST_MAX_DURATION = 3600
ICECOOL_MAX_LABEL_LENGTH = 64
ICECOOL_MAX_BATCH_ZONES = 50
ICECOOL_MAX_BATCH_READINGS = 200
ICECOOL_THERMOSTAT_MODE_OFF = 0
ICECOOL_THERMOSTAT_MODE_COOL = 1
ICECOOL_THERMOSTAT_MODE_HEAT = 2
ICECOOL_THERMOSTAT_MODE_AUTO = 3
ICECOOL_MAX_FAN_PRESETS = 8
ICECOOL_MAX_HUMIDITY_PERCENT = 100
ICECOOL_CALIBRATION_OFFSET_MAX = 1_000_000_000_000_000
ICECOOL_MAX_LINKED_ZONES = 16
ICECOOL_DEFAULT_RPC = "https://eth.llamarpc.com"
ICECOOL_CONFIG_DIR = ".icecool"
ICECOOL_ZONES_FILE = "zones.json"
ICECOOL_READINGS_FILE = "readings.json"
ICECOOL_SCHEDULES_FILE = "schedules.json"
ICECOOL_SEED_BASE = 0xFa3c8E1b

# -----------------------------------------------------------------------------
# EXCEPTIONS
# -----------------------------------------------------------------------------


class IceCoolZoneNotFoundError(Exception):
    def __init__(self, zone_id: str) -> None:
        super().__init__(f"Zone not found: {zone_id}")
        self.zone_id = zone_id


class IceCoolZoneArchivedError(Exception):
    def __init__(self, zone_id: str) -> None:
        super().__init__(f"Zone is archived: {zone_id}")
        self.zone_id = zone_id


class IceCoolSetpointOutOfBoundsError(Exception):
    def __init__(self, value: int, min_v: int, max_v: int) -> None:
        super().__init__(f"Setpoint {value} out of bounds [{min_v}, {max_v}]")
        self.value = value
        self.min_v = min_v
        self.max_v = max_v


class IceCoolReadingIndexError(Exception):
    def __init__(self, index: int, maximum: int) -> None:
        super().__init__(f"Reading index {index} out of range [0, {maximum})")
        self.index = index
        self.maximum = maximum


class IceCoolHysteresisBandError(Exception):
    def __init__(self, low: float, high: float) -> None:
        super().__init__(f"Invalid hysteresis band: low={low} must be < high={high}")
        self.low = low
        self.high = high


class IceCoolScheduleWindowError(Exception):
    def __init__(self, start: int, end: int) -> None:
        super().__init__(f"Invalid schedule window: start={start} must be < end={end}")
        self.start = start
        self.end = end


class IceCoolConfigError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class IceCoolRPCError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class IceCoolLabelTooLongError(Exception):
    def __init__(self, length: int, max_len: int) -> None:
        super().__init__(f"Label length {length} exceeds max {max_len}")
        self.length = length
        self.max_len = max_len


# -----------------------------------------------------------------------------
# DATA STRUCTURES
# -----------------------------------------------------------------------------


@dataclass
class ZoneRecord:
    zone_id: str
    zone_hash: str
    setpoint_decicelsius: int
    created_at: float
    cooling_preferred: bool
    last_suggested_setpoint: int = 0
    calibration_offset: int = 0
    humidity_snapshot: int = 0
    thermostat_mode: int = ICECOOL_THERMOSTAT_MODE_COOL
    frost_guard_enabled: bool = False
    label: str = ""

    def setpoint_celsius(self) -> float:
        return self.setpoint_decicelsius / 10.0

    def setpoint_fahrenheit(self) -> float:
        return self.setpoint_celsius() * 9 / 5 + 32


@dataclass
class SetpointReadingRecord:
    zone_id: str
    reading_index: int
    temp_scaled: int
    sensor_root: str
    recorded_at: float

    @property
    def temp_decicelsius(self) -> float:
        return self.temp_scaled / ICECOOL_TEMP_SCALE

    @property
    def temp_celsius(self) -> float:
        return self.temp_decicelsius / 10.0


@dataclass
class HysteresisBandRecord:
    zone_id: str
    band_index: int
    low_threshold_scaled: int
    high_threshold_scaled: int

    @property
    def low_celsius(self) -> float:
        return self.low_threshold_scaled / ICECOOL_TEMP_SCALE / 10.0

    @property
    def high_celsius(self) -> float:
        return self.high_threshold_scaled / ICECOOL_TEMP_SCALE / 10.0


@dataclass
class ScheduleWindowRecord:
    zone_id: str
    start_block: int
    end_block: int
    setpoint_decicelsius: int


@dataclass
class IceCoolConfig:
    rpc_url: str = ICECOOL_DEFAULT_RPC
    contract_address: str = ""
    private_key_path: str = ""
    chain_id: int = 1
    anchor_fee_wei: int = 1_000_000_000_000_000
    poll_interval_seconds: float = 15.0
    default_setpoint_decicelsius: int = 220  # 22.0 C
    log_level: str = "INFO"


# -----------------------------------------------------------------------------
# TEMPERATURE CONVERSION
# -----------------------------------------------------------------------------


def celsius_to_decicelsius(celsius: float) -> int:
    return int(round(celsius * 10))


def decicelsius_to_celsius(decicelsius: Union[int, float]) -> float:
    return decicelsius / 10.0


def fahrenheit_to_decicelsius(fahrenheit: float) -> int:
    return int(round((fahrenheit - 32) * 5 / 9 * 10))


def decicelsius_to_fahrenheit(decicelsius: Union[int, float]) -> float:
    return decicelsius / 10.0 * 9 / 5 + 32


def celsius_to_scaled(celsius: float) -> int:
    return int(round(celsius * 10 * ICECOOL_TEMP_SCALE))


def scaled_to_celsius(scaled: int) -> float:
    return scaled / ICECOOL_TEMP_SCALE / 10.0


def dewpoint_approx(temp_decicelsius: float, humidity_percent: int) -> float:
    if humidity_percent <= 0:
        return temp_decicelsius / 10.0
    t = temp_decicelsius / 10.0
    h = humidity_percent / 100.0
    a = 17.27
    b = 237.7
    numer = a * t / (b + t) + math.log(max(h, 0.01))
    denom = a - numer
    if abs(denom) < 1e-9:
        return t
    return (b * numer) / denom


# -----------------------------------------------------------------------------
# ZONE HASH & VALIDATION
# -----------------------------------------------------------------------------


def compute_zone_hash(zone_id: str, setpoint: int, cooling: bool, extra: str = "") -> str:
    payload = f"FridgAI.Climate.v12|{zone_id}|{setpoint}|{cooling}|{extra}"
    return hashlib.sha256(payload.encode()).hexdigest()


def bytes32_from_hex(s: str) -> str:
    if s.startswith("0x"):
        s = s[2:]
    return "0x" + s.zfill(64)[:64]


def validate_setpoint(decicelsius: int) -> None:
    if not (ICECOOL_MIN_SETPOINT_DECICELSIUS <= decicelsius <= ICECOOL_MAX_SETPOINT_DECICELSIUS):
        raise IceCoolSetpointOutOfBoundsError(
            decicelsius, ICECOOL_MIN_SETPOINT_DECICELSIUS, ICECOOL_MAX_SETPOINT_DECICELSIUS
        )


def validate_label(label: str) -> None:
    if len(label) > ICECOOL_MAX_LABEL_LENGTH:
        raise IceCoolLabelTooLongError(len(label), ICECOOL_MAX_LABEL_LENGTH)


def validate_hysteresis_band(low_scaled: int, high_scaled: int) -> None:
    if low_scaled >= high_scaled:
        raise IceCoolHysteresisBandError(
            scaled_to_celsius(low_scaled), scaled_to_celsius(high_scaled)
        )


def validate_schedule_window(start_block: int, end_block: int) -> None:
    if start_block >= end_block:
        raise IceCoolScheduleWindowError(start_block, end_block)


# -----------------------------------------------------------------------------
# HYSTERESIS LOGIC
# -----------------------------------------------------------------------------


def within_hysteresis(reading_scaled: int, low_scaled: int, high_scaled: int) -> bool:
    return low_scaled <= reading_scaled <= high_scaled


def suggest_cooling(reading_scaled: int, setpoint_scaled: int, high_threshold_scaled: int) -> bool:
    return reading_scaled > high_threshold_scaled


def suggest_heating(reading_scaled: int, setpoint_scaled: int, low_threshold_scaled: int) -> bool:
    return reading_scaled < low_threshold_scaled


# -----------------------------------------------------------------------------
# IN-MEMORY STORE
# -----------------------------------------------------------------------------


class IceCoolStore:
    def __init__(self) -> None:
        self._zones: Dict[str, ZoneRecord] = {}
        self._readings: Dict[str, List[SetpointReadingRecord]] = {}
        self._bands: Dict[str, List[HysteresisBandRecord]] = {}
        self._schedules: Dict[str, List[ScheduleWindowRecord]] = {}
        self._linked: Dict[str, List[str]] = {}
        self._archived: set = set()

    def add_zone(self, z: ZoneRecord) -> None:
        validate_setpoint(z.setpoint_decicelsius)
        if z.zone_id in self._zones:
            raise IceCoolConfigError(f"Zone already exists: {z.zone_id}")
        self._zones[z.zone_id] = z
        self._readings[z.zone_id] = []
        self._bands[z.zone_id] = []
        self._schedules[z.zone_id] = []
        self._linked[z.zone_id] = []

    def get_zone(self, zone_id: str) -> ZoneRecord:
        if zone_id not in self._zones:
            raise IceCoolZoneNotFoundError(zone_id)
        return self._zones[zone_id]

    def list_zone_ids(self) -> List[str]:
        return list(self._zones.keys())

    def archive_zone(self, zone_id: str) -> None:
        self.get_zone(zone_id)
        self._archived.add(zone_id)

    def is_archived(self, zone_id: str) -> bool:
        return zone_id in self._archived

    def add_reading(self, r: SetpointReadingRecord) -> None:
        self.get_zone(r.zone_id)
        if r.zone_id not in self._readings:
            self._readings[r.zone_id] = []
        arr = self._readings[r.zone_id]
        if r.reading_index >= ICECOOL_MAX_READINGS_PER_ZONE:
            raise IceCoolReadingIndexError(r.reading_index, ICECOOL_MAX_READINGS_PER_ZONE)
        while len(arr) <= r.reading_index:
            arr.append(None)
        arr[r.reading_index] = r

    def get_reading(self, zone_id: str, index: int) -> Optional[SetpointReadingRecord]:
        arr = self._readings.get(zone_id, [])
        if index >= len(arr):
            return None
        return arr[index]

    def reading_count(self, zone_id: str) -> int:
        arr = self._readings.get(zone_id, [])
        return sum(1 for x in arr if x is not None)

    def add_band(self, b: HysteresisBandRecord) -> None:
        self.get_zone(b.zone_id)
        validate_hysteresis_band(b.low_threshold_scaled, b.high_threshold_scaled)
        if b.zone_id not in self._bands:
            self._bands[b.zone_id] = []
        if len(self._bands[b.zone_id]) >= ICECOOL_MAX_HYSTERESIS_BANDS:
            raise IceCoolConfigError("Max hysteresis bands reached")
        self._bands[b.zone_id].append(b)

    def get_bands(self, zone_id: str) -> List[HysteresisBandRecord]:
        return list(self._bands.get(zone_id, []))

    def add_schedule_window(self, w: ScheduleWindowRecord) -> None:
        self.get_zone(w.zone_id)
        validate_schedule_window(w.start_block, w.end_block)
        validate_setpoint(w.setpoint_decicelsius)
        arr = self._schedules.get(w.zone_id, [])
        if len(arr) >= ICECOOL_MAX_SCHEDULE_WINDOWS:
            raise IceCoolConfigError("Max schedule windows reached")
        self._schedules[w.zone_id] = arr
        arr.append(w)

    def get_schedule_windows(self, zone_id: str) -> List[ScheduleWindowRecord]:
        return list(self._schedules.get(zone_id, []))

    def link_zones(self, zone_a: str, zone_b: str) -> None:
        self.get_zone(zone_a)
        self.get_zone(zone_b)
        if zone_a == zone_b:
            raise IceCoolConfigError("Cannot link zone to itself")
        for zid, links in self._linked.items():
            if zone_a == zid and zone_b in links:
                raise IceCoolConfigError("Zones already linked")
        self._linked.setdefault(zone_a, []).append(zone_b)
        self._linked.setdefault(zone_b, []).append(zone_a)

    def get_linked(self, zone_id: str) -> List[str]:
        return list(self._linked.get(zone_id, []))

    def effective_setpoint_at_block(self, zone_id: str, block_num: int) -> int:
        z = self.get_zone(zone_id)
        for w in self.get_schedule_windows(zone_id):
            if w.start_block <= block_num <= w.end_block:
                return w.setpoint_decicelsius
        return z.setpoint_decicelsius

    def save_to_dir(self, base_path: Path) -> None:
        base_path.mkdir(parents=True, exist_ok=True)
        zones_data = []
        for z in self._zones.values():
            zones_data.append({
                "zone_id": z.zone_id,
                "zone_hash": z.zone_hash,
                "setpoint_decicelsius": z.setpoint_decicelsius,
                "created_at": z.created_at,
                "cooling_preferred": z.cooling_preferred,
                "last_suggested_setpoint": z.last_suggested_setpoint,
                "calibration_offset": z.calibration_offset,
                "humidity_snapshot": z.humidity_snapshot,
                "thermostat_mode": z.thermostat_mode,
                "frost_guard_enabled": z.frost_guard_enabled,
                "label": z.label,
            })
        (base_path / ICECOOL_ZONES_FILE).write_text(json.dumps(zones_data, indent=2))
        readings_data = {}
        for zone_id, arr in self._readings.items():
            readings_data[zone_id] = [
                {
                    "reading_index": r.reading_index,
                    "temp_scaled": r.temp_scaled,
                    "sensor_root": r.sensor_root,
                    "recorded_at": r.recorded_at,
                }
                for r in arr if r is not None
            ]
        (base_path / ICECOOL_READINGS_FILE).write_text(json.dumps(readings_data, indent=2))
        sched_data = {}
        for zone_id, arr in self._schedules.items():
            sched_data[zone_id] = [
                {"start_block": w.start_block, "end_block": w.end_block, "setpoint_decicelsius": w.setpoint_decicelsius}
                for w in arr
            ]
        (base_path / ICECOOL_SCHEDULES_FILE).write_text(json.dumps(sched_data, indent=2))

    def load_from_dir(self, base_path: Path) -> None:
        zpath = base_path / ICECOOL_ZONES_FILE
        if not zpath.exists():
            return
        zones_data = json.loads(zpath.read_text())
        for d in zones_data:
            z = ZoneRecord(
                zone_id=d["zone_id"],
                zone_hash=d["zone_hash"],
                setpoint_decicelsius=d["setpoint_decicelsius"],
                created_at=d["created_at"],
                cooling_preferred=d["cooling_preferred"],
                last_suggested_setpoint=d.get("last_suggested_setpoint", 0),
                calibration_offset=d.get("calibration_offset", 0),
                humidity_snapshot=d.get("humidity_snapshot", 0),
                thermostat_mode=d.get("thermostat_mode", ICECOOL_THERMOSTAT_MODE_COOL),
                frost_guard_enabled=d.get("frost_guard_enabled", False),
                label=d.get("label", ""),
            )
            self._zones[z.zone_id] = z
            self._readings[z.zone_id] = []
            self._bands[z.zone_id] = []
            self._schedules[z.zone_id] = []
            self._linked[z.zone_id] = []
        rpath = base_path / ICECOOL_READINGS_FILE
        if rpath.exists():
            readings_data = json.loads(rpath.read_text())
            for zone_id, arr in readings_data.items():
                if zone_id not in self._readings:
                    self._readings[zone_id] = []
                for d in arr:
                    r = SetpointReadingRecord(
                        zone_id=zone_id,
                        reading_index=d["reading_index"],
                        temp_scaled=d["temp_scaled"],
                        sensor_root=d["sensor_root"],
                        recorded_at=d["recorded_at"],
                    )
                    while len(self._readings[zone_id]) <= r.reading_index:
                        self._readings[zone_id].append(None)
                    self._readings[zone_id][r.reading_index] = r
        spath = base_path / ICECOOL_SCHEDULES_FILE
        if spath.exists():
            sched_data = json.loads(spath.read_text())
            for zone_id, arr in sched_data.items():
                if zone_id not in self._schedules:
                    self._schedules[zone_id] = []
                for d in arr:
                    w = ScheduleWindowRecord(
                        zone_id=zone_id,
                        start_block=d["start_block"],
                        end_block=d["end_block"],
                        setpoint_decicelsius=d["setpoint_decicelsius"],
                    )
                    self._schedules[zone_id].append(w)


# -----------------------------------------------------------------------------
# CONFIG LOAD / SAVE
# -----------------------------------------------------------------------------


def config_path() -> Path:
    home = Path.home()
    return home / ICECOOL_CONFIG_DIR / "config.json"


def load_config() -> IceCoolConfig:
    p = config_path()
    if not p.exists():
        return IceCoolConfig()
    data = json.loads(p.read_text())
    return IceCoolConfig(
        rpc_url=data.get("rpc_url", ICECOOL_DEFAULT_RPC),
        contract_address=data.get("contract_address", ""),
        private_key_path=data.get("private_key_path", ""),
        chain_id=data.get("chain_id", 1),
        anchor_fee_wei=data.get("anchor_fee_wei", 1_000_000_000_000_000),
        poll_interval_seconds=data.get("poll_interval_seconds", 15.0),
        default_setpoint_decicelsius=data.get("default_setpoint_decicelsius", 220),
        log_level=data.get("log_level", "INFO"),
    )


def save_config(cfg: IceCoolConfig) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "rpc_url": cfg.rpc_url,
        "contract_address": cfg.contract_address,
        "private_key_path": cfg.private_key_path,
        "chain_id": cfg.chain_id,
        "anchor_fee_wei": cfg.anchor_fee_wei,
        "poll_interval_seconds": cfg.poll_interval_seconds,
        "default_setpoint_decicelsius": cfg.default_setpoint_decicelsius,
        "log_level": cfg.log_level,
    }, indent=2))


# -----------------------------------------------------------------------------
# CLI: ZONE COMMANDS
# -----------------------------------------------------------------------------


def cmd_zone_add(store: IceCoolStore, zone_id: str, setpoint_decicelsius: int, cooling: bool, label: str = "") -> None:
    h = compute_zone_hash(zone_id, setpoint_decicelsius, cooling)
    z = ZoneRecord(
        zone_id=zone_id,
        zone_hash=h,
        setpoint_decicelsius=setpoint_decicelsius,
        created_at=time.time(),
        cooling_preferred=cooling,
        label=label[:ICECOOL_MAX_LABEL_LENGTH] if label else "",
    )
    store.add_zone(z)
    print(f"Zone {zone_id} added with setpoint {setpoint_decicelsius} (0.1°C), cooling_preferred={cooling}")


def cmd_zone_list(store: IceCoolStore) -> None:
    for zid in store.list_zone_ids():
        z = store.get_zone(zid)
        print(f"  {z.zone_id}  setpoint={z.setpoint_decicelsius} ({(z.setpoint_decicelsius/10):.1f}°C)  cooling={z.cooling_preferred}  label={z.label or '-'}")


def cmd_zone_show(store: IceCoolStore, zone_id: str) -> None:
    z = store.get_zone(zone_id)
    n_read = store.reading_count(zone_id)
    bands = store.get_bands(zone_id)
    sched = store.get_schedule_windows(zone_id)
    linked = store.get_linked(zone_id)
    print(f"Zone: {z.zone_id}")
    print(f"  setpoint: {z.setpoint_decicelsius} ({z.setpoint_celsius():.1f}°C)")
    print(f"  cooling_preferred: {z.cooling_preferred}")
    print(f"  label: {z.label or '-'}")
    print(f"  readings: {n_read}")
    print(f"  hysteresis bands: {len(bands)}")
    print(f"  schedule windows: {len(sched)}")
    print(f"  linked zones: {linked}")


def cmd_reading_add(store: IceCoolStore, zone_id: str, temp_celsius: float, sensor_root: str = "") -> None:
    store.get_zone(zone_id)
    idx = store.reading_count(zone_id)
    scaled = celsius_to_scaled(temp_celsius)
    if not sensor_root:
        sensor_root = hashlib.sha256(f"{zone_id}{idx}{time.time()}".encode()).hexdigest()
    r = SetpointReadingRecord(
        zone_id=zone_id,
        reading_index=idx,
        temp_scaled=scaled,
        sensor_root=sensor_root,
        recorded_at=time.time(),
    )
    store.add_reading(r)
    print(f"Reading {idx} added for zone {zone_id}: {temp_celsius}°C (scaled={scaled})")


def cmd_band_add(store: IceCoolStore, zone_id: str, low_celsius: float, high_celsius: float) -> None:
    low_s = celsius_to_scaled(low_celsius)
    high_s = celsius_to_scaled(high_celsius)
    bands = store.get_bands(zone_id)
    idx = len(bands)
    b = HysteresisBandRecord(
        zone_id=zone_id,
        band_index=idx,
        low_threshold_scaled=low_s,
        high_threshold_scaled=high_s,
    )
    store.add_band(b)
    print(f"Hysteresis band {idx} added for {zone_id}: [{low_celsius}, {high_celsius}]°C")


def cmd_schedule_add(store: IceCoolStore, zone_id: str, start_block: int, end_block: int, setpoint_decicelsius: int) -> None:
    w = ScheduleWindowRecord(
        zone_id=zone_id,
        start_block=start_block,
        end_block=end_block,
        setpoint_decicelsius=setpoint_decicelsius,
    )
    store.add_schedule_window(w)
    print(f"Schedule window added for {zone_id}: blocks [{start_block}, {end_block}] setpoint={setpoint_decicelsius}")


def cmd_link(store: IceCoolStore, zone_a: str, zone_b: str) -> None:
    store.link_zones(zone_a, zone_b)
    print(f"Linked {zone_a} <-> {zone_b}")


# -----------------------------------------------------------------------------
# SIMULATE / SUGGEST
# -----------------------------------------------------------------------------


def suggest_mode(reading_celsius: float, setpoint_celsius: float, low_c: float, high_c: float) -> str:
    if reading_celsius > high_c:
        return "COOL"
    if reading_celsius < low_c:
        return "HEAT"
    return "HOLD"


def simulate_effective_setpoint(store: IceCoolStore, zone_id: str, block_num: int) -> int:
    return store.effective_setpoint_at_block(zone_id, block_num)


# -----------------------------------------------------------------------------
# MAIN CLI
# -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(prog=ICECOOL_APP_NAME, description="Household climatic control companion for FridgAI")
    parser.add_argument("--version", action="store_true", help="Show version")
    parser.add_argument("--config-dir", type=str, default="", help="Config directory (default: ~/.icecool)")
    sub = parser.add_subparsers(dest="command", help="Commands")

    # zone add
    p_add = sub.add_parser("zone-add", help="Add a zone")
    p_add.add_argument("zone_id", type=str, help="Zone ID")
    p_add.add_argument("--setpoint", type=float, default=22.0, help="Setpoint in Celsius")
    p_add.add_argument("--cooling", action="store_true", help="Prefer cooling")
    p_add.add_argument("--label", type=str, default="", help="Optional label")

    # zone list
    sub.add_parser("zone-list", help="List zones")

    # zone show
    p_show = sub.add_parser("zone-show", help="Show zone details")
    p_show.add_argument("zone_id", type=str)

    # reading add
    p_reading = sub.add_parser("reading-add", help="Add a temperature reading")
    p_reading.add_argument("zone_id", type=str)
    p_reading.add_argument("temp_celsius", type=float)
    p_reading.add_argument("--sensor-root", type=str, default="")

    # band add
    p_band = sub.add_parser("band-add", help="Add hysteresis band")
    p_band.add_argument("zone_id", type=str)
    p_band.add_argument("low_celsius", type=float)
    p_band.add_argument("high_celsius", type=float)

    # schedule add
    p_sched = sub.add_parser("schedule-add", help="Add schedule window")
    p_sched.add_argument("zone_id", type=str)
    p_sched.add_argument("start_block", type=int)
    p_sched.add_argument("end_block", type=int)
    p_sched.add_argument("--setpoint", type=float, default=22.0, help="Setpoint Celsius")

    # link
    p_link = sub.add_parser("link", help="Link two zones")
    p_link.add_argument("zone_a", type=str)
    p_link.add_argument("zone_b", type=str)

    # save / load
    p_save = sub.add_parser("save", help="Save store to config dir")
    p_save.add_argument("--path", type=str, default="", help="Override path")
    p_load = sub.add_parser("load", help="Load store from config dir")
    p_load.add_argument("--path", type=str, default="", help="Override path")

    # effective setpoint
    p_eff = sub.add_parser("effective-setpoint", help="Get effective setpoint at block")
    p_eff.add_argument("zone_id", type=str)
    p_eff.add_argument("block_num", type=int)

    args = parser.parse_args()

    if args.version:
        print(f"{ICECOOL_APP_NAME} v{ICECOOL_VERSION[0]}.{ICECOOL_VERSION[1]}")
        return 0

    config_dir = Path(args.config_dir) if args.config_dir else Path.home() / ICECOOL_CONFIG_DIR
    store = IceCoolStore()
    load_path = config_dir / "store"
    if (config_dir / "store" / ICECOOL_ZONES_FILE).exists():
        store.load_from_dir(load_path)

    if args.command == "zone-add":
        setpoint_d = celsius_to_decicelsius(args.setpoint)
        cmd_zone_add(store, args.zone_id, setpoint_d, args.cooling, args.label)
    elif args.command == "zone-list":
        cmd_zone_list(store)
    elif args.command == "zone-show":
        cmd_zone_show(store, args.zone_id)
    elif args.command == "reading-add":
        cmd_reading_add(store, args.zone_id, args.temp_celsius, args.sensor_root)
    elif args.command == "band-add":
        cmd_band_add(store, args.zone_id, args.low_celsius, args.high_celsius)
    elif args.command == "schedule-add":
        setpoint_d = celsius_to_decicelsius(args.setpoint)
        cmd_schedule_add(store, args.zone_id, args.start_block, args.end_block, setpoint_d)
    elif args.command == "link":
