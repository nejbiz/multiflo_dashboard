"""Small stdlib HTTP bridge for the Raspberry Pi touchscreen dashboard.

It owns one MultiFlo driver over the serial FT232 and drives the same
ProtocolRunner used everywhere else. No FastAPI/uvicorn dependency is needed, so
it runs on the offline Pi with only the already-installed pydantic + pyserial.
It also serves the widget HTML from the same origin, so the browser needs no
CORS handling.

Exposes only the endpoints the demo needs:

  GET  /                       -> the widget page
  GET  /v1/health              -> process/controller health
  GET  /v1/device              -> read-only identity, modules, program-step state
  GET  /v1/cassette-usage      -> persistent totals and recent ledger events
  POST /v1/reconcile           -> clear a retained marker after a fresh Ready check
  POST /v1/system/shutdown     -> safely power off the Raspberry Pi
  POST /v1/cassette-usage/{cassette}/reset -> record physical replacement
  GET  /v1/procedure-settings  -> stored guided-procedure overrides
  POST /v1/procedure-settings  -> replace the guided-procedure overrides
  GET  /v1/plate-geometry      -> effective and built-in plate defaults
  POST /v1/plate-geometry      -> replace persistent plate geometry overrides
  GET  /v1/maintenance/auto-prime -> automatic water-prime schedule and settings
  POST /v1/maintenance/auto-prime -> replace automatic water-prime volumes
  GET  /v1/protocols           -> stored dashboard protocol library
  POST /v1/protocols           -> create or replace one stored protocol
  DELETE /v1/protocols/{id}    -> remove one stored protocol
  POST /v1/protocols/validate  -> strict model validation, no hardware
  POST /v1/operations/*        -> start one validated operation
  POST /v1/runs                -> start one supported ordered protocol
  GET  /v1/runs/{run_id}       -> poll run state and results
  POST /v1/runs/{run_id}/abort -> cooperative abort
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Timer
from typing import Callable
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError as PydanticValidationError

from multiflo.codec import dispense_height_steps
from multiflo.driver import MultiFloDriver
from multiflo.errors import BusyError, MultiFloError
from multiflo.models import (
    CassetteType,
    MAX_DISPENSE_HEIGHT_STEPS,
    MIN_DISPENSE_HEIGHT_STEPS,
    PeristalticDispense,
    PeristalticPrime,
    PeristalticPurge,
    PlateType,
    Protocol,
    ProtocolStep,
    Shake,
    Soak,
    WELL_384_PLATE_TYPES,
)
from multiflo.runner import ProtocolRunner, RequestId, RunState, StepResult
from multiflo.transport import SerialByteTransport, SerialConfig


WIDGET_PATH = Path(__file__).with_name("widget.html")
STATE_DIR = Path(__file__).resolve().parent
CASSETTE_LIMITS_UL = {
    CassetteType.ONE_UL: 2_000_000,
    CassetteType.FIVE_UL: 5_000_000,
}
REQUEST_ID_ADAPTER = TypeAdapter(RequestId)
# The touchscreen protocol builder stores driver steps alongside two
# software-level steps the instrument knows nothing about: "wait" holds the
# sequence for a fixed time or until the operator confirms, and "repeat" runs
# the preceding block again. The dashboard expands them; the driver never sees
# them.
LIBRARY_STEP_MODELS = {
    "peristaltic_dispense": PeristalticDispense,
    "peristaltic_prime": PeristalticPrime,
    "peristaltic_purge": PeristalticPurge,
    "shake": Shake,
    "soak": Soak,
}
SOFTWARE_STEP_TYPES = ("wait", "repeat")
# Guided-procedure parameters the operator can retune from the touchscreen.
# The step counts are the contract with the dashboard's PROCEDURES table.
PROCEDURE_STEP_COUNTS = {"load": 2, "reagent": 4, "end": 2}
MAX_PROCEDURE_STEP_VOLUME_UL = 3000
PROCEDURE_FLOW_RATES = {"low", "medium", "high"}
AUTO_PRIME_INTERVAL_SECONDS = 2 * 60 * 60
AUTO_PRIME_POLL_SECONDS = 5.0
AUTO_PRIME_RETRY_SECONDS = 60
AUTO_PRIME_FLOW_RATE = "low"
AUTO_PRIME_DEFAULT_VOLUMES_UL = {
    CassetteType.ONE_UL.value: 50,
    CassetteType.FIVE_UL.value: 200,
}
MAX_LIBRARY_STEPS = 50
MAX_LIBRARY_PROTOCOLS = 100
MAX_WAIT_SECONDS = 86_400
MAX_REPEAT_COUNT = 99
PLATE_GEOMETRY_FIELDS = (
    "x_offset_steps",
    "y_offset_steps",
    "dispense_height_steps",
)
DASHBOARD_PLATE_TYPES = (
    PlateType.WELL_96,
    PlateType.DEEP_WELL_96,
    PlateType.WELL_384,
    PlateType.DEEP_WELL_384,
)
BUILT_IN_PLATE_GEOMETRY = {
    plate.value: {
        "x_offset_steps": 0,
        "y_offset_steps": 0,
        "dispense_height_steps": dispense_height_steps(
            PeristalticDispense(volume_ul=1, plate_type=plate)
        ),
    }
    for plate in DASHBOARD_PLATE_TYPES
}
SHUTDOWN_COMMAND = ("/usr/bin/sudo", "-n", "/usr/sbin/shutdown", "now")
SHUTDOWN_PERMISSION_COMMAND = (
    "/usr/bin/sudo",
    "-n",
    "-l",
    "/usr/sbin/shutdown",
    "now",
)


def schedule_system_shutdown() -> None:
    """Verify the narrow sudo rule, then invoke shutdown after the response."""

    permission = subprocess.run(
        SHUTDOWN_PERMISSION_COMMAND,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if permission.returncode:
        raise RuntimeError("shutdown permission is not configured")

    def invoke() -> None:
        result = subprocess.run(
            SHUTDOWN_COMMAND,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip() or f"exit status {result.returncode}"
            print(f"shutdown command failed: {detail}", flush=True)

    timer = Timer(0.5, invoke)
    timer.daemon = True
    timer.start()
OPERATION_ROUTES = {
    "/v1/operations/dispense": PeristalticDispense,
    "/v1/operations/prime": PeristalticPrime,
    "/v1/operations/purge": PeristalticPurge,
    "/v1/operations/shake": Shake,
    "/v1/operations/soak": Soak,
}


def pumped_volume_ul(step: ProtocolStep) -> int:
    """Estimate aggregate liquid moved through all cassette channels."""

    if isinstance(step, PeristalticDispense):
        if step.plate_type in WELL_384_PLATE_TYPES:
            column_count = 24 if step.columns == "all" else len(step.columns)
            row_count = 16 if step.row_sections == "all" else 8
        else:
            column_count = 12 if step.columns == "all" else len(step.columns)
            row_count = 8
        plate_volume = step.volume_ul * column_count * row_count
        pre_dispense_volume = (
            step.pre_dispense_volume_ul * step.pre_dispense_cycles * 8
        )
        return plate_volume + pre_dispense_volume
    if isinstance(step, (PeristalticPrime, PeristalticPurge)):
        return step.volume_ul * 8
    return 0


class CassetteUsageStore:
    """Append-only cassette usage ledger with restart-safe derived totals."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = Lock()
        self._totals_ul = {cassette: 0 for cassette in CASSETTE_LIMITS_UL}
        self._dispense_run_ids = {
            cassette: set() for cassette in CASSETTE_LIMITS_UL
        }
        self._events: list[dict[str, object]] = []
        self._event_ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                event_id = str(event["event_id"])
                cassette = CassetteType(str(event["cassette_type"]))
                event_type = str(event["event_type"])
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"invalid cassette usage event at line {line_number}: {error}"
                ) from error
            if cassette not in CASSETTE_LIMITS_UL:
                continue
            if event_id in self._event_ids:
                continue
            self._event_ids.add(event_id)
            if event_type == "liquid_moved":
                self._totals_ul[cassette] += int(event["volume_ul"])
                if event.get("operation") == "peristaltic_dispense":
                    self._dispense_run_ids[cassette].add(str(event["run_id"]))
            elif event_type == "cassette_replaced":
                self._totals_ul[cassette] = 0
                self._dispense_run_ids[cassette].clear()
            else:
                raise RuntimeError(
                    f"unknown cassette usage event {event_type!r} at line {line_number}"
                )
            self._events.append(event)

    def _append(self, event: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._event_ids.add(str(event["event_id"]))
        self._events.append(event)

    def record_step(
        self,
        run_id: UUID,
        step_index: int,
        step: ProtocolStep,
        result: StepResult,
        resolved_cassette: CassetteType,
    ) -> None:
        volume_ul = pumped_volume_ul(step)
        if volume_ul <= 0 or resolved_cassette not in CASSETTE_LIMITS_UL:
            return
        event_id = f"{run_id}:{step_index}"
        with self._lock:
            if event_id in self._event_ids:
                return
            event: dict[str, object] = {
                "version": 1,
                "event_id": event_id,
                "event_type": "liquid_moved",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": str(run_id),
                "step_index": step_index,
                "operation": step.operation,
                "cassette_type": resolved_cassette.value,
                "volume_ul": volume_ul,
                "device_status": result.device_status,
            }
            self._append(event)
            self._totals_ul[resolved_cassette] += volume_ul
            if isinstance(step, PeristalticDispense):
                self._dispense_run_ids[resolved_cassette].add(str(run_id))

    def reset(self, cassette: CassetteType) -> None:
        if cassette not in CASSETTE_LIMITS_UL:
            raise ValueError("usage is tracked only for 1ul and 5ul cassettes")
        with self._lock:
            event: dict[str, object] = {
                "version": 1,
                "event_id": f"replacement:{uuid4()}",
                "event_type": "cassette_replaced",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "cassette_type": cassette.value,
                "previous_volume_ul": self._totals_ul[cassette],
            }
            self._append(event)
            self._totals_ul[cassette] = 0
            self._dispense_run_ids[cassette].clear()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            cassettes: dict[str, object] = {}
            for cassette, limit_ul in CASSETTE_LIMITS_UL.items():
                used_ul = self._totals_ul[cassette]
                cassettes[cassette.value] = {
                    "used_ul": used_ul,
                    "used_ml": round(used_ul / 1000, 3),
                    "limit_ul": limit_ul,
                    "limit_ml": limit_ul // 1000,
                    "remaining_ml": round(max(limit_ul - used_ul, 0) / 1000, 3),
                    "percent": round(used_ul / limit_ul * 100, 3),
                    "recommended_limit_reached": used_ul >= limit_ul,
                    "plate_count": len(self._dispense_run_ids[cassette]),
                }
            return {
                "cassettes": cassettes,
                "recent_events": list(reversed(self._events[-20:])),
                "ledger_path": str(self.path),
            }


def _library_text(value: object, field: str, limit: int, required: bool = False) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > limit:
        raise ValueError(f"{field} must be {limit} characters or fewer")
    return text


def validate_library_step(raw: object, index: int) -> dict[str, object]:
    """Validate one stored builder step, driver-level or software-level."""

    if not isinstance(raw, dict):
        raise ValueError(f"step {index} must be an object")
    kind = raw.get("type")
    name = _library_text(raw.get("name"), f"step {index} name", 60)
    if kind == "wait":
        body = raw.get("wait")
        if not isinstance(body, dict):
            raise ValueError(f"step {index} requires a wait body")
        mode = body.get("mode")
        if mode not in ("timed", "confirm"):
            raise ValueError(f"step {index} wait mode must be timed or confirm")
        wait: dict[str, object] = {"mode": mode}
        if mode == "timed":
            try:
                seconds = int(body["seconds"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"step {index} timed wait needs seconds") from error
            if not 1 <= seconds <= MAX_WAIT_SECONDS:
                raise ValueError(
                    f"step {index} wait seconds must be between 1 and {MAX_WAIT_SECONDS}"
                )
            wait["seconds"] = seconds
        wait["message"] = _library_text(
            body.get("message"), f"step {index} wait message", 120
        )
        return {"name": name, "type": "wait", "wait": wait}
    if kind == "repeat":
        body = raw.get("repeat")
        if not isinstance(body, dict):
            raise ValueError(f"step {index} requires a repeat body")
        try:
            count = int(body["count"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"step {index} repeat needs a count") from error
        if not 2 <= count <= MAX_REPEAT_COUNT:
            raise ValueError(
                f"step {index} repeat count must be between 2 and {MAX_REPEAT_COUNT}"
            )
        return {"name": name, "type": "repeat", "repeat": {"count": count}}
    if kind in LIBRARY_STEP_MODELS:
        step = LIBRARY_STEP_MODELS[kind].model_validate(raw.get("step"))
        return {"name": name, "type": kind, "step": step.model_dump(mode="json")}
    supported = ", ".join([*LIBRARY_STEP_MODELS, *SOFTWARE_STEP_TYPES])
    raise ValueError(f"step {index} has unknown type {kind!r}; expected one of {supported}")


def validate_library_steps(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("a protocol needs at least one step")
    if len(raw) > MAX_LIBRARY_STEPS:
        raise ValueError(f"a protocol is limited to {MAX_LIBRARY_STEPS} steps")
    steps = [validate_library_step(step, index) for index, step in enumerate(raw)]
    block = 0
    for index, step in enumerate(steps):
        if step["type"] != "repeat":
            block += 1
            continue
        if not block:
            raise ValueError(f"step {index} repeats an empty block")
        block = 0
    return steps


class ProtocolLibrary:
    """Validated dashboard protocols stored as one JSON document on the Pi."""

    version = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = Lock()
        self._protocols: dict[str, dict[str, object]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            document = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as error:
            raise RuntimeError(f"invalid protocol library at {self.path}: {error}") from error
        for entry in document.get("protocols", []) if isinstance(document, dict) else []:
            try:
                protocol = {
                    "id": _library_text(entry["id"], "id", 64, required=True),
                    "name": _library_text(entry["name"], "name", 60, required=True),
                    "created_at": str(entry.get("created_at") or ""),
                    "updated_at": str(entry.get("updated_at") or ""),
                    "steps": validate_library_steps(entry.get("steps")),
                }
            except (KeyError, TypeError, ValueError, PydanticValidationError) as error:
                raise RuntimeError(
                    f"invalid protocol in {self.path}: {error}"
                ) from error
            self._protocols[str(protocol["id"])] = protocol

    def _write(self) -> None:
        document = {"version": self.version, "protocols": self._sorted()}
        temporary = self.path.with_name(self.path.name + ".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def _sorted(self) -> list[dict[str, object]]:
        return sorted(
            self._protocols.values(),
            key=lambda protocol: str(protocol["name"]).casefold(),
        )

    def list(self) -> list[dict[str, object]]:
        with self._lock:
            return self._sorted()

    def save(self, body: object) -> dict[str, object]:
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        name = _library_text(body.get("name"), "name", 60, required=True)
        steps = validate_library_steps(body.get("steps"))
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            raw_id = body.get("id")
            protocol_id = _library_text(raw_id, "id", 64) if raw_id else ""
            if protocol_id and protocol_id not in self._protocols:
                raise KeyError(protocol_id)
            if not protocol_id:
                if len(self._protocols) >= MAX_LIBRARY_PROTOCOLS:
                    raise ValueError(
                        f"the library is limited to {MAX_LIBRARY_PROTOCOLS} protocols"
                    )
                protocol_id = uuid4().hex
            created = (
                str(self._protocols[protocol_id]["created_at"])
                if protocol_id in self._protocols
                else now
            )
            protocol = {
                "id": protocol_id,
                "name": name,
                "created_at": created,
                "updated_at": now,
                "steps": steps,
            }
            self._protocols[protocol_id] = protocol
            self._write()
            return protocol

    def delete(self, protocol_id: str) -> None:
        with self._lock:
            if protocol_id not in self._protocols:
                raise KeyError(protocol_id)
            del self._protocols[protocol_id]
            self._write()


def validate_procedure_settings(body: object) -> dict[str, object]:
    """Validate guided-procedure volume and flow-rate overrides.

    Version-1 files stored each cassette as a bare volume list. They remain
    valid so deployed instruments keep their existing tuning; new dashboard
    writes use an object containing parallel ``volumes`` and ``speeds`` lists.
    """

    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    known_cassettes = {cassette.value for cassette in CASSETTE_LIMITS_UL}
    procedures: dict[str, object] = {}
    raw_procedures = body.get("procedures") or {}
    if not isinstance(raw_procedures, dict):
        raise ValueError("procedures must be an object")
    for procedure, per_cassette in raw_procedures.items():
        if procedure not in PROCEDURE_STEP_COUNTS:
            raise ValueError(f"unknown procedure {procedure!r}")
        if not isinstance(per_cassette, dict):
            raise ValueError(f"{procedure} must map a cassette to its step settings")
        entry: dict[str, object] = {}
        for cassette, raw_settings in per_cassette.items():
            if cassette not in known_cassettes:
                raise ValueError(f"unknown cassette {cassette!r}")
            expected = PROCEDURE_STEP_COUNTS[procedure]
            legacy = isinstance(raw_settings, list)
            volumes = raw_settings if legacy else raw_settings.get("volumes") if isinstance(raw_settings, dict) else None
            if not isinstance(volumes, list) or len(volumes) != expected:
                raise ValueError(f"{procedure} needs {expected} volumes for {cassette}")
            checked: list[int] = []
            for value in volumes:
                try:
                    number = int(value)
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f"{procedure} volumes must be whole numbers"
                    ) from error
                if not 1 <= number <= MAX_PROCEDURE_STEP_VOLUME_UL:
                    raise ValueError(
                        f"{procedure} volumes must be between 1 and "
                        f"{MAX_PROCEDURE_STEP_VOLUME_UL} uL/well"
                    )
                checked.append(number)
            if legacy:
                entry[cassette] = checked
                continue
            assert isinstance(raw_settings, dict)
            speeds = raw_settings.get("speeds")
            if not isinstance(speeds, list) or len(speeds) != expected:
                raise ValueError(f"{procedure} needs {expected} speeds for {cassette}")
            checked_speeds: list[str] = []
            for value in speeds:
                if not isinstance(value, str) or value not in PROCEDURE_FLOW_RATES:
                    raise ValueError(
                        f"{procedure} speeds must be low, medium, or high"
                    )
                checked_speeds.append(value)
            entry[cassette] = {"volumes": checked, "speeds": checked_speeds}
        if entry:
            procedures[procedure] = entry
    return {"version": 2, "procedures": procedures}


class ProcedureSettingsStore:
    """Operator overrides for guided-procedure parameters, stored on the Pi."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = Lock()
        self._settings = {"version": 2, "procedures": {}}
        if self.path.exists():
            try:
                self._settings = validate_procedure_settings(
                    json.loads(self.path.read_text(encoding="utf-8") or "{}")
                )
            except (ValueError, json.JSONDecodeError) as error:
                # Never block the touchscreen on a stale or hand-edited file:
                # fall back to the built-in volumes and say so on the console.
                print(
                    f"ignoring invalid procedure settings at {self.path}: {error}",
                    flush=True,
                )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return json.loads(json.dumps(self._settings))

    def replace(self, body: object) -> dict[str, object]:
        settings = validate_procedure_settings(body)
        with self._lock:
            temporary = self.path.with_name(self.path.name + ".tmp")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(settings, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self._settings = settings
            return json.loads(json.dumps(settings))


def validate_auto_prime_settings(body: object) -> dict[str, object]:
    """Validate operator-set automatic-prime volumes for both cassettes."""

    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    extra = set(body) - {"version", "volumes"}
    if extra:
        raise ValueError(f"unknown auto-prime field {sorted(extra)[0]!r}")
    raw_volumes = body.get("volumes")
    if not isinstance(raw_volumes, dict):
        raise ValueError("volumes must be an object")
    expected = set(AUTO_PRIME_DEFAULT_VOLUMES_UL)
    missing = expected - set(raw_volumes)
    extra_cassettes = set(raw_volumes) - expected
    if missing:
        raise ValueError(f"missing auto-prime volume for {sorted(missing)[0]}")
    if extra_cassettes:
        raise ValueError(f"unknown cassette {sorted(extra_cassettes)[0]!r}")
    volumes: dict[str, int] = {}
    for cassette in AUTO_PRIME_DEFAULT_VOLUMES_UL:
        value = raw_volumes[cassette]
        if type(value) is not int:
            raise ValueError(f"{cassette} auto-prime volume must be a whole number")
        if not 1 <= value <= MAX_PROCEDURE_STEP_VOLUME_UL:
            raise ValueError(
                f"{cassette} auto-prime volume must be between 1 and "
                f"{MAX_PROCEDURE_STEP_VOLUME_UL} uL/well"
            )
        volumes[cassette] = value
    return {"version": 1, "volumes": volumes}


class AutoPrimeStore:
    """Persistent automatic-prime settings and last liquid activity time."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = Lock()
        self._settings = {
            "version": 1,
            "volumes": dict(AUTO_PRIME_DEFAULT_VOLUMES_UL),
        }
        self._last_activity_at = self._now()
        if self.path.exists():
            try:
                document = json.loads(self.path.read_text(encoding="utf-8") or "{}")
                self._settings = validate_auto_prime_settings(
                    {"version": document.get("version"), "volumes": document.get("volumes")}
                )
                self._last_activity_at = self._parse_timestamp(
                    document.get("last_activity_at")
                )
            except (ValueError, json.JSONDecodeError) as error:
                print(
                    f"ignoring invalid auto-prime settings at {self.path}: {error}",
                    flush=True,
                )
        with self._lock:
            self._write_locked()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("auto-prime clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _parse_timestamp(value: object) -> datetime:
        if not isinstance(value, str):
            raise ValueError("last_activity_at must be an ISO timestamp")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("last_activity_at must include a timezone")
        return parsed.astimezone(timezone.utc)

    def _document_locked(self) -> dict[str, object]:
        return {
            **json.loads(json.dumps(self._settings)),
            "last_activity_at": self._last_activity_at.isoformat(),
        }

    def _write_locked(self) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(self._document_locked(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def snapshot(self, *, now: datetime | None = None) -> dict[str, object]:
        current = (now or self._now()).astimezone(timezone.utc)
        with self._lock:
            next_due = self._last_activity_at + timedelta(
                seconds=AUTO_PRIME_INTERVAL_SECONDS
            )
            remaining = max(0, math.ceil((next_due - current).total_seconds()))
            return {
                **self._document_locked(),
                "built_in_volumes": dict(AUTO_PRIME_DEFAULT_VOLUMES_UL),
                "flow_rate": AUTO_PRIME_FLOW_RATE,
                "interval_seconds": AUTO_PRIME_INTERVAL_SECONDS,
                "next_due_at": next_due.isoformat(),
                "seconds_until_due": remaining,
            }

    def replace(self, body: object) -> dict[str, object]:
        settings = validate_auto_prime_settings(body)
        with self._lock:
            self._settings = settings
            self._write_locked()
        return self.snapshot()

    def record_activity(self, *, at: datetime | None = None) -> None:
        activity_at = (at or self._now()).astimezone(timezone.utc)
        with self._lock:
            self._last_activity_at = activity_at
            self._write_locked()

    def record_step(
        self,
        _run_id: UUID,
        _step_index: int,
        step: ProtocolStep,
        _result: StepResult,
        _resolved_cassette: CassetteType,
    ) -> None:
        if isinstance(step, (PeristalticPrime, PeristalticDispense)):
            self.record_activity()


class AutoPrimeScheduler:
    """Start one slow water prime two hours after prime/dispense activity."""

    def __init__(
        self,
        runner: ProtocolRunner,
        store: AutoPrimeStore,
        *,
        poll_seconds: float = AUTO_PRIME_POLL_SECONDS,
        retry_seconds: float = AUTO_PRIME_RETRY_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.runner = runner
        self.store = store
        self.poll_seconds = poll_seconds
        self.retry_seconds = retry_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = Lock()
        self._check_lock = Lock()
        self._timer: Timer | None = None
        self._stopped = True
        self._active_run_id: UUID | None = None
        self._waiting_reason: str | None = None
        self._last_error: str | None = None
        self._retry_at: datetime | None = None

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("auto-prime clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def start(self) -> None:
        with self._lock:
            if not self._stopped:
                return
            self._stopped = False
            self._schedule_locked(0.5)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()

    def _schedule_locked(self, delay: float) -> None:
        timer = Timer(delay, self._poll)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _poll(self) -> None:
        try:
            self.check()
        except Exception as error:
            with self._lock:
                self._last_error = f"{type(error).__name__}: {error}"
            print(f"auto-prime scheduler check failed: {error}", flush=True)
        finally:
            with self._lock:
                if not self._stopped:
                    self._schedule_locked(self.poll_seconds)

    def _refresh_active_run(self, now: datetime) -> bool:
        with self._lock:
            run_id = self._active_run_id
        if run_id is None:
            return False
        run = self.runner.get(run_id)
        if run is not None and run.state not in {
            RunState.COMPLETED,
            RunState.ABORTED,
            RunState.FAILED,
            RunState.UNKNOWN_EXECUTION_STATE,
        }:
            return True
        with self._lock:
            self._active_run_id = None
            if run is None or run.state is not RunState.COMPLETED:
                state = "missing" if run is None else run.state.value
                self._last_error = f"automatic prime ended {state}"
                self._retry_at = now + timedelta(seconds=self.retry_seconds)
            else:
                self._last_error = None
                self._retry_at = None
        return False

    def check(self, *, now: datetime | None = None) -> bool:
        """Run one scheduler check. Return True only when a prime is accepted."""

        current = (now or self._now()).astimezone(timezone.utc)
        with self._check_lock:
            if self._refresh_active_run(current):
                return False
            schedule = self.store.snapshot(now=current)
            if schedule["seconds_until_due"] > 0:
                with self._lock:
                    self._waiting_reason = None
                return False
            with self._lock:
                if self._retry_at is not None and current < self._retry_at:
                    return False
            if self.runner.active_run_id is not None:
                with self._lock:
                    self._waiting_reason = "waiting for the active run"
                return False
            try:
                device = self.runner.describe_device()
            except Exception as error:
                with self._lock:
                    self._waiting_reason = "waiting for the instrument"
                    self._last_error = f"{type(error).__name__}: {error}"
                return False
            modules = device.modules
            safe = (
                device.connected
                and device.serial_matches_expected
                and not device.reconciliation_required
                and device.active_run_id is None
                and device.program_step_state == "ready"
                and modules is not None
                and modules.primary_peristaltic
                and modules.primary_cassette in (CassetteType.ONE_UL, CassetteType.FIVE_UL)
            )
            if not safe:
                with self._lock:
                    self._waiting_reason = "waiting for the instrument to be ready"
                return False
            cassette = modules.primary_cassette
            volume = schedule["volumes"][cassette.value]
            protocol = Protocol(
                name="Automatic maintenance prime",
                steps=[
                    PeristalticPrime(
                        volume_ul=volume,
                        flow_rate=AUTO_PRIME_FLOW_RATE,
                        cassette_type=cassette,
                    )
                ],
            )
            try:
                result = self.runner.start(
                    protocol,
                    operator_confirmed_idle=True,
                    request_id=f"auto-prime-{uuid4()}",
                )
            except Exception as error:
                with self._lock:
                    self._waiting_reason = "waiting to retry"
                    self._last_error = f"{type(error).__name__}: {error}"
                    self._retry_at = current + timedelta(seconds=self.retry_seconds)
                return False
            with self._lock:
                self._active_run_id = result.status.run_id
                self._waiting_reason = None
                self._last_error = None
                self._retry_at = None
            return True

    def snapshot(self) -> dict[str, object]:
        current = self._now()
        schedule = self.store.snapshot(now=current)
        with self._lock:
            active_run_id = self._active_run_id
            waiting_reason = self._waiting_reason
            last_error = self._last_error
        if active_run_id is not None:
            status = "running"
        elif schedule["seconds_until_due"] == 0:
            status = "waiting" if waiting_reason else "due"
        else:
            status = "scheduled"
        return {
            **schedule,
            "status": status,
            "active_run_id": None if active_run_id is None else str(active_run_id),
            "waiting_reason": waiting_reason,
            "last_error": last_error,
        }


def validate_plate_geometry_settings(body: object) -> dict[str, object]:
    """Validate persistent overrides for dashboard-supported plate geometry."""

    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    raw_plates = body.get("plates") or {}
    if not isinstance(raw_plates, dict):
        raise ValueError("plates must be an object")
    unknown = set(raw_plates) - set(BUILT_IN_PLATE_GEOMETRY)
    if unknown:
        raise ValueError(f"unknown plate type {sorted(unknown)[0]!r}")

    plates: dict[str, dict[str, int]] = {}
    limits = {
        "x_offset_steps": (-60, 60),
        "y_offset_steps": (-40, 40),
        "dispense_height_steps": (
            MIN_DISPENSE_HEIGHT_STEPS,
            MAX_DISPENSE_HEIGHT_STEPS,
        ),
    }
    for plate, raw_geometry in raw_plates.items():
        if not isinstance(raw_geometry, dict):
            raise ValueError(f"{plate} geometry must be an object")
        missing = set(PLATE_GEOMETRY_FIELDS) - set(raw_geometry)
        extra = set(raw_geometry) - set(PLATE_GEOMETRY_FIELDS)
        if missing:
            raise ValueError(f"{plate} geometry is missing {sorted(missing)[0]}")
        if extra:
            raise ValueError(f"{plate} geometry has unknown field {sorted(extra)[0]}")
        checked: dict[str, int] = {}
        for field in PLATE_GEOMETRY_FIELDS:
            value = raw_geometry[field]
            if type(value) is not int:
                raise ValueError(f"{plate} {field} must be a whole number")
            minimum, maximum = limits[field]
            if not minimum <= value <= maximum:
                raise ValueError(
                    f"{plate} {field} must be between {minimum} and {maximum} steps"
                )
            checked[field] = value
        if checked != BUILT_IN_PLATE_GEOMETRY[plate]:
            plates[plate] = checked
    return {"version": 1, "plates": plates}


class PlateGeometryStore:
    """Validated, restart-safe defaults for each dashboard plate type."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = Lock()
        self._settings: dict[str, object] = {"version": 1, "plates": {}}
        if self.path.exists():
            try:
                self._settings = validate_plate_geometry_settings(
                    json.loads(self.path.read_text(encoding="utf-8") or "{}")
                )
            except (ValueError, json.JSONDecodeError) as error:
                print(
                    f"ignoring invalid plate geometry at {self.path}: {error}",
                    flush=True,
                )

    def _effective_locked(self) -> dict[str, dict[str, int]]:
        overrides = self._settings["plates"]
        return {
            plate: dict(overrides.get(plate, built_in))
            for plate, built_in in BUILT_IN_PLATE_GEOMETRY.items()
        }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "version": 1,
                "plates": self._effective_locked(),
                "built_in": json.loads(json.dumps(BUILT_IN_PLATE_GEOMETRY)),
                "overridden_plates": sorted(self._settings["plates"]),
            }

    def replace(self, body: object) -> dict[str, object]:
        settings = validate_plate_geometry_settings(body)
        with self._lock:
            temporary = self.path.with_name(self.path.name + ".tmp")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(settings, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self._settings = settings
        return self.snapshot()

    def resolve_dispense(self, body: object) -> object:
        """Fill absent geometry without replacing an explicit caller value."""

        if not isinstance(body, dict):
            return body
        resolved = dict(body)
        plate = resolved.get("plate_type", PlateType.WELL_96.value)
        if isinstance(plate, PlateType):
            plate = plate.value
        with self._lock:
            geometry = self._effective_locked().get(str(plate))
        if geometry is None:
            return resolved
        for field, value in geometry.items():
            if field not in resolved or (
                field == "dispense_height_steps" and resolved[field] is None
            ):
                resolved[field] = value
        return resolved

    def resolve_protocol(self, body: object) -> object:
        if not isinstance(body, dict) or not isinstance(body.get("steps"), list):
            return body
        protocol = dict(body)
        steps: list[object] = []
        for raw_step in body["steps"]:
            if (
                isinstance(raw_step, dict)
                and raw_step.get("operation") == "peristaltic_dispense"
            ):
                steps.append(self.resolve_dispense(raw_step))
            else:
                steps.append(raw_step)
        protocol["steps"] = steps
        return protocol


class _Handler(BaseHTTPRequestHandler):
    server_version = "MultiFloDemoBridge/2.0"
    runner: ProtocolRunner  # injected on the server instance

    # --- helpers ---------------------------------------------------------
    def _send_json(self, code: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> object:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def log_message(self, fmt: str, *args) -> None:  # quieter default logging
        return

    # --- routing ---------------------------------------------------------
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        runner = self.server.runner  # type: ignore[attr-defined]
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/widget.html"):
            self._serve_widget()
            return
        if path == "/v1/health":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "multiflo-dashboard-bridge",
                    "controller_state": runner.state.value,
                    "reconciliation_required": runner.reconciliation_required,
                    "active_run_id": (
                        str(runner.active_run_id) if runner.active_run_id else None
                    ),
                },
            )
            return
        if path == "/v1/device":
            try:
                self._send_json(200, runner.describe_device().model_dump(mode="json"))
            except BusyError as error:
                self._send_json(409, {"detail": str(error)})
            except MultiFloError as error:
                self._send_json(503, {"detail": str(error)})
            return
        if path == "/v1/protocols":
            self._send_json(
                200,
                {"protocols": self.server.protocol_library.list()},  # type: ignore[attr-defined]
            )
            return
        if path == "/v1/procedure-settings":
            self._send_json(
                200,
                self.server.procedure_settings.snapshot(),  # type: ignore[attr-defined]
            )
            return
        if path == "/v1/plate-geometry":
            self._send_json(
                200,
                self.server.plate_geometry.snapshot(),  # type: ignore[attr-defined]
            )
            return
        if path == "/v1/maintenance/auto-prime":
            self._send_json(
                200,
                self.server.auto_prime_scheduler.snapshot(),  # type: ignore[attr-defined]
            )
            return
        if path == "/v1/cassette-usage":
            self._send_json(
                200,
                self.server.usage_store.snapshot(),  # type: ignore[attr-defined]
            )
            return
        if path.startswith("/v1/runs/"):
            self._get_run(path)
            return
        self._send_json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/v1/reconcile":
            self._reconcile()
            return
        if path == "/v1/system/shutdown":
            self._shutdown_system()
            return
        if path.startswith("/v1/cassette-usage/") and path.endswith("/reset"):
            self._reset_cassette_usage(path)
            return
        if path == "/v1/protocols/validate":
            self._validate()
            return
        if path == "/v1/protocols":
            self._save_protocol()
            return
        if path == "/v1/procedure-settings":
            self._save_procedure_settings()
            return
        if path == "/v1/plate-geometry":
            self._save_plate_geometry()
            return
        if path == "/v1/maintenance/auto-prime":
            self._save_auto_prime_settings()
            return
        if path == "/v1/runs":
            self._start_run()
            return
        if path in OPERATION_ROUTES:
            self._start_operation(path)
            return
        if path.startswith("/v1/runs/") and path.endswith("/abort"):
            self._abort_run(path)
            return
        self._send_json(404, {"detail": "not found"})

    def do_DELETE(self) -> None:
        path = self.path.split("?", 1)[0]
        if path.startswith("/v1/protocols/") and path != "/v1/protocols/validate":
            self._delete_protocol(path[len("/v1/protocols/"):])
            return
        self._send_json(404, {"detail": "not found"})

    # --- handlers --------------------------------------------------------
    def _serve_widget(self) -> None:
        try:
            body = WIDGET_PATH.read_bytes()
        except OSError as error:
            self._send_json(500, {"detail": f"widget not found: {error}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # The kiosk keeps a persistent profile, so its HTTP cache would
        # otherwise survive a deployment and serve the previous dashboard.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _shutdown_system(self) -> None:
        runner = self.server.runner  # type: ignore[attr-defined]
        try:
            body = self._read_json()
        except json.JSONDecodeError as error:
            self._send_json(400, {"detail": str(error)})
            return
        if not isinstance(body, dict) or body.get("operator_confirmed_shutdown") is not True:
            self._send_json(
                422,
                {"detail": "operator_confirmed_shutdown must be true"},
            )
            return
        if runner.active_run_id is not None:
            self._send_json(409, {"detail": "cannot shut down while a run is active"})
            return
        try:
            self.server.shutdown_callback()  # type: ignore[attr-defined]
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            self._send_json(503, {"detail": str(error)})
            return
        self._send_json(202, {"status": "shutting_down"})

    def _validate(self) -> None:
        try:
            data = self._read_json()
            protocol_body = self.server.plate_geometry.resolve_protocol(data)  # type: ignore[attr-defined]
            protocol = Protocol.model_validate(protocol_body)
        except PydanticValidationError as error:
            self._send_json(422, {"detail": json.loads(error.json())})
            return
        except (ValueError, json.JSONDecodeError) as error:
            self._send_json(400, {"detail": str(error)})
            return
        self._send_json(200, {"valid": True, "protocol": protocol.model_dump(mode="json")})

    def _save_protocol(self) -> None:
        library = self.server.protocol_library  # type: ignore[attr-defined]
        try:
            body = self._read_json()
        except json.JSONDecodeError as error:
            self._send_json(400, {"detail": str(error)})
            return
        try:
            protocol = library.save(body)
        except KeyError:
            self._send_json(404, {"detail": "protocol not found"})
            return
        except PydanticValidationError as error:
            self._send_json(422, {"detail": json.loads(error.json())})
            return
        except ValueError as error:
            self._send_json(422, {"detail": str(error)})
            return
        self._send_json(200, protocol)

    def _save_procedure_settings(self) -> None:
        store = self.server.procedure_settings  # type: ignore[attr-defined]
        try:
            body = self._read_json()
        except json.JSONDecodeError as error:
            self._send_json(400, {"detail": str(error)})
            return
        try:
            self._send_json(200, store.replace(body))
        except ValueError as error:
            self._send_json(422, {"detail": str(error)})

    def _save_plate_geometry(self) -> None:
        store = self.server.plate_geometry  # type: ignore[attr-defined]
        try:
            body = self._read_json()
        except json.JSONDecodeError as error:
            self._send_json(400, {"detail": str(error)})
            return
        try:
            self._send_json(200, store.replace(body))
        except ValueError as error:
            self._send_json(422, {"detail": str(error)})

    def _save_auto_prime_settings(self) -> None:
        scheduler = self.server.auto_prime_scheduler  # type: ignore[attr-defined]
        try:
            body = self._read_json()
        except json.JSONDecodeError as error:
            self._send_json(400, {"detail": str(error)})
            return
        try:
            scheduler.store.replace(body)
            self._send_json(200, scheduler.snapshot())
        except ValueError as error:
            self._send_json(422, {"detail": str(error)})

    def _delete_protocol(self, protocol_id: str) -> None:
        library = self.server.protocol_library  # type: ignore[attr-defined]
        try:
            library.delete(protocol_id)
        except KeyError:
            self._send_json(404, {"detail": "protocol not found"})
            return
        self._send_json(200, {"deleted": protocol_id})

    def _reconcile(self) -> None:
        runner = self.server.runner  # type: ignore[attr-defined]
        try:
            body = self._read_json()
        except json.JSONDecodeError as error:
            self._send_json(400, {"detail": str(error)})
            return
        if not isinstance(body, dict) or body.get("operator_confirmed_stationary") is not True:
            self._send_json(
                422,
                {"detail": "operator_confirmed_stationary must be true"},
            )
            return
        try:
            controller_state = runner.reconcile_startup()
        except BusyError as error:
            self._send_json(409, {"detail": str(error)})
            return
        except MultiFloError as error:
            self._send_json(503, {"detail": str(error)})
            return
        self._send_json(200, {"controller_state": controller_state.value})

    def _start_run(self) -> None:
        try:
            body = self._read_json()
        except json.JSONDecodeError as error:
            self._send_json(400, {"detail": str(error)})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"detail": "body must be a JSON object"})
            return
        if body.get("operator_confirmed_idle") is not True:
            self._send_json(422, {"detail": "operator_confirmed_idle must be true"})
            return
        try:
            protocol_body = self.server.plate_geometry.resolve_protocol(  # type: ignore[attr-defined]
                body.get("protocol")
            )
            protocol = Protocol.model_validate(protocol_body)
            request_id = REQUEST_ID_ADAPTER.validate_python(body.get("request_id"))
        except PydanticValidationError as error:
            self._send_json(422, {"detail": json.loads(error.json())})
            return
        self._submit_protocol(protocol, request_id)

    def _start_operation(self, path: str) -> None:
        try:
            body = self._read_json()
        except json.JSONDecodeError as error:
            self._send_json(400, {"detail": str(error)})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"detail": "body must be a JSON object"})
            return
        if body.get("operator_confirmed_idle") is not True:
            self._send_json(422, {"detail": "operator_confirmed_idle must be true"})
            return
        try:
            request_id = REQUEST_ID_ADAPTER.validate_python(body.get("request_id"))
            step_body = {
                key: value
                for key, value in body.items()
                if key not in {"request_id", "operator_confirmed_idle"}
            }
            if OPERATION_ROUTES[path] is PeristalticDispense:
                step_body = self.server.plate_geometry.resolve_dispense(  # type: ignore[attr-defined]
                    step_body
                )
            step = OPERATION_ROUTES[path].model_validate(step_body)
            protocol = Protocol(name=f"api:{step.operation}", steps=[step])
        except PydanticValidationError as error:
            self._send_json(422, {"detail": json.loads(error.json())})
            return
        self._submit_protocol(protocol, request_id)

    def _submit_protocol(self, protocol: Protocol, request_id: str) -> None:
        runner = self.server.runner  # type: ignore[attr-defined]
        try:
            result = runner.start(
                protocol,
                operator_confirmed_idle=True,
                request_id=request_id,
            )
        except BusyError as error:
            self._send_json(409, {"detail": str(error)})
            return
        except MultiFloError as error:
            self._send_json(503, {"detail": str(error)})
            return
        code = 200 if result.duplicate else 202
        self._send_json(code, result.status.model_dump(mode="json"))

    def _reset_cassette_usage(self, path: str) -> None:
        runner = self.server.runner  # type: ignore[attr-defined]
        raw_cassette = path[len("/v1/cassette-usage/"):-len("/reset")]
        try:
            cassette = CassetteType(raw_cassette)
        except ValueError:
            self._send_json(422, {"detail": "cassette must be 1ul or 5ul"})
            return
        if cassette not in CASSETTE_LIMITS_UL:
            self._send_json(422, {"detail": "cassette must be 1ul or 5ul"})
            return
        try:
            body = self._read_json()
        except json.JSONDecodeError as error:
            self._send_json(400, {"detail": str(error)})
            return
        if not isinstance(body, dict) or body.get("operator_confirmed_replacement") is not True:
            self._send_json(
                422,
                {"detail": "operator_confirmed_replacement must be true"},
            )
            return
        try:
            device = runner.describe_device()
        except BusyError as error:
            self._send_json(409, {"detail": str(error)})
            return
        if (
            not device.connected
            or device.modules is None
            or device.modules.primary_cassette is not cassette
        ):
            self._send_json(
                409,
                {"detail": f"the fitted cassette is not {cassette.value}"},
            )
            return
        self.server.usage_store.reset(cassette)  # type: ignore[attr-defined]
        self._send_json(
            200,
            self.server.usage_store.snapshot(),  # type: ignore[attr-defined]
        )

    def _get_run(self, path: str) -> None:
        runner = self.server.runner  # type: ignore[attr-defined]
        raw = path[len("/v1/runs/"):]
        try:
            run_id = UUID(raw)
        except ValueError:
            self._send_json(422, {"detail": "invalid run id"})
            return
        run = runner.get(run_id)
        if run is None:
            self._send_json(404, {"detail": "run not found"})
            return
        self._send_json(200, run.model_dump(mode="json"))

    def _abort_run(self, path: str) -> None:
        runner = self.server.runner  # type: ignore[attr-defined]
        raw = path[len("/v1/runs/"):-len("/abort")]
        try:
            run_id = UUID(raw)
        except ValueError:
            self._send_json(422, {"detail": "invalid run id"})
            return
        run = runner.abort(run_id)
        if run is None:
            self._send_json(404, {"detail": "run not found"})
            return
        self._send_json(200, run.model_dump(mode="json"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--http-port", type=int, default=8000)
    parser.add_argument(
        "--serial-port",
        default="/dev/serial/by-id/usb-BTI_MultiFlo_14071419-if00-port0",
    )
    parser.add_argument("--expected-serial", default="14071419")
    parser.add_argument("--read-timeout-seconds", type=float, default=3.0)
    parser.add_argument("--completion-timeout-seconds", type=float, default=600.0)
    parser.add_argument(
        "--marker-path",
        default=str(STATE_DIR / ".multiflo-active-run.json"),
    )
    parser.add_argument(
        "--usage-ledger-path",
        default=str(STATE_DIR / "cassette_usage.jsonl"),
    )
    parser.add_argument(
        "--protocol-library-path",
        default=str(STATE_DIR / "dashboard_protocols.json"),
    )
    parser.add_argument(
        "--procedure-settings-path",
        default=str(STATE_DIR / "procedure_settings.json"),
    )
    parser.add_argument(
        "--plate-geometry-path",
        default=str(STATE_DIR / "plate_geometry.json"),
    )
    parser.add_argument(
        "--auto-prime-settings-path",
        default=str(STATE_DIR / "auto_prime_settings.json"),
    )
    args = parser.parse_args()

    driver = MultiFloDriver(
        SerialByteTransport(
            args.serial_port,
            config=SerialConfig(read_timeout_seconds=args.read_timeout_seconds),
        ),
        expected_product_serial=args.expected_serial,
        completion_timeout_seconds=args.completion_timeout_seconds,
    )
    usage_store = CassetteUsageStore(args.usage_ledger_path)
    protocol_library = ProtocolLibrary(args.protocol_library_path)
    procedure_settings = ProcedureSettingsStore(args.procedure_settings_path)
    plate_geometry = PlateGeometryStore(args.plate_geometry_path)
    auto_prime_store = AutoPrimeStore(args.auto_prime_settings_path)

    def record_completed_step(
        run_id: UUID,
        step_index: int,
        step: ProtocolStep,
        result: StepResult,
        resolved_cassette: CassetteType,
    ) -> None:
        errors: list[Exception] = []
        for callback in (usage_store.record_step, auto_prime_store.record_step):
            try:
                callback(
                    run_id,
                    step_index,
                    step,
                    result,
                    resolved_cassette,
                )
            except Exception as error:
                errors.append(error)
        if errors:
            raise RuntimeError("; ".join(str(error) for error in errors))

    runner = ProtocolRunner(
        driver,
        crash_marker_path=args.marker_path,
        on_step_completed=record_completed_step,
    )
    auto_prime_scheduler = AutoPrimeScheduler(runner, auto_prime_store)

    server = ThreadingHTTPServer((args.host, args.http_port), _Handler)
    server.runner = runner  # type: ignore[attr-defined]
    server.usage_store = usage_store  # type: ignore[attr-defined]
    server.protocol_library = protocol_library  # type: ignore[attr-defined]
    server.procedure_settings = procedure_settings  # type: ignore[attr-defined]
    server.plate_geometry = plate_geometry  # type: ignore[attr-defined]
    server.auto_prime_scheduler = auto_prime_scheduler  # type: ignore[attr-defined]
    server.shutdown_callback = schedule_system_shutdown  # type: ignore[attr-defined]
    print(
        f"MultiFlo dashboard bridge on http://{args.host}:{args.http_port}/ "
        f"serial={args.serial_port} expected={args.expected_serial}",
        flush=True,
    )
    auto_prime_scheduler.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("shutting down", flush=True)
    finally:
        auto_prime_scheduler.stop()
        server.server_close()
        runner.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
