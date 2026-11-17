"""Three-terminal potentiometers through compilation and native numerical solvers."""

from dataclasses import replace
import math
from pathlib import Path
import unittest

import numpy as np

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.analysis import analyze, solve_input
from circuitlab.math.linear import NodalBackend
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.math.report import component_labels, pin_rows, result_rows
from circuitlab.model.graph import CircuitError
from circuitlab.model.potentiometer import expand_potentiometers


DIVIDER = "V1(10) between(high,0);\nP1(200,25%) between(high,wiper,0);"


def voltages(network):
    return dict(zip(network.nodes, network.voltage))


class PotentiometerTest(unittest.TestCase):
    def test_optional_value_position_units_and_terminal_order(self):
        for raw, value, position in (("", None, .5), ("()", None, .5),
                                     ("(,25%)", None, .25), ("(4.7k)", 4700, .5),
                                     ("(200 ohm,25%)", 200, .25), ("(200,.75)", 200, .75)):
            with self.subTest(raw=raw):
                circuit = compile_circuit(f"pVolume{raw} between(high,wiper,low);")
                part, = circuit.components
                self.assertEqual(part.kind, "P")
                self.assertEqual(part.terminal_nets, ("high", "wiper", "low"))
                self.assertIn("wiper", circuit.nodes)
                self.assertEqual((part.value, part.position), (value, position))
                self.assertIn("%", component_labels(circuit)[part.name][1])

    def test_invalid_values_and_positions_retain_source_line(self):
        for raw in ("0", "-10", "10+j2", "1e999", "10F", "200,-1%", "200,101%",
                    "200,j1", "200,1e999", "200,25V", "200,", "200,25%,50%"):
            with self.subTest(raw=raw), self.assertRaises(CircuitError) as failure:
                compile_circuit(f"# volume\nP1({raw}) between(high,wiper,0);")
            self.assertEqual(failure.exception.line, 2)

    def test_placeholders_render_but_never_become_zero_or_open_circuits(self):
        circuit = compile_circuit(DIVIDER.replace("(200,25%)", ""), render_only=True)
        for solve in (analyze, lambda c: NodalBackend().solve(c, ac=False), TimeDomain):
            with self.subTest(solve=solve), self.assertRaises(CircuitError) as failure:
                solve(circuit)
            self.assertEqual(failure.exception.code, "MissingComponentValue")

    def test_unloaded_divider_and_loaded_terminal_currents_and_power(self):
        unloaded = solve_input(DIVIDER).network
        self.assertAlmostEqual(voltages(unloaded)["wiper"], 2.5)
        self.assertAlmostEqual(unloaded.branch_current[1], .05)
        self.assertAlmostEqual(unloaded.branch_power[1], .5)
        result = solve_input(DIVIDER + "\nRload(50) between(wiper,0);\nmeasure = I P1;\nmeasure = P P1;")
        net = result.network
        self.assertEqual(net.names, ("V1", "P1", "Rload"))
        self.assertAlmostEqual(voltages(net)["wiper"], 10 / 7)
        np.testing.assert_allclose(net.branch_current, (-2 / 35, 2 / 35, 1 / 35))
        np.testing.assert_allclose(net.terminal_current["P1"], (2 / 35, -1 / 35, -1 / 35))
        self.assertAlmostEqual(sum(net.terminal_current["P1"]), 0)
        self.assertAlmostEqual(net.branch_power[1], 26 / 49)
        self.assertAlmostEqual(sum(net.branch_power), 0)
        self.assertAlmostEqual(result.readings[0][2], 2 / 35)
        self.assertAlmostEqual(result.readings[1][2], 26 / 49)
        self.assertTrue(any("P1 wiper current" in row[0] for row in pin_rows(result, {"wiper": "Tap"})))
        self.assertTrue(any("P1 low current" in row[0] for row in result_rows(result)))

    def test_ac_capacitive_loading_matches_complex_divider(self):
        source = DIVIDER.replace("V1(10)", "V1(10 Vrms)") + "\nf=1k;\nC1(1u) between(wiper,0);"
        net = solve_input(source).network
        lower = 1 / (1 / 50 + 1j * 2 * math.pi * 1000 * 1e-6)
        expected = 10 * lower / (150 + lower)
        self.assertAlmostEqual(voltages(net)["wiper"], expected)
        high, wiper, low = net.terminal_current["P1"]
        self.assertAlmostEqual(high, (10 - expected) / 150)
        self.assertAlmostEqual(low, -expected / 50)
        self.assertAlmostEqual(wiper, -(high + low))
        self.assertAlmostEqual(net.branch_power[1], abs(high) ** 2 * 150 + abs(low) ** 2 * 50)
        self.assertAlmostEqual(sum(net.branch_power), 0)

    def test_mixed_ac_dc_results_preserve_original_component_indices(self):
        source = DIVIDER.replace("V1(10)", "V1(4 V | 2 Vrms)") + "\nf=1k;\nRload(50) between(wiper,0);"
        result = solve_input(source)
        self.assertEqual(result.circuit.mode, "mixed")
        for net, supply in ((result.dc_network, 4), (result.network, 2)):
            self.assertEqual(net.names, ("V1", "P1", "Rload"))
            self.assertAlmostEqual(voltages(net)["wiper"], supply / 7)
            self.assertAlmostEqual(net.branch_current[2], supply / 350)
        self.assertTrue(result_rows(result))

    def test_end_stops_are_exact_shorts_for_dc_ac_and_time_domain(self):
        for position, expected in ((0, 0), (100, 10)):
            source = DIVIDER.replace("25%", f"{position}%") + "\nRload(50) between(wiper,0);"
            for ac in (False, True):
                with self.subTest(position=position, ac=ac):
                    circuit = compile_circuit(source.replace("V1(10)", "V1(10 Vrms)") if ac else source)
                    parts, _ = expand_potentiometers(circuit)
                    short, = [p for p in parts if p.kind == "V" and p.name != "V1"]
                    self.assertEqual(short.value, 0)
                    self.assertFalse(short.is_source)
                    net = analyze(circuit).network
                    self.assertAlmostEqual(voltages(net)["wiper"], expected)
                    self.assertAlmostEqual(sum(net.branch_power), 0)
                    if not ac:
                        self.assertAlmostEqual(voltages(TimeDomain(circuit).operating_point())["wiper"], expected)

    def test_wiper_tied_to_end_is_a_rheostat_without_redundant_short(self):
        for wiper, resistance in (("high", 50), ("0", 150)):
            source = DIVIDER.replace("between(high,wiper,0)", f"between(high,{wiper},0)")
            circuit = compile_circuit(source)
            for net in (analyze(circuit).network, TimeDomain(circuit).operating_point()):
                self.assertAlmostEqual(net.branch_power[1], 100 / resistance)
                self.assertAlmostEqual(net.branch_current[0], -10 / resistance)
        for position, wiper in ((0, "0"), (100, "high")):
            circuit = compile_circuit(DIVIDER.replace("25%", f"{position}%").replace(
                "between(high,wiper,0)", f"between(high,{wiper},0)"))
            self.assertAlmostEqual(analyze(circuit).network.branch_power[1], .5)

    def test_joined_end_pins_have_zero_end_voltage_but_positive_leg_power(self):
        net = solve_input("V1(10) between(wiper,0);\nP1(200,25%) between(0,wiper,0);").network
        self.assertAlmostEqual(net.branch_voltage[1], 0)
        self.assertAlmostEqual(net.branch_power[1], 100 / 150 + 100 / 50)
        self.assertAlmostEqual(sum(net.branch_power), 0)

    def test_transient_matches_explicit_legs_and_rc_charging(self):
        source = DIVIDER + "\nC1(10u) between(wiper,0);"
        explicit = source.replace("P1(200,25%) between(high,wiper,0);",
                                  "Ra(150) between(high,wiper);\nRb(50) between(wiper,0);")
        step, tau, initial = 1e-4, 37.5 * 10e-6, 2
        sims = [TimeDomain(compile_circuit(text), step) for text in (source, explicit)]
        for sim in sims:
            sim.set_initial_voltage("C1", initial)
        for count in range(1, 21):
            values = [sim.advance()[sim.nodes.index("wiper")] for sim in sims]
            self.assertAlmostEqual(values[0], values[1])
            self.assertAlmostEqual(values[0], 2.5 + (initial - 2.5) / (1 + step / tau) ** count)

    def test_nonlinear_bjt_bias_and_base_current_match_explicit_legs(self):
        source = "V1(5) between(vcc,0);\nP1(10k,25%) between(vcc,b,0);\nRc(1k) between(vcc,c);\ntransQ1(b,c,0,npn);"
        explicit = source.replace("P1(10k,25%) between(vcc,b,0);",
                                  "Ra(7.5k) between(vcc,b);\nRb(2.5k) between(b,0);")
        pot, reference = [solve_input(text).network for text in (source, explicit)]
        for node in ("vcc", "b", "c"):
            self.assertAlmostEqual(voltages(pot)[node], voltages(reference)[node])
        self.assertAlmostEqual(pot.base_current["Q1"], reference.base_current["Q1"])
        self.assertAlmostEqual(pot.terminal_current["P1"][1], -pot.base_current["Q1"])
        self.assertEqual(pot.names, ("V1", "P1", "Rc", "Q1"))

    def test_generated_names_cannot_collide_with_user_components(self):
        circuit = compile_circuit(DIVIDER + "\nR_P1_high(1k) between(wiper,0);\nV_P1_low(1) between(other,0);")
        parts, _ = expand_potentiometers(circuit)
        self.assertEqual(len(parts), len({p.name.casefold() for p in parts}))
        self.assertIn("R_P1_high_2", {p.name for p in parts})
        self.assertEqual(analyze(circuit).network.names, tuple(p.name for p in circuit.components))

    def test_scoped_component_parameters_and_linked_files_keep_wiper_nets(self):
        circuit = compile_circuit("""circuit volume(part,h,w,l) { part between(h,w,l); }
V1(10) between(high,0);
main { volume(P(200,25%),high,left,0); volume(P(200,75%),high,right,0); }
""")
        self.assertEqual([p.position for p in circuit.components if p.kind == "P"], [.25, .75])
        values = voltages(analyze(circuit).network)
        self.assertAlmostEqual(values["left"], 2.5)
        self.assertAlmostEqual(values["right"], 7.5)
        source = 'include("volume.ckt");\nconnector(high);\nV1(10) between(high,0);'
        child = 'connector(high);\nP1(200,25%) between(high,private,0);'
        circuit = compile_circuit(source, path=Path.cwd() / "main.ckt", read_source=lambda _: child)
        pot, = [p for p in circuit.components if p.kind == "P"]
        self.assertEqual(pot.wiper, "@file1.private")
        self.assertAlmostEqual(voltages(analyze(circuit).network)[pot.wiper], 2.5)

    def test_programmatic_pots_validate_total_and_position(self):
        circuit = compile_circuit(DIVIDER)
        for changes in ({"value": 0}, {"value": -1}, {"value": complex(1, 2)},
                        {"value": float("inf")}, {"position": float("nan")},
                        {"position": -1}, {"position": 2}, {"wiper": ""}):
            with self.subTest(changes=changes), self.assertRaises(CircuitError):
                expand_potentiometers(replace(circuit, components=[circuit.components[0],
                    replace(circuit.components[1], **changes)]))

    def test_existing_power_quantity_and_grouped_current_probe_still_work(self):
        result = solve_input("V=10\nR=200")
        self.assertAlmostEqual(result.calculated["P"], .5)
        result = solve_input("V1(10) between(high,0);\nmain { series(high,0) { R1=200; measure=Ix; } }")
        self.assertFalse(any(p.kind == "P" for p in result.circuit.components))
        self.assertTrue(any(p.kind == "V" and not p.is_source for p in result.circuit.components))
        self.assertTrue(any(abs(i - .05) < 1e-9 for i in result.network.branch_current))


if __name__ == "__main__":
    unittest.main()
