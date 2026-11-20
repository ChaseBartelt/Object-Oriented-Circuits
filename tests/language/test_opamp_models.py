"""Op-amp defaults and per-instance overrides survive electrical elaboration."""

from pathlib import Path
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.model.graph import CircuitError, OpAmpModel


class OpampDeclarationTest(unittest.TestCase):
    def test_bare_declaration_keeps_ideal_defaults_and_old_call_forms(self):
        for call in ("op1(a,b,out);", "op1(a,b,out,vcc,vee);"):
            part, = compile_circuit("opamp op1; " + call).components
            self.assertEqual(part.opamp, OpAmpModel())
        self.assertIsNone(compile_circuit("op1(a,b,out);").components[0].opamp)

    def test_all_properties_use_si_units_and_partial_blocks_keep_defaults(self):
        source = """opamp op1 {
            gain=19e6; bandwidth=7.2MHz; slew=2.5V/us;
            offset=20uV; supply_current=2.75mA;
        }
        op1(a,b,out,vcc,vee);"""
        model = compile_circuit(source).components[0].opamp
        for key, value in vars(OpAmpModel(19e6, 7.2e6, 2.5e6, 20e-6, 2.75e-3)).items():
            self.assertAlmostEqual(getattr(model, key), value)
        part, = compile_circuit("opamp op1 { offset=-2mV; } op1(a,b,out);").components
        self.assertEqual(part.opamp, OpAmpModel(offset=-.002))

    def test_method_instances_keep_independent_models_and_rail_bindings(self):
        source = """chip Stage(plus,minus,out,vcc,vee) {
            opamp op1 { gain=100; bandwidth=1MHz; }
            op1(plus,minus,out,vcc,vee);
        }
        main { Stage(a,b,x,pos,neg); Stage(c,d,y,high,low); op1(a,b,z); }"""
        first, second, plain = compile_circuit(source).components
        self.assertNotEqual(first.name, second.name)
        self.assertEqual(first.opamp, second.opamp)
        self.assertEqual((first.vplus, first.vminus), ("pos", "neg"))
        self.assertEqual((second.vplus, second.vminus), ("high", "low"))
        self.assertIsNone(plain.opamp)

    def test_forward_configuration_and_ideal_overrides(self):
        part, = compile_circuit("op1(a,b,out); opamp op1 { gain=ideal; slew=ideal; }").components
        self.assertEqual(part.opamp, OpAmpModel())

    def test_parameter_failures_report_the_property_line(self):
        for field in ("gain=0", "gain=-1", "bandwidth=0Hz", "slew=-1V/us",
                      "offset=1A", "offset=1+j2", "supply_current=-1mA",
                      "unknown=2", "gain=1e999", "gain=10; gain=20"):
            with self.subTest(field=field), self.assertRaises(CircuitError) as error:
                compile_circuit(f"opamp op1 {{\n{field};\n}}\nop1(a,b,out,vcc,vee);")
            self.assertEqual(error.exception.line, 2)

    def test_duplicates_unclosed_blocks_and_missing_supplies_are_diagnostics(self):
        for source, text in (
            ("opamp op1; opamp op1; op1(a,b,out);", "already declared"),
            ("main { opamp op1 { gain=10;", "Close"),
            ("opamp op1 { supply_current=1mA; } op1(a,b,out);", "both supply pins"),
        ):
            with self.subTest(source=source), self.assertRaisesRegex(CircuitError, text):
                compile_circuit(source)

    def test_linking_qualifies_power_pins_and_preserves_model(self):
        source = ('include("amp.ckt");\nconnector(input,out,pos,neg);\nV1(1) between(input,0);\n'
                  'Vp(5) between(pos,0);\nVn(-5) between(neg,0);\nRload(1k) between(out,0);')
        child = """connector(input,out,pos,neg);
            opamp op1 { supply_current=1mA; gain=100; }
            op1(input,out,out,pos,neg);"""
        circuit = compile_circuit(source, path=Path.cwd() / "main.ckt", read_source=lambda _: child)
        amp = next(p for p in circuit.components if p.kind == "O")
        self.assertEqual(amp.opamp.supply_current, .001)
        self.assertEqual((amp.vplus, amp.vminus), ("pos", "neg"))
