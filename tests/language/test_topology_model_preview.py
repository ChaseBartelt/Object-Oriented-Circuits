"""Topology previews retain unfinished model data without enabling false solves."""

from pathlib import Path
import unittest

from circuitlab.exporters.spice import spice_netlist
from circuitlab.language.compiler import compile_circuit
from circuitlab.math.analysis import analyze
from circuitlab.math.linear import NodalBackend
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.model.graph import CircuitError


SOURCE = """V1(1V) between(input,0);
op1(input,out,out);
analog op1 {
    gain = 100;
    response.time = 1us;
}
"""


class TopologyModelPreviewTest(unittest.TestCase):
    def test_preview_retains_the_wiring_and_original_unresolved_field(self):
        circuit = compile_circuit(SOURCE, render_only=True)
        self.assertEqual([p.kind for p in circuit.components], ["V", "O"])
        self.assertEqual(circuit.components[1].terminal_nets, ("input","out","out","0"))
        issue, = circuit.model_issues
        self.assertEqual((issue.parameter, issue.raw, issue.line), ("response.time","1us",5))
        self.assertEqual(circuit.components[1].opamp.gain, 100)

    def test_every_numerical_entry_point_rejects_an_unresolved_preview(self):
        circuit = compile_circuit(SOURCE, render_only=True)
        operations = (lambda: analyze(circuit), lambda: NodalBackend().solve(circuit, ac=False),
                      lambda: TimeDomain(circuit), lambda: spice_netlist(circuit))
        for index, operation in enumerate(operations):
            with self.subTest(operation=index):
                with self.assertRaises(CircuitError) as caught:
                    operation()
                self.assertEqual(caught.exception.code, "UnsupportedModelProperty")
                self.assertIn("response.time", caught.exception.message)

    def test_supported_configuration_solves_identically_in_a_preview(self):
        source = SOURCE.replace("    response.time = 1us;\n", "")
        normal = analyze(compile_circuit(source)).network
        preview = compile_circuit(source, render_only=True)
        self.assertFalse(preview.model_issues)
        actual = analyze(preview).network
        self.assertEqual(normal.names, actual.names)
        self.assertEqual(normal.nodes, actual.nodes)
        self.assertAlmostEqual(normal.voltage[normal.nodes.index("out")], actual.voltage[actual.nodes.index("out")])

    def test_unfinished_values_can_be_drawn_but_are_not_silently_used(self):
        source = "op1(a,b,out);\nanalog op1 {gain=unfinished;}\n"
        preview = compile_circuit(source, render_only=True)
        self.assertEqual(len(preview.components),1)
        self.assertEqual(len(preview.model_issues),1)
        with self.assertRaises(CircuitError):
            compile_circuit(source)
        with self.assertRaisesRegex(CircuitError,"Numerical model is incomplete"):
            analyze(preview)

    def test_method_model_and_shared_power_fields_do_not_hide_components(self):
        source = """circuit Stage(a,b) {R1(1k) between(a,b);}
            main {Stage(input,0);}
            analog Stage {offset=2mV; delay.rise=10ns;}
            analog {supply.current.total=1mA;}
        """
        preview = compile_circuit(source, render_only=True)
        self.assertEqual([p.kind for p in preview.components],["R"])
        self.assertTrue(any(issue.parameter == "delay.rise" for issue in preview.model_issues))
        self.assertTrue(any(issue.parameter == "supply.current.total" for issue in preview.model_issues))
        with self.assertRaises(CircuitError):
            compile_circuit(source)

    def test_included_preview_propagates_mode_and_issue_file_locations(self):
        source = 'include("child.ckt");\nconnector(out);\nV1(1V) between(out,0);\n'
        child = "connector(out);\nop1(out,out,out);\nanalog op1 {response.time=1us;}\n"
        preview = compile_circuit(source, path=Path.cwd()/"main.ckt", read_source=lambda _:child, render_only=True)
        self.assertEqual(len(preview.model_issues),1)
        self.assertEqual(preview.model_issues[0].path.name,"child.ckt")
        self.assertEqual(preview.model_issues[0].line,3)
        with self.assertRaises(CircuitError) as caught:
            analyze(preview)
        self.assertEqual(caught.exception.path.name,"child.ckt")

    def test_invalid_model_block_syntax_still_has_a_source_diagnostic(self):
        with self.assertRaises(CircuitError) as caught:
            compile_circuit("op1(a,b,out);\nanalog op1 {gain:100;}\n",render_only=True)
        self.assertEqual(caught.exception.line,2)
