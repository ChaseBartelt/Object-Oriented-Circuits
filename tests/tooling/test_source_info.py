"""Editor recognition remains useful without importing a graphical editor."""
# Use incomplete and Unicode-containing documents to verify tolerant editor recognition.
# UTF-16 positions, included signatures, and method names must resolve without constructing Qt.

from pathlib import Path
import json
import re
import subprocess
import sys
import unittest

from circuitlab.tooling.source_info import SourceInfo, accepts_calls, gate_inputs, methods


class SourceInfoTest(unittest.TestCase):
    def test_named_main_accepts_chip_calls_and_explains_generation(self):
        for header in ('main {', 'main() {', 'main Board {', 'main Board() {'):
            with self.subTest(header=header):
                self.assertTrue(accepts_calls(header + '\n'))
                self.assertFalse(accepts_calls(header + '\n}\n'))
                help_text = SourceInfo(header).hover_help(0, 1)
                self.assertIn('during circuit generation', help_text)
                self.assertIn('top-level module name', help_text)

    def test_shared_grammar_highlights_vector_ports_and_internal_wires(self):
        path = Path(__file__).resolve().parents[2] / 'ide/vscode/syntaxes/OOC.json'
        grammar = json.loads(path.read_text(encoding='utf-8'))['repository']
        directions = {role: re.compile(next(pattern['match'] for pattern in grammar['properties']['patterns']
                      if pattern.get('name') == f'meta.declaration.{role}.ooc'))
                      for role in ('input', 'output', 'wire')}
        for source, expected in (
            ('a[8], b[8] = input;', 'input'),
            ('data[16] = output;', 'output'),
            ('link[8] = wire;', 'wire'),
            ('enable = input;', 'input'),
            ('SW${i}, enable${i + 1} = input;', 'input'),
            ('link${i}[8] = wire;', 'wire'),
            ('SW[9:0], reset = input;', 'input'),
            ('LEDR[9:0] = output;', 'output'),
        ):
            with self.subTest(source=source):
                match = directions[expected].match(source)
                self.assertIsNotNone(match)
                self.assertEqual(match[3], expected)

    def test_shared_grammar_recognizes_chip_calls_and_expression_tokens(self):
        path = Path(__file__).resolve().parents[2] / 'ide/vscode/syntaxes/OOC.json'
        grammar = json.loads(path.read_text(encoding='utf-8'))['repository']
        call = re.compile(grammar['calls']['patterns'][-1]['match'])
        for name in ('SelectByte', 'CountBy50', 'Tone', '_Private'):
            self.assertEqual(call.search(f'{name}(a, b);')[0], name)
        self.assertIsNone(call.search('data = input;'))
        operators = [re.compile(pattern['match']) for pattern in grammar['operators']['patterns']
                     if 'match' in pattern]
        self.assertIn({'include': '#operators'}, grammar['expression']['patterns'])
        for token in ('+', '-', '+=', '-=', '!', '&', '|', '^', '&&', '||', '==', '<=',
                      '<<', '>>', '<<=', '>>=', '&=', '|=', '^='):
            with self.subTest(token=token):
                self.assertTrue(any(operator.fullmatch(token) for operator in operators))
        for token in ('[', ']'):
            self.assertIsNotNone(re.fullmatch(grammar['punctuation']['match'], token))

    def test_impedance_help_and_wire_hover_keep_the_actual_terminals(self):
        source = 'Zs(2-j2) between(input, inv);'
        info = SourceInfo(source)
        self.assertIn('Theoretical complex impedance', info.hover_help(0, 1))
        self.assertIn('does not vary with frequency', info.hover_help(0, 1))
        self.assertEqual(info.wire_at(0, source.index('input')), (1, 'Zs', 0, 'input'))
        self.assertEqual(info.wire_at(0, source.index('inv')), (1, 'Zs', 1, 'inv'))
        self.assertEqual(SourceInfo('Zs input inv 2-j2').wire_at(0, 9), (1, 'Zs', 1, 'inv'))
        declared = SourceInfo('circuit Ztest(a,b) { }\nZtest(a,b);')
        self.assertIn('Call this circuit', declared.hover_help(1, 1))

    def test_sevenseg_hover_describes_bit_order_low_nibble_and_decimal_point(self):
        for source in ('HEX0[7:0] = sevenseg(SW[3:0]);',
                       'y = sevenseg((a >> 4), point);',
                       'if (sevenseg(a) != 255) {', 'y = sevenseg('):
            with self.subTest(source=source):
                info = SourceInfo(source)
                column = source.index('sevenseg') + 1
                help_text = info.hover_help(0, column)
                self.assertIn('lowest 4 bits', help_text)
                self.assertIn('8-bit active-low', help_text)
                self.assertIn('bit 0 = a', help_text)
                self.assertIn('7 = decimal point', help_text)
                self.assertIn('optional point expression is nonzero', help_text)
                self.assertIsNone(info.device_at(0, column))
        for source in ('sevenseg = input;', '# y = sevenseg(a);'):
            self.assertEqual(SourceInfo(source).hover_help(0, source.index('sevenseg')), '')
        declared = SourceInfo('circuit sevenseg(a,b) { }\nsevenseg(a,b);')
        self.assertIn('Call this circuit', declared.hover_help(1, 1))

    def test_bare_component_values_keep_editor_wire_recognition(self):
        for kind in 'RCLVIZ':
            source = f'{kind}load between(input, output);'
            info = SourceInfo(source)
            self.assertEqual(info.wire_at(0, source.index('input')), (1, f'{kind}load', 0, 'input'))
            self.assertEqual(info.wire_at(0, source.index('output')), (1, f'{kind}load', 1, 'output'))
        self.assertIn('unspecified', SourceInfo('Zload between(a,b);').hover_help(0, 1))

    def test_potentiometer_hover_preserves_pin_order_and_other_p_names(self):
        for clause in ('(10k,25%)', '(200)', ''):
            source = f'P1{clause} between(high, wiper, low);'
            info = SourceInfo(source)
            help_text = info.hover_help(0, 1)
            self.assertIn('between(high, wiper, low)', help_text)
            self.assertIn('50% midpoint', help_text)
            self.assertIn('2.5k ohms from wiper to low', help_text)
            for index, pin in enumerate(('high', 'wiper', 'low')):
                self.assertEqual(info.wire_at(0, source.index(pin)), (1, 'P1', index, pin))
            self.assertIsNone(info.wire_at(0, 1))
        for source in ('P = 3W;', 'P0 = input;', 'P1 = P0;', '# P1(10k,25%) between(a,b,c);'):
            self.assertEqual(SourceInfo(source).hover_help(0, source.index('P')), '')
        declared = SourceInfo('circuit Pot(a,b) { }\nPot(a,b);')
        self.assertIn('Call this circuit', declared.hover_help(1, 1))

    def test_clock_and_timer_headers_are_not_electrical_wire_connections(self):
        for source, token, help_text in (
            ('clock(clk, 50MHz) {', '50MHz', 'external clock'),
            ('clock(clk, 50MHz) {', 'clk', 'rising edge'),
            ('clock(clk, 50MHz) { every(500ms) {', '500ms', 'first update'),
        ):
            with self.subTest(source=source, token=token):
                info = SourceInfo(source)
                column = source.index(token)
                self.assertIsNone(info.wire_at(0, column))
                self.assertIsNone(info.device_at(0, column))
                self.assertIn(help_text, info.hover_help(0, column))
        for name in ('clock', 'every'):
            source = f'circuit {name}(a,b) {{ R1(1k) between(a,b); }}\n{name}(input, out);'
            info = SourceInfo(source)
            self.assertEqual(info.wire_at(1, len(name) + 1), (2, name, 0, 'input'))
            self.assertIn('Call this circuit', info.hover_help(1, 1))

    def test_shared_grammar_recognizes_units_and_underscores_in_expressions(self):
        path = Path(__file__).resolve().parents[2] / 'ide/vscode/syntaxes/OOC.json'
        grammar = json.loads(path.read_text(encoding='utf-8'))['repository']
        self.assertIn({'include': '#numbers'}, grammar['expression']['patterns'])
        number = re.compile(grammar['numbers']['match'])
        for literal, value, unit in (
            ('50MHz', '50', 'MHz'), ('500ms', '500', 'ms'),
            ('24_999_999', '24_999_999', None), ('50_000kHz', '50_000', 'kHz'),
            ('1.25us', '1.25', 'us'), ('10k', '10', 'k'), ('1e-3', '1e-3', None),
        ):
            with self.subTest(literal=literal):
                match = number.fullmatch(literal)
                self.assertIsNotNone(match)
                self.assertEqual(match.groups(), (value, unit))

    def test_import_and_lookup_do_not_load_qt(self):
        script = """
import sys
from circuitlab.tooling.source_info import SourceInfo
assert SourceInfo('not(vcc, a, y);').device_at(0, 1) == ('builtin', 'NOT', 1)
assert not any(name.startswith('PySide6') for name in sys.modules)
assert not any(name.startswith('circuitlab.viewer') for name in sys.modules)
"""
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_incomplete_definitions_and_legacy_default_terminals(self):
        source = "series pair { R1 = 1k; }\nchip demo(vcc, a, y, gnd) {\n"
        self.assertEqual(methods(source), {
            "pair": ("series", "pair", "a, b"),
            "demo": ("chip", "demo", "vcc, a, y, gnd"),
        })
        self.assertTrue(accepts_calls(source))
        self.assertTrue(accepts_calls("main {\nfor (int i=0; i<3; i++) {\n"))
        self.assertFalse(accepts_calls("(battery supply)\nV = 5\n"))
        self.assertFalse(accepts_calls("main {\n}\n"))

    def test_imports_refresh_and_local_declarations_take_precedence(self):
        included = ["chip demo(vcc,a,y,gnd) { R1(1k) between(a,y); }"]
        info = SourceInfo('include("parts.ckt");\ndemo(vcc,a,y,0);', lambda _: included)
        self.assertEqual(info.device_at(1, 1), ("chip", "demo", ("vcc", "a", "y", "gnd")))
        included[0] = "chip demo(power,input,output,ground) { R1(1k) between(input,output); }"
        self.assertIn("demo(power, input, output, ground)", info.hover_help(1, 1))
        local = SourceInfo(info.source + "\nchip demo(p,a,b,g) {", lambda _: included)
        self.assertEqual(local.method_signatures()["demo"][2], "p, a, b, g")

    def test_device_help_and_declared_pin_orders(self):
        source = "chip timer(gnd, trig, out, vcc) {\nnot(vcc,trig,out);\n}\ntimer(0,a,y,vcc);"
        info = SourceInfo(source)
        self.assertEqual(info.device_at(0, 6), ("chip", "timer", ("gnd", "trig", "out", "vcc")))
        self.assertEqual(info.device_at(3, 2), ("chip", "timer", ("gnd", "trig", "out", "vcc")))
        self.assertEqual(info.device_at(1, 1), ("builtin", "NOT", 1))
        self.assertIn("transistor circuitry", info.hover_help(1, 1))
        self.assertIn("Non-inverting", SourceInfo("op1(a,b,y);").hover_help(0, 1))
        self.assertIn("base, collector, emitter", SourceInfo("transQ1(b,c,e,npn);").hover_help(0, 2))
        self.assertEqual(gate_inputs("nor", "vcc, (a, b, c), y"), 3)
        self.assertEqual(gate_inputs("and", "vcc, gnd, a, b, y"), 2)

    def test_pin_bank_signatures_expand_in_declared_order_and_preserve_vectors(self):
        source = 'chip Board(SW[2:0], data[8], LEDR[1:0]) {\nSW[2:0] = input;\n}\nBoard(a,b,c,data,y,z);'
        info = SourceInfo(source)
        pins = ('SW2', 'SW1', 'SW0', 'data[8]', 'LEDR1', 'LEDR0')
        self.assertEqual(info.device_at(0, 6), ('chip', 'Board', pins))
        self.assertEqual(info.device_at(3, 1), ('chip', 'Board', pins))
        self.assertEqual(methods(source)['board'][2], ', '.join(pins))
        for line, column in ((0, source.index('SW[')), (1, 1)):
            help_text = info.hover_help(line, column)
            self.assertIn('SW2, SW1, SW0', help_text)
            self.assertIn('descending, inclusive', help_text)
            self.assertIn('one vector signal', help_text)
        self.assertEqual(methods('circuit Analog(a[2:0],b) {')['analog'][2], 'a[2:0], b')
        self.assertEqual(methods('chip Unfinished(SW[9:], y) {')['unfinished'][2], 'SW[9:], y')
        self.assertNotIn('Pin bank:', SourceInfo('y = data[3:0];').hover_help(0, 6))

    def test_computed_wire_hover_uses_chip_scope_and_keeps_state_and_bit_assignments_distinct(self):
        source = '''chip Select(a, y) {
    a[8] = input; y[8] = output;
    requests[8] = a;
    winner[8] = a[0] ? requests : 0;
    phase[32] = 0;
    requests[3] = a[3];
    y = winner;
}'''
        info = SourceInfo(source)
        for line in (2, 3):
            help_text = info.hover_help(line, 7)
            self.assertIn('Computed wire: 8 bits', help_text)
            self.assertIn('follows the expression', help_text)
            self.assertIn('does not store state', help_text)
        for line in (1, 4, 5):
            self.assertNotIn('Computed wire', info.hover_help(line, 7))
        one_line = 'chip X(a,y) { a=input; y=output; w[8]=a; w[3]=a; y=w[0]; }'
        inline = SourceInfo(one_line)
        self.assertIn('Computed wire', inline.hover_help(0, one_line.index('w[8]')))
        self.assertNotIn('Computed wire', inline.hover_help(0, one_line.index('w[3]')))
        for text in ('circuit Analog(a,b) { x[8]=a; }', 'chip X(a,y) { clock(a,1Hz) { x[8]=a; } }'):
            self.assertNotIn('Computed wire', SourceInfo(text).hover_help(0, text.index('x[8]')))

    def test_modern_legacy_and_grouped_wire_locations(self):
        rows = [
            ("R1(1k) between(start, middle); R2 middle 0 1k;", "start", (1, "R1", 0, "start")),
            ("R1(1k) between(start, middle); R2 middle 0 1k;", "middle 0", (1, "R2", 0, "middle")),
            ("nor(vcc, (a, b), out);", "b)", (1, "nor", 2, "b")),
            ("transQ1(base, collector, emitter, npn);", "collector", (1, "transQ1", 1, "collector")),
        ]
        for source, token, expected in rows:
            with self.subTest(source=source, token=token):
                self.assertEqual(SourceInfo(source).wire_at(0, source.index(token)), expected)
        self.assertIsNone(SourceInfo("# R1(1k) between(a,b);").wire_at(0, 18))
        self.assertIsNone(SourceInfo("R1(1k) between(a,").wire_at(0, 14))

    def test_utf16_columns_and_crlf_lines_match_editor_coordinates(self):
        first = 'include("😀.ckt"); nor(vcc, (a, b), out);'
        info = SourceInfo(first + "\r\nR1(1k) between(out, 0);\r\n")
        device_column = len(first[:first.index("nor")].encode("utf-16-le")) // 2
        wire_column = len(first[:first.index("out")].encode("utf-16-le")) // 2
        self.assertEqual(info.device_at(0, device_column), ("builtin", "NOR", 2))
        self.assertEqual(info.wire_at(0, wire_column), (1, "nor", 3, "out"))
        self.assertEqual(info.wire_at(1, 15), (2, "R1", 0, "out"))
        emoji = first.index("😀")
        self.assertIsNone(info.position(0, emoji + 1))
        self.assertEqual(info.position(2, 0), ("", 0))

    def test_slash_comments_do_not_supply_hover_targets_or_shift_columns(self):
        row = "😀 */ nor(vcc, a, result); // not(vcc, b, fake);"
        info = SourceInfo("/*\nnot(vcc, a, hidden);\n" + row)
        self.assertIsNone(info.device_at(1, 0))
        self.assertIsNone(info.wire_at(1, 12))
        self.assertEqual(info.hover_help(1, 0), "")
        column = lambda name: len(row[:row.index(name)].encode("utf-16-le")) // 2
        self.assertEqual(info.device_at(2, column("nor")), ("builtin", "NOR", 1))
        self.assertEqual(info.wire_at(2, column("result")), (3, "nor", 2, "result"))
        self.assertIsNone(info.wire_at(2, column("fake")))
        include = 'include("chips//file/*part*/#v1.ckt"); // loader hint'
        self.assertIn("bundled chip", SourceInfo(include).hover_help(0, 0))

    def test_outside_document_positions_are_ignored(self):
        info = SourceInfo("nor(vcc,a,y);")
        for line, column in ((-1, 0), (1, 0), (0, -1), (0, 100), (0, 0.5)):
            with self.subTest(line=line, column=column):
                self.assertIsNone(info.device_at(line, column))
                self.assertIsNone(info.wire_at(line, column))
                self.assertEqual(info.hover_help(line, column), "")


if __name__ == "__main__":
    unittest.main()
