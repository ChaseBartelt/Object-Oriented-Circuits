"""Shared .ckt grammar, source rows, and automatic analysis mode detection."""
# These recognizers are shared by compilation and tolerant editor inspection.
# Statement splitting must ignore delimiters inside strings, calls, and interpolation while
# retaining original line numbers for errors and schematic source edits.

import re
from dataclasses import dataclass, field

from circuitlab.model.graph import CircuitError
from circuitlab.math.numbers import NUMBER, real, scalar

# Shared quantity-key groups used by source validation and export. These
# are model keys, so their spelling follows the quantity catalog rather
# than the case-insensitive spelling of structural language keywords.
FREQUENCY = {"f", "T", "w"}
AC_VOLTAGE = {"V", "Vmag", "Vpk", "Vpp"}
# Component prefixes select the unit passed to the restricted SI parser.
# P here is potentiometer total resistance; a power quantity named P
# uses the separate quantity catalog and does not use this component map.
UNITS = dict(R="ohm", C="F", L="H", X="ohm", Z="ohm", P="ohm", V="V", I="A")
# Reusable named section header: capture kind, name, and optional formal
# parameter text. A missing parameter list differs from an explicit empty
# one; method elaboration decides the default terminals and validates pins.
SECTION = re.compile(
    r"(series|parallel|circuit|gate|chip)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(([^()]*)\)\s*)?\{",
    re.I,
)
# Untyped reusable two-terminal sections use a,b when no list is supplied.
# Circuit/gate/chip declarations are net methods whose bodies flatten into
# ordinary statements instead of adding a series/parallel group wrapper.
DEFAULT_TERMINALS = ("a", "b")
NET_METHODS = {"circuit", "gate", "chip"}
# Recognize declaration annotations and pinout-only model markers here;
# the elaborator enforces their placement and meaning. Neither pattern
# by itself validates the surrounding declaration.
OVERRIDE = re.compile(r"@override", re.I)
UNAVAILABLE_MODEL = re.compile(r"model\s*=\s*unavailable", re.I)
# Anonymous structural groups optionally name their endpoints. Braced
# battery and transistor declarations instead require a device name;
# their field/value validation happens when the builder reads the body.
INLINE = re.compile(r"(series|parallel)\s*(?:\(([^()]*)\)\s*)?\{", re.I)
BATTERY_BRACE = re.compile(r"battery\s+([A-Za-z][A-Za-z0-9_]*)\s*\{", re.I)
TRANSISTOR = re.compile(r"transistor\s+([A-Za-z][A-Za-z0-9_]*)\s*\{", re.I)
# Declarations configure an op-number device; connection calls keep their old form.
OPAMP_BRACE = re.compile(r"(?:opamp|analog(?:\s*:\s*math)?)\s+(\([^()]*\)|[A-Za-z_][A-Za-z0-9_]*)\s*\{", re.I)
OPAMP_DECLARATION = re.compile(r"opamp\s+([A-Za-z_][A-Za-z0-9_]*)\s*;?", re.I)
MATH_BRACE = re.compile(r"(analog|digital)\s*(?::\s*math)?\s*\{", re.I)
MATH_INLINE = re.compile(r"(analog|digital)(?:\s*:\s*math)?\s+(.+)", re.I)
# Device-call name recognizers, such as transQ1(...) and switch1(...),
# let the method expander leave these calls for electrical construction.
# The separate positional transistor form captures its name and pin text.
TRANS_CALL = re.compile(r"trans([A-Za-z0-9_]+)", re.I)
SWITCH_CALL = re.compile(r"switch([A-Za-z0-9_]+)", re.I)
TRANS_POSITIONAL = re.compile(r"transistor\s+([A-Za-z][A-Za-z0-9_]*)\s*\(([^()]*)\)", re.I)
# CHIP recognizes a complete chip header. CHIP_START is intentionally
# more tolerant so incomplete or malformed chip-looking input can still
# reach digital diagnostics rather than being treated as an analog circuit.
CHIP = re.compile(r"chip\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^()]*)\)\s*\{", re.I)
CHIP_START = re.compile(
    r"chip[ \t]+(?:[^ \t({]+[ \t]*(?=[({])|[A-Za-z_][A-Za-z0-9_]*$)", re.I
)
# Recognize static-control openers when deciding whether a physical line
# needs brace-aware splitting. The control-flow parser validates the
# condition, loop clauses, and permitted statement placement later.
CONTROL_START = re.compile(r"(?:for|while|do|if|else)\b.*\{", re.I)
# Match interpolation and double-quoted text as whole tokens before looking
# for punctuation, so their internal braces/semicolons are not delimiters.
# An unfinished interpolation consumes the rest of its line for later
# validation; this tokenizer does not evaluate interpolated expressions.
BLOCK_TOKEN = re.compile(r'\$\{[^}]*(?:\}|$)|"[^"]*"|[{}();]')
# A do/while tail needs its final semicolon preserved when splitting.
# Most other statement terminators are discarded by the block splitter.
WHILE_TAIL = re.compile(r"while\s*\(.*\)", re.I)
# Keep named transistor-field order consistent with the positional device
# form. Restrict polarity and model-identifier characters independently
# of the electrical model parameters checked by the builder/exporter.
BJT_PINS = ("base", "collector", "emitter")
BJT_TYPES = {"npn", "pnp"}
BJT_MODEL_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
# Support the older (battery name) section spelling as well as braces.
# Vrms and rad/s assignments are recognizable AC source aliases, with
# optional spacing around the slash in angular-frequency notation.
BATTERY_MARKER = re.compile(r"\(\s*battery(?:\s+([^()=]+))?\s*\)", re.I)
BATTERY_ALIAS = re.compile(r"^\s*(Vrms|rad\s*/\s*s)\s*=", re.I)
# Electrical main allows main{...} or main(){...}. Digital assembly may
# also name its generated top module, so DIGITAL_MAIN accepts that label.
MAIN = re.compile(r"main\s*(?:\(\s*\))?\s*\{", re.I)
DIGITAL_MAIN = re.compile(r"main(?:\s+([A-Za-z_][A-Za-z0-9_]*))?\s*(?:\(\s*\))?\s*\{", re.I)
# Capture a call name and its argument text, allowing one nested level
# of parentheses for grouped pins or component arguments. This is a
# statement-shape recognizer, not a general expression parser.
CALL = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\s*\(((?:[^()]|\([^()]*\))*)\)")
# Top-level cross-file declarations: connector(...) contains pin text;
# include("...") contains a quoted path without embedded quotes/newlines.
# Each permits an optional semicolon; linkage validates names and paths.
CONNECTOR = re.compile(r"connector\s*\(([^()]*)\)\s*;?", re.I)
INCLUDE = re.compile(r'include\s*\(\s*"([^"\r\n]+)"\s*\)\s*;?', re.I)
# Reserve op followed by digits for ideal-amplifier calls. MARKER below
# recognizes legacy parenthesized group/source/amplifier start/end tokens
# so they can be separated from adjacent assignments on the same line.
OP = re.compile(r"op[0-9]+", re.I)
MARKER = re.compile(
    r"(\(\s*(?:end\s+)?(?:parallel|series|battery(?:\s+[^()=]+)?|opamp(?:\s+[A-Za-z][A-Za-z0-9_]*)?)\s*\))",
    re.I,
)
# Electrical node names are broader than method identifiers: node 0,
# dots, internal spaces, and hyphens are permitted. parse_wires() below
# normalizes horizontal whitespace before checking the whole name.
WIRE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_. \t-]*")
# Capture an assignment's key and remaining value text without interpreting
# that value. PIN_DIRECTION similarly recognizes digital input/output
# declaration shapes; signal widths, ranges, and names are validated later.
ASSIGNMENT = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\s*=\s*(.*)")
PIN_DIRECTION = re.compile(r"[\w, \t\[\]:]+\s*=\s*(input|output)", re.I)
# A typed method can bind a formal to a fixed component target such as R1
# or S1. A part argument instead supplies a caller-chosen component such
# as R(1k), P(10k,25%), or S(closed); each accepted prefix is explicit.
PARAMETER_TARGET = re.compile(r"[RCLXZVIS][A-Za-z0-9_]*", re.I)
PART_ARGUMENT = re.compile(r"([RCLZPS][A-Za-z0-9_]*)(?:\s*\(([^()]*)\))?", re.I)
# The bare placement form also recognizes a formal component slot before
# between(...). PLACEMENT additionally captures a parenthesized value,
# including one nested call such as a pulse source. Terminal count and
# component-specific values are handled by downstream parsers.
PART_PLACEMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s+between\s*\(([^()]*)\)", re.I)
PLACEMENT = re.compile(
    r"([A-Za-z][A-Za-z0-9_]*)\s*\(((?:[^()]|\([^()]*\))*)\)\s+between\s*\(([^()]*)\)", re.I
)
# Capture pulse field text without assigning timing semantics here.
# SOURCE_WIRE captures source name, value, and wire text for the shorthand
# V1 = value : wire = node (and its current-source counterpart).
PULSE = re.compile(r"pulse\s*\(([^()]*)\)", re.I)
SOURCE_WIRE = re.compile(
    r"([VI][A-Za-z0-9_]*)\s*=\s*([^:;]+)\s*:\s*wire\s*=\s*([^;]*)\s*;?", re.I
)
# Standalone cosine syntax captures peak amplitude, angular frequency,
# optional signed phase, and optional unit text. Multiplication signs
# may be omitted; the builder interprets phase in degrees and records
# the corresponding peak/frequency/phase quantities.
WAVE = re.compile(
    rf"([+-]?{NUMBER})\s*\*?\s*cos\s*\(\s*({NUMBER})\s*\*?\s*t\s*"
    rf"((?:[+-]\s*{NUMBER}\s*(?:°|deg)?)?)\s*\)\s*"
    r"([A-Za-zµμ]+|\[\s*[A-Za-zµμ]+\s*\])?"
)
# Detect RMS/peak/peak-to-peak voltage or current unit endings for mode
# inference. The numeric/whitespace boundary reduces accidental matches
# inside identifiers; this pattern does not itself parse a source value.
AC_SOURCE_SUFFIX = re.compile(r"(?:\d\s*|\s)[VA](?:rms|pk|pp)\s*$", re.I)


# A binding accompanies source rows through expansion. Frozen attributes
# make replacement explicit when changing scope/origin; dictionary fields
# have independent defaults but are not themselves made immutable.
@dataclass(frozen=True)
class Binding:
    """Map method parameters to caller nets and qualify local names per call."""

    # scope qualifies invocation-local names. mapping connects formal wires
    # to resolved caller nets; values carries typed argument source text.
    # in_main records implicit-chain context, instance links hierarchy/owners,
    # and path carries the source-file origin used by diagnostics/navigation.
    scope: str = ""
    mapping: dict[str, str] = field(default_factory=dict)
    in_main: bool = False
    values: dict[str, str] = field(default_factory=dict)
    instance: int | None = None
    path: object = None
    model_patch: bool = False

    # Ground node 0 stays global; other names use explicit formal bindings or instance-local scope.
    def wire(self, wire):
        # At document scope preserve the given node name. Inside a method, node 0
        # still stays global; explicit formals use the caller mapping, while an
        # undeclared local name becomes scope.name to isolate repeated calls.
        if not self.scope or wire == "0":
            return wire
        return self.mapping.get(wire, f"{self.scope}.{wire}")

    # Components use name_scope rather than the scope.name wire convention.
    # Part identities are qualified without consulting electrical pin mappings.
    def part(self, name):
        return f"{name}_{self.scope}" if self.scope else name


# Every normalized statement travels with its original one-based line
# and binding. Splitting/expansion can change row count without losing
# the location or invocation context of the source that produced it.
SourceRow = tuple[str, int, Binding]


def analysis_mode(source):
    """Infer the source mode from explicit units, frequency, and DC/AC pairs."""
    # Mode inference uses textual cues, not a solved circuit. Discard line
    # comments and braced transistor fields before looking for AC markers,
    # so device net/model names are less likely to be mistaken for source units.
    source = re.sub(r"#.*", "", source)
    # Transistor fields name nets and models, even if they end in Vpk or Arms.
    # Math operators (especially |) are not mixed-source separators. Model
    # parameters likewise do not choose mode. Count nested properties braces
    # instead of stopping at the first } inside an analog/digital block.
    visible, hidden = [], 0
    for text, _, _ in source_rows(source, legacy_markers=False):
        if hidden:
            hidden += text.count("{") - text.count("}")
        elif any(header.fullmatch(text) for header in (TRANSISTOR, OPAMP_BRACE, MATH_BRACE)):
            hidden = 1
        elif not MATH_INLINE.fullmatch(text):
            visible.append(text)
    source = "\n".join(visible)
    # For inference only, inspect fragments separated by statement/block
    # boundaries. Full statement splitting and syntax validation remain the
    # responsibility of source_rows() and the later compilation passes.
    rows = re.split(r"[;{}\r\n]+", source)
    # A single | denotes combined DC/AC source values and takes priority over
    # ordinary AC cues. Adjacent || is Boolean OR in static control flow, so
    # it must not select mixed analysis by itself.
    if any(re.search(r"(?<!\|)\|(?!\|)", row) for row in rows):
        return "mixed"
    for row in rows:
        # Frequency/phase/peak assignments, AC battery aliases, or explicit AC
        # source-unit suffixes select AC even before device topology is built.
        if (
            re.search(r"^\s*(?:f|T|w|Vpk|Vpp|phaseV|phaseI|Vdc)\s*=", row)
            or BATTERY_ALIAS.match(row)
            or AC_SOURCE_SUFFIX.search(row)
        ):
            return "ac"
        # Both supported value-bearing placement forms expose name and value in
        # the same capture positions. Inspect that value when an AC unit appears
        # inside parentheses rather than at the end of the complete statement.
        placement = PLACEMENT.fullmatch(row.strip()) or SOURCE_WIRE.fullmatch(
            row.strip()
        )
        if (
            placement
            and (placement[1][0].upper() == "Z" or (
                placement[1][0].upper() in "VI" and AC_SOURCE_SUFFIX.search(placement[2])
            ))
        ):
            return "ac"
        # Theoretical Z devices require AC interpretation even when their given
        # impedance is real. Cover legacy assignment/node-list syntax as well as
        # the bare between(...) form whose value may still be unspecified.
        if re.match(r"\s*Z[A-Za-z0-9_]*\s*(?:=|\s+\S+\s+\S+\s+\S)", row, re.I):
            return "ac"
        if (bare := PART_PLACEMENT.fullmatch(row.strip())) and bare[1][0].upper() == "Z":
            return "ac"
    # Without any mixed or AC cue, use the DC interpretation. The selected
    # mode is a default; later validation still checks whether statements
    # and quantities are valid in that analysis.
    return "dc"


# Route behavioral chip/assembly documents to the digital toolchain while
# preserving analog uses of chip as a component, wire, or method name.
# This is document recognition, not complete Verilog-language validation.
def has_chip(source):
    """Recognize chip files with input/output declarations for Verilog export."""
    depth = 0
    # Use normalized rows so comments and several statements on one physical
    # line are handled consistently with the compiler. Only declarations at
    # the outer body level count as digital input/output evidence.
    for row, _, _ in source_rows(source):
        if depth:
            if row == "}":
                depth -= 1
            elif row.endswith("{"):
                depth += 1
            # A chip-looking block with direct input/output declarations is behavioral.
            # Do not use declarations hidden inside deeper nested blocks as evidence.
            elif depth == 1 and PIN_DIRECTION.fullmatch(row):
                return True
        # Start inspecting a complete chip or main body. A valid chip header alone
        # is insufficient because electrical package methods share that keyword.
        elif CHIP.fullmatch(row) or DIGITAL_MAIN.fullmatch(row):
            depth = 1
        # Still recognize an incomplete/malformed chip header for digital errors,
        # while avoiding a complete reusable section header that may be electrical.
        elif CHIP_START.match(row) and not SECTION.fullmatch(row):
            return True
    return False


def _block_rows(line, depth):
    """Recognize delimiters outside calls, interpolation, and quoted paths."""
    # Scan one physical line at a time. start marks the un-emitted fragment;
    # parentheses is local to this line, while brace depth is passed between
    # lines by source_rows(). This pass splits text without validating blocks.
    rows = []
    start = parentheses = 0
    # Quoted strings/interpolations arrive as whole tokens and do not enter
    # the punctuation cases. Only standalone delimiter tokens change nesting
    # or create a statement boundary.
    for match in BLOCK_TOKEN.finditer(line):
        token = match[0]
        # Track parentheses so call arguments and for-loop header semicolons stay
        # inside their containing statement. Clamp unmatched closing parentheses
        # to zero here; later parsers are responsible for rejecting invalid syntax.
        if token == "(":
            parentheses += 1
        elif token == ")":
            parentheses = max(parentheses - 1, 0)
        # Outside parentheses, semicolons terminate rows and braces delimit blocks.
        # Slice the original text rather than reconstructing expressions so value
        # spelling, whitespace inside fragments, and quoted paths are preserved.
        elif not parentheses and token in {";", "{", "}"}:
            fragment = line[start:match.start()]
            # Keep an opening brace attached to its header so downstream recognizers
            # can match the whole block opener and then increase structural depth.
            if token == "{":
                rows.append(fragment + token)
                depth += 1
            # Flush any preceding statement before emitting a standalone closing row.
            # Never let unmatched closes produce negative splitter depth; block
            # validation happens after these rows have been produced.
            elif token == "}":
                rows.extend((fragment, token))
                depth = max(depth - 1, 0)
            else:
                # Ordinary terminators are dropped. Preserve while(...); specifically
                # because a do/while closing condition needs to remain distinguishable
                # from the opener of another while block.
                rows.append(fragment + ";" if WHILE_TAIL.fullmatch(fragment.strip()) else fragment)
            # Advance only after a real structural delimiter. Punctuation protected
            # by parentheses, quotes, or interpolation remains inside the next slice.
            start = match.end()
    # Retain the final fragment even without a terminator. Empty fragments are
    # harmless here; source_rows() strips and filters them before yielding.
    rows.append(line[start:])
    return "\n".join(rows), depth


# Split on structural boundaries rather than raw punctuation so calls and quoted paths are
# kept intact. The original line accompanies every normalized statement.
def _logical_lines(source):
    """Join continued pin lists/expressions, retaining the opening line number."""
    pending, opened, parentheses = [], 0, 0
    for number, raw in enumerate(source.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not pending:
            opened = number
        pending.append(line)
        for match in BLOCK_TOKEN.finditer(line):
            if match[0] == "(":
                parentheses += 1
            elif match[0] == ")":
                parentheses = max(0, parentheses - 1)
        # An ordinary statement terminator ends a malformed expression too.
        # Do not swallow its closing chip/main brace and lose the error line.
        # A for header is the one construct with legitimate inner semicolons.
        terminated = line.endswith(";") and not re.match(r"for\s*\(", pending[0], re.I)
        # Analog equations commonly place their RHS on following lines.
        # Join an unfinished assignment/operator too, without consuming a
        # terminated malformed expression and losing its source diagnostic.
        joined = " ".join(pending).rstrip()
        continued = bool(re.search(r"(?:=(?!=)|[+*/%,]|(?<!-)-)\s*$", joined))
        # A numeric percentage is a complete scalar in legacy quantities and
        # property defaults. Its suffix must not consume the next statement;
        # a trailing modulo after a name may still continue an expression.
        if re.search(rf"(?<![\w.]){NUMBER}\s*%$", joined):
            continued = False
        if (not parentheses and not continued) or terminated:
            yield opened, " ".join(pending)
            pending = []
            parentheses = 0
    if pending:
        yield opened, " ".join(pending)


def source_rows(source, *, legacy_markers=True):
    """Split statements while retaining the original line for diagnostics."""
    depth = 0
    # Count physical source lines once, including blank/comment-only lines.
    # All fragments emitted from a physical line retain this same location.
    for number, raw in _logical_lines(source):
        # This normalization treats the first # as the start of a line comment
        # before block tokenization, then removes outer whitespace.
        line = raw.split("#", 1)[0].strip()
        # Enable brace-aware splitting inside an active block or when a known
        # block header begins the line. Leave other top-level legacy rows on
        # their existing path rather than splitting raw punctuation everywhere.
        if depth or re.search(r"(?:^|[;{}])\s*(?:opamp\s+[A-Za-z_][A-Za-z0-9_]*\b|op[0-9]+\b\s*\(|(?:analog|digital)\b(?:\s*:\s*math)?)", line, re.I) or any(
            header.match(line)
            for header in (SECTION, MAIN, DIGITAL_MAIN, INLINE, BATTERY_BRACE, TRANSISTOR,
                           OPAMP_BRACE, OPAMP_DECLARATION, MATH_BRACE, MATH_INLINE, CHIP, CONTROL_START)
        ):
            line, depth = _block_rows(line, depth)
        # Legacy parenthesized blocks allow several assignments on the same line.
        # When enabled, put old parenthesized markers on separate rows. Only
        # marker-bearing lines also use the following whitespace-before-assignment
        # rewrite; callers can disable these legacy transformations entirely.
        if legacy_markers and MARKER.search(line):
            line = MARKER.sub(r"\n\1\n", line)
            # Separate recognized legacy component/measurement/wiring/frequency
            # assignments that share a marker line. Match a known key followed by =
            # instead of treating every space as a statement boundary.
            line = re.sub(
                r"[ \t]+((?:[RCLXZVIS][A-Za-z0-9_]*|measure|wire|ground|f|T|w|phase[VI]|rad\s*/\s*s)[ \t]*=)",
                r"\n\1",
                line,
                flags=re.I,
            )
        # Filter empty fragments and create a fresh neutral binding per statement.
        # Document/method elaboration will later attach paths, formal mappings,
        # and invocation scopes without changing the original line number.
        for item in map(str.strip, line.splitlines()):
            if item:
                yield item, number, Binding()


def parse_wires(raw: str, count: int | None = None) -> tuple[str, ...]:
    """Normalize and validate a comma-separated list of node names."""
    # Only an explicitly requested zero-argument list accepts empty text.
    # Otherwise splitting an empty item produces an invalid wire name rather
    # than silently treating it as a missing argument.
    if count == 0 and not raw.strip():
        return ()
    # Split commas, trim each name, and collapse runs of spaces/tabs to one
    # space. Preserve case and permitted punctuation; names are not Python
    # expressions and are never evaluated here.
    values = tuple(re.sub(r"[ \t]+", " ", value.strip()) for value in raw.split(","))
    # Check exact arity when supplied and validate every whole normalized name.
    # Repeated names are allowed here because two device terminals may be
    # connected to the same node; declaration-specific uniqueness is separate.
    if (count is not None and len(values) != count) or not all(
        WIRE.fullmatch(v) for v in values
    ):
        raise CircuitError(f"Use {count or 'valid'} wire names.")
    return values


# Position is measured from the low terminal; percentages and fractional inputs share the
# same normalized 0-to-1 representation used by the electrical model.
def parse_potentiometer(raw):
    """Parse total resistance and position measured from the low end (default 50%)."""
    # Accept total resistance alone or total resistance plus position. An
    # empty total resistance is a schematic placeholder, retained as None
    # rather than converted to an open circuit or a zero-ohm device.
    fields = raw.split(",")
    if len(fields) not in (1, 2):
        raise CircuitError("Use P1(total resistance, position%) between(high, wiper, low).")
    # The SI parser checks units and finiteness; real() excludes complex
    # resistance. A specified total must be strictly positive.
    value = real(scalar(fields[0], "ohm")) if fields[0].strip() else None
    if value is not None and value <= 0:
        raise CircuitError("A potentiometer needs a positive total resistance.")
    # Position is dimensionless: scalar() converts a trailing percent sign
    # to a fraction, so 25% and 0.25 are equivalent. Default to the midpoint
    # only when the second field is omitted; an explicit empty field is invalid.
    position = real(scalar(fields[1])) if len(fields) == 2 else 0.5
    # Allow exact endpoints as well as interior positions. The electrical
    # model handles their end-stop connections; this helper returns only
    # the validated total resistance and normalized position.
    if not 0 <= position <= 1:
        raise CircuitError("Use a potentiometer position from 0% to 100% (or a fraction from 0 to 1).")
    return value, position


# The native switch model uses finite on/off resistances for its two
# states. These constants are consumed elsewhere; parse_switch() returns
# the state Boolean rather than a resistance or a controlled-switch model.
SWITCH_ON_RESISTANCE = 1e-3
SWITCH_OFF_RESISTANCE = 1e12


# An omitted state is intentionally open, unlike an unspecified passive component value.
def parse_switch(raw):
    """Read a native switch state; omitted states start open."""
    # Ignore surrounding whitespace and letter case, but accept only the
    # explicit state spellings below. There is no general numeric truthiness:
    # only the literal strings 0 and 1 are numeric aliases.
    state = raw.strip().casefold()
    # An omitted state deliberately starts open. This differs from an omitted
    # passive component value, which represents an unspecified parameter.
    if state in {"", "open", "off", "0"}:
        return False
    if state in {"closed", "on", "1"}:
        return True
    raise CircuitError("Use S1(open) or S1(closed) between(a, b); on/off and 1/0 are also accepted.")
