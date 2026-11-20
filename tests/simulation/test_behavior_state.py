"""Stateful math advances only at accepted electrical endpoints."""

from dataclasses import replace
from pathlib import Path
import unittest

import numpy as np

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.behavior_state import BehaviorState
from circuitlab.math.behavioral import evaluate, linear_form
from circuitlab.math.linear import NodalBackend
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.model.graph import CircuitError, Component, MathEquation, Pulse


def behavior(tree, *, inputs=("input", "0"), source=1):
    circuit = compile_circuit(f"V1({source}V) between(input,0);")
    circuit.behavior.append(MathEquation("test_math", "out", "0", tree, inputs, line=8))
    return circuit


class StatefulMathRuntimeTest(unittest.TestCase):
    def test_source_level_rc_integral_matches_backward_euler_and_keeps_state_on_edit(self):
        source = """V1(1V) between(input,0);
            analog {
                property tau=500ms;
                V(out)=integrate((V(input)-V(out))/tau,0V);
            }
        """
        sim = TimeDomain(compile_circuit(source),.1)
        for count in range(1,6):
            self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")],1-1.2**-count)
        before = sim.state[sim.node_rows["out"]]
        accepted = dict(sim.behavior_state.memory)
        sim = sim.reconfigure(compile_circuit(source.replace("500ms","1s")))
        self.assertEqual(sim.behavior_state.memory,accepted)
        self.assertAlmostEqual(sim.time,.5)
        self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")],(before+.1)/1.1)

    def test_executable_includes_namespace_independent_internal_integrators(self):
        source = 'include("left.ckt");\ninclude("right.ckt");\nconnector(input);\nV1(1V) between(input,0);\n'
        children = {
            "left.ckt": "connector(input,a);\nanalog{phase=integrate(2*V(input),0V); V(a)=phase;}\n",
            "right.ckt": "connector(input,b);\nanalog{phase=integrate(3*V(input),0V); V(b)=phase;}\n",
        }
        read = lambda path: children[path.name]
        path = Path.cwd()/"state_entry.ckt"
        circuit = compile_circuit(source,path=path,read_source=read)
        sim = TimeDomain(circuit,.1)
        voltage = sim.advance()
        self.assertAlmostEqual(voltage[sim.nodes.index("a")],.2)
        self.assertAlmostEqual(voltage[sim.nodes.index("b")],.3)
        self.assertEqual(len(sim.behavior_state.memory),2)
        self.assertTrue(any(key.startswith("@file1.") for key in sim.behavior_state.memory))
        self.assertTrue(any(key.startswith("@file2.") for key in sim.behavior_state.memory))
        changed = compile_circuit(source.replace("V1(1V)","V1(2V)"),path=path,read_source=read)
        sim = sim.reconfigure(changed)
        voltage = sim.advance()
        self.assertAlmostEqual(voltage[sim.nodes.index("a")],.6)
        self.assertAlmostEqual(voltage[sim.nodes.index("b")],.9)

    def test_equivalent_transport_delay_expression_preserves_accepted_history(self):
        source = "V1(1V) between(input,0);\nanalog V(out)=delay(V(input),20ms+30ms);\n"
        sim = TimeDomain(compile_circuit(source),.1)
        sim.advance()
        memory = dict(sim.behavior_state.memory)
        changed = compile_circuit(source.replace("20ms+30ms","50ms"))
        sim = sim.reconfigure(changed)
        self.assertEqual(sim.behavior_state.memory,memory)
        self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")],1)
        with self.assertRaises(CircuitError) as caught:
            sim.reconfigure(compile_circuit(source.replace("20ms+30ms","100ms")))
        self.assertEqual(caught.exception.code,"TopologyChanged")

    def test_changing_state_operation_requires_reset_and_leaves_original_running(self):
        source = "V1(1V) between(input,0);\nanalog V(out)=integrate(V(input));\n"
        sim = TimeDomain(compile_circuit(source),.1)
        sim.advance()
        state, memory, time = sim.state.copy(), dict(sim.behavior_state.memory), sim.time
        changed = compile_circuit(source.replace("integrate(V(input))","delay(V(input),1ms)"))
        with self.assertRaises(CircuitError) as caught:
            sim.reconfigure(changed)
        self.assertEqual(caught.exception.code,"TopologyChanged")
        np.testing.assert_array_equal(sim.state,state)
        self.assertEqual(sim.behavior_state.memory,memory)
        self.assertEqual(sim.time,time)
        self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")],.2)

    def test_commit_is_atomic_when_a_later_state_fails(self):
        good = ("state","integrate","good",("v","input","0"),("c",0))
        bad = ("state","integrate","bad",("call","sqrt",("v","input","0")),("c",0))
        state = BehaviorState(.1,(good,bad))
        state.commit({"input":1,"0":0},.1)
        accepted = dict(state.memory)
        with self.assertRaises(CircuitError):
            state.commit({"input":-1,"0":0},.2)
        self.assertEqual(state.memory,accepted)
        self.assertEqual(state.time,.1)

    def test_parser_internal_variables_and_generated_b_sources_share_the_runtime(self):
        circuit = compile_circuit("""V1(2V) between(input,0);
            analog {
                property gain=3;
                rate=gain*V(input);
                phase=integrate(rate,time);
                V(out)=phase;
            }
        """)
        equation, = circuit.behavior
        # Numerical model expansion may already have materialized a B source.
        circuit = replace(circuit, components=[*circuit.components,
                          Component("generated", "B", "out", "0", behavior=equation)], behavior=[])
        sim = TimeDomain(circuit,.1)
        for count in range(1,4):
            self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")],.6*count)

    def test_backward_euler_integrator_and_gradient_follow_actual_step_size(self):
        tree = ("state", "integrate", "integral", ("v", "input", "0"), ("c", 2))
        sim = TimeDomain(behavior(tree, source=3), .1)
        for count in range(1, 5):
            voltage = sim.advance()
            self.assertAlmostEqual(voltage[sim.nodes.index("out")], 2 + .3 * count)
        sim = sim.reconfigure(sim.circuit, step=.05)
        self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")], 3.35)

    def test_feedback_integrator_solves_implicitly_instead_of_using_a_stale_rate(self):
        rate = ("binary", "/", ("binary", "-", ("v", "input", "0"), ("v", "out", "0")), ("c", .5))
        tree = ("state", "integrate", "rc", rate, ("c", 0))
        sim = TimeDomain(behavior(tree, inputs=("input", "out", "0")), .1)
        for count in range(1, 8):
            self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")], 1 - 1.2 ** -count)

    def test_dc_keeps_integrator_initial_value_and_ac_is_explicitly_unsupported(self):
        tree = ("state", "integrate", "integral", ("v", "input", "0"), ("c", 2))
        circuit = behavior(tree)
        self.assertAlmostEqual(NodalBackend().solve(circuit, ac=False).voltage[circuit.nodes.index("out")], 2)
        with self.assertRaises(CircuitError) as caught:
            NodalBackend().solve(circuit, ac=True, frequency=1000)
        self.assertEqual(caught.exception.code, "UnsupportedComponent")
        self.assertIsNone(linear_form(tree))

    def test_shared_state_reference_is_committed_once_and_newton_reads_are_pure(self):
        tree = ("state", "integrate", "integral", ("c", 3), ("c", 2))
        state = BehaviorState(.1, (tree,))
        for _ in range(40):
            self.assertEqual(evaluate(tree, {}, .1, state=state), (2.3, {}))
            self.assertFalse(state.memory)
        state.commit({}, .1)
        self.assertAlmostEqual(state.memory["integral"].value, 2.3)
        circuit = behavior(tree)
        circuit.behavior.append(MathEquation("second", "twice", "0", ("binary", "*", ("c", 2), tree), ("0",)))
        sim = TimeDomain(circuit, .1)
        voltage = sim.advance()
        self.assertAlmostEqual(voltage[sim.nodes.index("out")], 2.3)
        self.assertAlmostEqual(voltage[sim.nodes.index("twice")], 4.6)
        self.assertEqual(len(sim.behavior_state.memory), 1)

    def test_nested_integrators_use_one_consistent_backward_euler_snapshot(self):
        inner = ("state", "integrate", "first", ("c", 1), ("c", 0))
        outer = ("state", "integrate", "second", inner, ("c", 0))
        sim = TimeDomain(behavior(outer), .1)
        expected = 0
        for count in range(1, 5):
            expected += .1 * count * .1
            self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")], expected)

    def test_transport_delay_interpolates_and_survives_value_only_reconfigure(self):
        tree = ("state", "delay", "line", ("v", "input", "0"), ("c", .05))
        circuit = behavior(tree, source=1)
        sim = TimeDomain(circuit, .1)
        self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")], 1)
        changed = replace(circuit, components=[replace(circuit.components[0], value=3)])
        sim = sim.reconfigure(changed)
        self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")], 2)
        self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")], 3)
        direct = ("state", "delay", "direct", ("v", "input", "0"), ("c", 0))
        context = BehaviorState(.1, (direct,))
        self.assertEqual(evaluate(direct, {"input":2,"0":0}, .1, state=context), (2,{"input":1,"0":-1}))

    def test_transition_uses_distinct_rise_and_fall_delays(self):
        tree = ("state", "transition", "q", ("v", "input", "0"), ("c", .2), ("c", .3))
        circuit = behavior(tree, source=0)
        circuit.components[0] = replace(circuit.components[0], pulse=Pulse(0,1,.3,.01,.01,.4,2))
        sim = TimeDomain(circuit, .1)
        samples = [sim.advance()[sim.nodes.index("out")] for _ in range(12)]
        self.assertEqual(samples[:5], [0]*5)
        self.assertEqual(samples[5:10], [1]*5)
        self.assertEqual(samples[10:], [0,0])

    def test_short_inertial_pulses_cancel_pending_events(self):
        tree = ("state", "transition", "q", ("v", "input", "0"), ("c", .3), ("c", .2))
        state = BehaviorState(.1, (tree,))
        sequence = ((.1,0),(.2,1),(.3,0),(.4,0),(.5,0),(.6,0))
        for time, level in sequence:
            values = {"input":level,"0":0}
            self.assertEqual(evaluate(tree, values, time, state=state)[0], 0)
            state.commit(values, time)
        self.assertIsNone(state.memory["q"].pending)

    def test_failed_math_endpoint_rolls_back_state_and_clock(self):
        integral = ("state", "integrate", "integral", ("v", "input", "0"), ("c", 0))
        tree = ("binary", "+", integral, ("call", "sqrt", ("v", "input", "0")))
        circuit = behavior(tree)
        sim = TimeDomain(circuit, .1)
        sim.advance()
        changed = replace(circuit, components=[replace(circuit.components[0], value=-1)])
        sim = sim.reconfigure(changed)
        state, memories, time = sim.state.copy(), dict(sim.behavior_state.memory), sim.time
        with self.assertRaises(CircuitError):
            sim.advance()
        np.testing.assert_array_equal(sim.state, state)
        self.assertEqual(sim.behavior_state.memory, memories)
        self.assertEqual(sim.time, time)

    def test_adaptive_half_steps_do_not_alias_or_double_advance_parent_history(self):
        tree = ("state", "integrate", "integral", ("c", 1), ("c", 0))
        sim = TimeDomain(behavior(tree), .1)
        original = sim.advance_once
        def fail_once():
            sim.advance_once = original
            raise CircuitError("retry", code="NonlinearConvergence")
        sim.advance_once = fail_once
        self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")], .1)
        self.assertAlmostEqual(sim.time, .1)
        self.assertAlmostEqual(sim.behavior_state.memory["integral"].value, .1)
        self.assertIsNot(sim.behavior_state, sim.half_step.behavior_state)

    def test_failed_second_half_step_leaves_parent_history_uncommitted(self):
        tree = ("state", "integrate", "integral", ("c", 1), ("c", 0))
        circuit = behavior(tree)
        sim = TimeDomain(circuit, .1)
        half = TimeDomain(circuit, .05)
        sim.half_step = half
        sim.advance_once = lambda: (_ for _ in ()).throw(CircuitError("retry",code="NonlinearConvergence"))
        original, calls = half.advance, []
        def fail_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise CircuitError("bad model",code="MathDomain")
            return original(*args, **kwargs)
        half.advance = fail_second
        with self.assertRaises(CircuitError):
            sim.advance()
        self.assertFalse(sim.behavior_state.memory)
        self.assertEqual(sim.time, 0)
        np.testing.assert_array_equal(sim.state, np.zeros_like(sim.state))

    def test_bad_delay_times_are_source_located_and_do_not_commit(self):
        tree = ("state", "delay", "line", ("v", "input", "0"), ("c", -.1))
        circuit = behavior(tree)
        circuit.behavior[0] = replace(circuit.behavior[0], path=Path("bad.ckt"))
        sim = TimeDomain(circuit, .1)
        with self.assertRaises(CircuitError) as caught:
            sim.advance()
        self.assertEqual(caught.exception.line, 8)
        self.assertEqual(caught.exception.path, Path("bad.ckt"))
        self.assertFalse(sim.behavior_state.memory)
        self.assertEqual(sim.time, 0)

    def test_square_uses_cycles_and_validates_duty(self):
        for phase, expected in ((0,1),(.49,1),(.5,0),(.99,0),(1,1),(-.25,0)):
            self.assertEqual(evaluate(("call","square",("c",phase),("c",.5)),{})[0],expected)
        with self.assertRaises(ValueError):
            evaluate(("call","square",("c",0),("c",2)),{})
