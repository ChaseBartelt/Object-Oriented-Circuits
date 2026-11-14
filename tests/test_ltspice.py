"""Check that LTspice's symbol pins connect to the original circuit nets."""

import unittest
from pathlib import Path

from circuitlab.compiler import compile_circuit
from circuitlab.hood import hood_levels
from circuitlab.ltspice import export_hierarchy, export_schematic
from circuitlab.report import component_labels
from circuitlab.routing import schematic_layout
from circuitlab.spice import spice_circuit

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
PIN_OFFSETS = {
    "res": ((16, 16), (16, 96)),
    "cap": ((16, 0), (16, 64)),
    "ind": ((16, 16), (16, 96)),
    "voltage": ((0, 16), (0, 96)),
    "current": ((0, 0), (0, 80)),
    "npn": ((64, 0), (0, 48), (64, 96)),
    "pnp": ((64, 0), (0, 48), (64, 96)),
    r"OpAmps\opamp2": ((-32, 80), (-32, 48), (0, 32), (0, 96), (32, 64)),
}


def read_connections(text, extra_pins=None):
    """Resolve wires by physical endpoint and flags by net name."""
    adjacent, flags, symbols = {}, {}, []
    offsets = PIN_OFFSETS | (extra_pins or {})
    for row in text.splitlines():
        fields = row.split()
        if fields[0] == "WIRE":
            x, y, u, v = map(int, fields[1:])
            adjacent.setdefault((x, y), set()).add((u, v))
            adjacent.setdefault((u, v), set()).add((x, y))
        elif fields[0] == "FLAG":
            x, y = map(int, fields[1:3])
            flags.setdefault((x, y), set()).add(fields[3])
        elif fields[0] == "SYMBOL":
            _, symbol, x, y, orientation = fields
            pins = []
            for u, v in offsets[symbol]:
                for _ in range(int(orientation[1:]) // 90):
                    u, v = -v, u
                if orientation.startswith("M"):
                    u = -u
                pins.append((int(x) + u, int(y) + v))
            symbols.append((pins, {}))
        elif fields[0] == "SYMATTR":
            _, name, value = row.split(maxsplit=2)
            symbols[-1][1][name] = value

    def net_at(pin):
        reachable = {pin}
        queue = [pin]
        for point in queue:
            unseen = adjacent.get(point, set()) - reachable
            reachable.update(unseen)
            queue.extend(unseen)
        names = set().union(*(flags.get(point, set()) for point in reachable))
        if len(names) != 1:
            raise AssertionError(f"Pin {pin} connects to nets {names!r}")
        return names.pop()

    return {
        attrs["InstName"]: (tuple(map(net_at, pins)), attrs) for pins, attrs in symbols
    }


class LtspiceSchematicTest(unittest.TestCase):
    def test_555_exports_as_a_native_hierarchy_with_eight_connected_pins(self):
        circuit = compile_circuit((EXAMPLES / "timer555.ckt").read_text(encoding="utf-8-sig"))
        top = next(hood_levels(circuit, set()))[1]
        layout = schematic_layout(top, component_labels(top), "auto")
        sheet, files, used_labels = export_hierarchy(circuit, layout)
        self.assertFalse(used_labels)
        self.assertEqual(sheet.count("\nSYMBOL "), 6)
        self.assertIn("\nWIRE ", sheet)
        self.assertIn("!.op", sheet)
        self.assertIn(".model EC_BJT_NPN NPN", sheet)

        offsets, ports, sheets = {}, {}, {}
        for name, content in files.items():
            stem, suffix = name.rsplit(".", 1)
            if suffix == "asy":
                self.assertIn("SymbolType BLOCK", content)
                rows = [row.split() for row in content.splitlines()]
                offsets[stem] = tuple(tuple(map(int, row[1:3])) for row in rows if row[0] == "PIN")
                ports[stem] = tuple(row[2] for row in rows if row[:2] == ["PINATTR", "PinName"])
            else:
                sheets[stem] = content
                self.assertNotIn("!.op", content)
        self.assertEqual(ports.keys(), sheets.keys())
        for stem, content in sheets.items():
            rows = [row.split() for row in content.splitlines()]
            flags = {tuple(row[1:3]): row[3] for row in rows if row[0] == "FLAG"}
            self.assertCountEqual(
                [flags[tuple(row[1:3])] for row in rows if row[0] == "IOPIN"], ports[stem]
            )
        chip = next(stem for stem in ports if stem.startswith("ec_timer555_"))
        actual = read_connections(sheet, offsets)
        self.assertEqual(ports[chip], ("ground", "trig", "out", "reset_n", "ctrl", "thres", "disch", "vcc"))
        self.assertEqual(actual["X_timer555"][0],
                         ("0", "timing", "out", "reset_n", "control", "timing", "discharge", "supply"))
        self.assertIn("SYMBOL Misc\\jumper", sheets[chip])

        # Follow every sheet from the root and check that it contains each
        # original element exactly once, with no missing symbol dependencies.
        spice = spice_circuit(circuit)
        expected = {spice.parts[part.name].name: part for part in circuit.components}
        primitives = []
        parents = {}

        def representative(node):
            while node in parents:
                node = parents[node]
            return node

        def connect(a, b):
            a, b = representative(a), representative(b)
            if a != b:
                parents[b] = a

        def visit(content, scope=(), connections=None):
            actual = read_connections(content, offsets | {r"Misc\jumper": ((-32, 64), (32, 64))})
            for port, parent in (connections or {}).items():
                connect((scope, port), parent)
            pending = []
            for row in content.splitlines():
                key, *fields = row.split()
                if key == "SYMBOL":
                    symbol = fields[0]
                elif key == "SYMATTR" and fields[0] == "InstName":
                    name = fields[1]
                    nodes = tuple((scope, net) for net in actual[name][0])
                    if symbol in sheets:
                        pending.append((sheets[symbol], (*scope, name), dict(zip(ports[symbol], nodes))))
                    elif symbol == r"Misc\jumper":
                        connect(*nodes)
                    else:
                        primitives.append((name, nodes))
            for child in pending:
                visit(*child)

        visit(sheet)
        self.assertCountEqual([name for name, _ in primitives], list(expected))
        compiled_to_export, export_to_compiled = {}, {}
        for name, actual in primitives:
            part = expected[name]
            nets = {
                "Q": (part.p, part.base, part.n),
                "O": (part.in_plus, part.in_minus, part.n, part.n, part.p),
            }.get(part.kind, (part.p, part.n))
            for compiled, emitted in zip(nets, actual):
                emitted = representative(emitted)
                self.assertEqual(compiled_to_export.setdefault(compiled, emitted), emitted)
                self.assertEqual(export_to_compiled.setdefault(emitted, compiled), compiled)

    def check_export(self, circuit, mode):
        layout = schematic_layout(circuit, component_labels(circuit), mode)
        text, used_labels = export_schematic(circuit, layout)
        spice = spice_circuit(circuit)
        actual = read_connections(text)
        self.assertEqual(len(actual), len(circuit.components))
        for part in circuit.components:
            expected = spice.parts[part.name]
            pins, attributes = actual[expected.name]
            nets = (
                (part.in_plus, part.in_minus, part.n, part.n, part.p)
                if part.kind == "O"
                else (part.p, part.n)
            )
            self.assertEqual(pins, tuple(spice.nodes[net] for net in nets))
            self.assertEqual(attributes["Value"], expected.value)
            if expected.kind in "VI" and expected.extra:
                self.assertEqual(attributes["Value2"], expected.extra.strip())
            if expected.kind == "L":
                self.assertEqual(attributes["SpiceLine"], "Rser=0")
        return text, used_labels

    def test_bridge_uses_native_symbols_and_continuous_wires(self):
        circuit = compile_circuit("""
        V1 TOP 0 12
        R1 TOP LEFT 1k
        R2 LEFT 0 2k
        R3 TOP RIGHT 2k
        R4 RIGHT 0 1k
        R5 LEFT RIGHT 1k
        """)
        text, labels = self.check_export(circuit, "auto")
        self.assertFalse(labels)
        self.assertIn("\nWIRE ", text)
        self.assertIn("!.op\n", text)

    def test_current_source_keeps_arrow_polarity_and_wiring_style(self):
        circuit = compile_circuit("I1 0 out 10m\nR1 out 0 2k\nR2 out 0 4k")
        routed, _ = self.check_export(circuit, "auto")
        labeled, _ = self.check_export(circuit, "labeled")
        self.assertNotEqual(routed, labeled)
        self.assertIn("SYMBOL current ", routed)

    def test_ac_and_crowded_circuits_preserve_every_terminal(self):
        for example in (
            "rc_lowpass.ckt",
            "three_way_crossover.ckt",
            "three_band_mixer.ckt",
        ):
            with self.subTest(example=example):
                circuit = compile_circuit(
                    (EXAMPLES / example).read_text(encoding="utf-8-sig")
                )
                text, _ = self.check_export(circuit, "auto")
                self.assertIn("!.ac list ", text)
                if any(p.kind == "O" for p in circuit.components):
                    self.assertIn(
                        r"\n.subckt EC_IdealOpAmp plus minus unused return out", text
                    )
