"""Unsigned bus operations agree across lowering, stepping and event skipping."""
# Compare lowered expressions, ordinary cycle stepping, and accelerated stepping on the same
# unsigned widths so an optimization cannot hide a truncation, precedence, or wraparound error.

import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError


class BitShiftTest(unittest.TestCase):
    def test_bus_operations_and_dynamic_shifts(self):
        sim = DigitalSimulation('''chip Buses(a,b,n,left,right,both,either,different) {
            a[8],b[8],n[16]=input;
            left[16],right[16],both[8],either[8],different[8]=output;
            left=a << n; right=a >> n;
            both=a & b; either=a | b; different=a ^ b;
        }''')
        for a in range(256):
            b = 255 - a
            sim.set_input('a', a)
            sim.set_input('b', b)
            for count in (0, 1, 7, 8, 9, 65535):
                sim.set_input('n', count)
                self.assertEqual(sim.values['left'], (a << count) & 255 if count < 8 else 0)
                self.assertEqual(sim.values['right'], a >> count if count < 8 else 0)
            self.assertEqual(sim.values['both'], a & b)
            self.assertEqual(sim.values['either'], a | b)
            self.assertEqual(sim.values['different'], a ^ b)

    def test_shift_and_logical_not_precedence(self):
        sim = DigitalSimulation('''chip Order(a,n,x,y,z,flag) {
            a[8],n[8]=input;x[8],y[8],z[8]=output;flag=output;
            x=a << n + 1;
            y=a + 1 >> n;
            z=!a + n;
            flag=a << n == 4;
        }''')
        sim.set_input('a', 2)
        sim.set_input('n', 1)
        self.assertEqual([sim.values[p] for p in ('x','y','z','flag')], [8,1,1,1])
        sim.set_input('a', 0)
        self.assertEqual(sim.values['z'], 2)

    def test_compound_updates_read_previous_state_and_wrap(self):
        operations = {'-=': lambda a,b:a-b, '&=':lambda a,b:a&b,
                      '|=':lambda a,b:a|b, '^=':lambda a,b:a^b,
                      '<<=':lambda a,b:a<<b, '>>=':lambda a,b:a>>b}
        for op, expected in operations.items():
            with self.subTest(op=op):
                sim = DigitalSimulation(f'''chip Update(clk,a,y,prior) {{
                    clk=input;a[8]=input;y[8],prior[8]=output;
                    value[8]=129; old[8];
                    clock(clk,1kHz) {{ old=value;value {op} a | 1; }}
                    y=value;prior=old;
                }}''')
                sim.set_input('a', 2)
                value = 129
                for _ in range(4):
                    old, value = value, expected(value, 3) & 255
                    sim.step()
                    self.assertEqual((sim.values['y'], sim.values['prior']), (value, old))

    def test_partial_compounds_and_static_loop(self):
        sim = DigitalSimulation('''chip Partial(clk,y) {
            clk=input;y[8]=output; value[8]=145;
            clock(clk) {
                value[7:4] >>= 1;
                for(i=0;i<4;i+=1) { value[i] ^= 1; }
            }
            y=value;
        }''')
        sim.step()
        self.assertEqual(sim.values['y'], 78)
        sim.step()
        self.assertEqual(sim.values['y'], 33)

    def test_fast_plan_matches_individual_clock_cycles(self):
        source = '''chip Fast(clk,n,y) {
            clk=input;n[8]=input;y[8]=output;
            count[8]; result[8];
            clock(clk,1kHz) {
                count+=1;
                if ((count >> n) == 3) { result ^= 129; }
                else { result = count << n; }
            }
            y=result;
        }'''
        fast, slow = DigitalSimulation(source), DigitalSimulation(source)
        self.assertIsNotNone(fast._fast_plan)
        for count in (0, 1, 4, 8, 255):
            fast.set_input('n', count)
            slow.set_input('n', count)
            fast.advance(300)
            for _ in range(300):
                slow.step()
            self.assertEqual(fast.values, slow.values)

    def test_enormous_shift_count_returns_zero_without_allocating(self):
        source = '''chip Bounded(clk,n,y,z) {
            clk=input;n[4096]=input;y[8],z[8]=output;
            value[8]=255;
            clock(clk) { value <<= n; }
            y=value;z=value >> n;
        }'''
        sim = DigitalSimulation(source)
        sim.set_input('n', 1 << 4095)
        sim.advance(10)
        self.assertEqual((sim.values['y'], sim.values['z']), (0,0))

    def test_nested_chip_and_numbered_bank_shift(self):
        source = '''chip Shift(a[3:0],y[3:0]) {
            a[3:0]=input;y[3:0]=output;
            incoming[4]=a[3:0];shifted[4]=incoming << 1;
            y[3:0]=shifted;
        }
        main ShiftBoard { SW[3:0]=input;LEDR[3:0]=output;Shift(SW[3:0],LEDR[3:0]); }'''
        sim = DigitalSimulation(source)
        for value in range(16):
            for bit in range(4):
                sim.set_input(f'SW{bit}', (value >> bit) & 1)
            self.assertEqual(sum(sim.values[f'LEDR{bit}'] << bit for bit in range(4)), (value << 1) & 15)

    def test_invalid_or_undriven_compound_targets_fail(self):
        for statement in ('missing <<= 1;', 'a >>= 1;', 'y ^= 1;', 'value <<= ;'):
            with self.subTest(statement=statement), self.assertRaises(CircuitError):
                export_verilog(f'''chip Bad(clk,a,y){{clk,a=input;y=output;value[8];
                    clock(clk){{{statement}}}y=value[0];}}''')


if __name__ == '__main__':
    unittest.main()
