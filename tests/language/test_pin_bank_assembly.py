"""Numbered scalar pin ranges keep their order through assembly and metadata."""

import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError
from circuitlab.tooling.session import CircuitSession


PASS = "chip Pass(A[3:0],Y[3:0]){A[3:0]=input;Y[3:0]=output;Y[3:0]=A[3:0];}"


class PinBankAssemblyTest(unittest.TestCase):
    def assert_copy(self, source):
        sim = DigitalSimulation(source)
        for word in range(16):
            for bit in range(4):
                sim.set_input(f"SW{bit}", (word >> bit) & 1)
            self.assertEqual(sum(sim.values[f"LEDR{bit}"] << bit for bit in range(4)), word)
        return sim

    def test_main_calls_expand_descending_ranges_in_physical_order(self):
        source = PASS + "main{SW[3:0]=input;LEDR[3:0]=output;Pass(SW[3:0],LEDR[3:0]);}"
        sim = self.assert_copy(source)
        self.assertEqual(sim.inputs, ("SW3", "SW2", "SW1", "SW0"))
        session = CircuitSession(source=source)
        session.compile()
        self.assertFalse(session.issues)
        instance, = session.drawing.instances
        self.assertEqual(instance.pins, tuple((f"{formal}{bit}", f"{actual}{bit}")
                                            for formal, actual in (("A", "SW"), ("Y", "LEDR"))
                                            for bit in reversed(range(4))))
        self.assertEqual(dict(instance.signal_bindings)["A3"], "SW3")

    def test_nested_calls_and_local_wire_banks_preserve_scoped_bindings(self):
        source = PASS + """chip Twice(A[3:0],Y[3:0]) {
            A[3:0]=input;Y[3:0]=output;mid[3:0]=wire;
            Pass(A[3:0],mid[3:0]);Pass(mid[3:0],Y[3:0]);
        }
        main{SW[3:0]=input;LEDR[3:0]=output;Twice(SW[3:0],LEDR[3:0]);}
        """
        sim = self.assert_copy(source)
        session = CircuitSession(source=source)
        session.compile()
        self.assertFalse(session.issues)
        parent, first, second = session.drawing.instances
        self.assertEqual((parent.parent, first.parent, second.parent), (None, 0, 0))
        parent_names, first_names, second_names = map(lambda item: dict(item.signal_bindings), (parent, first, second))
        for bit in range(4):
            internal = parent_names[f"mid{bit}"]
            self.assertEqual(first_names[f"Y{bit}"], internal)
            self.assertEqual(second_names[f"A{bit}"], internal)
            self.assertIn(internal, sim.signals)
            self.assertEqual(sim.signal_kinds[internal], "wire")

    def test_nonzero_range_offsets_and_single_member_range(self):
        source = """chip Pass(A[1:0],Y[1:0]){A[1:0]=input;Y[1:0]=output;Y[1:0]=A[1:0];}
            main{SW[3:2]=input;LEDR[1:0]=output;Pass(SW[3:2],LEDR[1:0]);}
        """
        sim = DigitalSimulation(source)
        sim.set_input("SW3", 1)
        self.assertEqual((sim.values["LEDR1"], sim.values["LEDR0"]), (1, 0))
        sim.set_input("SW3", 0)
        sim.set_input("SW2", 1)
        self.assertEqual((sim.values["LEDR1"], sim.values["LEDR0"]), (0, 1))
        single = "chip One(a,y){a=input;y=output;y=a;} main{SW[3:3]=input;LEDR[1:1]=output;One(SW[3:3],LEDR[1:1]);}"
        sim = DigitalSimulation(single)
        sim.set_input("SW3", 1)
        self.assertEqual(sim.values["LEDR1"], 1)

    def test_real_vector_arguments_still_use_the_whole_signal(self):
        definition = "chip Bus(a,y){a[4]=input;y[4]=output;y=a;}"
        sim = DigitalSimulation(definition + "main{x[4]=input;y[4]=output;Bus(x,y);}")
        sim.set_input("x", 11)
        self.assertEqual(sim.values["y"], 11)
        with self.assertRaisesRegex(CircuitError, "declared vector by name"):
            export_verilog(definition + "main{x[4]=input;y[4]=output;Bus(x[3:0],y);}")
        with self.assertRaisesRegex(CircuitError, "declared vector by name"):
            export_verilog(definition + "chip Outer(a,y){a[4]=input;y[4]=output;Bus(a[3:0],y);} main{x[4]=input;y[4]=output;Outer(x,y);}")

    def test_scalar_wire_bank_visibility_in_chip_only_simulation(self):
        source = """chip Forward(a[1:0],y[1:0]) {
            a[1:0]=input;y[1:0]=output;mid[1:0]=wire;
            mid[1:0]=a[1:0];y[1:0]=mid[1:0];
        }"""
        sim = DigitalSimulation(source)
        self.assertEqual((sim.signals["mid0"], sim.signals["mid1"]), (1, 1))
        self.assertEqual(sim.signal_kinds["mid1"], "wire")
        sim.set_input("a1", 1)
        self.assertEqual((sim.values["mid1"], sim.values["y1"]), (1, 1))

    def test_bad_call_ranges_missing_pins_and_duplicate_declarations_are_rejected(self):
        for main in ("SW[3:0]=input;LEDR[3:0]=output;Pass(SW[0:3],LEDR[3:0]);",
                     "SW[2:0]=input;LEDR[3:0]=output;Pass(SW[3:0],LEDR[3:0]);",
                     "SW[3:0],SW3=input;LEDR[3:0]=output;Pass(SW[3:0],LEDR[3:0]);"):
            with self.subTest(main=main), self.assertRaises(CircuitError):
                export_verilog(PASS + "main{" + main + "}")


if __name__ == "__main__":
    unittest.main()
