"""Static FPGA loops, numbered pin banks, slices and conditional expressions."""
# Mix generated scalar banks with true vectors to catch bit-order and scope mistakes.
# Clocked slice updates must preserve untouched bits and read the old register value.

from pathlib import Path
import re
import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError


class DigitalSelectorsTest(unittest.TestCase):
    def test_loop_and_slice_copy_vectors_and_preserve_bit_order(self):
        for body in ('for(i=0;i<8;i=i+1){y[i]=a[i];}', 'y[7:0]=a[7:0];'):
            with self.subTest(body=body):
                source = f'chip Copy(a,y) {{ a[8]=input; y[8]=output; {body} }}'
                sim = DigitalSimulation(source)
                for value in (0, 1, 37, 128, 255):
                    sim.set_input('a', value)
                    self.assertEqual(sim.values['y'], value)
                self.assertNotIn('for (', export_verilog(source))

    def test_numbered_pin_banks_support_loops_slices_and_offset_order(self):
        inputs = ','.join(f'SW{i}' for i in range(8))
        outputs = ','.join(f'LEDR{i}' for i in range(4))
        for body in ('for(int i=0;i<4;i++){LEDR[i]=SW[7-i];}',
                     'LEDR[3:0]=SW[7:4];'):
            with self.subTest(body=body):
                sim = DigitalSimulation(f'chip Pins({inputs},{outputs}) {{{inputs}=input;{outputs}=output;{body}}}')
                for bit in range(8):
                    sim.set_input(f'SW{bit}', (0b10110000 >> bit) & 1)
                self.assertEqual(sum(sim.values[f'LEDR{i}'] << i for i in range(4)),
                                 0b1101 if '7-i' in body else 0b1011)

    def test_numbered_banks_are_renamed_inside_main_instances(self):
        for body in ('for(i=0;i<2;i++){LEDR[i]=SW[i];}', 'LEDR[1:0]=SW[1:0];'):
            source = ('chip Pass(SW0,SW1,LEDR0,LEDR1){SW0,SW1=input;LEDR0,LEDR1=output;'
                      + body + '} main {a,b=input;x,y=output;Pass(a,b,x,y);}')
            sim = DigitalSimulation(source)
            sim.set_input('a', 1)
            self.assertEqual((sim.values['x'], sim.values['y']), (1, 0))
            sim.set_input('a', 0); sim.set_input('b', 1)
            self.assertEqual((sim.values['x'], sim.values['y']), (0, 1))

    def test_nested_loops_use_static_indices_and_constants(self):
        source = '''chip Nested(a,y){a[8]=input;y[8]=output;const ROWS[2]=2;
            for(i=0;i<ROWS;i+=1){for(j=0;j<4;j++){y[i*4+j]=a[i*4+j];}}
        }'''
        sim = DigitalSimulation(source)
        sim.set_input('a', 165)
        self.assertEqual(sim.values['y'], 165)

    def test_conditional_partial_drivers_cover_each_bit_without_overlap(self):
        sim = DigitalSimulation('''chip Select(s,a,b,y){s=input;a[8],b[8]=input;y[8]=output;
            if(s){y[7:4]=a[3:0];y[3:0]=b[3:0];}else{y=a;}
        }''')
        sim.set_input('a', 0x35); sim.set_input('b', 0x6A)
        self.assertEqual(sim.values['y'], 0x35)
        sim.set_input('s', 1)
        self.assertEqual(sim.values['y'], 0x5A)

    def test_clocked_slices_keep_other_bits_and_use_old_rhs_state(self):
        source = '''chip Stored(clk,a,y){clk=input;a[4]=input;y[8]=output;q[8]=165;
            clock(clk){q[3:0]=a; q[1]=!q[1];} y=q;
        }'''
        sim = DigitalSimulation(source)
        sim.set_input('a', 0)
        sim.step()
        self.assertEqual(sim.values['y'], 0xA2)
        sim.step()
        self.assertEqual(sim.values['y'], 0xA0)
        for body, expected in (('q=0;q[3:0]=a;q[1]=!q[1];', 2),
                               ('q[3:0]=a;q=255;', 255)):
            other = DigitalSimulation(source.replace('q[3:0]=a; q[1]=!q[1];', body))
            other.step()
            self.assertEqual(other.values['y'], expected)

    def test_clocked_indexed_loop_and_slices_match_cycle_execution(self):
        source = '''chip Stored(clk,a,y){clk=input;a[8]=input;y[8]=output;q[8];
            clock(clk){for(i=0;i<8;i++){q[i]=a[7-i];}}y=q;
        }'''
        sim = DigitalSimulation(source)
        sim.set_input('a', 0b11010000)
        sim.advance(20)
        self.assertEqual(sim.values['y'], 0b00001011)

    def test_ternaries_are_right_associative_and_keep_branch_widths(self):
        for expression, expected in (
            ('a ? 3 : b ? 7 : 255', (255, 7, 3, 3)),
            ('a ? b ? 3 : 7 : 255', (255, 255, 7, 3)),
            ('(a ? 15 : 3) + 1', (4, 4, 0, 0)),
        ):
            sim = DigitalSimulation(f'chip Select(a,b,y){{a,b=input;y[8]=output;y={expression};}}')
            for a, b, value in zip((0, 0, 1, 1), (0, 1, 0, 1), expected):
                sim.set_input('a', a); sim.set_input('b', b)
                self.assertEqual(sim.values['y'], value)

    def test_ternary_state_assignment_uses_pre_edge_inputs_and_state(self):
        sim = DigitalSimulation('''chip State(clk,s,y){clk,s=input;y[3]=output;
            const WAITING[3]=1;const EARLY[3]=4;fsm[3];
            clock(clk,50MHz){fsm=s?EARLY:WAITING;}y=fsm;
        }''')
        sim.step(); self.assertEqual(sim.values['y'], 1)
        sim.set_input('s', 1)
        self.assertEqual(sim.values['y'], 1)
        sim.step(); self.assertEqual(sim.values['y'], 4)

    def test_bad_ranges_drivers_and_runtime_loop_bounds_are_diagnostics(self):
        for body in ('y[8]=a[0];', 'y[0:7]=a;', 'y[7:4]=a[3:0];',
                     'y[7:0]=a;y[0]=1;', 'y[7:0]=a[99:92];',
                     'for(i=0;i<a;i++){y[i]=a[i];}',
                     'for(i=0;i<8;i=i){y[i]=a[i];}',
                     'y=a?1;', 'y=a?:1;'):
            with self.subTest(body=body), self.assertRaises(CircuitError):
                export_verilog(f'chip Bad(a,y){{a[8]=input;y[8]=output;{body}}}')

    def test_scalar_bank_feedback_is_not_hidden_from_cycle_detection(self):
        with self.assertRaisesRegex(CircuitError, 'Combinational loop'):
            export_verilog('chip Bad(LEDR0,LEDR1){LEDR0,LEDR1=output;LEDR[1:0]=LEDR[1:0];}')

    def test_reaction_timer_current_source_and_loop_slice_variants_compile(self):
        path = Path(__file__).resolve().parents[2] / 'examples/advanced/fpga/03_reaction_timer.ckt'
        source = path.read_text(encoding='utf-8')
        # Use the current user's source in memory; never rewrite their example.
        slice_mapping = r'LEDR\[7:0\]\s*=\s*elapsed\[7:0\]\s*;'
        if not (re.search(r'LEDR0\s*=\s*elapsed\[0\]', source) or re.search(slice_mapping, source)):
            self.skipTest('Current reaction timer has a customized output mapping')
        source = re.sub(r'LEDR[0-7]\s*=\s*elapsed\[[0-7]\]\s*;', '', source)
        source = re.sub(slice_mapping, '', source)
        for mapping in ('for(i=0;i<8;i=i+1){LEDR[i]=elapsed[i];}', 'LEDR[7:0]=elapsed[7:0];'):
            program = source.rsplit('}', 1)[0] + mapping + '\n}'
            self.assertIn('always @(posedge MAX10_CLK1_50)', export_verilog(program))
            sim = DigitalSimulation(program)
            sim.set_input('SW0', 1)
            sim.advance(500000)
            self.assertEqual(sim.values['fsm'], 1)


if __name__ == '__main__':
    unittest.main()
