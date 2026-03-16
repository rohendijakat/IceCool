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
        cmd_link(store, args.zone_a, args.zone_b)
    elif args.command == "save":
        path = Path(args.path) if args.path else load_path
        store.save_to_dir(path)
        print(f"Saved to {path}")
    elif args.command == "load":
        path = Path(args.path) if args.path else load_path
        store.load_from_dir(path)
        print(f"Loaded from {path}")
    elif args.command == "effective-setpoint":
        sp = simulate_effective_setpoint(store, args.zone_id, args.block_num)
        print(f"Effective setpoint: {sp} (0.1°C) = {decicelsius_to_celsius(sp):.1f}°C")
    else:
        parser.print_help()
        return 0

    if args.command in ("zone-add", "reading-add", "band-add", "schedule-add", "link"):
        store.save_to_dir(load_path)
    return 0


# -----------------------------------------------------------------------------
# WEB3 / CONTRACT INTERACTION (STUB)
# -----------------------------------------------------------------------------


def get_web3_provider(rpc_url: str):
    try:
        from web3 import Web3
        return Web3(Web3.HTTPProvider(rpc_url))
    except ImportError:
        return None


def contract_call_register_zone(
    w3: Any,
    contract_address: str,
    zone_id_hex: str,
    setpoint_decicelsius: int,
    zone_hash_hex: str,
    cooling_preferred: bool,
    value_wei: int,
    private_key: Optional[str] = None,
) -> Optional[str]:
    if not w3 or not contract_address:
        return None
    try:
        from web3 import Web3
        acct = w3.eth.account.from_key(private_key) if private_key else None
        # ABI fragment for registerZone(bytes32,uint16,bytes32,bool)
        # In production use full ABI and contract.functions.registerZone(...)
        return "0x" + os.urandom(32).hex()
    except Exception:
        return None


def contract_call_record_reading(
    w3: Any,
    contract_address: str,
    zone_id_hex: str,
    reading_index: int,
    temp_scaled: int,
    sensor_root_hex: str,
    private_key: Optional[str] = None,
) -> Optional[str]:
    if not w3 or not contract_address:
        return None
    return "0x" + os.urandom(32).hex()


def fetch_zone_from_chain(w3: Any, contract_address: str, zone_id_hex: str) -> Optional[Dict[str, Any]]:
    if not w3 or not contract_address:
        return None
    return None


# -----------------------------------------------------------------------------
# HTTP API SERVER (OPTIONAL)
# -----------------------------------------------------------------------------


def run_api_server(store: IceCoolStore, host: str = "127.0.0.1", port: int = 8765) -> None:
    try:
        from http.server import HTTPServer, BaseHTTPRequestHandler
    except ImportError:
        print("HTTP server not available")
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/zones":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                zones = [{"zone_id": zid, "setpoint_decicelsius": store.get_zone(zid).setpoint_decicelsius} for zid in store.list_zone_ids()]
                self.wfile.write(json.dumps(zones).encode())
            elif self.path.startswith("/zone/"):
                zid = self.path.split("/")[-1]
                try:
                    z = store.get_zone(zid)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "zone_id": z.zone_id,
                        "setpoint_decicelsius": z.setpoint_decicelsius,
                        "cooling_preferred": z.cooling_preferred,
                        "label": z.label,
                    }).encode())
                except IceCoolZoneNotFoundError:
                    self.send_response(404)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer((host, port), Handler)
    print(f"Serving at http://{host}:{port}")
    server.serve_forever()


# -----------------------------------------------------------------------------
# BATCH HELPERS
# -----------------------------------------------------------------------------


def batch_add_readings(
    store: IceCoolStore,
    zone_id: str,
    temps_celsius: List[float],
    sensor_roots: Optional[List[str]] = None,
) -> int:
    store.get_zone(zone_id)
    if len(temps_celsius) > ICECOOL_MAX_BATCH_READINGS:
        raise IceCoolConfigError(f"Batch size {len(temps_celsius)} > {ICECOOL_MAX_BATCH_READINGS}")
    start = store.reading_count(zone_id)
    for i, t in enumerate(temps_celsius):
        sr = (sensor_roots[i] if sensor_roots and i < len(sensor_roots) else "") or hashlib.sha256(f"{zone_id}{start+i}{time.time()}".encode()).hexdigest()
        r = SetpointReadingRecord(zone_id=zone_id, reading_index=start + i, temp_scaled=celsius_to_scaled(t), sensor_root=sr, recorded_at=time.time())
        store.add_reading(r)
    return len(temps_celsius)


def batch_add_zones(
    store: IceCoolStore,
    zone_ids: List[str],
    setpoints_decicelsius: List[int],
    cooling_preferred: List[bool],
    labels: Optional[List[str]] = None,
) -> int:
    if len(zone_ids) > ICECOOL_MAX_BATCH_ZONES:
        raise IceCoolConfigError(f"Batch size {len(zone_ids)} > {ICECOOL_MAX_BATCH_ZONES}")
    if len(zone_ids) != len(setpoints_decicelsius) or len(zone_ids) != len(cooling_preferred):
        raise IceCoolConfigError("Length mismatch")
    labels = labels or [""] * len(zone_ids)
    for i, zid in enumerate(zone_ids):
        validate_setpoint(setpoints_decicelsius[i])
        h = compute_zone_hash(zid, setpoints_decicelsius[i], cooling_preferred[i])
        z = ZoneRecord(
            zone_id=zid,
            zone_hash=h,
            setpoint_decicelsius=setpoints_decicelsius[i],
            created_at=time.time(),
            cooling_preferred=cooling_preferred[i],
            label=(labels[i] or "")[:ICECOOL_MAX_LABEL_LENGTH],
        )
        store.add_zone(z)
    return len(zone_ids)


# -----------------------------------------------------------------------------
# EXPORT / IMPORT
# -----------------------------------------------------------------------------


def export_zones_csv(store: IceCoolStore, path: Path) -> None:
    lines = ["zone_id,setpoint_decicelsius,setpoint_celsius,cooling_preferred,label"]
    for zid in store.list_zone_ids():
        z = store.get_zone(zid)
        lines.append(f"{z.zone_id},{z.setpoint_decicelsius},{z.setpoint_celsius():.2f},{z.cooling_preferred},{z.label or ''}")
    path.write_text("\n".join(lines))


def import_zones_csv(store: IceCoolStore, path: Path) -> int:
    text = path.read_text()
    count = 0
    for line in text.strip().split("\n")[1:]:
        parts = line.split(",")
        if len(parts) < 4:
            continue
        zid = parts[0].strip()
        setpoint_d = int(parts[1].strip())
        cooling = parts[3].strip().lower() in ("true", "1", "yes")
        label = parts[4].strip() if len(parts) > 4 else ""
        try:
            cmd_zone_add(store, zid, setpoint_d, cooling, label)
            count += 1
        except Exception:
            pass
    return count


# -----------------------------------------------------------------------------
# LOGGING HELPERS
# -----------------------------------------------------------------------------


def log_debug(msg: str) -> None:
    if os.environ.get("ICECOOL_LOG_LEVEL", "INFO") == "DEBUG":
        print(f"[DEBUG] {msg}")


def log_info(msg: str) -> None:
    print(f"[INFO] {msg}")


def log_warning(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)


def log_error(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)


# -----------------------------------------------------------------------------
# THERMOSTAT MODE NAMES
# -----------------------------------------------------------------------------


def thermostat_mode_name(mode: int) -> str:
    if mode == ICECOOL_THERMOSTAT_MODE_OFF:
        return "OFF"
    if mode == ICECOOL_THERMOSTAT_MODE_COOL:
        return "COOL"
    if mode == ICECOOL_THERMOSTAT_MODE_HEAT:
        return "HEAT"
    if mode == ICECOOL_THERMOSTAT_MODE_AUTO:
        return "AUTO"
    return "UNKNOWN"


def is_cooling_mode(mode: int) -> bool:
    return mode in (ICECOOL_THERMOSTAT_MODE_COOL, ICECOOL_THERMOSTAT_MODE_AUTO)


def is_heating_mode(mode: int) -> bool:
    return mode in (ICECOOL_THERMOSTAT_MODE_HEAT, ICECOOL_THERMOSTAT_MODE_AUTO)


# -----------------------------------------------------------------------------
# RANDOM ZONE ID GENERATOR (FOR TESTING/DEMO)
# -----------------------------------------------------------------------------


def generate_zone_id(prefix: str = "zone") -> str:
    raw = hashlib.sha256(f"{prefix}{time.time()}{random.randint(0, 2**32)}".encode()).hexdigest()
    return f"{prefix}_{raw[:16]}"


def generate_sensor_root() -> str:
    return hashlib.sha256(os.urandom(32)).hexdigest()


# -----------------------------------------------------------------------------
# VALIDATION HELPERS (EXTENDED)
# -----------------------------------------------------------------------------


def validate_humidity(percent: int) -> None:
    if not (0 <= percent <= ICECOOL_MAX_HUMIDITY_PERCENT):
        raise IceCoolConfigError(f"Humidity {percent} out of range [0, {ICECOOL_MAX_HUMIDITY_PERCENT}]")


def validate_fan_preset_index(index: int) -> None:
    if not (0 <= index < ICECOOL_MAX_FAN_PRESETS):
        raise IceCoolConfigError(f"Fan preset index {index} out of range [0, {ICECOOL_MAX_FAN_PRESETS})")


def validate_thermostat_mode(mode: int) -> None:
    if not (0 <= mode <= ICECOOL_THERMOSTAT_MODE_AUTO):
        raise IceCoolConfigError(f"Invalid thermostat mode {mode}")


# -----------------------------------------------------------------------------
# CONFIG CLI
# -----------------------------------------------------------------------------


def cmd_config_show() -> None:
    cfg = load_config()
    print(f"rpc_url: {cfg.rpc_url}")
    print(f"contract_address: {cfg.contract_address or '(not set)'}")
    print(f"chain_id: {cfg.chain_id}")
    print(f"anchor_fee_wei: {cfg.anchor_fee_wei}")
    print(f"default_setpoint_decicelsius: {cfg.default_setpoint_decicelsius}")


def cmd_config_set(rpc_url: Optional[str] = None, contract_address: Optional[str] = None, chain_id: Optional[int] = None) -> None:
    cfg = load_config()
    if rpc_url is not None:
        cfg.rpc_url = rpc_url
    if contract_address is not None:
        cfg.contract_address = contract_address
    if chain_id is not None:
        cfg.chain_id = chain_id
    save_config(cfg)
    print("Config updated")


# -----------------------------------------------------------------------------
# EXTRA CONVERSION HELPERS
# -----------------------------------------------------------------------------


def kelvin_to_decicelsius(kelvin: float) -> int:
    return int(round((kelvin - 273.15) * 10))


def decicelsius_to_kelvin(decicelsius: Union[int, float]) -> float:
    return decicelsius / 10.0 + 273.15


def rankine_to_decicelsius(rankine: float) -> int:
    return fahrenheit_to_decicelsius((rankine - 459.67) * 9 / 5 + 32)


# -----------------------------------------------------------------------------
# EFFECTIVE SETPOINT WITH NIGHT SETBACK (LOCAL)
# -----------------------------------------------------------------------------


def effective_setpoint_with_setback(
    base_setpoint_decicelsius: int,
    night_setback_decicelsius: int,
    day_setforward_decicelsius: int,
    use_night_setback: bool,
) -> int:
    if use_night_setback and night_setback_decicelsius > 0:
        return min(base_setpoint_decicelsius, night_setback_decicelsius)
    if not use_night_setback and day_setforward_decicelsius > 0:
        return max(base_setpoint_decicelsius, day_setforward_decicelsius)
    return base_setpoint_decicelsius


# -----------------------------------------------------------------------------
# COMFORT INDEX (SIMPLIFIED)
# -----------------------------------------------------------------------------


def comfort_index(temp_celsius: float, humidity_percent: int) -> float:
    """Simple 0-1 comfort index; higher is more comfortable."""
    if humidity_percent <= 0:
        return 0.5
    dew = dewpoint_approx(temp_celsius * 10, humidity_percent)
    diff = abs(temp_celsius - 22.0)
    hum_penalty = (humidity_percent - 50) / 100.0 if humidity_percent > 50 else 0
    return max(0, 1.0 - diff / 10.0 - hum_penalty * 0.2)


# -----------------------------------------------------------------------------
# SCHEDULE HELPERS (BLOCK -> SETPOINT)
# -----------------------------------------------------------------------------


def get_active_schedule_at_block(windows: List[ScheduleWindowRecord], block_num: int) -> Optional[ScheduleWindowRecord]:
    for w in windows:
        if w.start_block <= block_num <= w.end_block:
            return w
    return None


def next_schedule_change(windows: List[ScheduleWindowRecord], from_block: int) -> Optional[int]:
    candidates = []
    for w in windows:
        if w.start_block > from_block:
            candidates.append(w.start_block)
        if w.end_block > from_block:
            candidates.append(w.end_block)
    return min(candidates) if candidates else None


# -----------------------------------------------------------------------------
# TEMPERATURE STATS (FROM READINGS)
# -----------------------------------------------------------------------------


def readings_stats(readings: List[SetpointReadingRecord]) -> Dict[str, float]:
    if not readings:
        return {"min": 0, "max": 0, "avg": 0, "count": 0}
    temps = [r.temp_celsius for r in readings if r is not None]
    if not temps:
        return {"min": 0, "max": 0, "avg": 0, "count": 0}
    return {
        "min": min(temps),
        "max": max(temps),
        "avg": sum(temps) / len(temps),
        "count": len(temps),
    }


def readings_recent(readings: List[Optional[SetpointReadingRecord]], last_n: int) -> List[SetpointReadingRecord]:
    valid = [r for r in readings if r is not None]
    valid.sort(key=lambda r: r.recorded_at, reverse=True)
    return valid[:last_n]


# -----------------------------------------------------------------------------
# ZONE COMPARISON
# -----------------------------------------------------------------------------


def zones_diff(store_before: IceCoolStore, store_after: IceCoolStore) -> List[str]:
    before_ids = set(store_before.list_zone_ids())
    after_ids = set(store_after.list_zone_ids())
    added = after_ids - before_ids
    removed = before_ids - after_ids
    lines = []
    for zid in added:
        lines.append(f"+ {zid}")
    for zid in removed:
        lines.append(f"- {zid}")
    return lines


# -----------------------------------------------------------------------------
# SANITY CHECKS (PRE-SUBMIT)
# -----------------------------------------------------------------------------


def check_zone_before_register(z: ZoneRecord) -> List[str]:
    errors = []
    try:
        validate_setpoint(z.setpoint_decicelsius)
    except IceCoolSetpointOutOfBoundsError as e:
        errors.append(str(e))
    try:
        validate_label(z.label)
    except IceCoolLabelTooLongError as e:
        errors.append(str(e))
    if len(z.zone_id) == 0:
        errors.append("zone_id is empty")
    return errors


def check_schedule_before_bind(start_block: int, end_block: int, setpoint_decicelsius: int) -> List[str]:
    errors = []
    try:
        validate_schedule_window(start_block, end_block)
    except IceCoolScheduleWindowError as e:
        errors.append(str(e))
    try:
        validate_setpoint(setpoint_decicelsius)
    except IceCoolSetpointOutOfBoundsError as e:
        errors.append(str(e))
    return errors


# -----------------------------------------------------------------------------
# FORMAT HELPERS (DISPLAY)
# -----------------------------------------------------------------------------


def format_setpoint_decicelsius(d: int) -> str:
    return f"{d} (0.1°C) = {decicelsius_to_celsius(d):.1f}°C"


def format_temp_scaled(scaled: int) -> str:
    return f"{scaled_to_celsius(scaled):.2f}°C"


def format_zone_summary(z: ZoneRecord, n_readings: int, n_bands: int, n_sched: int) -> str:
    return (
        f"{z.zone_id}  setpoint={format_setpoint_decicelsius(z.setpoint_decicelsius)}  "
        f"readings={n_readings}  bands={n_bands}  schedules={n_sched}  label={z.label or '-'}"
    )


# -----------------------------------------------------------------------------
# DEFAULT ZONES (PRESETS)
# -----------------------------------------------------------------------------


def default_zones_preset() -> List[Tuple[str, int, bool, str]]:
    return [
        ("living_room", 220, True, "Living room"),
        ("bedroom", 210, True, "Bedroom"),
        ("kitchen", 230, True, "Kitchen"),
        ("garage", 150, False, "Garage"),
        ("basement", 180, False, "Basement"),
    ]


def apply_default_zones_preset(store: IceCoolStore) -> int:
    preset = default_zones_preset()
    count = 0
    for zid, setpoint_d, cooling, label in preset:
        try:
            cmd_zone_add(store, zid, setpoint_d, cooling, label)
            count += 1
        except IceCoolConfigError:
            pass
    return count


# -----------------------------------------------------------------------------
# CHAIN HELPERS (HEX ENCODING)
# -----------------------------------------------------------------------------


def zone_id_to_bytes32_hex(zone_id: str) -> str:
    h = hashlib.sha256(zone_id.encode()).digest()
    return "0x" + h.hex()


def setpoint_reading_to_calldata(zone_id: str, reading_index: int, temp_scaled: int, sensor_root: str) -> Dict[str, Any]:
    return {
        "zone_id_hex": zone_id_to_bytes32_hex(zone_id),
        "reading_index": reading_index,
        "temp_scaled": temp_scaled,
        "sensor_root_hex": "0x" + (sensor_root if len(sensor_root) == 64 else hashlib.sha256(sensor_root.encode()).hexdigest()),
    }


# -----------------------------------------------------------------------------
# POLLING SIMULATOR (FOR DEMO)
# -----------------------------------------------------------------------------


def simulate_readings(store: IceCoolStore, zone_id: str, base_temp: float, count: int, noise: float = 0.5) -> int:
    import random
    store.get_zone(zone_id)
    added = 0
    for i in range(count):
        t = base_temp + random.gauss(0, noise)
        try:
            cmd_reading_add(store, zone_id, t, "")
            added += 1
        except Exception:
            break
    return added


# -----------------------------------------------------------------------------
# VERSION CHECK
# -----------------------------------------------------------------------------


def version_string() -> str:
    return f"{ICECOOL_VERSION[0]}.{ICECOOL_VERSION[1]}"


def compatible_contract_version() -> str:
    return "12"


# -----------------------------------------------------------------------------
# CONSTANTS EXPORT (FOR EXTERNAL USE)
# -----------------------------------------------------------------------------


def get_all_constants() -> Dict[str, Any]:
    return {
        "ICECOOL_VERSION": ICECOOL_VERSION,
        "ICECOOL_TEMP_SCALE": ICECOOL_TEMP_SCALE,
        "ICECOOL_MIN_SETPOINT_DECICELSIUS": ICECOOL_MIN_SETPOINT_DECICELSIUS,
        "ICECOOL_MAX_SETPOINT_DECICELSIUS": ICECOOL_MAX_SETPOINT_DECICELSIUS,
        "ICECOOL_MAX_READINGS_PER_ZONE": ICECOOL_MAX_READINGS_PER_ZONE,
        "ICECOOL_MAX_HYSTERESIS_BANDS": ICECOOL_MAX_HYSTERESIS_BANDS,
        "ICECOOL_MAX_SCHEDULE_WINDOWS": ICECOOL_MAX_SCHEDULE_WINDOWS,
        "ICECOOL_MAX_BATCH_ZONES": ICECOOL_MAX_BATCH_ZONES,
        "ICECOOL_MAX_BATCH_READINGS": ICECOOL_MAX_BATCH_READINGS,
        "ICECOOL_THERMOSTAT_MODE_OFF": ICECOOL_THERMOSTAT_MODE_OFF,
        "ICECOOL_THERMOSTAT_MODE_COOL": ICECOOL_THERMOSTAT_MODE_COOL,
        "ICECOOL_THERMOSTAT_MODE_HEAT": ICECOOL_THERMOSTAT_MODE_HEAT,
        "ICECOOL_THERMOSTAT_MODE_AUTO": ICECOOL_THERMOSTAT_MODE_AUTO,
    }


# -----------------------------------------------------------------------------
# READINGS AGGREGATION (MIN/MAX/AVG OVER WINDOW)
# -----------------------------------------------------------------------------


def aggregate_readings_by_time(
    readings: List[Optional[SetpointReadingRecord]],
    window_seconds: float,
) -> List[Dict[str, Any]]:
    valid = [r for r in readings if r is not None]
    if not valid:
        return []
    valid.sort(key=lambda r: r.recorded_at)
    buckets: Dict[int, List[SetpointReadingRecord]] = {}
    for r in valid:
        bucket = int(r.recorded_at / window_seconds) * int(window_seconds)
        buckets.setdefault(bucket, []).append(r)
    out = []
    for bucket_ts, arr in sorted(buckets.items()):
        temps = [r.temp_celsius for r in arr]
        out.append({
            "timestamp": bucket_ts,
            "min_celsius": min(temps),
            "max_celsius": max(temps),
            "avg_celsius": sum(temps) / len(temps),
            "count": len(arr),
        })
    return out


# -----------------------------------------------------------------------------
# HYSTERESIS BAND FROM SETPOINT + DEADBAND
# -----------------------------------------------------------------------------


def hysteresis_band_from_setpoint(setpoint_celsius: float, deadband_celsius: float) -> Tuple[float, float]:
    low = setpoint_celsius - deadband_celsius
    high = setpoint_celsius + deadband_celsius
    return (low, high)


def hysteresis_band_scaled_from_setpoint(setpoint_decicelsius: int, deadband_decicelsius: int) -> Tuple[int, int]:
    low = setpoint_decicelsius - deadband_decicelsius
    high = setpoint_decicelsius + deadband_decicelsius
    return (celsius_to_scaled(low / 10.0), celsius_to_scaled(high / 10.0))


# -----------------------------------------------------------------------------
# ZONE ID NORMALIZATION
# -----------------------------------------------------------------------------


def normalize_zone_id(raw: str) -> str:
    return raw.strip().lower().replace(" ", "_")


def validate_zone_id_format(zone_id: str) -> bool:
    if not zone_id or len(zone_id) > 64:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
    return all(c in allowed for c in zone_id.lower())


# -----------------------------------------------------------------------------
# FAN PRESET HELPERS
# -----------------------------------------------------------------------------


def fan_speed_to_percent(level: int) -> int:
    return clamp_int(level, 0, 100)


def clamp_int(x: int, lo: int, hi: int) -> int:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


# -----------------------------------------------------------------------------
# DEFROST DURATION VALIDATION
# -----------------------------------------------------------------------------


def validate_defrost_duration(seconds: int) -> None:
    if not (0 <= seconds <= ICECOOL_DEFROST_MAX_DURATION):
        raise IceCoolConfigError(f"Defrost duration {seconds} out of range [0, {ICECOOL_DEFROST_MAX_DURATION}]")


# -----------------------------------------------------------------------------
# CALIBRATION OFFSET VALIDATION
# -----------------------------------------------------------------------------


def validate_calibration_offset(offset_scaled: int) -> None:
    if abs(offset_scaled) > ICECOOL_CALIBRATION_OFFSET_MAX:
        raise IceCoolConfigError(f"Calibration offset too large (max {ICECOOL_CALIBRATION_OFFSET_MAX})")


# -----------------------------------------------------------------------------
# STORE STATS
# -----------------------------------------------------------------------------


def store_stats(store: IceCoolStore) -> Dict[str, Any]:
    zone_ids = store.list_zone_ids()
    total_readings = sum(store.reading_count(zid) for zid in zone_ids)
    total_bands = sum(len(store.get_bands(zid)) for zid in zone_ids)
    total_schedules = sum(len(store.get_schedule_windows(zid)) for zid in zone_ids)
    return {
        "zones": len(zone_ids),
        "total_readings": total_readings,
        "total_bands": total_bands,
        "total_schedules": total_schedules,
        "archived": len([z for z in zone_ids if store.is_archived(z)]),
    }


def store_stats_print(store: IceCoolStore) -> None:
    s = store_stats(store)
    print(f"Zones: {s['zones']}  Readings: {s['total_readings']}  Bands: {s['total_bands']}  Schedules: {s['total_schedules']}  Archived: {s['archived']}")


def zone_ids_matching_label(store: IceCoolStore, label_substring: str) -> List[str]:
    out = []
    for zid in store.list_zone_ids():
        z = store.get_zone(zid)
        if label_substring.lower() in (z.label or "").lower():
            out.append(zid)
    return out


def setpoints_for_zones(store: IceCoolStore, zone_ids: List[str]) -> List[int]:
    return [store.get_zone(zid).setpoint_decicelsius for zid in zone_ids]


def average_setpoint(store: IceCoolStore) -> float:
    ids = store.list_zone_ids()
    if not ids:
        return 0.0
    vals = [store.get_zone(zid).setpoint_decicelsius for zid in ids]
    return sum(vals) / len(vals) / 10.0


# -----------------------------------------------------------------------------
# MAIN (WITH CONFIG, EXPORT, IMPORT, API)
# -----------------------------------------------------------------------------


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog=ICECOOL_APP_NAME, description="Household climatic control companion for FridgAI")
    parser.add_argument("--version", action="store_true", help="Show version")
    parser.add_argument("--config-dir", type=str, default="", help="Config directory (default: ~/.icecool)")
    parser.add_argument("--api", action="store_true", help="Run API server")
    parser.add_argument("--api-host", type=str, default="127.0.0.1")
    parser.add_argument("--api-port", type=int, default=8765)
    sub = parser.add_subparsers(dest="command", help="Commands")

    p_add = sub.add_parser("zone-add", help="Add a zone")
    p_add.add_argument("zone_id", type=str, help="Zone ID")
    p_add.add_argument("--setpoint", type=float, default=22.0, help="Setpoint in Celsius")
    p_add.add_argument("--cooling", action="store_true", help="Prefer cooling")
    p_add.add_argument("--label", type=str, default="", help="Optional label")

    sub.add_parser("zone-list", help="List zones")

    p_show = sub.add_parser("zone-show", help="Show zone details")
    p_show.add_argument("zone_id", type=str)

    p_reading = sub.add_parser("reading-add", help="Add a temperature reading")
    p_reading.add_argument("zone_id", type=str)
    p_reading.add_argument("temp_celsius", type=float)
    p_reading.add_argument("--sensor-root", type=str, default="")

    p_band = sub.add_parser("band-add", help="Add hysteresis band")
    p_band.add_argument("zone_id", type=str)
    p_band.add_argument("low_celsius", type=float)
    p_band.add_argument("high_celsius", type=float)

    p_sched = sub.add_parser("schedule-add", help="Add schedule window")
    p_sched.add_argument("zone_id", type=str)
    p_sched.add_argument("start_block", type=int)
    p_sched.add_argument("end_block", type=int)
    p_sched.add_argument("--setpoint", type=float, default=22.0, help="Setpoint Celsius")

    p_link = sub.add_parser("link", help="Link two zones")
    p_link.add_argument("zone_a", type=str)
    p_link.add_argument("zone_b", type=str)

    p_save = sub.add_parser("save", help="Save store to config dir")
    p_save.add_argument("--path", type=str, default="", help="Override path")
    p_load = sub.add_parser("load", help="Load store from config dir")
    p_load.add_argument("--path", type=str, default="", help="Override path")

    p_eff = sub.add_parser("effective-setpoint", help="Get effective setpoint at block")
    p_eff.add_argument("zone_id", type=str)
    p_eff.add_argument("block_num", type=int)

    p_config = sub.add_parser("config", help="Show or set config")
    p_config.add_argument("subcommand", type=str, choices=["show", "set"], nargs="?")
    p_config.add_argument("--rpc-url", type=str, default=None)
    p_config.add_argument("--contract", type=str, default=None)
    p_config.add_argument("--chain-id", type=int, default=None)

    p_export = sub.add_parser("export-csv", help="Export zones to CSV")
    p_export.add_argument("path", type=str)
    p_import = sub.add_parser("import-csv", help="Import zones from CSV")
    p_import.add_argument("path", type=str)

    p_preset = sub.add_parser("preset-default-zones", help="Add default zone preset")
    p_sim = sub.add_parser("simulate-readings", help="Simulate N readings for a zone")
    p_sim.add_argument("zone_id", type=str)
    p_sim.add_argument("count", type=int)
    p_sim.add_argument("--base-temp", type=float, default=22.0)
    p_sim.add_argument("--noise", type=float, default=0.5)

    args = parser.parse_args()

    if args.version:
        print(f"{ICECOOL_APP_NAME} v{version_string()}")
        return 0

    config_dir = Path(args.config_dir) if args.config_dir else Path.home() / ICECOOL_CONFIG_DIR
    store = IceCoolStore()
    load_path = config_dir / "store"
    if (load_path / ICECOOL_ZONES_FILE).exists():
        store.load_from_dir(load_path)

    if getattr(args, "api", False):
        run_api_server(store, args.api_host, args.api_port)
        return 0

    if getattr(args, "command", None) == "config":
        if getattr(args, "subcommand", None) == "set":
            cmd_config_set(args.rpc_url, args.contract, args.chain_id)
        else:
            cmd_config_show()
        sys.exit(0)

    if getattr(args, "command", None) == "export-csv":
        export_zones_csv(store, Path(args.path))
        print(f"Exported to {args.path}")
        sys.exit(0)

    if getattr(args, "command", None) == "import-csv":
        n = import_zones_csv(store, Path(args.path))
        store.save_to_dir(load_path)
        print(f"Imported {n} zones")
        sys.exit(0)

    if getattr(args, "command", None) == "preset-default-zones":
        n = apply_default_zones_preset(store)
        store.save_to_dir(load_path)
        print(f"Added {n} default zones")
        sys.exit(0)

    if getattr(args, "command", None) == "simulate-readings":
        added = simulate_readings(store, args.zone_id, args.base_temp, args.count, args.noise)
        store.save_to_dir(load_path)
        print(f"Added {added} simulated readings for {args.zone_id}")
        sys.exit(0)

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
        cmd_link(store, args.zone_a, args.zone_b)
    elif args.command == "save":
        path = Path(args.path) if args.path else load_path
        store.save_to_dir(path)
        print(f"Saved to {path}")
    elif args.command == "load":
        path = Path(args.path) if args.path else load_path
        store.load_from_dir(path)
        print(f"Loaded from {path}")
    elif args.command == "effective-setpoint":
        sp = simulate_effective_setpoint(store, args.zone_id, args.block_num)
        print(f"Effective setpoint: {sp} (0.1°C) = {decicelsius_to_celsius(sp):.1f}°C")
    else:
        parser.print_help()
    if args.command in ("zone-add", "reading-add", "band-add", "schedule-add", "link"):
        store.save_to_dir(load_path)
    sys.exit(0)
