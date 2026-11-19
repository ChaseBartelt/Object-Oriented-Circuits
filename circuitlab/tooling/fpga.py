"""Pure build/program preparation for the verified DE10-Lite HYBRID01 profile.

No command is executed here. The viewer runs argv lists asynchronously and asks
the user to program explicitly. Board constants ship with the Python runtime.
"""
# These helpers prepare files and argv lists; they do not launch Quartus or hardware access.
# Programming eligibility depends on matching source, board settings, constraints, and verified
# build artifacts, not merely on a tool returning a zero exit status.

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

from circuitlab.exporters.clocked import frequency
from circuitlab.exporters.verilog import compile_chips, _signal_list


# Terasic DE10-Lite User Manual, Tables 3-2, 3-4, 3-5, 3-6 and 3-7:
# https://www.mouser.com/datasheet/2/598/DE10-Lite_User_Manual-1100361.pdf
SWITCH_PINS = dict(zip((f"SW{i}" for i in range(10)),
                       ("C10", "C11", "D12", "C12", "A12", "B12", "A13", "A14", "B14", "F15")))
LED_PINS = dict(zip((f"LEDR{i}" for i in range(10)),
                    ("A8", "A9", "A10", "B10", "D13", "C13", "E14", "D14", "A11", "B11")))
# Common-anode displays: segments a..g are bits 0..6, decimal point is bit 7.
# A low output lights a segment; keep this electrical polarity in OOC and HDL.
HEX_PINS = {
    f"HEX{digit}{bit}": pin
    for digit, pins in enumerate((
        ("C14", "E15", "C15", "C16", "E16", "D17", "C17", "D15"),
        ("C18", "D18", "E18", "B16", "A17", "A18", "B17", "A16"),
        ("B20", "A20", "B19", "A21", "B21", "C22", "B22", "A19"),
        ("F21", "E22", "E21", "C19", "C20", "D19", "E17", "D22"),
        ("F18", "E20", "E19", "J18", "H19", "F19", "F20", "F17"),
        ("J20", "K20", "L18", "N18", "M20", "N19", "N20", "L19"),
    ))
    for bit, pin in enumerate(pins)
}
PORTS = {"MAX10_CLK1_50": "input", **dict.fromkeys(SWITCH_PINS, "input"),
         **dict.fromkeys(LED_PINS, "output"), **dict.fromkeys(HEX_PINS, "output"), "AUDIO_OUT": "output"}
PINS = {"MAX10_CLK1_50": "P11", **SWITCH_PINS, **LED_PINS, **HEX_PINS, "AUDIO_OUT": "V10"}
DEVICE = "10M50DAF484C7G"
TOP = "HYBRID01"
QSF_HEADER = '''set_global_assignment -name FAMILY "MAX 10"
set_global_assignment -name DEVICE 10M50DAF484C7G
set_global_assignment -name TOP_LEVEL_ENTITY HYBRID01
set_global_assignment -name VERILOG_FILE build/HYBRID01.v
set_global_assignment -name SDC_FILE HYBRID01.sdc
set_global_assignment -name PROJECT_OUTPUT_DIRECTORY build/output_files
set_global_assignment -name NUM_PARALLEL_PROCESSORS 2
set_global_assignment -name RESERVE_ALL_UNUSED_PINS "AS INPUT TRI-STATED"
'''


def _constraints(ports, synchronizers):
    """Constrain each declared board pin and each independent switch crossing."""
    qsf = QSF_HEADER.splitlines()
    qsf.extend(f"set_location_assignment PIN_{PINS[pin]} -to {pin}" for pin in ports)
    qsf.extend(f'set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to {pin}' for pin in ports)
    if "AUDIO_OUT" in ports:
        qsf.append('set_instance_assignment -name CURRENT_STRENGTH_NEW "4MA" -to AUDIO_OUT')
    qsf.extend(f'set_instance_assignment -name CURRENT_STRENGTH_NEW "8MA" -to {pin}'
               for pin in ports if pin in LED_PINS)
    outputs = " ".join(pin for pin, direction in ports.items() if direction == "output")
    if "MAX10_CLK1_50" not in ports:
        inputs = " ".join(pin for pin, direction in ports.items() if direction == "input")
        # An absolute propagation-delay budget, not a clock or sampling rate.
        paths = f"-from [get_ports {{{inputs}}}] -to [get_ports {{{outputs}}}]"
        sdc = ["# Clockless switch logic: 20 ns maximum pin-to-pin delay; no clock is created.",
               f"set_max_delay 20.000 {paths}", f"set_min_delay 0.000 {paths}"]
        return "\n".join(qsf) + "\n", "\n".join(sdc) + "\n"
    registers = [register for pair in synchronizers.values() for register in pair]
    for setting in ('PRESERVE_REGISTER ON', 'SYNCHRONIZER_IDENTIFICATION "FORCED IF ASYNCHRONOUS"'):
        qsf.extend(f"set_instance_assignment -name {setting} -to {register}" for register in registers)
    qsf.append("set_global_assignment -name OPTIMIZE_FOR_METASTABILITY ON")
    sdc = ["create_clock -name MAX10_CLK1_50 -period 20.000 [get_ports {MAX10_CLK1_50}]",
           "derive_clock_uncertainty"]
    for pin, (first, _) in synchronizers.items():
        sdc.extend([
            f"set first_stage [get_registers {{{first}}}]",
            "if {[get_collection_size $first_stage] != 1} {",
            f'    error "HYBRID01 requires exactly one {first} synchronizer register"',
            "}",
            f"set_false_path -from [get_ports {{{pin}}}] -to $first_stage",
        ])
    sdc.append(f"set_false_path -to [get_ports {{{outputs}}}]")
    return "\n".join(qsf) + "\n", "\n".join(sdc) + "\n"


# Keep the original oscillator's explicit synchronizer project compatible.
QSF, SDC = _constraints({"MAX10_CLK1_50": "input", "SW0": "input", "LEDR0": "output", "AUDIO_OUT": "output"},
                        {"SW0": ("sw_meta", "sw_sync")})
INPUTS = ("build/HYBRID01.v", "HYBRID01.qsf", "HYBRID01.sdc")
UNCONSTRAINED = ("Illegal Clocks", "Unconstrained Clocks", "Unconstrained Input Ports",
                 "Unconstrained Input Port Paths", "Unconstrained Output Ports", "Unconstrained Output Port Paths")
ERROR = re.compile(r"(?im)^\s*(?:error|fatal(?: error)?|critical warning)\b")
CABLE = re.compile(r"(?m)^\s*\d+\)\s+(.+?)\s*$")


class FpgaError(ValueError):
    """A build or hardware result is not suitable for this board profile."""


def source_digest(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def canonical_constraints(text):
    """Ignore comments and only the two harmless fields Quartus appends."""
    ignored = re.compile(r'set_global_assignment -name (?:LAST_QUARTUS_VERSION "[^"\n]+"|'
                         r'RESERVE_ALL_UNUSED_PINS_NO_OUTPUT_GND "AS INPUT TRI-STATED")')
    return "\n".join(line for line in map(str.strip, text.splitlines())
                     if line and not line.startswith("#") and not ignored.fullmatch(line))


def _input_hashes(directory):
    result = {}
    for name in INPUTS:
        text = (directory / name).read_text(encoding="utf-8-sig")
        result[name] = source_digest(canonical_constraints(text) if name.endswith((".qsf", ".sdc")) else text)
    return result


@dataclass(frozen=True)
class FpgaProject:
    directory: Path
    source_sha256: str

    @property
    def path(self):
        return self.directory

    def matches_source(self, source):
        return self.source_sha256 == source_digest(source)


# Add board-specific switch conditioning only for the supported board profile; ordinary
# Verilog export stays board-neutral and must not acquire hidden synchronizers.
def compile_de10_lite(source):
    """Apply board input conditioning without changing board-neutral HDL export.

    Declaring MAX10_CLK1_50 adds two stages to each physical switch. Omitting
    it selects direct, clockless combinational logic instead.
    The original explicit SW0-only synchronizer remains supported.
    """
    modules = compile_chips(source)
    if len(modules) != 1 or modules[0][0] != TOP:
        raise FpgaError("The DE10-Lite profile requires one HYBRID01 chip.")
    _, declared_ports, body, hdl = modules[0]
    directions = dict(declared_ports)
    for statement in body:
        if declaration := re.fullmatch(r"(.+?)\s*=\s*(input|output)", statement, re.I):
            if any(width is not None for width in _signal_list(declaration[1], 1).values()):
                raise FpgaError("DE10-Lite physical ports must be scalar pins. Keep buses inside chips and connect individual bits to board outputs.")
    for pin, direction in directions.items():
        if PORTS.get(pin) != direction:
            raise FpgaError(f"Unsupported DE10-Lite port or direction: {pin}. Use MAX10_CLK1_50 and SW0–SW9 "
                            "as inputs; LEDR0–LEDR9, HEX0[7:0]–HEX5[7:0], and optional AUDIO_OUT as outputs.")
    switches = [pin for pin in SWITCH_PINS if pin in directions]
    if not switches or "output" not in directions.values():
        raise FpgaError("Declare at least one SW0–SW9 input and at least one board output. "
                        "Add MAX10_CLK1_50 for clocked logic and automatic switch synchronization.")
    for row in (row for row in body if re.match(r"clock\b", row)):
        match = re.fullmatch(r"clock\s*\(\s*MAX10_CLK1_50\s*,\s*([^()]+)\)\s*\{", row)
        if not match or frequency(match[1], 1) != 50_000_000:
            raise FpgaError("All HYBRID01 clock blocks must use MAX10_CLK1_50 at 50MHz to match the board constraints.")
    hdl += "\n"
    if "MAX10_CLK1_50" not in directions:
        if re.search(r"\b(?:reg|always|initial)\b", hdl):
            raise FpgaError("Clockless DE10-Lite chips must be combinational: use wire for computed signals, "
                            "or declare MAX10_CLK1_50 for stored state and clock blocks.")
        return hdl, *_constraints(directions, {})
    if _manual_synchronizer(body, hdl):
        if switches != ["SW0"]:
            raise FpgaError("For multiple switches, remove the manual sw_meta/sw_sync declarations and assignments "
                            "and read SW0 directly. Each switch will be synchronized automatically.")
        return hdl, *_constraints(directions, {"SW0": ("sw_meta", "sw_sync")})

    synchronizers = {pin: (f"ooc_{pin.lower()}_meta", f"ooc_{pin.lower()}_sync") for pin in switches}
    declarations, updates = [], []
    for pin, (first, second) in synchronizers.items():
        declarations.extend([f"    reg {first} = 1'b0;", f"    reg {second} = 1'b0;"])
        updates.extend([f"        {first} <= {pin};", f"        {second} <= {first};"])

    # Separate module scopes allow user state names to coincide with these
    # compiler-generated names. Only the known module declaration is renamed.
    user_hdl = hdl.replace("module HYBRID01 (", "module ooc_user_HYBRID01 (", 1)
    ports = ",\n".join(f"    {direction} wire {pin}" for pin, direction in declared_ports)
    connections = ",\n".join(
        f"        .{pin}({synchronizers[pin][1] if pin in synchronizers else pin})"
        for pin, _ in declared_ports
    )
    declarations_text = "\n".join(declarations)
    updates_text = "\n".join(updates)
    wrapper = f"""// DE10-Lite: each physical switch is synchronized before entering OOC logic.
module HYBRID01 (
{ports}
);
{declarations_text}
    always @(posedge MAX10_CLK1_50) begin
{updates_text}
    end
    ooc_user_HYBRID01 ooc_logic (
{connections}
    );
endmodule

"""
    qsf, sdc = _constraints(directions, synchronizers)
    return wrapper + user_hdl, qsf, sdc


def _manual_synchronizer(body, hdl):
    """Recognize the previous explicit two-stage form, never declarations alone."""
    internal_hdl = hdl.split(");", 1)[1]
    if not re.search(r"\breg\s+(?:\[[^\]]+\]\s*)?(sw_meta|sw_sync)\s*=", internal_hdl):
        return False
    scopes, writes = [], {"sw_meta": [], "sw_sync": []}
    for index, row in enumerate(body):
        if row.endswith("{"):
            scopes.append(index)
        elif row == "}":
            scopes.pop()
        elif scopes and (match := re.fullmatch(r"(sw_meta|sw_sync)\s*(=|\+=)\s*(.+)", row)):
            writes[match[1]].append((tuple(scopes), match[2], match[3].strip()))
    valid = all(re.search(rf"\breg {name} = 1'b[01];", internal_hdl) for name in writes)
    valid = valid and all(len(assignments) == 1 for assignments in writes.values())
    if valid:
        meta_scope, meta_op, meta_value = writes["sw_meta"][0]
        sync_scope, sync_op, sync_value = writes["sw_sync"][0]
        valid = (len(meta_scope) == 1 and meta_scope == sync_scope
                 and meta_op == sync_op == "=" and meta_value == "SW0" and sync_value == "sw_meta")
    # The asynchronous pin may only reach the first stage, and that stage may
    # only reach the second (declaration, assignment target, assignment source).
    valid = valid and len(re.findall(r"\bSW0\b", internal_hdl)) == 1
    valid = valid and len(re.findall(r"\bsw_meta\b", internal_hdl)) == 3
    if not valid:
        raise FpgaError("The explicit sw_meta/sw_sync synchronizer must have two unconditional stages. "
                        "Remove those declarations/assignments and read SW0 directly to use automatic synchronization.")
    return True


# Write the generated HDL and board constraints together with fingerprints used later
# to reject programming from mismatched source or stale build settings.
def prepare_project(source, build_dir):
    hdl, qsf, sdc = compile_de10_lite(source)
    root = Path(build_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="hybrid01-", dir=root))
    (directory / "build").mkdir()
    (directory / "build/HYBRID01.v").write_text(hdl, encoding="utf-8")
    (directory / "HYBRID01.qpf").write_text('PROJECT_REVISION = "HYBRID01"\n', encoding="utf-8")
    (directory / "HYBRID01.qsf").write_text(qsf, encoding="utf-8")
    (directory / "HYBRID01.sdc").write_text(sdc, encoding="utf-8")
    project = FpgaProject(directory, source_digest(source))
    record = {"source_sha256": project.source_sha256, "inputs": _input_hashes(directory),
              "input_mtime_ns": max((directory / name).stat().st_mtime_ns for name in INPUTS)}
    (directory / "ooc-build.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return project


def discover_quartus(path=""):
    candidates = []
    if path:
        explicit = Path(path).expanduser()
        candidates.append(explicit.parent if explicit.is_file() else explicit)
    else:
        found = shutil.which("quartus_sh.exe")
        if found:
            candidates.append(Path(found).parent)
        for root in (Path("C:/altera_lite"), Path("C:/intelFPGA_lite"), Path("C:/intelFPGA")):
            if root.is_dir():
                candidates.extend(sorted(root.glob("*/quartus/bin64"), reverse=True))
    for candidate in candidates:
        if all((candidate / exe).is_file() for exe in ("quartus_sh.exe", "quartus_pgm.exe", "jtagconfig.exe")):
            return candidate.resolve()
    raise FpgaError("Select the Quartus bin64 folder containing quartus_sh.exe, quartus_pgm.exe and jtagconfig.exe.")


def build_command(project, quartus_bin):
    return [str(Path(quartus_bin) / "quartus_sh.exe"), "--flow", "compile", TOP]


def cable_list_command(quartus_bin):
    return [str(Path(quartus_bin) / "quartus_pgm.exe"), "-l"]


def chain_command(quartus_bin):
    return [str(Path(quartus_bin) / "jtagconfig.exe"), "--enum"]


def _hardware_errors(output):
    if ERROR.search(output) or re.search(r"(?i)(unable to|cannot|can't) (?:scan|access|connect|open)|no jtag hardware|operation(?:\(s\))? failed", output):
        raise FpgaError("Quartus could not access JTAG hardware. Check the USB cable, board power and USB-Blaster driver.\n" + output.strip())


def parse_cables(output):
    _hardware_errors(output)
    cables = list(dict.fromkeys(match[1] for match in CABLE.finditer(output)))
    if not cables:
        raise FpgaError("No USB-Blaster cables found. Connect the powered board and check its driver.")
    return cables


def verify_chain(output, cable):
    _hardware_errors(output)
    matches = list(CABLE.finditer(output))
    for index, match in enumerate(matches):
        if match[1] != cable:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(output)
        rows = output[match.end():end]
        devices = re.findall(r"(?im)^\s*([0-9a-f]{8})\s+(.+?)\s*$", rows)
        if "!" in rows or len(devices) != 1 or devices[0][0].upper() != "031050DD" or "10M50DA" not in devices[0][1].upper():
            raise FpgaError("Select a chain containing exactly one 10M50DA device (JTAG ID 031050DD).")
        return
    raise FpgaError("The selected USB-Blaster is no longer present. Refresh the cable list.")


def programmer_success(output):
    _hardware_errors(output)
    if not re.search(r"Info\s*\(209011\)\s*:\s*Successfully performed operation\(s\)", output, re.I):
        raise FpgaError("Quartus did not confirm that the FPGA was programmed.\n" + output.strip())
    return True


# Check expected artifacts and source/settings fingerprints in addition to build success.
# A stale image from an earlier compilation is not a verified result of this request.
def verify_build(project):
    directory = project.directory
    try:
        record = json.loads((directory / "ooc-build.json").read_text(encoding="utf-8"))
        if record["source_sha256"] != project.source_sha256 or record["inputs"] != _input_hashes(directory):
            raise FpgaError("The generated project changed; build again before programming.")
        out = directory / "build/output_files"
        names = ("HYBRID01.sof", "HYBRID01.sta.summary", "HYBRID01.sta.rpt", "HYBRID01.flow.rpt")
        artifacts = [out / name for name in names]
        if any(not p.is_file() or not p.stat().st_size or p.stat().st_mtime_ns < record["input_mtime_ns"] for p in artifacts):
            raise FpgaError("A fresh, complete Quartus build is required before programming.")
        summary, timing, flow = (p.read_text(encoding="utf-8", errors="replace") for p in artifacts[1:])
        if not re.search(r";\s*Flow Status\s*;\s*Successful\b", flow) or not re.search(rf";\s*Device\s*;\s*{DEVICE}\b", flow):
            raise FpgaError("Quartus did not report a successful build for the DE10-Lite device.")
        if ERROR.search(timing + "\n" + flow):
            raise FpgaError("Resolve Quartus errors or critical warnings before programming.")
        clocked = "create_clock " in canonical_constraints((directory / "HYBRID01.sdc").read_text())
        required = (" Setup ", " Hold ", " Minimum Pulse Width ") if clocked else (" Setup ", " Hold ")
        sections = re.findall(r"(?m)^Type\s*:\s*(.+)\nSlack\s*:\s*(\S+)", summary)
        if len(sections) != len(re.findall(r"(?m)^Type\s*:", summary)) or not all(
                any(word in kind for kind, _ in sections) for word in required):
            raise FpgaError("Quartus timing results are incomplete.")
        slacks = (Decimal(value) for _, value in sections)
        if any(not slack.is_finite() or slack < 0 for slack in slacks):
            raise FpgaError("Timing failed. Resolve negative setup/hold/pulse-width slack before programming.")
        for label in UNCONSTRAINED:
            matches = re.findall(rf";\s*{re.escape(label)}\s*;\s*(\d+)\s*;\s*(\d+)\s*;", timing)
            if not matches or any(int(value) for match in matches for value in match):
                raise FpgaError("The design has missing or unconstrained timing paths. Review the timing report.")
        fingerprints = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in artifacts}
        verified = directory / "ooc-verified.json"
        if verified.exists() and json.loads(verified.read_text()) != fingerprints:
            raise FpgaError("Verified programming files changed; create a new build before programming.")
        verified.write_text(json.dumps(fingerprints, indent=2), encoding="utf-8")
        return artifacts[0]
    except (OSError, KeyError, TypeError, json.JSONDecodeError, InvalidOperation) as error:
        raise FpgaError("Cannot verify the Quartus build; rebuild before programming.") from error


# Construct the programmer invocation only after checking the selected cable, chain, and
# verified image; command preparation itself does not access the device.
def program_command(project, quartus_bin, cable, *, source):
    if not project.matches_source(source):
        raise FpgaError("The OOC source changed after this build. Build again before programming.")
    if not cable or any(char in cable for char in "\r\n\0"):
        raise FpgaError("Select a USB-Blaster cable first.")
    sof = verify_build(project)
    if ";" in str(sof):
        raise FpgaError("Quartus programming paths cannot contain a semicolon. Choose another build directory.")
    return [str(Path(quartus_bin) / "quartus_pgm.exe"), "-m", "JTAG", "-c", cable, "-o", f"P;{sof}@1"]
