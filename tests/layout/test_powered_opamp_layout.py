"""Optional op-amp supply contacts stay connected through layout edits."""
# Power contacts above/below the amplifier must remain attached in every wiring style.
# Move, rotate, and save/restore cases guard against a visually present but disconnected rail.

import unittest
from pathlib import Path

from circuitlab.exporters.ltspice import export_hierarchy
from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.editing import move_symbol
from circuitlab.layout.geometry import package_body_corners, rotate_point, symbol_pins
from circuitlab.layout.grid import align_layout
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.layout.routing import fast_layout, schematic_layout
from circuitlab.layout.shorten import wire_length
from circuitlab.math.report import component_labels
from tests.exporters.test_ltspice import read_connections


EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
FOLLOWERS = """
include("LM324");
Vsignal(2V) between(input, 0);
Vpositive(5V) between(pos, 0);
Vnegative(5V) between(0, neg);
main {
    LM324(out1, out1, input, pos, 0, out2, out2, out3, out3, 0, neg, 0, out4, out4);
}
"""
PACKAGE_STAGE = """
include("LT1677");
Vsignal(2V) between(input, 0);
Vpositive(5V) between(pos, 0);
Vnegative(-5V) between(neg, 0);
main {
    Rin(10k) between(input, minus);
    Rfeedback(10k) between(minus, out);
    LT1677(0, minus, 0, neg, nc, out, pos, trim2);
}
"""
SWITCHED_AMPLIFIER = """
battery PosSupply { V = 2; wire = pos; }
battery NegSupply { Vneg = 5; wire = neg; }
main {
    S1(closed) between(pos, switched);
    R2(10k) between(switched, minus);
    R1(10k) between(minus, out);
    op1(0, minus, out, pos, neg);
}
"""


def fresh_package_layout(source=FOLLOWERS, *, mode="auto"):
    full = compile_circuit(source, path=EXAMPLES / "model_overrides.ckt", render_only=True)
    _, circuit, _, _ = next(hood_levels(full, set()))
    drawing = schematic_layout(circuit, component_labels(circuit), mode)
    return full, circuit, align_layout(circuit, drawing)


class PoweredOpampLayoutTest(unittest.TestCase):
    def assert_inline_source(self, drawing, package, name):
        source = next(symbol for symbol in drawing.symbols if symbol[0].name == name)
        part, a, b, _ = source
        turns = drawing.rotations.get(name, 0)
        pins = {net: rotate_point(at, a, b, turns)
                for net, at in symbol_pins(*source, grid=drawing.pin_grid)}
        hot = part.n if part.p == "0" else part.p
        device_pin = dict(symbol_pins(*package, grid=drawing.pin_grid))[hot]
        attached, grounded = pins[hot], pins["0"]
        outward = -1 if device_pin[0] == package[1][0] else 1
        self.assertEqual(attached[1], device_pin[1])
        self.assertEqual(grounded[1], device_pin[1])
        self.assertGreater(outward * (grounded[0] - attached[0]), 0)
        self.assertGreaterEqual(outward * (attached[0] - device_pin[0]), 0)
        self.assertTrue(all(a[1] == b[1] == device_pin[1] for a, b in drawing.wires.get(hot, ())))
        self.assertTrue(any(
            at[1] == device_pin[1] and outward * (at[0] - grounded[0]) >= 0
            and drawing.ground_rotations.get(at) == (1 if outward < 0 else 3)
            for at in drawing.grounds
        ))

    def test_routed_package_sources_and_ground_pins_follow_the_pin_rows(self):
        full, circuit, drawing = fresh_package_layout(mode="routed")
        self.assertIsNotNone(drawing)
        self.assertTrue(preserves_nets(circuit, drawing))
        package = next(symbol for symbol in drawing.symbols if symbol[0].kind == "H")
        package_pins = list(symbol_pins(*package, grid=drawing.pin_grid))
        for name in ("Vsignal", "Vpositive", "Vnegative"):
            with self.subTest(source=name):
                self.assert_inline_source(drawing, package, name)
        for net, (x, y) in package_pins:
            if net != "0":
                continue
            outward = -1 if x == package[1][0] else 1
            self.assertTrue(any(
                at[1] == y and outward * (at[0] - x) >= 0
                and drawing.ground_rotations.get(at) == (1 if outward < 0 else 3)
                for at in drawing.grounds
            ))

        restored = restore_layout(pack_layout(drawing), circuit, ())
        self.assertIsNotNone(restored)
        self.assertEqual(restored.ground_rotations, drawing.ground_rotations)
        self.assertEqual(list(restored.pins()), list(drawing.pins()))
        sheet, files, _ = export_hierarchy(full, restored)
        block_pins = {
            filename.removesuffix(".asy"): tuple(
                tuple(map(int, row.split()[1:3])) for row in text.splitlines() if row.startswith("PIN ")
            )
            for filename, text in files.items() if filename.endswith(".asy")
        }
        connections = read_connections(sheet, block_pins)
        self.assertEqual(connections["Vsignal"][0], ("input", "0"))
        self.assertEqual(connections["Vpositive"][0], ("pos", "0"))
        self.assertEqual(connections["Vnegative"][0], ("0", "neg"))

    def test_package_feedback_stage_keeps_supplies_above_the_chips(self):
        _, circuit, drawing = fresh_package_layout(PACKAGE_STAGE)
        self.assertIsNotNone(drawing)
        self.assertTrue(preserves_nets(circuit, drawing))
        package = next(symbol for symbol in drawing.symbols if symbol[0].kind == "H")
        top = package_body_corners(*package[:3], drawing.pin_grid)[1][1]
        for name in ("Vnegative", "Vpositive"):
            source = next(symbol for symbol in drawing.symbols if symbol[0].name == name)
            _, a, b, _ = source
            self.assertGreater(min(a[1], b[1]), top)

    def test_shared_supply_stays_on_general_routing(self):
        source = FOLLOWERS.replace("main {", "main {\n    Rload(10k) between(pos, 0);")
        _, circuit, drawing = fresh_package_layout(source)
        self.assertIsNotNone(drawing)
        self.assertTrue(preserves_nets(circuit, drawing))
        source = next(symbol for symbol in drawing.symbols if symbol[0].name == "Vpositive")
        part, a, b, _ = source
        turns = drawing.rotations.get(part.name, 0)
        pins = [rotate_point(at, a, b, turns) for _, at in symbol_pins(*source, grid=drawing.pin_grid)]
        self.assertEqual(pins[0][0], pins[1][0])

    def test_switched_amplifier_keeps_input_chain_feedback_and_ground_local(self):
        circuit = compile_circuit(SWITCHED_AMPLIFIER, render_only=True)
        drawing = schematic_layout(circuit, component_labels(circuit), "auto")
        drawing = align_layout(circuit, drawing)
        self.assertIsNotNone(drawing)
        self.assertTrue(preserves_nets(circuit, drawing))
        self.assertLess(wire_length(drawing), 45)
        symbols = {symbol[0].name: symbol for symbol in drawing.symbols}
        amp = dict(symbol_pins(*symbols["op1"], grid=drawing.pin_grid))
        for name in ("S1", "R2"):
            _, a, b, _ = symbols[name]
            self.assertEqual(a[1], amp["minus"][1])
            self.assertEqual(b[1], amp["minus"][1])
        _, a, b, _ = symbols["R1"]
        self.assertEqual(a[1], b[1])
        self.assertNotEqual(a[1], amp["minus"][1])
        x, y = amp["0"]
        self.assertTrue(any(
            at[1] == y and at[0] < x and drawing.ground_rotations.get(at) == 1
            for at in drawing.grounds
        ))

    def test_optional_supply_pins_leave_the_original_signal_geometry_unchanged(self):
        legacy = compile_circuit('op1(plus,minus,out);', render_only=True).components[0]
        powered = compile_circuit('op1(plus,minus,out,vcc,vee);', render_only=True).components[0]
        original = list(symbol_pins(legacy, (2, 3), (6, 3), '+', grid=.25))
        actual = list(symbol_pins(powered, (2, 3), (6, 3), '+', grid=.25))
        self.assertEqual(actual[:3], original)
        self.assertEqual(actual[3:], [('vcc', (4, 4.25)), ('vee', (4, 1.75))])

    def test_supply_wires_route_in_every_style_and_survive_rotation_and_save(self):
        circuit = compile_circuit(
            'V1(2) between(input,0);\nV2(5) between(vcc,0);\nV3(-5) between(vee,0);\n'
            'R1(10k) between(input,minus);\nR2(10k) between(out,minus);\n'
            'op1(0,minus,out,vcc,vee);'
        )
        for mode in ('auto', 'routed', 'labeled', 'fast'):
            with self.subTest(mode=mode):
                labels = component_labels(circuit)
                drawing = (fast_layout(circuit, labels) if mode == 'fast'
                           else schematic_layout(circuit, labels, mode))
                self.assertTrue(preserves_nets(circuit, drawing))
                amp = next(symbol for symbol in drawing.symbols if symbol[0].name == 'op1')
                self.assertEqual([net for net, _ in symbol_pins(*amp)][-2:], ['vcc', 'vee'])
                self.assertIn('vcc', drawing.wires)
                self.assertIn('vee', drawing.wires)

        single = compile_circuit('op1(0,minus,out,vcc,vee);', render_only=True)
        drawing = align_layout(single, fast_layout(single, component_labels(single)))
        self.assertIsNotNone(drawing)
        for turns in range(4):
            with self.subTest(turns=turns):
                moved = move_symbol(single, drawing, 'op1', (2, 2), rotate=turns)
                self.assertIsNotNone(moved)
                self.assertTrue(preserves_nets(single, moved))
                restored = restore_layout(pack_layout(moved), single, ())
                self.assertIsNotNone(restored)
                self.assertEqual(list(restored.pins()), list(moved.pins()))
                self.assertEqual(len(list(restored.pins())), 5)


if __name__ == '__main__':
    unittest.main()
