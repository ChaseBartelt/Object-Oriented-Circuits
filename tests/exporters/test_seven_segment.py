"""Active-low hexadecimal decoder lowering, simulation and clocked use."""
# Use all hexadecimal digits and decimal-point states as an explicit active-low oracle.
# The same decoder must behave consistently in expressions, nested assembly, and clocked logic.

import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError


# Independent per-glyph lit segment names, bit order a..g, followed by DP.
GLYPHS = ('abcdef', 'bc', 'abdeg', 'abcdg', 'bcfg', 'acdfg', 'acdefg', 'abc',
          'abcdefg', 'abcdfg', 'abcefg', 'cdefg', 'adef', 'bcdeg', 'adefg', 'aefg')


def mask(digit, point=False):
    lit = GLYPHS[digit] + ('.' if point else '')
    return sum((segment not in lit) << bit for bit, segment in enumerate('abcdefg.'))


class SevenSegmentTest(unittest.TestCase):
    def test_all_hexadecimal_digits_and_decimal_point(self):
        source = '''chip Display(value, point, segments) {
            value[4], point = input; segments[8] = output;
            segments = sevenseg(value, point);
        }'''
        sim = DigitalSimulation(source)
        self.assertEqual(sim.clocks, ())
        for digit in range(16):
            for point in (0, 1):
                sim.set_input('value', digit)
                sim.set_input('point', point)
                self.assertEqual(sim.values['segments'], mask(digit, point))
        hdl = export_verilog(source)
        self.assertNotIn('sevenseg(', hdl)
        self.assertNotIn('function', hdl)
        self.assertNotIn('reg ', hdl)

    def test_default_point_is_off_and_wider_values_use_low_four_bits(self):
        sim = DigitalSimulation('''chip Display(value, segments) {
            value[8] = input; segments[8] = output;
            segments = sevenseg(value);
        }''')
        for value in range(256):
            sim.set_input('value', value)
            self.assertEqual(sim.values['segments'], mask(value & 15))

    def test_scalar_inputs_literals_and_expression_arguments(self):
        for expression, expected in (('sevenseg(0)', mask(0)), ('sevenseg(15)', mask(15)),
                                     ('sevenseg(16)', mask(0)), ('sevenseg(31, 3)', mask(15, True)),
                                     ('sevenseg(2 + 4, !0)', mask(6, True))):
            with self.subTest(expression=expression):
                sim = DigitalSimulation(f'chip Display(y){{y[8]=output;y={expression};}}')
                self.assertEqual(sim.values['y'], expected)
        sim = DigitalSimulation('chip Display(a,y){a=input;y[8]=output;y=sevenseg(a);}')
        for bit in (0, 1):
            sim.set_input('a', bit)
            self.assertEqual(sim.values['y'], mask(bit))

    def test_decoder_can_be_a_ternary_branch_or_have_conditional_arguments(self):
        sim = DigitalSimulation('''chip Display(a,b,choose,point,y) {
            a[4],b[4],choose,point=input;y[8]=output;
            y = choose ? sevenseg(a, point) : sevenseg(b);
        }''')
        sim.set_input('a', 12)
        sim.set_input('b', 9)
        sim.set_input('point', 1)
        self.assertEqual(sim.values['y'], mask(9))
        sim.set_input('choose', 1)
        self.assertEqual(sim.values['y'], mask(12, True))
        nested = DigitalSimulation('''chip Display(a,b,choose,point,y) {
            a[4],b[4],choose,point=input;y[8]=output;
            y = sevenseg(choose ? a : b, point ? 2 : 0);
        }''')
        nested.set_input('b', 14)
        nested.set_input('point', 1)
        self.assertEqual(nested.values['y'], mask(14, True))

    def test_clocked_decode_uses_sampled_input(self):
        sim = DigitalSimulation('''chip Display(clk,value,point,y) {
            clk, value[4], point=input;y[8]=output;held[8]=255;
            clock(clk,1kHz){held=sevenseg(value,point);} y=held;
        }''')
        self.assertEqual(sim.values['y'], 255)
        sim.set_input('value', 5)
        sim.set_input('point', 1)
        self.assertEqual(sim.values['y'], 255)
        sim.step()
        self.assertEqual(sim.values['y'], mask(5, True))
        sim.set_input('value', 10)
        self.assertEqual(sim.values['y'], mask(5, True))
        sim.step()
        self.assertEqual(sim.values['y'], mask(10, True))

    def test_numbered_segment_outputs_and_nested_chip_assembly(self):
        sim = DigitalSimulation('''chip Decoder(a,segments){a[4]=input;segments[8]=output;segments=sevenseg(a);}
            chip Display(value,y){value[4]=input;y[8]=output;Decoder(value,y);}
            main {value[4]=input;segments[8]=output;Display(value,segments);}
        ''')
        sim.set_input('value', 8)
        self.assertEqual(sim.values['segments'], mask(8))
        bank = DigitalSimulation('''chip Display(SW[3:0],HEX0[7:0]){
            SW[3:0]=input;HEX0[7:0]=output;HEX0[7:0]=sevenseg(SW[3:0]);
        }''')
        bank.set_input('SW0', 1)
        self.assertEqual(sum(bank.values[f'HEX0{i}'] << i for i in range(8)), mask(1))

    def test_invalid_calls_remain_source_diagnostics(self):
        for expression in ('sevenseg()', 'sevenseg(,1)', 'sevenseg(1,)', 'sevenseg(1,2,3)',
                           'sevenseg(unknown)', 'sevenseg(-1)', 'sevenseg(1', 'sevenseg(a[4])'):
            source = f'chip Bad(a,y){{\n a[4]=input;y[8]=output;\n y={expression};\n}}'
            with self.subTest(expression=expression), self.assertRaises(CircuitError):
                export_verilog(source)

    def test_builtin_call_does_not_conflict_with_a_signal_named_sevenseg(self):
        chip = '''chip Display(a,y){a[4]=input;y[8]=output;
            sevenseg[8]=sevenseg(a);y=sevenseg;}'''
        for source in (chip, chip + '\nmain {a[4]=input;y[8]=output;Display(a,y);}'):
            with self.subTest(assembled=source != chip):
                sim = DigitalSimulation(source)
                sim.set_input('a', 13)
                self.assertEqual(sim.values['y'], mask(13))


if __name__ == '__main__':
    unittest.main()
