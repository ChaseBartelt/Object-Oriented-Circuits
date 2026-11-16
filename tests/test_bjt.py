"""Exercise BJT blocks through compilation, drawing, and SPICE export."""

import unittest
from pathlib import Path

from circuitlab.math.analysis import solve_input
from circuitlab.language.compiler import compile_circuit
from circuitlab.model.graph import CircuitError
from circuitlab.layout.checks import preserves_nets
from circuitlab.exporters.ltspice import export_schematic
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.math.report import component_labels
from circuitlab.layout.routing import schematic_layout
from circuitlab.exporters.spice import export_spice
from test_ltspice import read_connections

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "bjt_switch.ckt"
TIMER = Path(__file__).parent / "fixtures" / "refactor" / "timer555.ckt"


class BjtTest(unittest.TestCase):
    def test_timer555_operating_point_with_reset_held_low(self):
        source = TIMER.read_text(encoding="utf-8")
        network = solve_input(source).network
        nodes = dict(zip(network.nodes, network.voltage))
        self.assertAlmostEqual(nodes["reset_n"], 0)
        self.assertAlmostEqual(nodes["out"], 0.081, places=3)
        self.assertAlmostEqual(nodes["timing"], nodes["discharge"], places=7)

    def test_timer555_runs_through_switching_edges_with_reset_released(self):
        source = TIMER.read_text(encoding="utf-8")
        circuit = compile_circuit(source.replace("VReset(0)", "VReset(5)"))
        simulation = TimeDomain(circuit, 10e-6)
        out = simulation.nodes.index("out")
        samples = [simulation.advance()[out] for _ in range(520)]
        edges = sum((a > 2.5) != (b > 2.5) for a, b in zip(samples, samples[1:]))
        self.assertGreaterEqual(edges, 4)
        self.assertLess(min(samples), 0.2)
        self.assertGreater(max(samples), 4)
        self.assertAlmostEqual(simulation.time, 520 * 10e-6)

    def test_switch_exports_all_three_pins_in_each_wiring_style(self):
        source = EXAMPLE.read_text(encoding="ascii")
        for polarity in ("npn", "pnp"):
            text = source.replace(", npn);", f", {polarity});")
            circuit = compile_circuit(text, render_only=True)
            self.assertIn("b", circuit.nodes)
            model = f"EC_BJT_{polarity.upper()}"
            netlist = export_spice(text)
            self.assertIn(f"Q1 out b 0 {model}\n", netlist)
            self.assertIn(f".model {model} {polarity.upper()}\n", netlist)
            for mode in ("auto", "routed", "labeled"):
                with self.subTest(polarity=polarity, mode=mode):
                    layout = schematic_layout(circuit, component_labels(circuit), mode)
                    self.assertTrue(preserves_nets(circuit, layout))
                    sheet, _ = export_schematic(circuit, layout)
                    self.assertIn(f"SYMBOL {polarity} ", sheet)
                    pins, attributes = read_connections(sheet)["Q1"]
                    self.assertEqual(pins, ("out", "b", "0"))
                    self.assertEqual(attributes["Value"], model)

    def test_reusable_method_keeps_instance_names_and_named_model(self):
        source = """
        circuit pair(input, output, gnd) {
            RB(1k) between(input, tap);
            transistor Q1 { collector = output; emitter = gnd; base = tap;
                            type = NPN; model = 2N3904; }
        }
        main { pair(in, out1, 0); pair(in, out2, 0); }
        """
        circuit = compile_circuit(source)
        q1, q2 = [part for part in circuit.components if part.kind == "Q"]
        self.assertEqual((q1.name, q2.name), ("Q1_pair_1", "Q1_pair_2"))
        self.assertEqual((q1.p, q2.p), ("out1", "out2"))
        self.assertEqual((q1.base, q2.base), ("pair_1.tap", "pair_2.tap"))
        self.assertEqual(q1.n, "0")
        exported = export_spice(source)
        self.assertIn("Q1_pair_1 out1 pair_1_tap 0 2N3904", exported)
        self.assertIn("2N3904 (NPN)", exported)
        self.assertNotIn(".model 2N3904", exported)

    def test_direct_main_tied_pins_and_mode_detection(self):
        source = (
            "main { transistor Q1 { base=x3Vpk; collector=x3Vpk;"
            " emitter=0; type=npn; } }"
        )
        circuit = compile_circuit(source)
        self.assertEqual(circuit.mode, "dc")
        self.assertIn("Q1 x3Vpk x3Vpk 0 EC_BJT_NPN", export_spice(source))

    def test_positional_forms_accept_a_model_and_pin_order(self):
        for name in ("transQ1", "transistor Q1", "trans1"):
            with self.subTest(name=name):
                netlist = export_spice(f"main {{ {name}(b, c, 0, pnp, 2N3906); }}")
                self.assertIn("Q1 c b 0 2N3906", netlist)
        for args in ("b, c, 0", "b, c, 0, mosfet", "b, c, 0, npn,"):
            with self.subTest(args=args):
                with self.assertRaises(CircuitError):
                    compile_circuit(f"transQ1({args});")

    def test_parser_reports_invalid_fields_and_duplicate_names(self):
        prefix = "transistor Q1 { base=b; collector=c; emitter=0; "
        valid = prefix + "type=npn; }"
        invalid = (
            (prefix + "}", "needs these fields: type"),
            (prefix + "type=mosfet; }", "type = npn or type = pnp"),
            (prefix + "base=x; type=npn; }", 'Set "base" once'),
            (prefix + "type=npn; extra=y; }", 'Unknown transistor field "extra"'),
            (prefix + "type=npn; model=x (bad); }", "one SPICE model name"),
            (prefix + "type=npn;", "Close transistor Q1"),
            (valid + "\n" + valid.replace("Q1", "q1"), "unique component name"),
        )
        for source, message in invalid:
            with self.subTest(source=source):
                with self.assertRaisesRegex(CircuitError, message):
                    compile_circuit(source)

    def test_native_solver_biases_generic_npn_and_pnp(self):
        source = EXAMPLE.read_text(encoding="ascii")
        for sign, text in ((1, source), (-1, source.replace("(5)", "(-5)")
                         .replace("(3.3)", "(-3.3)").replace("npn", "pnp"))):
            with self.subTest(sign=sign):
                network = solve_input(text).network
                output = network.voltage[network.nodes.index("out")]
                self.assertAlmostEqual(sign * output, 0.068, places=3)
                self.assertGreater(sign * network.base_current["Q1"], 0)
        with self.assertRaisesRegex(CircuitError, "Named BJT models require SPICE"):
            solve_input(source.replace("npn", "npn, 2N3904"))
