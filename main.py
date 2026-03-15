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
