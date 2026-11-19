"""Optional ideal-opamp supply pins are visible without changing signal math."""
# Optional supply terminals describe wiring without introducing rail clipping or artificial
# floating numerical nodes into the ideal three-signal-pin amplifier model.

import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.analysis import analyze
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.model.graph import CircuitError


class OpampSupplyTest(unittest.TestCase):
    def test_three_pin_form_keeps_existing_terminal_signature(self):
        circuit = compile_circuit("op1(plus,minus,out);")
        part, = circuit.components
        self.assertEqual((part.vplus, part.vminus), ("", ""))
        self.assertEqual(part.terminal_nets, ("plus", "minus", "out", "0"))

    def test_five_pin_form_binds_both_supply_identifiers(self):
        circuit = compile_circuit("op1(plus,minus,out,pos,neg);")
        part, = circuit.components
        self.assertEqual((part.vplus, part.vminus), ("pos", "neg"))
        self.assertEqual(part.terminal_nets, ("plus", "minus", "out", "0", "pos", "neg"))
        self.assertEqual(circuit.grounds, {"0"})
        self.assertNotIn("pos", circuit.nodes)
        self.assertNotIn("neg", circuit.nodes)

    def test_supply_identifiers_do_not_require_sources_for_ideal_math(self):
        source = "V1(2) between(input,0);\nop1(input,out,out,pos,neg);"
        circuit = compile_circuit(source)
        for network in (analyze(circuit).network, TimeDomain(circuit).operating_point()):
            with self.subTest(network=network):
                self.assertAlmostEqual(network.voltage[network.nodes.index("out")], 2)
                self.assertNotIn("pos", network.nodes)
                self.assertNotIn("neg", network.nodes)

    def test_connected_sources_are_measurable_without_clipping(self):
        source = """
        Vsignal(8) between(input,0);
        Vpositive(2) between(pos,0);
        Vnegative(-5) between(neg,0);
        op1(input,out,out,pos,neg);
        measure = V out 0;
        measure = V pos 0;
        measure = V neg 0;
        """
        result = analyze(compile_circuit(source))
        self.assertEqual([reading[2] for reading in result.readings], [8, 2, -5])

    def test_supply_connectors_do_not_add_unknowns_to_signal_solver(self):
        source = "connector(pos,neg);\nV1(2) between(input,0);\nop1(input,out,out,pos,neg);"
        circuit = compile_circuit(source)
        self.assertEqual(circuit.connector_pins, ("pos", "neg"))
        self.assertNotIn("pos", circuit.nodes)
        self.assertNotIn("neg", circuit.nodes)
        network = analyze(circuit).network
        self.assertAlmostEqual(network.voltage[network.nodes.index("out")], 2)
        with self.assertRaisesRegex(CircuitError, '"missing" is not a net'):
            compile_circuit(source.replace("connector(pos,neg)", "connector(pos,missing)"))

    def test_supply_pins_follow_method_bindings_and_wire_tap_aliases(self):
        source = """
        circuit buffer(input, output, high, low) { op1(input,output,output,high,low); }
        main {
            parallel(pos,0) { wire = positive_alias; R1 = 1k; }
            parallel(neg,0) { wire = negative_alias; R2 = 1k; }
            buffer(input,out,positive_alias,negative_alias);
        }
        """
        circuit = compile_circuit(source)
        part, = [part for part in circuit.components if part.kind == "O"]
        self.assertEqual((part.vplus, part.vminus), ("pos", "neg"))
        self.assertEqual((part.in_plus, part.in_minus, part.p), ("input", "out", "out"))

    def test_accepts_three_or_five_pins_only_with_source_locations(self):
        for raw in ("a,b", "a,b,c,d", "a,b,c,d,e,f", "a,b,c,d,", "a,b,c,,e"):
            with self.subTest(raw=raw), self.assertRaises(CircuitError) as error:
                compile_circuit(f"# op amp\nop1({raw});")
            self.assertEqual(error.exception.line, 2)
            self.assertIn("vplus, vminus", error.exception.message)


if __name__ == "__main__":
    unittest.main()
