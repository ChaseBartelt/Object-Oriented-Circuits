"""Cycle checks of the emitted Verilog subset, independent of the OOC parser.

This small evaluator is not a general HDL simulator or FPGA synthesis check.
"""
import random
import re
import unittest
from pathlib import Path
from circuitlab.exporters.verilog import export_verilog
from circuitlab.model.graph import CircuitError

ROOT = Path(__file__).resolve().parents[2]


class EmittedClock:
    def __init__(self, text):
        self.widths, self.state, self.constants, self.wires = {}, {}, {}, []
        for kind, size, name, expression in re.findall(
                r"(?:^|\n)\s*(reg|localparam|wire)\s*(\[\d+:0\])?\s+(\w+)\s*=\s*([^;]+);", text):
            width = int(size[1:].split(':')[0]) + 1 if size else 1
            if kind == 'wire':
                self.wires.append((name, width, expression))
            else:
                value = self.value(expression, {})
                if kind == 'reg':
                    self.widths[name] = width
                    self.state[name] = value
                else:
                    self.constants[name] = value
        rows = [row.strip() for row in text.splitlines()]
        start = next(i for i, row in enumerate(rows) if row.startswith('always @')) + 1
        self.rows, self.index = rows, start
        self.body = self.block()

    @staticmethod
    def value(expression, scope):
        # Each emitted temporary contains at most one operator. Verilog logical
        # operators return 0/1, unlike Python's operand-returning and/or.
        choice = re.fullmatch(r"\(([^?:]+)\?([^?:]+):([^?:]+)\)", expression.strip())
        if choice:
            return EmittedClock.value(choice[2] if EmittedClock.value(choice[1], scope) else choice[3], scope)
        logical = '&&' in expression or '||' in expression
        expression = re.sub(r"\d+'([db])([0-9]+)", lambda m: str(int(m[2], 10 if m[1] == 'd' else 2)), expression)
        expression = re.sub(r"(\w+)\[(\d+)\]", lambda m: str((scope[m[1]] >> int(m[2])) & 1), expression)
        expression = re.sub(r'!(?!=)', ' not ', expression).replace('&&', ' and ').replace('||', ' or ')
        value = eval(expression.strip(), {'__builtins__': {}}, scope)
        return int(bool(value)) if logical else int(value)

    def block(self):
        result = []
        while self.rows[self.index] != 'end':
            row = self.rows[self.index]
            self.index += 1
            if row.startswith('if ('):
                result.append(self.conditional(row))
            else:
                name, expression = row.removesuffix(';').split(' <= ')
                result.append(('set', name, expression))
        self.index += 1
        return result

    def conditional(self, row):
        condition = row[4:-7]
        yes = self.block()
        no = []
        other = self.rows[self.index]
        if other == 'else begin':
            self.index += 1
            no = self.block()
        elif other.startswith('else if ('):
            self.index += 1
            no = [self.conditional(other[5:])]
        return ('if', condition, yes, no)

    def tick(self, **inputs):
        old = {**self.constants, **self.state, **inputs}
        for name, width, expression in self.wires:
            old[name] = self.value(expression, old) & ((1 << width) - 1)
        updates = {}
        def run(body):
            for item in body:
                if item[0] == 'if':
                    run(item[2] if self.value(item[1], old) else item[3])
                else:
                    updates[item[1]] = self.value(item[2], old) & ((1 << self.widths[item[1]]) - 1)
        run(self.body)
        self.state.update(updates)
        return self.state.copy()


class ClockedTest(unittest.TestCase):
    def test_selected_increment_evaluator_preserves_width_and_nonzero_conditions(self):
        source = '''chip SelectStep(clk,y) { clk=input; y=output;
            state[4]=15; condition[2]=2; increment[5]=31;
            clock(clk) { if (condition) { state += increment; } else { state += 1; } }
            y=state[3];
        }'''
        sim = EmittedClock(export_verilog(source))
        self.assertEqual(sim.tick()['state'], 14)
        sim.state['condition'] = 0
        self.assertEqual(sim.tick()['state'], 15)

    def test_else_prefix_state_and_whitespace_are_not_control_clauses(self):
        for whitespace in (' ', '  ', '\t'):
            source = ('chip Whitespace(clk,a,y) { clk,a=input; y=output; '
                      f'const{whitespace}STEP[4]=1; else_count[4]; flag; '
                      'clock(clk) { if (a) { flag=0; } '
                      f'else{whitespace}{{ flag=1; }} '
                      'if (a) { flag=1; } else_count += STEP; } y=flag; }')
            with self.subTest(whitespace=whitespace):
                sim = EmittedClock(export_verilog(source))
                self.assertEqual(sim.tick(a=0)['else_count'], 1)
                self.assertEqual(sim.tick(a=1)['else_count'], 2)

    def test_long_else_if_chain_is_flat_and_does_not_crash(self):
        source = ('chip Chain(clk,y) { clk=input; y=output; x[12]; '
                  'clock(clk) { if (x==0) { x=1; } ' +
                  ' '.join(f'else if (x=={i}) {{ x={i+1}; }}' for i in range(1, 1101)) +
                  ' else { x=0; } } y=x[0]; }')
        generated = export_verilog(source)
        self.assertEqual(generated.count('else if ('), 1100)
        self.assertLess(len(generated), 250000)

    def test_frequency_inside_every_uses_rounded_interval_update_rate(self):
        for interval, cycles, expected_step in (('500ms', 4, 128), ('312.5ms', 3, 96)):
            source = ('chip TimedTone(clk,enable,y) { clk,enable=input; y=output; phase[8]; '
                      f'clock(clk,8Hz) {{ every({interval}) {{ if (enable) {{ phase+=1Hz; }} }} }} y=phase[7]; }}')
            with self.subTest(interval=interval):
                sim = EmittedClock(export_verilog(source))
                for edge in range(1, cycles * 4 + 1):
                    self.assertEqual(sim.tick(enable=1)['phase'], (edge // cycles * expected_step) % 256)
        with self.assertRaisesRegex(CircuitError, 'phase update frequency'):
            export_verilog('chip Bad(clk,y) { clk=input; y=output; phase[32]; '
                           'clock(clk,50MHz) { every(500ms) { phase+=440Hz; } } y=phase[31]; }')

    def test_plus_equals_frequency_matches_explicit_increment(self):
        short = (ROOT / 'tests/fixtures/digital/HYBRID01.ckt').read_text()
        long = short.replace('phase +=', 'phase = phase +')
        self.assertIn('phase += 440Hz;', short)
        self.assertEqual(export_verilog(short), export_verilog(long))

    def test_plus_equals_preserves_rhs_precedence_old_state_and_wrapping(self):
        for body, initial, expected in (
            ('x += 1 | 2;', 1, 4),
            ('x += 1; x += 1;', 1, 2),
            ('x += 1;', 7, 0),
            ('every(1s) { x += 2; }', 1, 3),
        ):
            with self.subTest(body=body):
                source = (f'chip Add(clk,y) {{ clk=input; y=output; x[3]={initial}; '
                          f'clock(clk,1Hz) {{ {body} }} y=x[0]; }}')
                self.assertEqual(EmittedClock(export_verilog(source)).tick()['x'], expected)

    def test_plus_equals_retains_clock_validation(self):
        for body in ('y += 1;', 'clk += 1;', 'missing += 1;', 'x += ;', 'x += 440Hz;'):
            with self.subTest(body=body), self.assertRaises(CircuitError):
                export_verilog('chip Bad(clk,y) { clk=input; y=output; x[32]; '
                               'clock(clk) { ' + body + ' } y=x[0]; }')

    def test_every_fires_after_full_interval_and_repeats(self):
        for duration, period in (('500ms', 4), ('0.5s', 4), ('500_000us', 4),
                                 ('500000µs', 4), ('500000μs', 4), ('500000000ns', 4),
                                 ('312.5ms', 3), ('125ms', 1)):
            with self.subTest(duration=duration):
                source = ('chip Blink(clk,y) { clk=input; y=output; led; '
                          f'clock(clk,8Hz) {{ every({duration}) {{ led=!led; }} }} y=led; }}')
                sim = EmittedClock(export_verilog(source))
                self.assertEqual([sim.tick()['led'] for _ in range(12)],
                                 [(edge // period) % 2 for edge in range(1, 13)])

    def test_every_blocks_have_independent_timers_and_do_not_pause_other_state(self):
        source = '''chip Timers(clk,enable,y) {
            clk,enable=input; y=output; fast; slow; ticks[8]; ooc_every_0;
            clock(clk,8Hz) {
                every(250ms) { fast=!fast; }
                every(500ms) { if (enable) { slow=!slow; } }
                ticks=ticks+1;
            }
            y=fast ^ slow;
        }'''
        sim = EmittedClock(export_verilog(source))
        self.assertEqual(sim.widths['ooc_every_0'], 1)
        for edge in range(1, 17):
            state = sim.tick(enable=int(edge > 4))
            self.assertEqual(state['ticks'], edge)
            self.assertEqual(state['fast'], (edge // 2) % 2)
            self.assertEqual(state['slow'], max(0, edge // 4 - 1) % 2)

    def test_every_rejects_invalid_intervals_and_placement(self):
        for rate, body in (
            ('', 'every(500ms) { led=!led; }'),
            (',8Hz', 'every(0ms) { led=!led; }'),
            (',8Hz', 'every(-500ms) { led=!led; }'),
            (',8Hz', 'every(1ms) { led=!led; }'),
            (',8Hz', 'every(500) { led=!led; }'),
            (',8Hz', 'every(500Hz) { led=!led; }'),
            (',8Hz', 'if (led) { every(500ms) { led=!led; } }'),
            (',8Hz', 'every(500ms) { every(500ms) { led=!led; } }'),
        ):
            with self.subTest(body=body), self.assertRaises(CircuitError):
                export_verilog('chip Bad(clk,y) { clk=input; y=output; led; '
                               f'clock(clk{rate}) {{ {body} }} y=led; }}')
        with self.assertRaises(CircuitError):
            export_verilog('chip Bad(clk,y) { clk=input; y=output; led; every(500ms) { led=!led; } y=led; }')

    def test_frequency_increments_use_clock_and_destination_width(self):
        for width, rate, tone, expected in (
            (32, '50MHz', '440Hz', 37796),
            (32, '50_000kHz', '0.880kHz', 75591),
            (16, '1MHz', '1.5kHz', 98),
            (8, '256Hz', '2.5Hz', 3),
            (8, '256Hz', '128Hz', 128),
            (8, '256Hz', '0Hz', 0),
            (32, '0.05GHz', '440 Hz', 37796),
        ):
            with self.subTest(width=width, rate=rate, tone=tone):
                source = (f'chip Tone(clk,y) {{ clk=input; y=output; phase[{width}]; '
                          f'clock(clk, {rate}) {{ phase=phase+{tone}; }} y=phase[{width-1}]; }}')
                sim = EmittedClock(export_verilog(source))
                self.assertEqual(sim.tick()['phase'], expected)
                sim.state['phase'] = (1 << width) - 1
                self.assertEqual(sim.tick()['phase'], (expected - 1) % (1 << width))

    def test_hz_in_signal_names_does_not_change_integer_arithmetic(self):
        source = ('chip Names(clk,y) { clk=input; y=output; value[8]; '
                  'const STEP440Hz[8]=4; clock(clk) { value=value+STEP440Hz; } y=value[0]; }')
        self.assertEqual(EmittedClock(export_verilog(source)).tick()['value'], 4)

    def test_frequency_literals_report_invalid_or_ambiguous_contexts(self):
        for header, declaration, expression in (
            ('clk', 'phase[32]', 'phase+440Hz'),
            ('clk, 0Hz', 'phase[32]', 'phase+440Hz'),
            ('clk, -50MHz', 'phase[32]', 'phase+440Hz'),
            ('clk, 50', 'phase[32]', 'phase+440Hz'),
            ('clk, 50MHz', 'phase[32]', 'phase+26MHz'),
            ('clk, 50MHz', 'phase[8]', 'phase+0.01Hz'),
            ('clk, 50MHz', 'phase', 'phase+440Hz'),
            ('clk, 50MHz', 'phase[32]', '440Hz'),
            ('clk, 50MHz', 'phase[32]', 'phase+440Hz+1'),
            ('clk, 50MHz', 'phase[32]', 'phase-440Hz'),
        ):
            with self.subTest(header=header, expression=expression), self.assertRaises(CircuitError):
                export_verilog(f'chip Bad(clk,y) {{ clk=input; y=output; {declaration}; '
                               f'clock({header}) {{ phase={expression}; }} y=0; }}')
        with self.assertRaisesRegex(CircuitError, 'Conflicting frequencies'):
            export_verilog('chip Bad(clk,y) { clk=input; y=output; a[32]; b[32]; '
                           'clock(clk,50MHz) { a=a+440Hz; } clock(clk,25MHz) { b=b+440Hz; } y=0; }')

    def test_unsigned_operators_and_last_executed_assignment(self):
        expressions = {
            'x + 1': 0, 'x - 1': 6, '(x + 1) == 0': 1,
            'x > 2': 1, 'x >= 7': 1, 'x < 7': 0, 'x <= 7': 1,
            'x != 7': 0, 'x & 2': 2, 'x | 8': 15, 'x ^ 3': 4,
            'x && 2': 1, 'x || 2': 1, '!x': 0, 'x[2]': 1,
            'x - 2 + 1': 6, 'x & 2 + 1': 3,
        }
        for expression, expected in expressions.items():
            with self.subTest(expression=expression):
                source = ('chip Ops(clk,y) { clk=input; y=output; x[3]=7; result[5]; '
                          'clock(clk) { result=17; result=' + expression + '; } y=result[0]; }')
                self.assertEqual(EmittedClock(export_verilog(source)).tick()['result'], expected)

    def test_hybrid_loads_in_viewer_and_exports_without_analog_solving(self):
        from circuitlab.tooling.session import CircuitSession
        session = CircuitSession()
        session.load(ROOT / 'tests/fixtures/digital/HYBRID01.ckt')
        session.compile()
        self.assertFalse(session.issues)
        self.assertTrue(session.preview_only)
        self.assertIsNone(session.solution)
        self.assertEqual(session.drawing.instances[0].name, 'HYBRID01')
        self.assertIn('always @(posedge MAX10_CLK1_50)', session.verilog())

    def test_hybrid_matches_vhdl_old_state_and_rollover(self):
        text = (ROOT / 'tests/fixtures/digital/HYBRID01.ckt').read_text()
        emitted = export_verilog(text)
        self.assertIn('assign AUDIO_OUT = phase[31];', emitted)
        self.assertIn('assign LEDR0 = led_state;', emitted)
        self.assertIn('always @(posedge MAX10_CLK1_50)', emitted)
        rng = random.Random(17)
        for phase, counter in ((0, 0), (2**32-100, 24999998), (2**32-75591, 24999999)):
            sim = EmittedClock(emitted)
            timer = next(name for name in sim.state if name.startswith('ooc_every_'))
            self.assertEqual(sim.widths[timer], 25)
            sim.state.update(phase=phase)
            sim.state[timer] = counter
            expected = sim.state.copy()
            expected['blink_counter'] = expected.pop(timer)
            for _ in range(500):
                switch = rng.randrange(2)
                previous = expected.copy()
                expected['sw_meta'] = switch
                expected['sw_sync'] = previous['sw_meta']
                expected['phase'] = (previous['phase'] + (75591 if previous['sw_sync'] else 37796)) % 2**32
                expected['blink_counter'] = 0 if previous['blink_counter'] == 24999999 else previous['blink_counter'] + 1
                if previous['blink_counter'] == 24999999:
                    expected['led_state'] = 1 - previous['led_state']
                actual = sim.tick(SW0=switch)
                actual['blink_counter'] = actual.pop(timer)
                self.assertEqual(actual, expected)

    def test_switch_selection_takes_effect_on_third_edge(self):
        sim = EmittedClock(export_verilog((ROOT / 'tests/fixtures/digital/HYBRID01.ckt').read_text()))
        self.assertEqual([sim.tick(SW0=1)['phase'] for _ in range(3)], [37796, 75592, 151183])

    def test_nested_else_if_hold_and_unsigned_expression_widths(self):
        text = """chip Nested(clk,a,b,y) {
            clk,a,b = input; y = output; x[3]=7; flag;
            clock(clk) {
                if (a) { if (b) { x = x + 1; } }
                else if (b) { x = x - 1; }
                else { flag = !flag; }
            }
            y = x[0] ^ flag;
        }"""
        sim = EmittedClock(export_verilog(text))
        self.assertEqual(sim.tick(a=1,b=0)['x'], 7)
        self.assertEqual(sim.tick(a=1,b=1)['x'], 0)
        self.assertEqual(sim.tick(a=0,b=1)['x'], 7)
        self.assertEqual(sim.tick(a=0,b=0)['flag'], 1)

    def test_constants_and_internal_temporary_names_do_not_collide(self):
        text = """chip Names(clk,y) {
            clk=input; y=output; x[4]; ooc_expr_0;
            const STEP[4]=2;
            clock(clk) { x = x + STEP; }
            y = x[1];
        }"""
        generated = export_verilog(text)
        self.assertIn("localparam [3:0] STEP = 4'd2;", generated)
        self.assertIn('wire [3:0] ooc_expr_1', generated)
        self.assertEqual(EmittedClock(generated).tick()['x'], 2)

    def test_invalid_clocks_drivers_and_expressions_are_diagnostics(self):
        bodies = (
            'clock(missing) { x=1; }', 'clock(y) { x=1; }',
            'clock(clk) { y=1; }', 'clock(clk) { clk=1; }',
            'clock(clk) { STEP=1; }', 'clock(clk) { missing=1; }',
            'clock(clk) { x=-1; }', 'clock(clk) { x=x*2; }',
            'clock(clk) { x=x[4]; }', 'clock(clk) { x=1.2; }',
            'clock(clk) { x=1; } clock(clk) { x=2; }',
            'clock(clk) { else { x=1; } }', 'if (clk) { x=1; }',
            'const STEP[4]=1;', 'const missing[4];',
        )
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(CircuitError):
                export_verilog('chip Bad(clk,y) { clk=input; y=output; x[4]; const STEP[4]=2; ' + body + ' y=x[0]; }')

    def test_explicitly_sized_constant_and_scalar_state_initialization(self):
        text = """chip Preset(clk,y) { clk=input; y=output;
            const ON=1; flag; clock(clk) { flag=ON; } y=flag;
        }"""
        sim = EmittedClock(export_verilog(text))
        self.assertEqual(sim.state['flag'], 0)
        self.assertEqual(sim.tick()['flag'], 1)
