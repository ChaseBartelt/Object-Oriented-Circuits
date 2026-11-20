"""Model overrides alter behavior while preserving imported chip topology."""

from pathlib import Path
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.model.graph import CircuitError


QUAD = """chip Quad(input,out1,out2,out3,out4,vp,vn) {
    op1(input,out1,out1,vp,vn); op2(input,out2,out2,vp,vn);
    op3(input,out3,out3,vp,vn); op4(input,out4,out4,vp,vn);
    analog (op1,op2,op3,op4) {gain=100000;gbw=1.2MHz;slew=0.5V/us;offset=3mV;}
    @override
    analog op3 {offset=5mV;}
}
"""
MAIN = "main {Quad(input,a,b,c,d,pos,neg);}"


class ModelOverrideTest(unittest.TestCase):
    def test_analog_digital_and_single_equation_no_longer_need_math_suffix(self):
        circuit = compile_circuit("analog {V(a)=2V;}\nanalog V(out)=V(a)*2;\ndigital {logic=V(out)>1V;}")
        self.assertEqual(len(circuit.behavior), 3)
        self.assertFalse(circuit.components)

    def test_group_properties_apply_to_all_targets_and_local_override_is_specific(self):
        circuit = compile_circuit(QUAD + MAIN)
        for part in circuit.components:
            self.assertEqual(part.opamp.bandwidth, 1.2e6)
            self.assertEqual(part.opamp.offset, .005 if part.name.startswith("op3_") else .003)

    def test_imported_chip_patch_inherits_pins_and_does_not_change_connections(self):
        base = 'include("quad.ckt");\n' + MAIN
        patch = """@override
            chip Quad {analog (op1,op2,op3,op4) {gbw=2MHz;slew=1V/us;}}
        """
        def compile(source):
            return compile_circuit(source, path=Path.cwd() / "main.ckt", read_source=lambda _: QUAD)
        original, modified = compile(base), compile(base + "\n" + patch)
        self.assertEqual([(p.name,p.terminal_nets) for p in original.components],
                         [(p.name,p.terminal_nets) for p in modified.components])
        self.assertEqual(original.instances, modified.instances)
        for part in modified.components:
            self.assertEqual(part.opamp.bandwidth, 2e6)
            self.assertEqual(part.opamp.slew, 1e6)
            self.assertEqual(part.opamp.offset, .005 if part.name.startswith("op3_") else .003)

    def test_external_individual_override_wins_without_overwriting_other_fields(self):
        patch = "@override\nchip Quad {@override; analog op3 {offset=7mV;}}"
        source = 'include("quad.ckt");\n' + MAIN + "\n" + patch
        circuit = compile_circuit(source, path=Path.cwd() / "main.ckt", read_source=lambda _: QUAD)
        part = next(p for p in circuit.components if p.name.startswith("op3_"))
        self.assertEqual(part.opamp.offset, .007)
        self.assertEqual(part.opamp.gain, 100000)

    def test_conflicts_are_rejected_independently_of_override_order(self):
        for first, second in ((2,3),(3,2)):
            patches = "\n".join(f"@override\nchip Quad {{analog (op1,op2) {{gbw={value}MHz;}}}}" for value in (first,second))
            with self.subTest(order=(first,second)), self.assertRaisesRegex(CircuitError, "Conflicting model overrides"):
                compile_circuit('include("quad.ckt");\n' + MAIN + patches,
                                path=Path.cwd() / "main.ckt", read_source=lambda _: QUAD)

    def test_chip_patches_cannot_change_pins_or_add_connections(self):
        for patch in ("@override\nchip Quad(x,y) {analog op1 {gain=5;}}",
                      "@override\nchip Quad {Rextra(1k) between(a,b);}"):
            with self.subTest(patch=patch), self.assertRaises(CircuitError):
                compile_circuit('include("quad.ckt");\n' + MAIN + patch,
                                path=Path.cwd() / "main.ckt", read_source=lambda _: QUAD)

    def test_unknown_chip_and_unknown_component_are_diagnostics(self):
        with self.assertRaisesRegex(CircuitError, "original chip"):
            compile_circuit("@override\nchip Missing {analog op1 {gain=5;}}")
        with self.assertRaisesRegex(CircuitError, "without connections"):
            compile_circuit("op1(a,b,out);\nanalog op2 {gain=5;}")

    def test_digital_behavior_can_be_overridden_without_a_second_chip_definition(self):
        source = """chip Compare(a,b,out){digital{out=V(a)>V(b);}}
            @override
            chip Compare {@override; digital{out=V(a)>=V(b);}}
            main{Compare(a,b,out);}
        """
        circuit = compile_circuit(source)
        self.assertEqual(len(circuit.behavior), 1)
        self.assertEqual(circuit.behavior[0].tree[1], (">=",))

    def test_eight_argument_opamp_preserves_all_eight_identifiers(self):
        for name in ("op1", "op2"):
            circuit = compile_circuit(f"{name}(plus,minus,out,vcc,gnd,g1,g8,bypass);")
            part, = circuit.components
            self.assertEqual(part.kind, "A")
            self.assertEqual(part.terminal_nets, ("plus","minus","out","vcc","gnd","g1","g8","bypass"))
            self.assertEqual(part.audio.gain_default, 20)

    def test_dotted_audio_properties_match_the_legacy_aliases(self):
        fields = "gain.default=25; gain.max=150; gain.resistor=2k; output.bias=.4; " \
                 "supply.current=3mA; output.type=power_amplifier; bypass.resistance=40k;"
        source = "op1(plus,minus,out,vcc,gnd,g1,g8,bypass); analog op1 {" + fields + "}"
        dotted = compile_circuit(source).components[0].audio
        # Replace property separators only; decimal numeric literals retain dots.
        import re
        underscored = re.sub(r"[A-Za-z]+\.[A-Za-z]+(?=\s*=)",
                            lambda m: m[0].replace(".", "_"), source)
        self.assertEqual(dotted, compile_circuit(underscored).components[0].audio)
        self.assertEqual((dotted.gain_default, dotted.gain_max, dotted.supply_current), (25,150,.003))

    def test_dotted_properties_work_in_imported_chip_patches(self):
        source = ('include("LM386");\n@override\nchip LM386 {analog op1 {gain.default=30; supply.current=2mA;}}\n'
                  'main { LM386(g1,minus,plus,gnd,out,vcc,bypass,g8); }')
        part, = compile_circuit(source).components
        self.assertEqual(part.audio.gain_default, 30)
        self.assertEqual(part.audio.supply_current, .002)

    def test_dotted_and_underscored_aliases_cannot_hide_duplicate_properties(self):
        with self.assertRaisesRegex(CircuitError, "already declared"):
            compile_circuit("op1(a,b,out,p,n,g1,g8,bp); analog op1 {gain.default=20; gain_default=30;}")
        with self.assertRaisesRegex(CircuitError, "Conflicting model overrides"):
            compile_circuit("op1(a,b,out,p,n,g1,g8,bp); analog op1 {gain.default=20;} analog op1 {gain_default=30;}")

    def test_multiline_chip_header_and_opamp_call_keep_pin_order(self):
        circuit = compile_circuit("""chip Audio(g1,minus,plus,gnd,
            out,vcc,bypass,g8) {
            op1(plus,minus,out,vcc,gnd,
                g1,g8,bypass);
            analog op1 {gain.default=20;}
        }
        main {Audio(g1,b,a,0,out,rail,bp,g8);}
        """)
        part, = circuit.components
        self.assertEqual(part.terminal_nets, ("a","b","out","rail","0","g1","g8","bp"))
