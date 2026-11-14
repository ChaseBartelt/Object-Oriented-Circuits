"""Check that LTspice's symbol pins connect to the original circuit nets."""

import unittest
from pathlib import Path

from circuitlab.compiler import compile_circuit
from circuitlab.ltspice import export_schematic
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
    r"OpAmps\opamp2": ((-32, 80), (-32, 48), (0, 32), (0, 96), (32, 64)),
}


def read_connections(text):
    """Resolve wires by physical endpoint and flags by net name."""
    adjacent, flags, symbols = {}, {}, []
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
            for u, v in PIN_OFFSETS[symbol]:
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
