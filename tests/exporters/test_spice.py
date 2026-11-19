"""Regression tests for the LTspice netlist produced from circuit source."""
# Check exported names, model terminals, source polarity, and scoped ground identities.
# A file that parses as SPICE is insufficient if it silently changes the electrical graph.

import unittest
from math import tau
from pathlib import Path

from circuitlab.model.graph import CircuitError
from circuitlab.exporters.spice import export_spice
from circuitlab.exporters.verilog import export_verilog
from circuitlab.language.syntax import has_chip

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class SpiceExportTest(unittest.TestCase):
    def test_powered_opamp_keeps_explicit_rails_without_changing_the_ideal_model(self):
        netlist = export_spice(
            'V1(2) between(input,0);\nR1(10k) between(input,minus);\n'
            'R2(10k) between(out,minus);\nop1(0,minus,out,vcc,vee);\n'
            'op2(out,buffered,buffered);'
        )
        self.assertIn('X_op1 out 0 0 minus vcc vee EC_IdealOpAmpPowered\n', netlist)
        self.assertIn('.subckt EC_IdealOpAmpPowered out return plus minus vplus vminus\n', netlist)
        self.assertIn('X_op2 buffered 0 out buffered EC_IdealOpAmp\n', netlist)
        self.assertIn('.subckt EC_IdealOpAmp out return plus minus\n', netlist)

    def test_mechanical_switch_exports_its_selected_state_without_control_pins(self):
        for state, resistance in (("closed", "0.001"), ("open", "1000000000000")):
            with self.subTest(state=state):
                netlist = export_spice(
                    'V1(5) between(input,0);\n'
                    f'S1({state}) between(input,out);\nR1(1k) between(out,0);'
                )
                self.assertIn(f"R_S1 input out {resistance}\n", netlist)
                self.assertIn(f"Mechanical switch 'S1' is {state}", netlist)
                self.assertNotIn(".model", netlist)
                self.assertNotIn("EC_SW", netlist)

    def test_controlled_switch_keeps_its_four_terminal_spice_model(self):
        netlist = export_spice(
            'V1(5) between(control,0);\nV2(2) between(input,0);\n'
            'switch1(control,0,input,out,2.5,10,1G);\nR1(1k) between(out,0);'
        )
        self.assertIn("switch1 input out control 0 EC_SW1\n", netlist)
        self.assertIn(".model EC_SW1 SW(Ron=10 Roff=1000000000 Vt=2.5", netlist)

    def test_current_splitter_preserves_polarity_and_parallel_wiring(self):
        netlist = export_spice("""
        I1 = 10m : wire = in;
        main {
            parallel {
                R1 = 2k;
                R2 = 4k;
                wire = out;
                R3 = 12k;
            }
        }
        """)
        self.assertEqual(
            netlist,
            "* Electric Circuits SPICE export\n"
            "* Wire alias 'out' = in\n"
            "I1 0 in DC 0.01\n"
            "R1 in 0 2000\n"
            "R2 in 0 4000\n"
            "R3 in 0 12000\n\n"
            ".op\n.end\n",
        )

    def test_methods_preserve_scopes_and_avoid_case_insensitive_collisions(self):
        netlist = export_spice("""
        V1 = 12 : wire = Vin;
        circuit leg(input, output) {
            R1(1k) between(input, tap);
            R2(1k) between(tap, output);
        }
        main {
            leg(Vin, a);
            leg(Vin, A);
            RloadA(2k) between(a, 0);
            RloadB(3k) between(A, 0);
        }
        """)
        self.assertIn("R1_leg_1 Vin leg_1_tap 1000\n", netlist)
        self.assertIn("R2_leg_1 leg_1_tap a 1000\n", netlist)
        self.assertIn("R1_leg_2 Vin leg_2_tap 1000\n", netlist)
        self.assertIn("R2_leg_2 leg_2_tap A_2 1000\n", netlist)
        self.assertIn("RloadA a 0 2000\nRloadB A_2 0 3000\n", netlist)

    def test_named_ground_domains_preserve_separate_returns(self):
        netlist = export_spice("""
        battery V1 { V = 6; wire = vin; ground = agnd; }
        R1 vin agnd 1000
        R2 vin bgnd 1000
        R3 bgnd agnd 1000
        battery V2 { V = 12; wire = other; ground = cgnd; }
        R4 other cgnd 1000
        """)
        self.assertIn("V1 vin agnd DC 6\n", netlist)
        self.assertIn("V2 other cgnd DC 12\n", netlist)
        self.assertIn("R2 vin bgnd 1000\nR3 bgnd agnd 1000\n", netlist)
        refs = [line for line in netlist.splitlines() if line.startswith("V_EC_ref")]
        self.assertEqual(
            [line.split()[1:] for line in refs],
            [["agnd", "0", "DC", "0"], ["cgnd", "0", "DC", "0"]],
        )

    def test_mixed_export_preserves_rms_phase_and_exact_opamp_model(self):
        netlist = export_spice("""
        V1 in 0 4 V | 2<-90 Vrms
        I1 0 out 1mA | 0.002<90 A
        w = 1000
        R1 in filtered 1k
        C1 filtered 0 1u
        L1 out load 10m
        R2 load 0 1k
        op1(filtered, out, out);
        """)
        self.assertIn("V1 in 0 DC 4 AC 2 -90\n", netlist)
        self.assertIn("I1 0 out DC 0.001 AC 0.002 90\n", netlist)
        self.assertIn("L1 out load 0.01 Rser=0\n", netlist)
        self.assertIn("X_op1 out 0 filtered out EC_IdealOpAmp\n", netlist)
        self.assertIn("Eoutput out return out mid 1\n", netlist)
        self.assertIn("Einput mid return plus minus 1\n", netlist)
        command = next(line for line in netlist.splitlines() if line.startswith(".ac"))
        self.assertAlmostEqual(float(command.split()[-1]), 1000 / tau)

    def test_legacy_groups_export_as_a_network(self):
        netlist = export_spice("""
        V = 12
        (series)
        R1 = 2k
        R2 = 1k
        (end series)
        """)
        self.assertIn("Vsupply in 0 DC 12\n", netlist)
        self.assertIn("R1 in g1_1 2000\nR2 g1_1 0 1000\n", netlist)

    def test_reactance_is_preserved_at_the_selected_frequency(self):
        netlist = export_spice("""
        f = 1000
        V = 2
        (series)
        X1 = 50
        X2 = -25
        (end series)
        """)
        parts = {
            line.split()[0]: line.split()[3]
            for line in netlist.splitlines()
            if line.startswith(("L_", "C_"))
        }
        self.assertAlmostEqual(tau * 1000 * float(parts["L_X1"]), 50)
        self.assertAlmostEqual(-1 / (tau * 1000 * float(parts["C_X2"])), -25)

    def test_export_reports_inputs_without_a_runnable_graph_or_frequency(self):
        with self.assertRaisesRegex(CircuitError, "requires a DC or AC circuit"):
            export_spice("V = 12\nR = 1k")
        with self.assertRaisesRegex(CircuitError, "Enter a frequency"):
            export_spice("V1 in 0 1 Vrms\nC1 in 0 1u")

    def test_regression_fixtures_use_the_correct_exporter(self):
        paths = sorted(FIXTURES.rglob("*.ckt"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(fixture=path.relative_to(FIXTURES)):
                source = path.read_text(encoding="utf-8-sig")
                if has_chip(source):
                    self.assertTrue(export_verilog(source).endswith("endmodule\n"))
                    with self.assertRaisesRegex(CircuitError, "Export Verilog"):
                        export_spice(source)
                else:
                    self.assertTrue(export_spice(source).endswith(".end\n"))
