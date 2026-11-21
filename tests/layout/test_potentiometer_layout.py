"""Native potentiometer pins survive routing, rotation and saved placements."""
# All three potentiometer terminals must survive automatic routing and saved-layout restore.
# The wiper position changes its glyph/value, while rotations move every contact consistently.

from dataclasses import replace
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.automatic import series_runs
from circuitlab.layout.buffered_divider import buffered_divider_layout
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.editing import move_symbol
from circuitlab.layout.geometry import component_pins, symbol_pins
from circuitlab.layout.grid import align_layout
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.layout.routing import fast_layout, schematic_layout
from circuitlab.layout.shorten import clear_of_bodies, wire_length
from circuitlab.math.report import component_labels


class PotentiometerLayoutTest(unittest.TestCase):
    def test_buffered_divider_draws_an_inline_source_and_straight_wiper_lead(self):
        for source in ("Vs(5) between(supply,0);", "Vs(-5) between(0,supply);"):
            with self.subTest(source=source):
                circuit = compile_circuit(
                    source + "\nPadjust(10k,25%) between(supply,tap,0);\n"
                    "op1(tap,buffered,buffered);\nRreturn(1k) between(0,buffered);",
                    render_only=True,
                )
                drawing = schematic_layout(circuit, component_labels(circuit), "auto")
                drawing = align_layout(circuit, drawing)
                self.assertIsNotNone(drawing)
                self.assertTrue(preserves_nets(circuit, drawing))
                self.assertFalse(drawing.crossings)
                self.assertFalse(drawing.connectors)
                edges = [edge for wires in drawing.wires.values() for edge in wires]
                self.assertTrue(clear_of_bodies(edges, drawing))

                symbols = {symbol[0].name: symbol for symbol in drawing.symbols}
                voltage = dict(symbol_pins(*symbols["Vs"], grid=drawing.pin_grid))
                pot = dict(symbol_pins(*symbols["Padjust"], grid=drawing.pin_grid))
                amp = dict(symbol_pins(*symbols["op1"], grid=drawing.pin_grid))
                load = dict(symbol_pins(*symbols["Rreturn"], grid=drawing.pin_grid))
                self.assertEqual(pot["supply"][1], voltage["supply"][1])
                self.assertEqual(voltage["supply"][1], voltage["0"][1])
                self.assertGreater(pot["supply"][0], voltage["supply"][0])
                self.assertGreater(voltage["supply"][0], voltage["0"][0])
                self.assertGreater(pot["supply"][1], pot["0"][1])
                self.assertEqual(pot["tap"][1], amp["tap"][1])
                self.assertEqual(drawing.wires["tap"], [(pot["tap"], amp["tap"])])
                self.assertEqual(load["buffered"][1], load["0"][1])
                self.assertLess(load["buffered"][0], load["0"][0])
                load_ground, = [pos for pos in drawing.grounds if pos[1] == load["0"][1]]
                self.assertGreater(load_ground[0], load["0"][0])
                self.assertEqual(drawing.ground_rotations[load_ground], 3)
                for pin in (pot["0"], voltage["0"]):
                    ground, = [pos for pos in drawing.grounds if pos[1] == pin[1]]
                    self.assertLess(ground[0], pin[0])
                    self.assertEqual(drawing.ground_rotations[ground], 1)

    def test_buffered_divider_rejects_a_shared_source(self):
        circuit = compile_circuit(
            "Vs(5) between(supply,0);\nPadjust(10k,25%) between(supply,tap,0);\n"
            "op1(tap,buffered,buffered);\nRother(1k) between(supply,0);",
            render_only=True,
        )
        self.assertIsNone(buffered_divider_layout(circuit, component_labels(circuit)))

    def test_three_fixed_pins_follow_high_wiper_low_order(self):
        part = compile_circuit('P1(200,25%) between(high,tap,low);').components[0]
        a, b = (2, 3), (6, 3)
        expected = [('high', (2, 3.75)), ('tap', b), ('low', (2, 2.25))]
        for position in (0, .25, .5, 1):
            with self.subTest(position=position):
                candidate = replace(part, position=position)
                self.assertEqual(list(symbol_pins(candidate, a, b, '+', grid=.25)), expected)
                pins = component_pins([candidate])
                self.assertEqual([(net, left) for _, net, left, _ in pins],
                                 [('high', True), ('tap', False), ('low', True)])
                self.assertEqual(tuple(net for _, net, _, _ in pins), candidate.terminal_nets)

    def test_loaded_and_tied_wipers_route_as_three_terminal_parts(self):
        for wiper in ('tap', 'high', '0'):
            source = f'V1(5) between(high,0);\nP1(10k,25%) between(high,{wiper},0);'
            if wiper == 'tap':
                source += '\nR1(1k) between(tap,0);'
            circuit = compile_circuit(source, render_only=True)
            labels = component_labels(circuit)
            pot_index = next(i for i, part in enumerate(circuit.components) if part.kind == 'P')
            runs, reversed_parts = series_runs(circuit)
            self.assertNotIn(pot_index, reversed_parts)
            self.assertFalse(any(pot_index in run for run in runs))
            for mode in ('auto', 'routed', 'labeled', 'fast'):
                with self.subTest(wiper=wiper, mode=mode):
                    drawing = fast_layout(circuit, labels) if mode == 'fast' else schematic_layout(circuit, labels, mode)
                    self.assertTrue(preserves_nets(circuit, drawing))
                    pot = next(symbol for symbol in drawing.symbols if symbol[0].name == 'P1')
                    self.assertEqual(pot[0].kind, 'P')
                    self.assertEqual(len(list(symbol_pins(*pot))), 3)

    def test_all_rotations_move_and_restore_on_grid(self):
        circuit = compile_circuit('P1(200,25%) between(high,tap,low);', render_only=True)
        drawing = align_layout(circuit, fast_layout(circuit, component_labels(circuit)))
        self.assertIsNotNone(drawing)
        original = pack_layout(drawing)
        for turns in range(4):
            with self.subTest(turns=turns):
                moved = move_symbol(circuit, drawing, 'P1', (2, 2), rotate=turns)
                self.assertIsNotNone(moved)
                self.assertTrue(preserves_nets(circuit, moved))
                for _, pin in moved.pins():
                    self.assertTrue(all(abs(value * 4 - round(value * 4)) < 1e-7 for value in pin))
                restored = restore_layout(pack_layout(moved), circuit, ())
                self.assertIsNotNone(restored)
                self.assertEqual(list(restored.pins()), list(moved.pins()))
                self.assertTrue(preserves_nets(circuit, restored))
        self.assertEqual(pack_layout(drawing), original)

    def test_value_and_position_changes_reuse_saved_geometry(self):
        circuit = compile_circuit('P1(200,25%) between(high,tap,low);', render_only=True)
        drawing = fast_layout(circuit, component_labels(circuit))
        record = pack_layout(drawing)
        edited = replace(circuit, components=[replace(circuit.components[0], value=1000, position=.75)])
        restored = restore_layout(record, edited, ())
        self.assertIsNotNone(restored)
        self.assertEqual(pack_layout(restored), record)
        self.assertEqual(restored.symbols[0][0].position, .75)
        self.assertEqual(restored.symbols[0][0].value, 1000)


class GeneratedSpacingTest(unittest.TestCase):
    def test_buffered_stages_have_shorter_connections(self):
        circuit = compile_circuit('V1(1) between(input,0);\n'
                                  'R1(1k) between(input,first);\nC1(10n) between(first,0);\n'
                                  'op1(first,buffered,buffered);\n'
                                  'R2(1k) between(buffered,second);\nC2(10n) between(second,0);\n'
                                  'op2(second,out,out);')
        drawing = schematic_layout(circuit, component_labels(circuit), 'auto')
        self.assertTrue(preserves_nets(circuit, drawing))
        symbols = {part.name: (a, b) for part, a, b, _ in drawing.symbols}
        self.assertLess(abs(symbols['R2'][0][0] - symbols['op1'][1][0]), 4)

    def test_compact_passive_network_has_shorter_component_spans(self):
        circuit = compile_circuit('V1(5) between(vcc,0);\nmain { series(vcc,0) {'
                                  ' R1=1k; parallel { R2=2k; R3=3k; } R4=1k; } }')
        drawing = schematic_layout(circuit, component_labels(circuit), 'auto')
        self.assertTrue(preserves_nets(circuit, drawing))
        for part, a, b, _ in drawing.symbols:
            if part.kind == 'R':
                self.assertLessEqual(abs(a[0] - b[0]) + abs(a[1] - b[1]), 4)

    def test_tight_router_uses_short_leads_without_changing_saved_layouts(self):
        circuit = compile_circuit('V1(5) between(vcc,0);\nR1(1k) between(vcc,out);\nR2(2k) between(out,0);')
        drawing = fast_layout(circuit, component_labels(circuit))
        self.assertTrue(preserves_nets(circuit, drawing))
        self.assertLess(wire_length(drawing), 6)
        record = pack_layout(drawing)
        restored = restore_layout(record, circuit, ())
        self.assertEqual(pack_layout(restored), record)


if __name__ == '__main__':
    unittest.main()
