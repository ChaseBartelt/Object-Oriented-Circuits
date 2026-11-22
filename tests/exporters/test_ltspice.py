"""Check that LTspice's symbol pins connect to the original circuit nets."""
# Read the exported symbol/wire connectivity independently of its visual appearance.
# Every physical terminal must retain its original net through rotation and hierarchy export.

import unittest
from pathlib import Path

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.geometry import wire_axis
from circuitlab.exporters.ltspice import export_hierarchy, export_schematic
from circuitlab.math.numbers import scalar
from circuitlab.layout.persistence import read_placement, restore_layout
from circuitlab.math.report import component_labels
from circuitlab.layout.routing import fast_layout, schematic_layout
from circuitlab.layout.editing import move_symbol
from circuitlab.exporters.spice import spice_circuit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
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
    def test_powered_opamp_keeps_both_supply_nets_on_rotated_ltspice_pins(self):
        circuit = compile_circuit(
            'V1(2) between(input,0);\nV2(5) between(vcc,0);\nV3(-5) between(vee,0);\n'
            'R1(10k) between(input,minus);\nR2(10k) between(out,minus);\n'
            'op1(0,minus,out,vcc,vee);'
        )
        original = schematic_layout(circuit, component_labels(circuit), 'auto')
        for turns in range(4):
            with self.subTest(turns=turns):
                drawing = move_symbol(circuit, original, 'op1', (2, 2), rotate=turns)
                self.assertIsNotNone(drawing)
                sheet, _ = export_schematic(circuit, drawing)
                pins, attributes = read_connections(sheet)['X_op1']
                self.assertEqual(pins, ('0', 'minus', 'vcc', 'vee', 'out'))
                self.assertEqual(attributes['Value'], 'EC_IdealOpAmpPowered')
                self.assertIn('.subckt EC_IdealOpAmpPowered plus minus vplus vminus out', sheet)
                self.assertIn('Eoutput out 0 out mid 1', sheet)

    def test_mechanical_switch_export_preserves_state_and_rotated_contact_nets(self):
        for state in ('open', 'closed'):
            circuit = compile_circuit(
                'V1(5) between(input,0);\n'
                f'S1({state}) between(input,out);\nR1(1k) between(out,0);'
            )
            original = schematic_layout(circuit, component_labels(circuit), 'auto')
            for turns in range(4):
                with self.subTest(state=state, turns=turns):
                    drawing = move_symbol(circuit, original, 'S1', (2, 2), rotate=turns)
                    self.assertIsNotNone(drawing)
                    sheet, _ = export_schematic(circuit, drawing)
                    actual = read_connections(sheet)
                    pins, attributes = actual['R_S1']
                    self.assertEqual(pins, ('input', 'out'))
                    self.assertEqual(scalar(attributes['Value'], 'ohm'),
                                     .001 if state == 'closed' else 1e12)
                    self.assertIn(f"Mechanical switch 'S1' is {state}", sheet)
                    self.assertNotIn('SYMBOL sw ', sheet)

    def test_saved_555_layout_keeps_wires_and_symbol_rotations(self):
        path = FIXTURES / "refactor" / "timer555.ckt"
        circuit = compile_circuit(path.read_text(encoding="utf-8-sig"))
        top = next(hood_levels(circuit, set()))[1]
        mode, records = read_placement(path)
        layout = restore_layout(records[None, mode], top, ())
        self.assertTrue(layout.rotations)

        sheet, files, used_labels = export_hierarchy(circuit, layout)
        self.assertFalse(used_labels)
        self.assertIn("\nWIRE ", sheet)
        self.assertRegex(sheet, r"SYMBOL voltage \S+ \S+ R90")
        self.assertRegex(sheet, r"SYMBOL cap \S+ \S+ R0")
        offsets = {
            name[:-4]: tuple(tuple(map(int, row.split()[1:3])) for row in text.splitlines()
                             if row.startswith("PIN "))
            for name, text in files.items() if name.endswith(".asy")
        }
        actual = read_connections(sheet, offsets)
        self.assertEqual(actual["VCC"][0], ("supply", "0"))
        self.assertEqual(actual["VReset"][0], ("reset_n", "0"))
        self.assertEqual(actual["RA"][0], ("supply", "discharge"))
        self.assertEqual(actual["RB"][0], ("discharge", "timing"))
        self.assertEqual(actual["CT"][0], ("timing", "0"))
        self.assertEqual(actual["RA"][1]["Value"], "10k")
        self.assertEqual(actual["CT"][1]["Value"], "100n")
        self.assertEqual(actual["X_timer555"][0],
                         ("0", "timing", "out", "reset_n", "control", "timing", "discharge", "supply"))

    def test_diagonal_leads_remain_wired_after_export(self):
        circuit = compile_circuit("V1(5) between(in, 0);\nR1(1k) between(in, out);\nR2(1k) between(out, 0);")
        layout = schematic_layout(circuit, component_labels(circuit), "auto")
        moved = move_symbol(circuit, layout, "R1", (1, 2), diagonal=True)
        self.assertTrue(any(wire_axis(edge) is None for edges in moved.wires.values() for edge in edges))

        text, used_labels = export_schematic(circuit, moved)
        self.assertFalse(used_labels)
        wires = [tuple(map(int, row.split()[1:])) for row in text.splitlines() if row.startswith("WIRE ")]
        self.assertTrue(any(x != u and y != v for x, y, u, v in wires))
        self.assertEqual(read_connections(text)["R1"][0], ("in", "out"))

    def test_four_sided_package_keeps_native_symbol_pins_on_their_nets(self):
        pins = tuple(f"pin{i}" for i in range(1, 41))
        nets = tuple(f"net{i}" for i in range(1, 41))
        inside = "\n".join(f"R{i}(1k) between({pins[i]}, {pins[i + 1]});"
                           for i in range(0, 40, 2))
        loaded = (0, 10, 20, 30)
        loads = "\n".join(f"Rload{i}(1k) between({nets[i]}, 0);" for i in loaded)
        circuit = compile_circuit(
            f"chip Wide({', '.join(pins)}) {{\n{inside}\n}}\n"
            f"main {{\nWide({', '.join(nets)});\n{loads}\n}}",
            render_only=True,
        )
        top = next(hood_levels(circuit, set()))[1]
        layout = fast_layout(top, component_labels(top))

        sheet, files, used_labels = export_hierarchy(circuit, layout)
        symbol, = (name for name in files if name.endswith(".asy"))
        contacts = [row.split()[1:] for row in files[symbol].splitlines() if row.startswith("PIN ")]
        self.assertEqual([row[2] for row in contacts],
                         ["LEFT"] * 10 + ["BOTTOM"] * 10 + ["RIGHT"] * 10 + ["TOP"] * 10)
        self.assertFalse(used_labels)
        offsets = {symbol.removesuffix(".asy"): [tuple(map(int, row[:2])) for row in contacts]}
        actual = read_connections(sheet, offsets)
        self.assertEqual(actual["X_Wide"][0], nets)
        for i in loaded:
            self.assertEqual(actual[f"Rload{i}"][0], (nets[i], "0"))

    def test_555_exports_as_a_native_hierarchy_with_eight_connected_pins(self):
        circuit = compile_circuit((FIXTURES / "refactor" / "timer555.ckt").read_text(encoding="utf-8-sig"))
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
            if expected.kind in {"R", "C", "L"}:
                unit = {"R": "ohm", "C": "F", "L": "H"}[expected.kind]
                self.assertEqual(scalar(attributes["Value"], unit), float(expected.value))
            else:
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
            FIXTURES / "rc_lowpass.ckt",
            FIXTURES / "three_way_crossover.ckt",
            FIXTURES / "three_band_mixer.ckt",
        ):
            with self.subTest(example=example):
                circuit = compile_circuit(
                    example.read_text(encoding="utf-8-sig")
                )
                text, _ = self.check_export(circuit, "auto")
                self.assertIn("!.ac list ", text)
                if any(p.kind == "O" for p in circuit.components):
                    self.assertIn(
                        r"\n.subckt EC_IdealOpAmp plus minus unused return out", text
                    )
