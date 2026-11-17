"""Numbered pin shorthand preserves scalar ports, ordering, and board behavior."""

import unittest

from circuitlab.exporters.verilog import compile_chips, export_verilog
from circuitlab.language.pin_banks import expand_pin_banks
from circuitlab.language.syntax import has_chip
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError
from circuitlab.tooling.fpga import FpgaError, compile_de10_lite


SOURCE = '''chip HYBRID01(SW[9:0], LEDR[9:0]) {
    SW[9:0] = input;
    LEDR[9:0] = output;
    LEDR[9:0] = SW[9:0];
}'''


class PinBankTest(unittest.TestCase):
    def test_header_and_declarations_expand_to_identical_hdl_and_board_constraints(self):
        inputs = ', '.join(f'SW{i}' for i in range(9, -1, -1))
        outputs = ', '.join(f'LEDR{i}' for i in range(9, -1, -1))
        explicit = SOURCE.replace('SW[9:0]', inputs, 2).replace('LEDR[9:0]', outputs, 2)
        self.assertEqual(export_verilog(SOURCE), export_verilog(explicit))
        self.assertEqual(compile_de10_lite(SOURCE), compile_de10_lite(explicit))
        self.assertTrue(has_chip(SOURCE))
        ports = compile_chips(SOURCE)[0][1]
        self.assertEqual(ports, tuple((f'SW{i}', 'input') for i in range(9, -1, -1))
                         + tuple((f'LEDR{i}', 'output') for i in range(9, -1, -1)))

    def test_all_1024_patterns_in_native_and_clockless_board_profiles(self):
        for profile in ('ooc', 'de10-lite'):
            sim = DigitalSimulation(SOURCE, profile)
            for value in range(1024):
                for bit in range(10):
                    sim.set_input(f'SW{bit}', (value >> bit) & 1)
                self.assertEqual(sum(sim.values[f'LEDR{i}'] << i for i in range(10)), value)
            self.assertEqual(sim.cycles, 0)
            self.assertEqual(sim.clocks, ())

    def test_banks_mix_with_scalar_pins_and_clocked_behavior(self):
        source = '''chip HYBRID01(MAX10_CLK1_50, SW[1:0], LEDR[1:0]) {
            MAX10_CLK1_50, SW[1:0] = input;
            LEDR[1:0] = output;
            held[2];
            clock(MAX10_CLK1_50,50MHz) { held = SW[1:0]; }
            LEDR[1:0] = held;
        }'''
        sim = DigitalSimulation(source, 'de10-lite')
        sim.set_input('SW1', 1)
        for _ in range(2):
            sim.step()
            self.assertEqual(sim.values['LEDR1'], 0)
        sim.step()
        self.assertEqual((sim.values['LEDR1'], sim.values['LEDR0']), (1, 0))

    def test_nonzero_and_single_pin_ranges_keep_written_order(self):
        sim = DigitalSimulation('''chip Offset(a[7:4], en, y[3:0], flag[5:5]) {
            a[7:4], en = input; y[3:0], flag[5:5] = output;
            y[3:0] = a[7:4]; flag5 = en;
        }''')
        for i in range(4, 8):
            sim.set_input(f'a{i}', (0b1101 >> (i - 4)) & 1)
        self.assertEqual([sim.values[f'y{i}'] for i in range(4)], [1, 0, 1, 1])
        sim.set_input('en', 1)
        self.assertEqual(sim.values['flag5'], 1)

    def test_true_vectors_keep_their_existing_width_semantics(self):
        sim = DigitalSimulation('''chip Mixed(a, b[1:0], y) {
            a[4], b[1:0] = input; y[4] = output;
            y = b0 ? a : b1 ? 15 : 0;
        }''')
        self.assertEqual(sim.signals['a'], 4)
        self.assertEqual(sim.signals['b0'], 1)
        sim.set_input('a', 10)
        sim.set_input('b0', 1)
        self.assertEqual(sim.values['y'], 10)
        with self.assertRaises(FpgaError):
            compile_de10_lite('chip HYBRID01(SW0,LEDR0){SW0[1]=input;LEDR0=output;LEDR0=SW0[0];}')

    def test_invalid_ranges_and_duplicates_are_source_diagnostics(self):
        for bank in ('a[0:3]', 'a[-1:0]', 'a[1.5:0]', 'a[3:]', 'a[:0]', 'a[3:1:0]',
                     'a[limit:0]', 'a[4096:4095]', 'a[' + '9' * 5000 + ':0]'):
            with self.subTest(bank=bank), self.assertRaises(CircuitError) as caught:
                export_verilog(f'chip Invalid({bank},y) {{ {bank}=input; y=output; y=0; }}')
            self.assertEqual(caught.exception.line, 1)
        for header, declaration in (('a[2:0],a1,y', 'a[2:0]'),
                                     ('a[2:0],y', 'a[2:0],a1')):
            with self.subTest(header=header, declaration=declaration), self.assertRaises(CircuitError):
                export_verilog(f'chip Invalid({header}){{{declaration}=input;y=output;y=0;}}')

    def test_range_expansion_is_bounded_and_supports_decimal_separators(self):
        self.assertEqual(expand_pin_banks('SW[0_9:0_8], data[10]', 1), 'SW9, SW8, data[10]')
        self.assertEqual(expand_pin_banks('pin[4095:4095]', 1), 'pin4095')
        for raw in ('a[4095:0],b', 'a[4095:0],b[1:0]'):
            with self.subTest(raw=raw), self.assertRaises(CircuitError):
                expand_pin_banks(raw, 1)


if __name__ == '__main__':
    unittest.main()
