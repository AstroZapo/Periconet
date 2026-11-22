"""Interpretación del estado de NINA y selección conservadora de capturas."""
import hashlib
import json
import time
from astronomy import number


def normalize(value):
    return str(value or "").strip().casefold()


def fingerprint(item):
    return hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()


def sexagesimal(data, fields):
    major, minutes, seconds = (number(data.get(field)) for field in fields)
    if major is None or minutes is None or seconds is None:
        return None
    if not 0 <= minutes <= 60 or not 0 <= seconds < 60:
        return None
    return abs(major) + minutes / 60 + seconds / 3600


def target_coordinates(data):
    inputs = data.get("InputCoordinates")
    inputs = inputs if isinstance(inputs, dict) else {}
    for coords in (inputs.get("Coordinates"), data.get("Coordinates"), inputs):
        if not isinstance(coords, dict):
            continue
        ra, dec = number(coords.get("RA")), number(coords.get("Dec"))
        if ra is None:
            degrees = number(coords.get("RADegrees"))
            if degrees is not None:
                ra = degrees / 15
            elif (number(coords.get("RAHours")) or 0) >= 0:
                ra = sexagesimal(coords, ("RAHours", "RAMinutes", "RASeconds"))
        if dec is None:
            dec = sexagesimal(coords, ("DecDegrees", "DecMinutes", "DecSeconds"))
            if dec is not None and (coords.get("NegativeDec") is True or number(coords["DecDegrees"]) < 0):
                dec = -dec
        if ra is not None and dec is not None and 0 <= ra <= 24 and -90 <= dec <= 90:
            return coords, ra % 24, dec
    return {}, None, None


def target_from_data(data, path=""):
    if not isinstance(data, dict) or not str(data.get("TargetName") or "").strip():
        return None
    # El secuenciador serializa InputCoordinates por componentes; Target
    # Scheduler envía RA y DEC decimales en Coordinates.
    coords, ra, dec = target_coordinates(data)
    name = str(data["TargetName"]).strip()
    return {"key": fingerprint([path, normalize(name), ra, dec]), "name": name,
            "ra": ra, "dec": dec, "ra_string": coords.get("RAString") or (f"{ra:.4f} h" if ra is not None else ""),
            "dec_string": coords.get("DecString") or (f"{dec:.4f}°" if dec is not None else ""),
            "epoch": coords.get("Epoch"), "filter_summary": {}}


def active_target(sequence, *, matching_name=None, key_path=None):
    """Solo ramas en ejecución. Ante objetivos simultáneos ambiguos, no inferir."""
    candidates = []
    expected_name = normalize(matching_name)

    def visit(node, path):
        if isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, f"{path}/{index}")
        elif isinstance(node, dict):
            status = str(node.get("Status", "")).upper()
            if status and status != "RUNNING":
                return
            if status == "RUNNING":
                target = target_from_data(node.get("Target"), key_path or path)
                if target and (not expected_name or normalize(target["name"]) == expected_name):
                    candidates.append(target)
            visit(node.get("Items", []), path + "/Items")
    visit(sequence, "sequence")
    if len(candidates) == 1:
        return candidates[0]
    return None


class CaptureTracker:
    def __init__(self):
        self.target = None
        self.key = None
        self.history = []
        self.floor = 0
        self.initialized = False
        self.generation = 0

    def set_target(self, target):
        key = target.get("key") if target else None
        changed = key != self.key
        if changed:
            self.floor = len(self.history)
            self.generation += 1
        self.key, self.target = key, target
        return changed

    def update_history(self, history):
        # Al conectar no se puede probar que una imagen anterior pertenezca
        # al tramo actual del objetivo: se espera una captura nueva.
        reset = len(history) < len(self.history) or (self.history and history and
                fingerprint(self.history[0]) != fingerprint(history[0]))
        if not self.initialized or reset:
            self.floor = len(history)
            self.initialized = True
            self.generation += 1
        self.history = history
        return reset

    def matching(self, item):
        return bool(self.target and normalize(item.get("TargetName")) == normalize(self.target["name"])
                    and str(item.get("ImageType", "")).upper() == "LIGHT")

    def latest(self):
        for index in range(len(self.history) - 1, self.floor - 1, -1):
            if self.matching(self.history[index]):
                return index, self.history[index]
        return None

    def summary(self):
        summary = {}
        for item in self.history:
            if self.matching(item):
                entry = summary.setdefault(item.get("Filter") or "Sin filtro", {"count": 0, "total_seconds": 0})
                entry["count"] += 1
                entry["total_seconds"] += max(0, number(item.get("ExposureTime")) or 0)
        return summary


def exposure_state(camera, previous=None):
    camera = camera or {}
    connected = camera.get("Connected", False)
    state = camera.get("CameraState", "Desconocido") if connected else "Desconectado"
    exposing = connected and bool(camera.get("IsExposing") or state == "Exposing")
    end_time = camera.get("ExposureEndTime") if exposing else None
    observed_at_ms = None
    if end_time:
        if previous and previous.get("end_time") == end_time:
            observed_at_ms = previous.get("observed_at_ms")
        if observed_at_ms is None:
            # La API facilita la hora de fin. Conservamos el primer sondeo de esta toma.
            observed_at_ms = time.time_ns() // 1_000_000
    return {"is_exposing": exposing, "camera_state": state,
            "end_time": end_time, "observed_at_ms": observed_at_ms, "duration": 0}
