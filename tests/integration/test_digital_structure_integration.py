"""Use behavioral chip assembly through the existing document and board APIs."""
# Exercise the same assembled digital design through sessions, simulation, CLI checks,
# and board constraints so no integration path invents a different signal scope or state.

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from circuitlab.math.digital import DigitalSimulation
from circuitlab.tooling.fpga import FpgaError, compile_de10_lite, prepare_project
from circuitlab.tooling.session import CircuitSession


ROOT = Path(__file__).resolve().parents[2]
SELECTOR = """chip Select(choose, a, b, y) {
    choose = input;
    a[8], b[8] = input;
    y[8] = output;
    if (choose) { y = a; } else { y = b; }
}
main ByteSelector {
    choose = input;
    a[8], b[8] = input;
    y[8] = output;
    Select(choose, a, b, y);
}
"""
BOARD = """chip SwitchLamp(a, y) {
    a = input;
    y = output;
    y = a;
}
main HYBRID01 {
    MAX10_CLK1_50, SW0, SW1 = input;
    LEDR0, LEDR1 = output;
    for (int i = 0; i < 2; i++) {
        SwitchLamp(SW${i}, LEDR${i});
    }
}
"""


class DigitalStructureIntegrationTest(unittest.TestCase):
    def test_session_renders_assembled_chips_and_export_runs_with_bus_values(self):
        session = CircuitSession(source=SELECTOR)
        session.compile()
        self.assertFalse(session.issues)
        self.assertTrue(session.preview_only)
        self.assertIsNone(session.solution)
        self.assertEqual([item.name for item in session.drawing.instances], ['Select'])
        self.assertEqual(session.drawing.instances[0].pins,
                         (('choose', 'choose'), ('a', 'a'), ('b', 'b'), ('y', 'y')))
        self.assertIn('module ByteSelector', session.verilog())
        sim = DigitalSimulation(session.source)
        sim.set_input('a', 201)
        sim.set_input('b', 37)
        self.assertEqual(sim.values['y'], 37)
        sim.set_input('choose', 1)
        self.assertEqual(sim.values['y'], 201)

    def test_assembled_main_uses_existing_board_constraints_and_sync(self):
        session = CircuitSession(source=BOARD)
        session.compile()
        self.assertFalse(session.issues)
        self.assertEqual([part.name for part in session.drawing.instances], ['SwitchLamp', 'SwitchLamp'])
        self.assertEqual([part.pins for part in session.drawing.instances],
                         [(('a', 'SW0'), ('y', 'LEDR0')), (('a', 'SW1'), ('y', 'LEDR1'))])
        hdl, qsf, sdc = compile_de10_lite(BOARD)
        self.assertIn('module HYBRID01', hdl)
        for assignment in ('PIN_P11 -to MAX10_CLK1_50', 'PIN_C10 -to SW0',
                           'PIN_C11 -to SW1', 'PIN_A8 -to LEDR0', 'PIN_A9 -to LEDR1'):
            self.assertIn(assignment, qsf)
        self.assertIn('-period 20.000', sdc)
        sim = DigitalSimulation(BOARD, 'de10-lite')
        sim.set_input('SW1', 1)
        self.assertEqual(sim.values['LEDR1'], 0)
        sim.step()
        self.assertEqual(sim.values['LEDR1'], 0)
        sim.step()
        self.assertEqual(sim.values['LEDR1'], 1)
        self.assertEqual(sim.values['LEDR0'], 0)
        with TemporaryDirectory() as directory:
            project = prepare_project(BOARD, directory)
            self.assertTrue(project.matches_source(BOARD))
            self.assertEqual((project.directory / 'build/HYBRID01.v').read_text(), hdl)

    def test_physical_board_pins_cannot_be_redeclared_as_buses(self):
        source = """chip Forward(a, y) {
            a[8] = input; y[8] = output; y = a;
        }
        main HYBRID01 {
            MAX10_CLK1_50 = input; SW0[8] = input; LEDR0[8] = output;
            Forward(SW0, LEDR0);
        }"""
        with self.assertRaises(FpgaError):
            compile_de10_lite(source)

    def test_named_main_cli_checks_without_invoking_electrical_solver(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / 'byte selector.ckt'
            source.write_text(SELECTOR, encoding='utf-8')
            checked = subprocess.run(
                [sys.executable, '-m', 'circuitlab.tooling.cli', 'check', str(source)],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            solved = subprocess.run(
                [sys.executable, '-m', 'circuitlab.tooling.cli', 'solve', str(source)],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn('Check passed (digital chip)', checked.stdout)
        self.assertNotEqual(solved.returncode, 0)
        self.assertIn('Digital chips use the Verilog compiler', solved.stderr)

    def test_two_counter_instances_have_independent_state_with_shared_clock(self):
        source = """chip Count(clk, enable, data) {
            clk, enable = input;
            data[8] = output;
            value[8];
            clock(clk, 10Hz) { if (enable) { value += 50; } }
            data = value;
        }
        main Counters {
            clk, a, b = input;
            first[8], second[8] = output;
            Count(clk, a, first);
            Count(clk, b, second);
        }
        """
        sim = DigitalSimulation(source)
        sim.set_input('a', 1)
        sim.step()
        self.assertEqual((sim.values['first'], sim.values['second']), (50, 0))
        sim.set_input('a', 0)
        sim.set_input('b', 1)
        sim.advance(6)
        self.assertEqual((sim.values['first'], sim.values['second']), (50, 44))
        sim.reset()
        self.assertEqual((sim.values['first'], sim.values['second']), (0, 0))
