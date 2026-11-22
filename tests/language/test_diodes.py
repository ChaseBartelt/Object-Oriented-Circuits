"""Diode syntax, defaults, scoping and validation without changing other devices."""
import unittest
from circuitlab.language.compiler import compile_circuit
from circuitlab.model.graph import CircuitError
from circuitlab.language.diodes import parse_diode


class DiodeLanguageTest(unittest.TestCase):
    def test_presets_and_aliases_keep_terminal_order(self):
        for name, kind in (("D1", "rectifier"), ("D.rect1", "rectifier"), ("D.Tun1", "tunnel"),
                           ("D.Schot1", "schottky"), ("D.Zener1", "zener"), ("D.LED1", "led"),
                           ("D.PIN1", "pin"), ("D.Varactor1", "varactor"), ("D.Photo1", "photo"),
                           ("D.Avalanche1", "avalanche")):
            with self.subTest(name=name):
                c = compile_circuit(f"{name}() between(signal, 0);", "dc")
                part, = c.components
                self.assertEqual((part.name, part.kind, part.p, part.n), (name, "D", "signal", "0"))
                self.assertEqual(part.diode.kind, kind)
                c.require_values()

    def test_override_units_and_validation(self):
        model = parse_diode("D.Schot1", "is=2nA, n=1.2, rs=3ohm, bv=25V, cj=12pF")
        self.assertAlmostEqual(model.saturation, 2e-9)
        self.assertEqual((model.ideality, model.resistance, model.breakdown), (1.2, 3, 25))
        self.assertAlmostEqual(model.capacitance, 12e-12)
        self.assertEqual(parse_diode("D.Zener1", "6.2V").breakdown, 6.2)
        for name, raw in (("D.unknown1", ""), ("D1", "rs=-2"), ("D1", "is=0"),
                          ("D1", "foo=2"), ("D1", "n=0"), ("D1", "cj=-2pF"),
                          ("D1", "0V"), ("D1", "n=1,n=2"), ("D1", "m=1"),
                          ("D1", "is=1V"), ("D1", "0.7V,is=1nA")):
            with self.subTest(name=name, raw=raw), self.assertRaises(CircuitError):
                parse_diode(name, raw)

    def test_methods_groups_counters_and_generated_names(self):
        source = """chip Clamp(a, k) {
            D.Schot1() between(a,k);
        }
        V1(5) between(supply,0);
        main {
            wi = count;
            R1(1k) between(supply, wi*);
            Clamp(wi^,0);
            for(int i=0; i<2; i++) { D.rect*{i}() between(supply,0); }
        }"""
        c = compile_circuit(source, "dc")
        self.assertEqual(sum(part.kind == "D" for part in c.components), 3)
        self.assertTrue(any(part.diode.kind == "schottky" for part in c.components if part.kind == "D"))
        c = compile_circuit("V1(5) between(in,0);\nmain { series(in,0) { R1(1k); D.LED1(2V); } }", "dc")
        self.assertTrue(any(part.kind == "D" and part.diode.kind == "led" for part in c.components))

    def test_component_arguments_in_reusable_circuits(self):
        source = """circuit Clamp(element, a, k) { element between(a,k); }
        V1(5) between(in,0);
        main { Clamp(D.Zener1(5.1V), 0, in); }"""
        c = compile_circuit(source, "dc")
        diode = next(part for part in c.components if part.kind == "D")
        self.assertEqual(diode.diode.breakdown, 5.1)
        self.assertEqual(diode.p, "0")

    def test_default_diodes_need_no_parentheses(self):
        for name in ("D1", "D.rect1", "D.Schot1", "D.Tun1"):
            with self.subTest(name=name):
                c = compile_circuit(f"{name} between(a,0);", "dc")
                self.assertIsNotNone(c.components[0].diode)
                c = compile_circuit(f"V1(5) between(in,0);\nmain {{ series(in,0) {{ R1(1k); {name}; }} }}", "dc")
                self.assertEqual(sum(part.kind == "D" for part in c.components),1)
