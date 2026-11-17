"""Complete combinational behavior and unsigned data ports in digital chips."""

import unittest

from circuitlab.exporters.verilog import compile_chips, export_verilog
from circuitlab.model.graph import CircuitError


class DigitalBehaviorTest(unittest.TestCase):
    def test_vector_ports_keep_header_order_and_assign_whole_data(self):
        source = '''chip Data(y, a, b) {
            a[8], b[8] = input;
            y[8] = output;
            y = a + b;
        }'''
        name, directions, _, hdl = compile_chips(source)[0]
        self.assertEqual(name, 'Data')
        self.assertEqual(directions, (('y', 'output'), ('a', 'input'), ('b', 'input')))
        self.assertIn('output wire [7:0] y', hdl)
        self.assertIn('input wire [7:0] a', hdl)
        self.assertIn('(a + b)', hdl)
        self.assertNotIn('always', hdl)

    def test_complete_if_else_builds_a_mux_without_state(self):
        hdl = export_verilog('''chip Select(sel, a, b, y) {
            sel = input; a[8], b[8] = input; y[8] = output;
            if (sel) { y = a; } else { y = b; }
        }''')
        self.assertIn('(sel ? a : b)', hdl)
        self.assertNotIn('reg ', hdl)
        self.assertNotIn('always', hdl)

    def test_nested_branches_and_multiple_outputs_are_complete(self):
        hdl = export_verilog('''chip Select(a,b,y,z) {
            a,b = input; y,z = output;
            if (a) {
                if (b) { y=1; z=0; } else { y=0; z=1; }
            } else if (b) { y=1; z=1; } else { y=0; z=0; }
        }''')
        self.assertEqual(hdl.count(' ? '), 6)
        self.assertIn('assign y =', hdl)
        self.assertIn('assign z =', hdl)

    def test_unsigned_comparisons_boolean_and_arithmetic_outputs(self):
        source = '''chip Flags(a,b,zero,less,both,bit_sum) {
            a[8],b[8]=input; zero,less,both,bit_sum=output;
            zero=!a; less=a<b; both=a&&b; bit_sum=!a+b[0];
        }'''
        hdl = export_verilog(source)
        self.assertIn(' = !a;', hdl)
        self.assertIn('(a < b)', hdl)
        self.assertIn('(a && b)', hdl)

    def test_internal_wire_declarations_and_forward_drivers(self):
        hdl = export_verilog('''chip Wired(a,y) {
            a[8]=input; y[8]=output; link[8]=wire;
            y=link; link=a+50;
        }''')
        self.assertIn('wire [7:0] link =', hdl)
        self.assertIn('assign y = link;', hdl)
        self.assertNotIn('reg ', hdl)

    def test_comb_and_clock_helpers_have_distinct_names(self):
        hdl = export_verilog('''chip Counter(clk,a,y) {
            clk,a=input; y[8]=output; count[8];
            clock(clk,50MHz) { if(a) { count+=50; } }
            if(a) { y=count+1; } else { y=count; }
        }''')
        self.assertIn('ooc_comb_', hdl)
        self.assertIn('ooc_expr_', hdl)
        self.assertIn('always @(posedge clk)', hdl)

    def test_rejects_incomplete_paths_multiple_drivers_and_state_assignment(self):
        for body in (
            'if(a) { y=b; }',
            'if(a) { y=b; } else { }',
            'if(a) { y=b; z=a; } else { y=a; }',
            'if(a) { y=b; y=a; } else { y=a; }',
            'y=a; if(b) { y=b; } else { y=0; }',
            'if(a) { held=1; y=b; } else { held=0; y=a; }',
            'if(a) { y=b; } else if(b) { y=a; }',
            'for(int i=0;i<3;i++) { y=a; }',
            'while(a) { y=b; }',
        ):
            with self.subTest(body=body), self.assertRaises(CircuitError):
                export_verilog(f'chip Bad(a,b,y,z) {{ a,b=input; y,z=output; held; {body} }}')

    def test_rejects_undriven_wires_collisions_and_combinational_feedback(self):
        for body in (
            'net=wire; y=a;',
            'net=wire; net=wire; net=a; y=net;',
            'net[8]=wire; net[8]; net=0; y=a;',
            'y=wire; y=a;',
            'net=wire; net=!y; y=net;',
            'net=wire; if(a) { net=!net; } else { net=0; } y=net;',
            'net=wire; net=a; net=!a; y=net;',
        ):
            with self.subTest(body=body), self.assertRaises(CircuitError):
                export_verilog(f'chip Bad(a,y) {{ a=input; y=output; {body} }}')

    def test_width_limits_and_scalar_data_loss_are_reported(self):
        for declaration in ('a[0]=input;', 'a[4097]=input;', 'a[-1]=input;',
                            'a[]=input;', 'a[2+2]=input;'):
            with self.subTest(declaration=declaration), self.assertRaises(CircuitError):
                export_verilog(f'chip Bad(a,y) {{ {declaration} y=output; y=0; }}')
        with self.assertRaisesRegex(CircuitError, 'one-bit'):
            export_verilog('chip Bad(a,y) { a[8]=input; y=output; y=a; }')

    def test_clock_blocks_are_unconditional_and_have_one_bit_inputs(self):
        with self.assertRaisesRegex(CircuitError, 'directly inside'):
            export_verilog('''chip Bad(clk,a,y) {
                clk,a=input; y=output; count;
                if(a) { clock(clk,1Hz) { count=!count; } y=count; }
                else { y=0; }
            }''')
        with self.assertRaisesRegex(CircuitError, 'one bit'):
            export_verilog('''chip Bad(clk,y) {
                clk[8]=input; y=output; count;
                clock(clk,1Hz) { count=!count; } y=count;
            }''')

    def test_long_else_if_chains_fail_with_a_source_diagnostic(self):
        branches = 'if(a) {y=0;} ' + 'else if(a) {y=0;} ' * 70 + 'else {y=1;}'
        with self.assertRaisesRegex(CircuitError, '64 branches'):
            export_verilog('chip Bad(a,y) {a=input; y=output; ' + branches + '}')


if __name__ == '__main__':
    unittest.main()
