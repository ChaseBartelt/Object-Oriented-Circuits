"""Diagram metadata binds source signals to the existing runtime's real slots."""
# Use compiler metadata to identify the real runtime slots behind visible diagram names.
# Nested instances and renamed signals must not share state merely because captions resemble.

import unittest

from circuitlab.math.digital import DigitalSimulation
from circuitlab.tooling.session import CircuitSession


SOURCE = """chip HYBRID01(MAX10_CLK1_50,SW0,LEDR0) {
    MAX10_CLK1_50,SW0=input; LEDR0=output; held;
    clock(MAX10_CLK1_50,50MHz) {held=SW0;}
    LEDR0=held;
}"""


class DigitalBindingsTest(unittest.TestCase):
    def test_native_core_names_have_identity_bindings(self):
        sim = DigitalSimulation(SOURCE)
        self.assertTrue(all(source == actual for source, actual in sim.source_bindings.items()))
        self.assertEqual(sim.source_bindings["held"], "held")
        sim.set_input("SW0", 1)
        sim.step()
        self.assertEqual(sim.values[sim.source_bindings["held"]], 1)

    def test_board_core_ports_bind_to_real_synchronizer_outputs(self):
        sim = DigitalSimulation(SOURCE, "de10-lite")
        self.assertEqual(sim.source_bindings["SW0"], "ooc_sw0_sync")
        self.assertEqual(sim.source_bindings["held"], "ooc_logic.held")
        self.assertEqual(sim.source_bindings["MAX10_CLK1_50"], "MAX10_CLK1_50")
        self.assertEqual(sim.source_bindings["LEDR0"], "LEDR0")
        sim.set_input("SW0", 1)
        for expected_input, expected_output in ((0, 0), (1, 0), (1, 1)):
            sim.step()
            self.assertEqual(sim.values[sim.source_bindings["SW0"]], expected_input)
            self.assertEqual(sim.values[sim.source_bindings["held"]], expected_output)

    def test_manual_synchronizer_core_stays_unwrapped(self):
        source = """chip HYBRID01(MAX10_CLK1_50,SW0,LEDR0) {
            MAX10_CLK1_50,SW0=input; LEDR0=output; sw_meta; sw_sync;
            clock(MAX10_CLK1_50,50MHz){sw_meta=SW0;sw_sync=sw_meta;}
            LEDR0=sw_sync;
        }"""
        sim = DigitalSimulation(source, "de10-lite")
        self.assertTrue(all(source == actual for source, actual in sim.source_bindings.items()))
        self.assertEqual(sim.source_bindings["SW0"], "SW0")
        sim.set_input("SW0", 1)
        sim.advance(2)
        self.assertEqual(sim.values[sim.source_bindings["sw_sync"]], 1)

    def test_clockless_board_source_has_identity_bindings_and_live_internal_wires(self):
        source = "chip HYBRID01(SW0,LEDR0){SW0=input;LEDR0=output;inverted=wire;inverted=!SW0;LEDR0=inverted;}"
        sim = DigitalSimulation(source, "de10-lite")
        self.assertTrue(all(source == actual for source, actual in sim.source_bindings.items()))
        self.assertEqual(sim.source_bindings["SW0"], "SW0")
        self.assertEqual(sim.source_bindings["inverted"], "inverted")
        self.assertEqual(sim.values["LEDR0"], 1)
        sim.set_input("SW0", 1)
        self.assertEqual(sim.values[sim.source_bindings["inverted"]], 0)
        self.assertEqual(sim.values["LEDR0"], 0)
        self.assertEqual(sim.cycles, 0)

    def test_instance_binding_composes_with_board_runtime_binding(self):
        source = """chip Sample(clk,a,y) {
            clk,a=input; y=output; held;
            clock(clk,50MHz){held=a;} y=held;
        }
        main HYBRID01 {
            MAX10_CLK1_50,SW0,SW1=input; LEDR0,LEDR1=output;
            Sample(MAX10_CLK1_50,SW0,LEDR0); Sample(MAX10_CLK1_50,SW1,LEDR1);
        }"""
        session = CircuitSession(source=source)
        session.compile()
        self.assertFalse(session.issues)
        sim = DigitalSimulation(source, "de10-lite")
        bindings = [{local: sim.source_bindings[global_name]
                     for local, global_name in instance.signal_bindings}
                    for instance in session.drawing.instances]
        self.assertEqual([mapping["a"] for mapping in bindings], ["ooc_sw0_sync", "ooc_sw1_sync"])
        self.assertNotEqual(bindings[0]["held"], bindings[1]["held"])
        sim.set_input("SW0", 1)
        sim.advance(3)
        self.assertEqual([sim.values[mapping["held"]] for mapping in bindings], [1, 0])


if __name__ == "__main__":
    unittest.main()
