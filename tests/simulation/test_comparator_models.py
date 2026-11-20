"""Configured comparators keep open collectors and model propagation latency."""

from dataclasses import replace
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.analysis import analyze
from circuitlab.math.comparators import expand_comparators
from circuitlab.math.linear import NodalBackend
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.math.report import pin_rows, result_rows
from circuitlab.model.graph import CircuitError, Component, MathEquation, Probe, WireTap


def comparator_source(name, signal="0.5V", *, pull="Rpull(1k) between(pos,out);", emitter="0"):
    """Give every package channel a real return and external output pull-up."""
    calls = {
        "LM311": f"LM311({emitter},input,reference,0,balance,strobe,out,pos);",
        "LM393": "LM393(out,reference,input,0,reference,reference,out2,pos);",
        "LM339": ("LM339(out,out2,pos,reference,reference,reference,input,reference,reference,"
                  "reference,reference,0,out4,out3);"),
    }
    extra = ""
    if name == "LM311" and emitter != "0":
        extra += f"Vemitter(1V) between({emitter},0);\n"
    if name in {"LM393", "LM339"}:
        extra += "Rpull2(10k) between(pos,out2);\n"
    if name == "LM339":
        extra += "Rpull3(10k) between(pos,out3);\nRpull4(10k) between(pos,out4);\n"
    return (f'include("{name}");\nVpower(5V) between(pos,0);\n'
            f"Vin({signal}) between(input,0);\nVreference(1V) between(reference,0);\n"
            f"{extra}{pull}\nmain {{{calls[name]}}}\n")


def voltage(network, node="out"):
    return network.voltage[network.nodes.index(node)].real


def sample(sim, node="out"):
    values = sim.advance()
    return values[sim.nodes.index(node)].real


def change_source(sim, source):
    return sim.reconfigure(compile_circuit(source))


def first_crossing(sim, *, high, node="out", threshold=2.5, timeout=3e-6):
    began = sim.time
    samples = int(timeout / sim.step) + 2
    for _ in range(samples):
        value = sample(sim, node)
        if (value > threshold) == high:
            return sim.time - began
    raise AssertionError(f"{node} never switched {'high' if high else 'low'} within {timeout:g} s")


def pulse_edges(sim, duration):
    """Observe input/output crossings from the actual solved pulse waveform."""
    input_edges, output_edges = [], []
    previous_input = previous_output = None
    for _ in range(round(duration / sim.step)):
        values = sim.advance()
        input_high = (values[sim.nodes.index("input")].real + .002
                      >= values[sim.nodes.index("reference")].real)
        output_high = values[sim.nodes.index("out")].real > 2.5
        if previous_input is not None and input_high != previous_input:
            input_edges.append((bool(input_high), sim.time))
        if previous_output is not None and output_high != previous_output:
            output_edges.append((bool(output_high), sim.time))
        previous_input, previous_output = input_high, output_high
    return input_edges, output_edges


class ComparatorModelTest(unittest.TestCase):
    def assert_delay(self, measured, expected, step):
        # Input changes are observed on endpoints; the deadline can likewise
        # land between endpoints. Keep tolerance tied to sampling resolution.
        self.assertGreaterEqual(measured, expected - step / 4)
        self.assertLessEqual(measured, expected + 2.1 * step)

    def test_all_three_library_models_compile_and_solve_both_output_levels(self):
        for name in ("LM311", "LM339", "LM393"):
            for signal, high in (("0.5V", False), ("1.5V", True)):
                with self.subTest(chip=name, signal=signal):
                    circuit = compile_circuit(comparator_source(name, signal))
                    self.assertFalse(circuit.model_issues)
                    for network in (analyze(circuit).network, TimeDomain(circuit).operating_point()):
                        self.assertGreater(voltage(network), 4.9) if high else self.assertLess(voltage(network), .5)
                        self.assertEqual(network.names, tuple(part.name for part in circuit.components))

    def test_open_collector_cannot_drive_high_without_an_external_pullup(self):
        for name in ("LM311", "LM339", "LM393"):
            with self.subTest(chip=name):
                circuit = compile_circuit(comparator_source(name, "1.5V", pull=""))
                network = analyze(circuit).network
                self.assertAlmostEqual(voltage(network), 0, places=6)

    def test_low_level_calibration_is_the_drop_at_four_milliamps(self):
        for name, expected in (("LM339", .150), ("LM393", .130)):
            with self.subTest(chip=name):
                source = comparator_source(name, pull="Ipull(4mA) between(pos,out);")
                circuit = compile_circuit(source)
                for network in (NodalBackend().solve(circuit, ac=False), TimeDomain(circuit).operating_point()):
                    self.assertAlmostEqual(voltage(network), expected, places=5)

    def test_lm311_collector_returns_to_its_emitter_not_implicitly_to_ground(self):
        source = comparator_source("LM311", emitter="emitter")
        source = source.replace("Vreference(1V)", "Vreference(2V)")
        circuit = compile_circuit(source)
        network = analyze(circuit).network
        self.assertGreater(voltage(network), 1)
        self.assertLess(voltage(network), 1.5)
        high = source.replace("Vin(0.5V)", "Vin(3V)")
        self.assertGreater(voltage(analyze(compile_circuit(high)).network), 4.9)

    def test_input_offset_is_applied_before_the_comparison(self):
        # The library's +2 mV input-referred offset makes 0.999 V exceed
        # the 1 V reference, while 0.997 V remains below it.
        for signal, high in (("0.999V", True), ("0.997V", False)):
            with self.subTest(signal=signal):
                network = analyze(compile_circuit(comparator_source("LM393", signal))).network
                self.assertGreater(voltage(network), 4.9) if high else self.assertLess(voltage(network), .5)

    def test_chip_bias_current_is_drawn_once_in_addition_to_external_pullups(self):
        for name, total in (("LM339", .0008), ("LM393", .00045)):
            with self.subTest(chip=name):
                circuit = compile_circuit(comparator_source(name))
                for network in (analyze(circuit).network, TimeDomain(circuit).operating_point()):
                    currents = dict(zip(network.names, network.branch_current))
                    pullup_draw = sum(value for key, value in currents.items() if key.startswith("Rpull"))
                    self.assertAlmostEqual((-currents["Vpower"] - pullup_draw).real, total, places=10)

    def test_lm311_has_distinct_collector_rise_and_fall_delays(self):
        step = 5e-9
        low, high = comparator_source("LM311"), comparator_source("LM311", "1.5V")
        sim = TimeDomain(compile_circuit(low), step)
        self.assertLess(sample(sim), .5)
        sim = change_source(sim, high)
        rise = first_crossing(sim, high=True, timeout=400e-9)
        self.assert_delay(rise, 115e-9, step)
        sim = change_source(sim, low)
        fall = first_crossing(sim, high=False, timeout=400e-9)
        self.assert_delay(fall, 165e-9, step)
        self.assertGreater(fall, rise)

    def test_lm311_rise_and_fall_delays_follow_a_real_pulse_source(self):
        step = 5e-9
        source = comparator_source("LM311", "pulse(0.5V,1.5V,100ns,20ns,20ns,300ns,1us)")
        sim = TimeDomain(compile_circuit(source), step)
        input_edges, output_edges = pulse_edges(sim, 2e-6)
        self.assertEqual([high for high, _ in input_edges], [True,False,True,False])
        self.assertEqual([high for high, _ in output_edges], [True,False,True,False])
        # This also verifies that the source itself crosses after its declared
        # start, rather than simply testing a reconfigured DC source again.
        self.assertGreater(input_edges[0][1], 100e-9)
        self.assertLessEqual(input_edges[0][1], 120e-9)
        for (input_high, input_time), (output_high, output_time) in zip(input_edges, output_edges):
            self.assertEqual(input_high, output_high)
            self.assert_delay(output_time-input_time,115e-9 if input_high else 165e-9,step)

    def test_lm393_response_time_applies_to_each_edge_of_a_real_pulse_source(self):
        step = 25e-9
        source = comparator_source("LM393", "pulse(0.5V,1.5V,1us,100ns,100ns,3us,10us)")
        sim = TimeDomain(compile_circuit(source), step)
        input_edges, output_edges = pulse_edges(sim, 16e-6)
        self.assertEqual([high for high, _ in input_edges], [True,False,True,False])
        self.assertEqual([high for high, _ in output_edges], [True,False,True,False])
        for (input_high, input_time), (output_high, output_time) in zip(input_edges, output_edges):
            self.assertEqual(input_high, output_high)
            self.assert_delay(output_time-input_time,1.3e-6,step)

    def test_response_time_is_the_fallback_for_both_edge_directions(self):
        step = 25e-9
        for name in ("LM339", "LM393"):
            with self.subTest(chip=name):
                low, high = comparator_source(name), comparator_source(name, "1.5V")
                sim = TimeDomain(compile_circuit(low), step)
                self.assertLess(sample(sim), .5)
                sim = change_source(sim, high)
                self.assert_delay(first_crossing(sim, high=True), 1.3e-6, step)
                sim = change_source(sim, low)
                self.assert_delay(first_crossing(sim, high=False), 1.3e-6, step)

    def test_explicit_directional_delay_overrides_response_time_only_for_that_direction(self):
        patch = "@override\nchip LM393 {analog oocCollectorComparator {delay.rise=250ns;}}\n"
        low = comparator_source("LM393") + patch
        high = comparator_source("LM393", "1.5V") + patch
        step = 25e-9
        sim = TimeDomain(compile_circuit(low), step)
        self.assertLess(sample(sim), .5)
        sim = change_source(sim, high)
        self.assert_delay(first_crossing(sim, high=True), 250e-9, step)
        sim = change_source(sim, low)
        self.assert_delay(first_crossing(sim, high=False), 1.3e-6, step)

    def test_a_short_returning_input_pulse_cancels_a_pending_transition(self):
        low, high = comparator_source("LM393"), comparator_source("LM393", "1.5V")
        step = 25e-9
        sim = TimeDomain(compile_circuit(low), step)
        self.assertLess(sample(sim), .5)
        sim = change_source(sim, high)
        for _ in range(8):
            self.assertLess(sample(sim), .5)
        sim = change_source(sim, low)
        for _ in range(80):
            self.assertLess(sample(sim), .5)

    def test_repeated_chips_keep_separate_levels_pending_edges_and_supply_totals(self):
        source = """include("LM393");
            Vpower(5V) between(pos,0);
            Va(0.5V) between(a,0);
            Vb(1.5V) between(b,0);
            Vreference(1V) between(reference,0);
            Rpull1(1k) between(pos,out1);
            Rpull2(1k) between(pos,out2);
            Rpull3(1k) between(pos,out3);
            Rpull4(1k) between(pos,out4);
            main {
                LM393(out1,reference,a,0,a,reference,out2,pos);
                LM393(out3,reference,b,0,b,reference,out4,pos);
            }
        """
        circuit = compile_circuit(source)
        original = tuple((p.name,p.kind,p.terminal_nets) for p in circuit.components)
        sim = TimeDomain(circuit, 25e-9)
        values = sim.advance()
        self.assertLess(values[sim.nodes.index("out1")], .5)
        self.assertGreater(values[sim.nodes.index("out3")], 4.9)
        sim = change_source(sim, source.replace("Va(0.5V)", "Va(1.5V)"))
        for _ in range(10):
            values = sim.advance()
            self.assertLess(values[sim.nodes.index("out1")], .5)
            self.assertGreater(values[sim.nodes.index("out3")], 4.9)
        for _ in range(50):
            values = sim.advance()
        self.assertGreater(values[sim.nodes.index("out1")], 4.9)
        self.assertGreater(values[sim.nodes.index("out3")], 4.9)
        self.assertEqual(tuple((p.name,p.kind,p.terminal_nets) for p in circuit.components), original)
        network = sim.operating_point()
        currents = dict(zip(network.names,network.branch_current))
        pulls = sum(value for name,value in currents.items() if name.startswith("Rpull"))
        self.assertAlmostEqual((-currents["Vpower"]-pulls).real,.0009,places=10)

    def test_negative_or_wrong_unit_timing_has_a_source_diagnostic(self):
        for field in ("delay.rise=-1ns", "delay.fall=1V", "response.time=-2us"):
            source = comparator_source("LM393") + "@override\nchip LM393 {analog oocCollectorComparator {\n" + field + ";\n}}\n"
            with self.subTest(field=field):
                with self.assertRaises(CircuitError) as caught:
                    compile_circuit(source)
                self.assertIsNotNone(caught.exception.line)

    def test_overlay_preserves_display_parts_and_marks_unresolved_interior_values(self):
        circuit = compile_circuit(comparator_source("LM393"))
        display_parts = tuple(circuit.components)
        result = analyze(circuit)
        network = result.network
        self.assertEqual(tuple(circuit.components),display_parts)
        self.assertEqual(network.names,tuple(part.name for part in display_parts))
        self.assertEqual(network.nodes,circuit.simulation_nodes)
        self.assertAlmostEqual(sum(network.branch_power),0)
        for comparator in circuit.comparators:
            self.assertTrue(comparator.internal_nodes <= network.unavailable_nodes)
            self.assertFalse(comparator.internal_nodes.intersection(network.nodes))
            self.assertTrue(set(comparator.members)-{comparator.output_name} <= network.unavailable_components)
            self.assertNotIn(comparator.output_name,network.unavailable_components)
            self.assertIn("open-collector",network.modeled_components[comparator.output_name])
            self.assertAlmostEqual(sum(amps for _,_,amps in network.modeled_terminals[comparator.output_name]),0)
        hidden = next(iter(network.unavailable_nodes))
        self.assertIn("Not resolved",pin_rows(result,{hidden:"interior"})[0][2])
        self.assertTrue(any("macromodel" in str(row) for row in result_rows(result)))

    def test_explicit_internal_probes_reject_placeholder_values_but_output_current_remains_measurable(self):
        circuit = compile_circuit(comparator_source("LM393"))
        comparator = circuit.comparators[0]
        internal_part = next(name for name in comparator.members if name != comparator.output_name)
        internal_node = next(iter(comparator.internal_nodes))
        for probe in (Probe("I",(internal_part,),1),Probe("P",(internal_part,),1),
                      Probe("V",(internal_node,"0"),1)):
            with self.subTest(probe=probe):
                with self.assertRaises(CircuitError) as caught:
                    analyze(replace(circuit,probes=[probe]))
                self.assertEqual(caught.exception.code,"UnavailableModelProbe")
        result = analyze(replace(circuit,probes=[Probe("I",(comparator.output_name,),1)]))
        measured = result.readings[0][2]
        pullup = result.network.branch_current[result.network.names.index("Rpull")]
        self.assertAlmostEqual(measured,pullup)

    def test_external_wiring_or_math_cannot_secretly_reconnect_removed_internals(self):
        source = comparator_source("LM393")
        circuit = compile_circuit(source)
        internal = next(iter(circuit.comparators[0].internal_nodes))
        for extra in (f"Rextra(1G) between({internal},0);",f"analog V(copy)=V({internal});"):
            with self.subTest(extra=extra),self.assertRaises(CircuitError):
                compile_circuit(source+"\n"+extra)

    def test_expansion_rechecks_output_terminals_after_programmatic_graph_changes(self):
        circuit = compile_circuit(comparator_source("LM393"))
        comparator = circuit.comparators[0]
        output = next(part for part in circuit.components if part.name == comparator.output_name)
        for changed in (replace(output,p="other"), replace(output,n="other"),
                        replace(output,kind="R"), replace(output,base="new_interior")):
            with self.subTest(part=changed):
                components = [changed if part.name == output.name else part for part in circuit.components]
                with self.assertRaises(CircuitError) as caught:
                    expand_comparators(replace(circuit,components=components))
                self.assertEqual(caught.exception.code,"ComparatorModel")
                self.assertEqual(caught.exception.line,comparator.line)
                self.assertEqual(caught.exception.path,comparator.path)

    def test_expansion_rechecks_external_access_to_internals_after_compilation(self):
        circuit = compile_circuit(comparator_source("LM393"))
        comparator = circuit.comparators[0]
        internal = next(iter(comparator.internal_nodes))
        # Inspect the expression tree too: a caller can replace it without
        # refreshing the cached inputs used by the normal compiler pipeline.
        equation = MathEquation("copy","copy","0",("v",internal,"0"),())
        changes = (
            replace(circuit,components=[*circuit.components,Component("Rextra","R",internal,"0",1e9)]),
            replace(circuit,behavior=[equation]),
            replace(circuit,behavior=[replace(equation,output=internal,tree=("c",0))]),
            replace(circuit,components=[*circuit.components,Component("Bextra","B","copy","0",behavior=equation)]),
            replace(circuit,connector_pins=(*circuit.connector_pins,internal)),
            replace(circuit,wire_taps=[*circuit.wire_taps,WireTap("extra",internal,comparator.output_name,1)]),
        )
        for modified in changes:
            with self.subTest(components=len(modified.components),behavior=modified.behavior,
                              connectors=modified.connector_pins,taps=modified.wire_taps):
                with self.assertRaises(CircuitError) as caught:
                    expand_comparators(modified)
                self.assertEqual(caught.exception.code,"ComparatorModel")
                self.assertEqual(caught.exception.line,comparator.line)

    def test_expansion_still_accepts_declared_pin_connections_and_public_ports(self):
        circuit = compile_circuit(comparator_source("LM393")+"analog V(copy)=V(out);\n")
        # A requested public port is metadata; the solver separately decides
        # which analyses are available for a stateful comparator model.
        expanded = expand_comparators(replace(circuit,port=("out","0")))
        self.assertEqual(expanded.port,("out","0"))
        self.assertEqual(expanded.behavior,circuit.behavior)
        self.assertFalse(expanded.comparators)
