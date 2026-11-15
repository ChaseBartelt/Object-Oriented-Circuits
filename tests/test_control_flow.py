"""Compile-time control flow through the circuit compiler and downstream tools."""

import unittest
from pathlib import Path

from circuitlab.analysis import analyze, solve_input
from circuitlab.compiler import compile_circuit
from circuitlab.graph import CircuitError
from circuitlab.layout_checks import preserves_nets
from circuitlab.report import component_labels
from circuitlab.routing import schematic_layout
from circuitlab.spice import export_spice
from circuitlab.verilog import export_verilog


class ControlFlowTest(unittest.TestCase):
    def test_for_while_do_and_branches_define_one_electrical_graph(self):
        source = """
        VSupply(12) between(vcc, 0);
        main {
            let total = 0;
            for (int i = 0; i < 3; i++) {
                RTop${i}(1k) between(vcc, out${i});
                if (i == 0) {
                    RLow${i}(1k) between(out${i}, 0);
                } else if (i == 1 && !(i == 0)) {
                    RLow${i}(${(i + 1) * 1000}) between(out${i}, 0);
                } else {
                    RLow${i}(3k) between(out${i}, 0);
                }
                total += i;
            }
            while (total > 1) {
                RW${total}(10k) between(vcc, 0);
                --total;
            }
            do {
                RD${total}(20k) between(vcc, 0);
                total++;
            } while (false);
            measure = V out0 0;
            measure = V out1 0;
            measure = V out2 0;
        }
        """
        result = solve_input(source)
        self.assertEqual(
            [part.name for part in result.circuit.components],
            [
                "VSupply", "RTop0", "RLow0", "RTop1", "RLow1", "RTop2", "RLow2",
                "RW3", "RW2", "RD1",
            ],
        )
        for reading, expected in zip(result.readings, (6, 8, 9), strict=True):
            self.assertAlmostEqual(reading[2], expected)
        top_line = next(
            line for line, row in enumerate(source.splitlines(), 1) if "RTop${i}" in row
        )
        self.assertEqual(
            [result.circuit.component_lines[f"RTop{i}"] for i in range(3)], [top_line] * 3
        )
        drawing = schematic_layout(result.circuit, component_labels(result.circuit), "labeled")
        self.assertTrue(preserves_nets(result.circuit, drawing))
        self.assertIn("RLow1 out1 0 2000\n", export_spice(source))

    def test_each_method_call_has_independent_variables_and_internal_nets(self):
        result = solve_input("""
        V1(12) between(vcc, 0);
        circuit divider(positive, output, gnd) {
            let i = 0;
            while (i < 2) {
                RTop${i}(1k) between(positive, tap${i});
                if (i == 1) {
                    RTail${i}(1k) between(tap${i}, output);
                } else {
                    RTail${i}(1k) between(tap${i}, gnd);
                }
                i = i + 1;
            }
            RLoad(2k) between(output, gnd);
        }
        main {
            divider(vcc, left, 0);
            divider(vcc, right, 0);
            measure = V left 0;
            measure = V right 0;
        }
        """)
        self.assertEqual(len({part.name for part in result.circuit.components}), 11)
        taps = [node for node in result.network.nodes if node.endswith((".tap0", ".tap1"))]
        self.assertEqual(len(taps), 4)
        for reading in result.readings:
            self.assertAlmostEqual(reading[2], 6)

    def test_loop_control_and_short_circuit_conditions(self):
        circuit = compile_circuit("""
        V1(5) between(vcc, 0);
        main {
            for (i = 0; i < 5; i++) {
                if (i == 1) { continue; }
                if (i == 3) { break; }
                let j = 0;
                do {
                    j++;
                    if (j < 2) { continue; }
                    R${i}${j}(1k) between(vcc, 0);
                    if (true || 1 / 0 > 0) { break; }
                } while (j < 3);
            }
            bool disabled = false;
            if (disabled && 1 / 0 > 0) { RUnused(1k) between(vcc, 0); }
            for (;;) {
                ROnce(1k) between(vcc, 0);
                break;
            }
        }
        """)
        self.assertEqual(
            [part.name for part in circuit.components], ["V1", "R02", "R22", "ROnce"]
        )

    def test_linked_method_with_control_flow_solves_and_exports(self):
        path = Path.cwd() / "control-flow-main.ckt"
        source = """
        include("control-flow-load.ckt");
        V1(8) between(vcc, 0);
        main {
            RTop(1k) between(vcc, out);
            load(out, 0);
        }
        """
        child = """
        circuit load(p, n) {
            for (let i = 0; i < 2; i++) {
                RLoad${i}(2k) between(p, n);
            }
        }
        """

        def read(included):
            self.assertEqual(included, path.with_name("control-flow-load.ckt"))
            return child

        circuit = compile_circuit(source, path=path, read_source=read)
        network = analyze(circuit).network
        self.assertAlmostEqual(network.voltage[network.nodes.index("out")], 4)
        result = export_spice(source, path=path, read_source=read)
        self.assertEqual(result.count(" out 0 2000\n"), 2)

    def test_reports_included_nonterminating_loop_at_original_line(self):
        path = Path.cwd() / "control-flow-main.ckt"
        source = 'include("spin.ckt");\nV1(5) between(vcc, 0);\nmain { spin(vcc); }'
        child = "circuit spin(p) {\n    while (true) { }\n}"
        with self.assertRaises(CircuitError) as failure:
            compile_circuit(source, path=path, read_source=lambda _: child)
        self.assertRegex(str(failure.exception), r"(?i)iteration|limit|budget")
        self.assertEqual(failure.exception.line, 2)
        self.assertEqual(failure.exception.path, path.with_name("spin.ckt"))

    def test_unknown_compile_time_voltage_is_reported_at_the_condition(self):
        source = "V1(5) between(vcc, 0);\nmain {\n    if (vcc > 2) { R1 vcc 0 1k; }\n}"
        with self.assertRaises(CircuitError) as failure:
            compile_circuit(source)
        self.assertIn("vcc", str(failure.exception))
        self.assertEqual(failure.exception.line, 3)

    def test_auto_analysis_uses_only_emitted_sources(self):
        source = """
        main {
            if (true || false) {
                VDC(5) between(vcc, 0);
            } else {
                VAC(3 Vpk) between(vcc, 0);
                f = 1000;
            }
            RLoad(1k) between(vcc, 0);
            measure = V vcc 0;
        }
        """
        result = solve_input(source)
        self.assertEqual(result.circuit.mode, "dc")
        self.assertAlmostEqual(result.readings[0][2], 5)

    def test_control_flow_and_legacy_structural_braces_can_be_mixed(self):
        expanded = """
        V1(10) between(vcc, 0);
        circuit stage(p, n) {
            series(p, n) {
                R1 = 1k;
                R2 = 1k;
            }
        }
        main {
            stage(vcc, out);
            RLoad(2k) between(out, 0);
            measure = V out 0;
        }
        """
        generated = expanded.replace(
            "series(p, n) {\n                R1 = 1k;\n                R2 = 1k;\n            }",
            "if (true) { series(p, n) { for (let i = 1; i <= 2; i++) { R${i} = 1k; } } }",
        )
        self.assertEqual(export_spice(generated), export_spice(expanded))
        self.assertAlmostEqual(solve_input(generated).readings[0][2], 5)

    def test_generated_transistor_names_and_fields_use_each_iteration(self):
        source = """
        main {
            for (int i = 0; i < 2; i++) {
                transistor Q${i} {
                    base = b${i};
                    collector = c${i};
                    emitter = 0;
                    type = npn;
                }
            }
        }
        """
        circuit = compile_circuit(source)
        self.assertEqual(
            [(part.name, part.base, part.p, part.n) for part in circuit.components],
            [("Q0", "b0", "c0", "0"), ("Q1", "b1", "c1", "0")],
        )
        netlist = export_spice(source)
        self.assertIn("Q0 c0 b0 0 EC_BJT_NPN\n", netlist)
        self.assertIn("Q1 c1 b1 0 EC_BJT_NPN\n", netlist)

    def test_loop_keywords_still_work_as_legacy_bare_tap_names(self):
        for name in ("continue", "break"):
            with self.subTest(name=name):
                source = f"V = 5\n(series)\nR1 = 1k\n{name}\nR2 = 1k\n(end series)"
                circuit = compile_circuit(source)
                self.assertIn(name, circuit.named_wires)

    def test_analog_control_flow_is_rejected_in_standalone_verilog(self):
        source = """
        chip Logic(a, y) {
            a = input;
            y = output;
            if (true) { y = a; } else { y = !a; }
        }
        """
        with self.assertRaises(CircuitError):
            export_verilog(source)

