"""Read Quartus measurements without changing the programming verification gate.

Missing measurements stay unknown. Timing path extraction only reads the fitted
netlist; it neither recompiles the design nor accesses a programmer.
"""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time

from circuitlab.tooling import fpga


RESOURCE_LABELS = {
    "logic_elements": "Total logic elements",
    "registers": "Total registers",
    "pins": "Total pins",
    "memory_bits": "Total memory bits",
    "multipliers": "Embedded Multiplier 9-bit elements",
    "plls": "Total PLLs",
}
METRICS = ("fmax_mhz", "clock_mhz", "setup_slack_ns", "hold_slack_ns", "pulse_slack_ns")
PATH_FILE = "ooc-timing-paths.tsv"
REQUEST_FILE = "ooc-timing-request.json"


def _number(text):
    try:
        number = float(str(text).strip().replace(",", ""))
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _minimum(values):
    return min((value for value in values if value is not None), default=None)


def _record(project):
    record = json.loads((project.directory / "ooc-build.json").read_text(encoding="utf-8"))
    if (not re.fullmatch(r"[a-f0-9]{64}", project.source_sha256)
            or record["source_sha256"] != project.source_sha256
            or record["inputs"] != fpga._input_hashes(project.directory)
            or not isinstance(record["input_mtime_ns"], int)):
        raise ValueError("The generated project changed; rebuild to obtain matching measurements.")
    return record


def _fresh_text(path, stamp, notes):
    try:
        if path.stat().st_mtime_ns < stamp:
            notes.append(f"Ignoring stale report: {path.name}.")
            return ""
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        notes.append(f"Report unavailable: {path.name}.")
        return ""


def _properties(text):
    return {key.strip(): value.strip()
            for key, separator, value in (line.partition(":") for line in text.splitlines())
            if separator}


def _resource(text):
    match = re.match(r"^([\d,]+)(?:\s*/\s*([\d,]+))?(?:\s|$)", text)
    if not match:
        return {"used": None, "total": None}
    return {"used": int(match[1].replace(",", "")),
            "total": int(match[2].replace(",", "")) if match[2] else None}


def _timing_sections(text):
    return [{"type": match[1].strip(), "slack_ns": _number(match[2])}
            for match in re.finditer(r"(?m)^Type\s*:\s*(.+)\nSlack\s*:\s*(\S+)", text)]


def _clock_tables(text):
    clocks, fmax = [], []
    section = ""
    for line in text.splitlines():
        if not line.startswith(";"):
            continue
        cells = [cell.strip() for cell in line.strip().strip(";").split(";")]
        if len(cells) == 1 and cells[0]:
            section = cells[0]
        elif section == "Clocks" and len(cells) >= 4 and cells[0] != "Clock Name":
            period = _number(cells[2])
            if period is not None and period > 0:
                clocks.append({"name": cells[0], "period_ns": period, "frequency_mhz": 1000 / period})
        elif section.endswith("Fmax Summary") and len(cells) >= 3 and cells[0] != "Fmax":
            raw, restricted = (_number(cell.removesuffix("MHz").strip()) for cell in cells[:2])
            # Restricted Fmax includes device constraints. Do not substitute the
            # unrestricted value if the restricted result is absent or invalid.
            fmax.append({"corner": section.removesuffix(" Fmax Summary"), "clock": cells[2],
                         "fmax_mhz": raw if raw is not None and raw > 0 else None,
                         "restricted_fmax_mhz": restricted if restricted is not None and restricted > 0 else None})
    return clocks, fmax


def _read_paths(project, record, notes):
    path = project.directory / PATH_FILE
    if not path.exists():
        notes.append("Detailed timing paths are unavailable; run timing-path analysis after a completed fit.")
        return []
    try:
        request = json.loads((project.directory / REQUEST_FILE).read_text(encoding="utf-8"))
        if (request["source_sha256"] != project.source_sha256 or request["inputs"] != record["inputs"]
                or path.stat().st_mtime_ns < request["requested_ns"]):
            raise ValueError("stale timing-path extraction")
        rows = path.read_text(encoding="utf-8").splitlines()
        if rows[:2] != ["OOC_TIMING_PATHS\t1", "SOURCE\t" + project.source_sha256] or rows[-1:] != ["END"]:
            raise ValueError("incomplete timing-path extraction")
        paths = []
        for line in rows[2:-1]:
            cells = line.split("\t")
            if len(cells) != 9 or cells[0] != "PATH":
                raise ValueError("invalid timing-path row")
            corner, start, end = cells[1:4]
            slack, delay, levels, logic, routing = map(_number, cells[4:])
            if not start or not end or slack is None or delay is None:
                raise ValueError("invalid timing-path measurements")
            paths.append({"corner": corner, "from_node": start, "to_node": end,
                          "slack_ns": slack, "data_delay_ns": delay,
                          "logic_levels": int(levels) if levels is not None and levels.is_integer() else None,
                          "logic_delay_ns": logic, "routing_delay_ns": routing})
        return sorted(paths, key=lambda item: item["slack_ns"])
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        notes.append(f"Detailed timing paths unavailable: {error}.")
        return []


def read_report(project, *, source_path="", verified=False, verification_error=""):
    """Return JSON-safe measurements from this build, including failed timing.

``verified`` records the caller's existing programming verification result; this
function does not grant permission to program. Partial reports may be useful for
diagnosing a failed build, so resource and timing availability are independent.
    """
    result = {
        "schema": 1, "captured_at": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source_path), "source_sha256": project.source_sha256,
        "build_directory": str(project.directory), "device": "", "quartus_version": "",
        "constraints_sha256": "", "verified": bool(verified),
        "verification_error": str(verification_error),
        "metrics": dict.fromkeys(METRICS),
        "resources": {key: {"used": None, "total": None} for key in RESOURCE_LABELS},
        "paths": [], "clocks": [], "fmax_corners": [], "timing_checks": [], "notes": [],
    }
    notes = result["notes"]
    try:
        record = _record(project)
    except (OSError, ValueError, KeyError, TypeError) as error:
        result["verified"] = False
        notes.append(f"Measurements unavailable: {error}")
        return result
    constraints = {key: value for key, value in record["inputs"].items() if key.endswith((".qsf", ".sdc"))}
    result["constraints_sha256"] = hashlib.sha256(json.dumps(constraints, sort_keys=True).encode()).hexdigest()
    result["clockless"] = "create_clock " not in fpga.canonical_constraints(
        (project.directory / "HYBRID01.sdc").read_text())
    out = project.directory / "build/output_files"
    stamp = record["input_mtime_ns"]
    fit = _fresh_text(out / f"{fpga.TOP}.fit.summary", stamp, notes)
    summary = _fresh_text(out / f"{fpga.TOP}.sta.summary", stamp, notes)
    timing = _fresh_text(out / f"{fpga.TOP}.sta.rpt", stamp, notes)
    props = _properties(fit)
    result["device"] = props.get("Device", "")
    result["quartus_version"] = props.get("Quartus Prime Version", "")
    for key, label in RESOURCE_LABELS.items():
        result["resources"][key] = _resource(props.get(label, ""))
    dedicated = _resource(props.get("Dedicated logic registers", ""))
    if dedicated["used"] == result["resources"]["registers"]["used"]:
        result["resources"]["registers"]["total"] = dedicated["total"]
    result["timing_checks"] = _timing_sections(summary)
    for label, key in ((" Setup ", "setup_slack_ns"), (" Hold ", "hold_slack_ns"),
                       (" Minimum Pulse Width ", "pulse_slack_ns")):
        result["metrics"][key] = _minimum(row["slack_ns"] for row in result["timing_checks"] if label in row["type"])
    result["clocks"], result["fmax_corners"] = _clock_tables(timing)
    result["metrics"]["clock_mhz"] = (result["clocks"][0]["frequency_mhz"] if len(result["clocks"]) == 1 else None)
    result["metrics"]["fmax_mhz"] = _minimum(row["restricted_fmax_mhz"] for row in result["fmax_corners"])
    result["paths"] = _read_paths(project, record, notes)
    if any(row["slack_ns"] is None for row in result["timing_checks"]):
        notes.append("At least one timing check has an unavailable or invalid slack value.")
    if any(row["restricted_fmax_mhz"] is None for row in result["fmax_corners"]):
        notes.append("At least one Fmax corner is unavailable; the displayed minimum covers reported numeric corners only.")
    if not summary or not timing:
        result["verified"] = False
    inapplicable = {"clock_mhz", "fmax_mhz", "pulse_slack_ns"} if result["clockless"] else set()
    if any(value is None for key, value in result["metrics"].items() if key not in inapplicable):
        notes.append("Some measurements are unavailable; unknown values are not zero.")
    if result["clockless"]:
        notes.append("Clockless combinational logic: clock rate, Fmax, and pulse-width checks do not apply. "
                     "Setup/hold slack checks the 20 ns maximum / 0 ns minimum pin-to-pin delay budget.")
    else:
        notes.append("Cell delay includes register clock-to-Q; routing delay excludes the launch clock network.")
        notes.append("Fmax is a fitted estimate for same-clock paths, not the running clock rate; setup/hold constraints still apply.")
    return result


def report_command(project, quartus_bin):
    """Prepare and return an asynchronous Quartus STA command for fitted paths."""
    record = _record(project)
    script = project.directory / "ooc-performance.tcl"
    script.write_text(_TCL.replace("@SOURCE_SHA256@", project.source_sha256), encoding="utf-8")
    request = {"source_sha256": project.source_sha256, "inputs": record["inputs"], "requested_ns": time.time_ns()}
    (project.directory / REQUEST_FILE).write_text(json.dumps(request), encoding="utf-8")
    return [str(Path(quartus_bin) / "quartus_sta.exe"), "-t", str(script)]


_TCL = r'''# OOC: read the fitted timing database without changing build constraints.
load_package sta
project_open HYBRID01
create_timing_netlist
read_sdc
proc clean_field {value} {
    return [string map [list "\t" " " "\r" " " "\n" " "] $value]
}
set output [open "ooc-timing-paths.tsv" w]
fconfigure $output -encoding utf-8 -translation lf
puts $output "OOC_TIMING_PATHS\t1"
puts $output "SOURCE\t@SOURCE_SHA256@"
foreach_in_collection corner [get_available_operating_conditions] {
    set_operating_conditions $corner
    update_timing_netlist
    set label "[get_operating_conditions_info $corner -model] [get_operating_conditions_info $corner -voltage]mV [get_operating_conditions_info $corner -temperature]C"
    foreach_in_collection path [get_timing_paths -setup -npaths 5 -nworst 1 -detail path_only] {
        set from [get_node_info -name [get_path_info $path -from]]
        set to [get_node_info -name [get_path_info $path -to]]
        set slack [get_path_info $path -slack]
        set delay [get_path_info $path -data_delay]
        set levels [get_path_info $path -num_logic_levels]
        set logic 0.0
        set routing 0.0
        foreach_in_collection point [get_path_info $path -arrival_points] {
            set type [get_point_info $point -type]
            if {$type eq "cell" || $type eq "utco"} {
                set logic [expr {$logic + [get_point_info $point -incremental_delay]}]
            } elseif {$type eq "ic"} {
                set routing [expr {$routing + [get_point_info $point -incremental_delay]}]
            }
        }
        # Unknown point categories must not produce an invented breakdown.
        if {abs($logic + $routing - $delay) > 0.005} {
            set logic ""
            set routing ""
        }
        set values [list PATH [clean_field $label] [clean_field $from] [clean_field $to] $slack $delay $levels $logic $routing]
        puts $output [join $values "\t"]
    }
}
puts $output END
close $output
delete_timing_netlist
project_close
'''
