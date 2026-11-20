"""Rail-relative output constraints affect solving, not just chip previews."""

import math
import unittest

import numpy as np

from circuitlab.exporters.spice import export_spice
from circuitlab.language.compiler import compile_circuit
from circuitlab.math.linear import NodalBackend
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.model.graph import CircuitError, VoltageLimit


def voltage(network, net="out"):
    return network.voltage[network.nodes.index(net)]


class OpampOutputLimitTest(unittest.TestCase):
    def test_ideal_gain_saturates_at_either_rail_and_retains_unsaturated_feedback(self):
        for drive, expected in ((10,4),(-10,-4),(1,1)):
            source = f"""V1({drive}V) between(input,0);
                Vp(5V) between(pos,0);
                Vn(-5V) between(neg,0);
                op1(input,out,out,pos,neg);
                analog op1 {{output.min=vminus+1V; output.max=vplus-1V;}}
            """
            circuit = compile_circuit(source)
            self.assertEqual(circuit.components[-1].opamp.output_min, VoltageLimit(1,0,1))
            for network in (NodalBackend().solve(circuit,ac=False),TimeDomain(circuit).operating_point()):
                with self.subTest(drive=drive):
                    self.assertAlmostEqual(voltage(network),expected)

    def test_open_loop_ideal_gain_has_a_real_saturated_operating_point(self):
        for plus, minus, expected in ((1,0,2),(0,1,-2)):
            circuit = compile_circuit(f"V1({plus}) between(plus,0);\nV2({minus}) between(minus,0);\n"
                                      "op1(plus,minus,out);\nanalog op1 {output.min=-2V; output.max=2V;}")
            self.assertAlmostEqual(voltage(NodalBackend().solve(circuit,ac=False)),expected)

    def test_finite_gain_is_preserved_inside_the_output_interval(self):
        for drive, expected in ((1,100/101),(10,1.5),(-10,-1.5)):
            circuit = compile_circuit(f"V1({drive}) between(input,0);\nop1(input,out,out);\n"
                                      "analog op1 {gain=100; output.min=-1.5V; output.max=1.5V;}")
            for network in (NodalBackend().solve(circuit,ac=False),TimeDomain(circuit).operating_point()):
                self.assertAlmostEqual(voltage(network),expected)

    def test_bundled_lm358_followers_converge_with_shared_supply_current_and_rail_limits(self):
        circuit = compile_circuit('include("LM358");\nVpower(5V) between(pos,0);\n'
                                  'Vsignal(1V) between(input,0);\n'
                                  'main {LM358(out1,out1,input,0,input,out2,out2,pos);}\n')
        for network in (NodalBackend().solve(circuit,ac=False),TimeDomain(circuit).operating_point()):
            self.assertAlmostEqual(voltage(network,"out1"),100000*1.003/100001)
            self.assertAlmostEqual(voltage(network,"out2"),100000*1.003/100001)
            self.assertAlmostEqual(network.branch_current[network.names.index("Vpower")],-.0007)

    def test_saturated_output_current_and_power_still_satisfy_kcl(self):
        circuit = compile_circuit("V1(10V) between(input,0);\nRload(100) between(out,0);\n"
                                  "op1(input,out,out);\nanalog op1 {output.max=2V;}")
        for network in (NodalBackend().solve(circuit,ac=False),TimeDomain(circuit).operating_point()):
            self.assertAlmostEqual(voltage(network),2)
            self.assertAlmostEqual(network.branch_current[network.names.index("op1")],-.02)
            self.assertAlmostEqual(sum(network.branch_power),0)

    def test_bound_tracks_an_unknown_loaded_supply_in_the_same_newton_solve(self):
        circuit = compile_circuit("""V1(10V) between(input,0);
            Vp(5V) between(source,0);
            Rsupply(1k) between(source,pos);
            op1(input,out,out,pos,unused_low);
            analog op1 {supply.current=1mA; output.max=vplus-1V;}
        """.replace("unused_low", "0"))
        for network in (NodalBackend().solve(circuit,ac=False),TimeDomain(circuit).operating_point()):
            self.assertAlmostEqual(voltage(network,"pos"),4)
            self.assertAlmostEqual(voltage(network),3)

    def test_a_one_rail_bound_does_not_create_an_unused_supply_unknown(self):
        circuit = compile_circuit("V1(10V) between(input,0);\nVp(5V) between(pos,0);\n"
                                  "op1(input,out,out,pos,unused_low);\nanalog op1 {output.max=vplus-1V;}")
        self.assertNotIn("unused_low",circuit.nodes)
        self.assertAlmostEqual(voltage(NodalBackend().solve(circuit,ac=False)),4)

    def test_one_sided_limits_work_without_explicit_power_pins(self):
        circuit = compile_circuit("V1(-10V) between(input,0);\nop1(input,out,out);\nanalog op1 {output.min=-2V;}")
        self.assertAlmostEqual(voltage(NodalBackend().solve(circuit,ac=False)),-2)

    def test_pole_history_tracks_the_clipped_output_and_recovers_without_windup(self):
        text = ("V1(10V) between(input,0);\nop1(input,out,out);\n"
                "analog op1 {gbw=100Hz; output.min=0V; output.max=1V;}")
        sim = TimeDomain(compile_circuit(text),.001)
        for _ in range(5):
            self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")],1)
        sim = sim.reconfigure(compile_circuit(text.replace("V1(10V)","V1(.1V)")))
        k = math.tau * 100 * .001
        self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")],(1+k*.1)/(1+k))

    def test_slew_and_bandwidth_combine_with_a_physical_output_bound(self):
        circuit = compile_circuit("V1(10V) between(input,0);\nop1(input,out,out);\n"
                                  "analog op1 {gbw=100Hz; slew=20V/s; output.min=0V; output.max=.5V;}")
        sim = TimeDomain(circuit,.01)
        for expected in (.2,.4,.5,.5):
            self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")],expected)

    def test_a_new_hard_rail_takes_precedence_over_slew_history(self):
        text = ("V1(10V) between(input,0);\nVp(5V) between(pos,0);\nop1(input,out,out,pos,0);\n"
                "analog op1 {slew=10V/s; output.min=0V; output.max=vplus-1V;}")
        sim = TimeDomain(compile_circuit(text),.1)
        for expected in (1,2,3,4):
            self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")],expected)
        sim = sim.reconfigure(compile_circuit(text.replace("Vp(5V)","Vp(2V)")))
        self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")],1)

    def test_reversed_actual_limits_fail_without_committing_transient_state(self):
        circuit = compile_circuit("V1(1V) between(input,0);\nop1(input,out,out);\n"
                                  "analog op1 {output.min=2V; output.max=1V;}")
        sim = TimeDomain(circuit,.01)
        before = sim.state.copy()
        with self.assertRaisesRegex(CircuitError,"output.min exceeds output.max"):
            sim.advance()
        self.assertEqual(sim.time,0)
        np.testing.assert_array_equal(sim.state,before)

    def test_native_ac_and_port_analysis_report_the_missing_bias_linearization(self):
        circuit = compile_circuit("V1(1V) between(input,0);\nop1(input,out,out);\nanalog op1 {output.max=2V;}")
        for options in ({"ac":True,"frequency":1000},{"ac":False,"port":("out","0")}):
            with self.assertRaisesRegex(CircuitError,"output limits require DC or time simulation"):
                NodalBackend().solve(circuit,**options)

    def test_mixed_dc_bias_uses_offsets_instead_of_ac_peak_values(self):
        circuit = compile_circuit("V1(1V | 10Vrms) between(input,0);\nf=1kHz;\n"
                                  "op1(input,out,out);\nanalog op1 {output.max=2V;}")
        self.assertEqual(circuit.mode,"mixed")
        self.assertAlmostEqual(voltage(NodalBackend().solve(circuit,ac=False)),1)

    def test_spice_static_limit_preserves_ideal_gain_without_a_finite_gain_substitute(self):
        text = export_spice("V1(10V) between(input,0);\nop1(input,out,out);\n"
                            "analog op1 {output.min=-2V; output.max=2V;}")
        self.assertIn("Boutput out return V=limit(V(out,return)+V(plus,minus)+(0),(-2),(2))",text)
        self.assertNotIn("1000000*",text)

    def test_spice_dynamic_limit_uses_the_actual_output_as_its_pole_state(self):
        text = export_spice("V1(10V) between(input,0);\nVp(5V) between(pos,0);\nop1(input,out,out,pos,0);\n"
                            "analog op1 {gbw=1kHz; slew=2V/us; output.min=0V; output.max=vplus-1V;}")
        self.assertIn("ddt(V(out,return))",text)
        self.assertIn("(-1+1*V(vplus,return))",text)
        self.assertIn(",-2000000,2000000)",text)
        self.assertNotIn("Cstate state return",text)
