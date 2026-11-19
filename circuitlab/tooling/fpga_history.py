"""Explicit, per-source FPGA performance baselines and numerical comparisons.

Snapshots describe Quartus reports, not functional equivalence. Merely building
another design never replaces a saved baseline.
"""
# A baseline is an explicit, per-source snapshot, while latest measurements are separate.
# Compare only compatible reports and preserve missing measurements as unknown; historical
# performance data never authorizes programming a stale FPGA image.

import hashlib
import json
import math
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile


METRICS = ("fmax_mhz", "clock_mhz", "setup_slack_ns", "hold_slack_ns", "pulse_slack_ns")
RESOURCES = ("logic_elements", "registers", "pins", "memory_bits", "multipliers", "plls")
MAX_BASELINE_BYTES = 512 * 1024


def _source_key(source_path):
    if not isinstance(source_path, (str, os.PathLike)) or not str(source_path).strip():
        return None
    try:
        return os.path.normcase(str(Path(source_path).expanduser().resolve()))
    except (OSError, ValueError):
        return None


def _snapshot_path(build_dir, source_path, folder):
    key = _source_key(source_path)
    if key is None:
        raise ValueError("Save the OOC source before saving its performance baseline.")
    filename = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
    return Path(build_dir) / folder / filename


def _number(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value))
    except OverflowError:
        return False


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{64}", value) is not None


def _valid_report(report, source_path):
    if not isinstance(report, dict) or report.get("schema") != 1 or report.get("verified") is not True:
        return False
    if _source_key(report.get("source_path")) != _source_key(source_path):
        return False
    if not all(isinstance(report.get(key), str) and report[key].strip()
               for key in ("source_path", "device", "quartus_version")):
        return False
    if not all(_hash(report.get(key)) for key in ("source_sha256", "constraints_sha256")):
        return False
    metrics, resources = report.get("metrics"), report.get("resources")
    if not isinstance(metrics, dict) or not isinstance(resources, dict):
        return False
    if any(value is not None and not _number(value) for value in metrics.values()):
        return False
    for resource in resources.values():
        if not isinstance(resource, dict):
            return False
        for key in ("used", "total"):
            value = resource.get(key)
            if value is not None and (not _number(value) or value < 0):
                return False
    if not isinstance(report.get("paths", []), list) or any(
            not isinstance(item, dict) for item in report.get("paths", [])):
        return False
    for path in report.get("paths", []):
        if any(path.get(key) is not None and not _number(path[key]) for key in (
                "slack_ns", "data_delay_ns", "logic_delay_ns", "routing_delay_ns", "logic_levels")):
            return False
        if any(path.get(key) is not None and not isinstance(path[key], str)
               for key in ("corner", "from_node", "to_node")):
            return False
    if report.get("verification_error") is not None and not isinstance(report["verification_error"], str):
        return False
    if not isinstance(report.get("notes", []), list) or any(
            not isinstance(item, str) for item in report.get("notes", [])):
        return False
    # A clock annotation by itself is not a fitted performance measurement.
    return (any(_number(metrics.get(key)) for key in METRICS if key != "clock_mhz")
            or any(_number(resources.get(key, {}).get("used")) for key in RESOURCES))


def _load_snapshot(build_dir, source_path, folder):
    try:
        with _snapshot_path(build_dir, source_path, folder).open("rb") as stream:
            data = stream.read(MAX_BASELINE_BYTES + 1)
        if len(data) > MAX_BASELINE_BYTES:
            return None
        def invalid_constant(value):
            raise ValueError(f"Non-finite JSON number: {value}")

        report = json.loads(data, parse_constant=invalid_constant)
        return report if _valid_report(report, source_path) else None
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        return None


def _save_snapshot(build_dir, source_path, report, folder):
    destination = _snapshot_path(build_dir, source_path, folder)
    if not _valid_report(report, source_path):
        raise ValueError("A verified build with matching source and measured results is required for a baseline.")
    data = (json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    if len(data) > MAX_BASELINE_BYTES:
        raise ValueError("The performance baseline is too large to save.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=destination.parent, prefix="baseline-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_baseline(build_dir, source_path):
    """Return the explicitly saved baseline, or None for missing/damaged files."""
    return _load_snapshot(build_dir, source_path, "performance-baselines")


# Replace a baseline only on an explicit request and only with a report suitable for that role.
def save_baseline(build_dir, source_path, report):
    """Atomically save an explicitly selected verified report for this source."""
    _save_snapshot(build_dir, source_path, report, "performance-baselines")


def load_latest(build_dir, source_path):
    """Restore the last verified measurements; never authorize programming."""
    return _load_snapshot(build_dir, source_path, "performance-latest")


def save_latest(build_dir, source_path, report):
    """Retain verified measurements without replacing the comparison baseline."""
    _save_snapshot(build_dir, source_path, report, "performance-latest")


# Subtract compatible numeric measurements while retaining unknown fields as unknown.
# Differences in tool/profile/clock settings prevent misleading direct comparisons.
def compare_reports(current, baseline):
    """Return current-minus-baseline deltas only for comparable build settings.

    A timing failure can still have useful measurements; current need not be a
    verified programming image. The caller must display its verification status.
    """
    result = {"compatible": False, "reasons": [],
              "metrics": dict.fromkeys(METRICS), "resources": dict.fromkeys(RESOURCES)}
    reasons = result["reasons"]
    if not isinstance(baseline, dict):
        reasons.append("No performance baseline saved for this source.")
        return result
    if not isinstance(current, dict):
        reasons.append("Build this source to obtain current performance results.")
        return result
    current_path, baseline_path = _source_key(current.get("source_path")), _source_key(baseline.get("source_path"))
    if current_path is None or baseline_path is None or current_path != baseline_path:
        reasons.append("The reports belong to different or unknown source files.")
    for key, label in (("device", "FPGA device"), ("quartus_version", "Quartus version"),
                       ("constraints_sha256", "build constraints")):
        before, after = baseline.get(key), current.get(key)
        if not isinstance(before, str) or not before or not isinstance(after, str) or not after:
            reasons.append(f"The {label} are unavailable for comparison.")
        elif before != after:
            reasons.append(f"The {label} changed between builds.")
    old_metrics, new_metrics = baseline.get("metrics", {}), current.get("metrics", {})
    old_resources, new_resources = baseline.get("resources", {}), current.get("resources", {})
    if not all(isinstance(value, dict) for value in (old_metrics, new_metrics, old_resources, new_resources)):
        reasons.append("Performance measurements are missing or malformed.")
        return result
    old_clock, new_clock = old_metrics.get("clock_mhz"), new_metrics.get("clock_mhz")
    if not _number(old_clock) or not _number(new_clock) or old_clock <= 0 or new_clock <= 0:
        reasons.append("The requested clock is unavailable for comparison.")
    elif old_clock != new_clock:
        reasons.append("The requested clock changed between builds.")
    if reasons:
        return result
    result["compatible"] = True
    for key in METRICS:
        before, after = old_metrics.get(key), new_metrics.get(key)
        if _number(before) and _number(after):
            delta = after - before
            result["metrics"][key] = delta if _number(delta) else None
    for key in RESOURCES:
        before, after = old_resources.get(key, {}), new_resources.get(key, {})
        if not isinstance(before, dict) or not isinstance(after, dict):
            continue
        before, after = before.get("used"), after.get("used")
        if _number(before) and _number(after):
            delta = after - before
            result["resources"][key] = delta if _number(delta) else None
    return result
