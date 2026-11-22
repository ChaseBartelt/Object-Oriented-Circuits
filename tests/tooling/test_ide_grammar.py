"""Check OOC scopes with the same TextMate engine shipped by VS Code."""
# Tokenize with the editor's TextMate engine so assertions describe real highlighting scopes.
# Comments, incomplete text, and similar signal names must not be mistaken for valid syntax.

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
GRAMMAR = ROOT / "ide/vscode/syntaxes/OOC.json"


def vscode_engine():
    """Find stable/Insiders installs, including versioned Windows resources."""
    roots = []
    for command in ("code", "code-insiders"):
        if executable := shutil.which(command):
            roots.append(Path(executable).resolve().parent.parent)
    for variable in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        if directory := os.environ.get(variable):
            parent = Path(directory) / "Programs" if variable == "LOCALAPPDATA" else Path(directory)
            roots.extend(parent / name for name in ("Microsoft VS Code", "Microsoft VS Code Insiders"))
    roots.extend((Path("/usr/share/code"), Path("/usr/share/code-insiders")))
    for root in dict.fromkeys(roots):
        executable = next((root / name for name in ("Code.exe", "Code - Insiders.exe", "code", "code-insiders")
                           if (root / name).is_file()), None)
        if executable is None:
            continue
        for app in (root / "resources/app", *root.glob("*/resources/app")):
            for directory in ("node_modules.asar", "node_modules"):
                modules = app / directory
                wasm = app / (directory + ".unpacked" if directory.endswith(".asar") else directory)
                wasm /= "vscode-oniguruma/release/onig.wasm"
                if modules.exists() and wasm.is_file():
                    return executable, modules, wasm
    return None


TOKENIZE = r"""
const fs = require('fs');
const [modules, wasmPath, grammarPath] = process.argv.slice(1);
const textmate = require(modules + '/vscode-textmate');
const oniguruma = require(modules + '/vscode-oniguruma');
(async () => {
    const bytes = fs.readFileSync(wasmPath);
    await oniguruma.loadWASM(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
    const registry = new textmate.Registry({
        onigLib: Promise.resolve(oniguruma),
        loadGrammar: async scope => scope === 'source.ooc'
            ? textmate.parseRawGrammar(fs.readFileSync(grammarPath, 'utf8'), grammarPath) : null,
    });
    const grammar = await registry.loadGrammar('source.ooc');
    const sources = JSON.parse(fs.readFileSync(0, 'utf8'));
    const result = {};
    for (const [name, source] of Object.entries(sources)) {
        let stack = textmate.INITIAL;
        result[name] = source.split('\n').map(line => {
            const parsed = grammar.tokenizeLine(line, stack);
            stack = parsed.ruleStack;
            return {line, tokens: parsed.tokens};
        });
    }
    process.stdout.write(JSON.stringify(result));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


class GrammarStructureTest(unittest.TestCase):
    def test_declared_port_roles_have_concrete_color_defaults(self):
        package = json.loads((ROOT / 'ide/vscode/package.json').read_text(encoding='utf-8'))
        rules = package['contributes']['configurationDefaults']['editor.tokenColorCustomizations']['textMateRules']
        colors = {scope: rule['settings']['foreground'] for rule in rules
                  for scope in ([rule['scope']] if isinstance(rule['scope'], str) else rule['scope'])}
        for direction, color in (('input', '#f6a652'), ('output', '#659cd7'), ('wire', '#4ec9b0')):
            for role in ('storage.modifier.pin-direction.ooc',):
                self.assertEqual(colors[f'meta.declaration.{direction}.ooc {role}'], color)
        self.assertEqual(colors['keyword.operator.instruction.arm.ooc'], '#b45a5a')
        self.assertEqual(colors['keyword.control.directive.arm.ooc'], '#b45a5a')
        self.assertEqual(colors['variable.language.register.arm.ooc'], '#9cdcfe')
        self.assertEqual(colors['constant.numeric.arm.ooc'], '#ce9178')
        self.assertEqual(colors['keyword.control.clear.ooc'], '#659cd7')
        self.assertEqual(colors['string.quoted.double.ooc'], '#aed6ff')
        self.assertEqual(colors['entity.name.section.port-group.ooc'], '#d4d4d4')
        for direction in ('input', 'output', 'wire'):
            self.assertNotIn(f'meta.declaration.{direction}.ooc variable.other.readwrite.port.ooc', colors)
            self.assertNotIn(f'meta.declaration.{direction}.ooc entity.name.section.ooc', colors)
        for family, color in (('comparison', '#82aaff'), ('logical', '#c792ea'),
                              ('bitwise', '#4ec9b0'), ('shift', '#ffb86c'),
                              ('ternary', '#dcdcaa'), ('arithmetic', '#e6b98b'),
                              ('assignment', '#9aa0a6')):
            self.assertEqual(colors[f'keyword.operator.{family}.ooc'], color)

    def test_named_groups_and_legacy_roles_exclude_widths_references_and_strings(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is unavailable for the editor declaration check.')
        source = ('pins = "memory_a,\ninput fake { IGNORE[32]; }";\n'
                  'input bus_addr { CLK; for(int i=0; i<2; i++) { pin${i}[8]; R*{i}[32]; } }\n'
                  'output status { DONE[32]; }\nwire internal { scratch[32]; }\n'
                  'legacy[8]=input; result[8]=output;\n'
                  'result=legacy; Call(legacy,result); result=left*right;')
        script = ('const fs=require("fs"); const scanner=require(process.argv[1]); '
                  'process.stdout.write(JSON.stringify(scanner.analyzeSource(fs.readFileSync(0,"utf8"))));')
        completed = subprocess.run([node, '-e', script, str(ROOT / 'ide/vscode/structure.js')],
                                   input=source.encode(), capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        parsed = json.loads(completed.stdout)
        groups = [item for item in parsed['structure'] if item['kind'] in {'input', 'output', 'wire'}]
        self.assertEqual([(item['name'], item['detail']) for item in groups],
                         [('bus_addr', 'input'), ('status', 'output'), ('internal', 'wire')])
        roles = [(source[item['start']:item['end']], item['direction']) for item in parsed['portTokens'] if item['kind'] != 'label']
        self.assertIn(('input', 'input'), roles)
        self.assertIn(('output', 'output'), roles)
        self.assertIn(('wire', 'wire'), roles)
        self.assertFalse(any(text in {'bus_addr', 'pin', 'R', 'DONE', 'scratch', 'legacy',
                                     'result', 'IGNORE', 'fake', '8', '32', 'i', 'left', 'right'}
                             for text, _ in roles))

    def test_grouped_ports_have_member_symbols_and_preserve_source_ranges(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is unavailable for the editor outline check.')
        source = ('chip Grouped(CLK, word0, word1, PC) {\n'
                  'input { CLK; for(int i=0; i<2; i++) { word${i}[8]; } }\n'
                  'output { PC[32]; }\n'
                  '// output { ignored; }\n'
                  'if (CLK) { PC=word0; } else { PC=word1; }\n}')
        script = ('const fs=require("fs"); const scanner=require(process.argv[1]); '
                  'process.stdout.write(JSON.stringify(scanner.analyzeSource(fs.readFileSync(0,"utf8"))));')
        completed = subprocess.run([node, '-e', script, str(ROOT / 'ide/vscode/structure.js')],
                                   input=source.encode(), capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        parsed = json.loads(completed.stdout)
        children = parsed['structure'][0]['children']
        self.assertEqual([child['kind'] for child in children], ['input', 'output', 'if', 'else'])
        self.assertEqual(children[0]['children'][0]['name'], 'CLK')
        generated = children[0]['children'][1]['children'][0]
        self.assertEqual((generated['name'], generated['kind'], generated['detail']),
                         ('word${i}', 'pin', 'input [8]'))
        pc = children[1]['children'][0]
        self.assertEqual((pc['name'], pc['detail']), ('PC', 'output [32]'))
        for symbol in (generated, pc):
            self.assertEqual(source[symbol['selectionStart']:symbol['selectionEnd']], symbol['name'])
        self.assertFalse(parsed['references'])

    def test_repository_references_and_patterns_are_valid(self):
        grammar = json.loads(GRAMMAR.read_text(encoding="utf-8"))
        self.assertEqual(grammar["scopeName"], "source.ooc")

        def inspect(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "include" and child.startswith("#"):
                        self.assertIn(child[1:], grammar["repository"])
                    elif key in {"match", "begin", "end"}:
                        re.compile(child)
                    else:
                        inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)

        inspect(grammar)

    def test_indexed_loop_and_switch_blocks_have_outline_nodes(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is unavailable for the editor outline check.')
        source = ('chip Follow(SW[7:0],LEDR[7:0]) { SW[7:0]=input; LEDR[7:0]=output; '
                  'for (i=0; i<8; i=i+1) { LEDR[i]=SW[i]; } '
                  'switch(fsm) { case EARLY: LEDR[7:0]=data[7:0]; break; '
                  'default: LEDR[7:0]=SW1 ? EARLY : WAITING; break; } }')
        script = ('const fs=require("fs"); const scanner=require(process.argv[1]); '
                  'process.stdout.write(JSON.stringify(scanner.analyzeSource(fs.readFileSync(0,"utf8"))));')
        completed = subprocess.run([node, '-e', script, str(ROOT / 'ide/vscode/structure.js')],
                                   input=source, capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        parsed = json.loads(completed.stdout)
        self.assertEqual(parsed['structure'][0]['name'], 'Follow')
        self.assertEqual(parsed['structure'][0]['detail'], 'chip (SW[7:0],LEDR[7:0])')
        self.assertEqual([child['kind'] for child in parsed['structure'][0]['children']], ['for', 'switch'])
        self.assertFalse(parsed['references'])


class TextMateGrammarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        engine = vscode_engine()
        if engine is None:
            raise unittest.SkipTest("VS Code with bundled TextMate/Oniguruma is unavailable; grammar structure checks still run.")
        executable, modules, wasm = engine
        sources = {
            "operator_families": "\n".join((
                "a < b; a > b; a <= b; a >= b; a == b; a != b;",
                "a && b || !c;",
                "a & b | c ^ ~d;",
                "a << b >> c;",
                "a + b - c * d / e % f; a++; b--;",
                "a = b; a += b; a -= b; a *= b; a /= b; a %= b; a &= b; a |= b; a ^= b; a <<= b; a >>= b;",
                "word[31:0] = (flag ?",
                "    left[7:0]",
                "    : other ? x : y); label: word=0;",
                '// && || != <<= ? :',
                'pins="a && b,',
                'x ? y : z >= |= <<=";',
            )),
            "clear_block": ('clock(clk) { if(reset) { clear { scalar, memory, flags[3:0] }; } }\n'
                            '// clear { ignored; }\npins = "first,\nclear { string_text; }";'),
            "named_ports": "\n".join((
                "input bus_addr { input_value[32], other_value[32]; compare_zero; }",
                "output fault { next_value[32]; }",
                "wire internal { temporary[8]; }",
                "old_input[8] = input;",
                "old_output[8] = output;",
                "old_wire[8] = wire;",
                "next_value = input_value; Child(input_value,next_value);",
            )),
            "quoted_lists": 'memory_pins = "first[32],\nUPPER, lower, 0x20, // literal text\ninput fake { PC[8]; }";\ninput real { actual[8]; }',
            "arm_palette": "\n".join((
                "arm(address, word, valid) {",
                ".word 0x20000400, reset + 1;",
                "reset: lDr R0, literal; // assembly note",
                "MOVS r1, #5; @ alternate note",
                "ADDS r0, #2;",
                "B reset;",
                "PUSH {r4-r7, lr};",
                "literal: .byte 12;",
                "}",
                "# outside legacy comment",
            )),
            "diodes": "D.Schot1() between(anode, cathode);\nD.Zener1(5.1V) between(0, regulated);",
            "port_groups": "\n".join((
                "chip Grouped(CLK, RESET, PC, MSP) {",
                "    input {",
                "        CLK, RESET; SW[3:0];",
                "        for (int i = 0; i < 2; i++) {",
                "            word${i}[8];",
                "        }",
                "    }",
                "    output {",
                "        PC[32], MSP[32];",
                "        IR[16], NZCV[4];",
                "        BOOTED, FAULT;",
                "    }",
                "    phase[32] = 0;",
                "}",
                "// input { ignored; }",
            )),
            "fpga": "\n".join((
                "chip HYBRID01(clk, SW0, LEDR0) {",
                "    clk, SW0 = input; LEDR0 = output;",
                "    const STEP[32] = 24_999_999;",
                "    phase[32] = 0; flag;",
                "    clock(clk, 50MHz) {",
                "        phase += STEP;",
                "        every(500ms) { flag = !flag; }",
                "        if ((phase[31] ^ SW0) == 1) { flag = 0; }",
                "    }",
                "    LEDR0 = phase[31];",
                "}",
            )),
            "generation": "main Board {\nfor (int i = 0; i < 3; i++) {\nlink${i}[8] = wire;\nCopy(link${i}, out${i});\n}\n}",
            "bus_logic": "\n".join((
                "for (int i = 0; i < 8; i++) {",
                "    LEDR[i] = SW[i];",
                "}",
                "LEDR[7:0] = data[7:0];",
                "fsm = SW1 ? EARLY : WAITING;",
                "if ((SW1 ? EARLY : WAITING) == fsm) { LEDR[7:0] = 0; }",
                "for (i=0; i<8; i=i+1) { LEDR[i] = SW[i]; }",
            )),
            "pin_banks": "\n".join((
                "chip HYBRID01(SW[9:0], LEDR[9:0]) {",
                "    SW[9:0] = input;",
                "    LEDR[9:0] = output;",
                "    data[10] = wire;",
                "}",
            )),
            "inline_wires": "\n".join((
                "chip Select(SW[9:0], LEDR[9:0]) {",
                "    SW[9:0] = input; LEDR[9:0] = output;",
                "    requests[8] = SW[7:0];",
                "    winner[8] = SW9 ? requests : 0;",
                "    phase[32] = 0;",
                "    requests[3] = SW3;",
                "}",
            )),
            "switch_logic": "\n".join((
                "switch(fsm) {",
                "case EARLY: LEDR[7:0] = data[7:0];",
                "    break;",
                "case WAITING: break;",
                "default: LEDR[7:0] = 0; break;",
                "}",
                "# switch(fsm) { case EARLY: break; default: break; }",
            )),
            "analog": 'R1(1k) between(input, out); C1(100n) between(out, 0);\ntransQ1(base, out, 0, npn);\ninclude("chips/clock.ckt"); # phase[31] += 50MHz',
            "potentiometer": 'P1(10k,25%) between(high,wiper,low);\nPbare between(high,wiper,low);\nP = 3W;\nchip Pins(P0,P1) { P0 = input; P1 = output; }',
            "incomplete": "data[8 # unfinished selector\nphase += 440Hz;\nlet enabled = true;",
            "thumb_rom": "arm(address, word) {\n.word reset + 1;\nreset: MOVS r0, #5; // immediate\n}\n# legacy comment",
            "sevenseg": "\n".join((
                "HEX0[7:0] = sevenseg(SW[3:0]);",
                "if (sevenseg(value >> 4, point) != 255) {",
                "    bits <<= 1; bits >>= amount;",
                "    bits &= mask; bits |= flags; bits ^= toggles;",
                "    let digit = sevenseg(value << 1);",
                "}",
                "sevenseg = input;",
                "# HEX0[7:0] = sevenseg(SW[3:0]);",
            )),
        }
        cls.example_names = []
        for path in sorted((ROOT / "examples/advanced/fpga").glob("*.ckt")):
            sources[path.name] = path.read_text(encoding="utf-8")
            cls.example_names.append(path.name)
        completed = subprocess.run(
            [str(executable), "-e", TOKENIZE, str(modules), str(wasm), str(GRAMMAR)],
            input=json.dumps(sources), capture_output=True, text=True, timeout=30,
            env={**os.environ, "ELECTRON_RUN_AS_NODE": "1"},
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        cls.rows = json.loads(completed.stdout)

    def assert_scope(self, case, number, text, scope, *, occurrence=0):
        row = self.rows[case][number]
        start = -1
        for _ in range(occurrence + 1):
            start = row["line"].index(text, start + 1)
        for column in range(start, start + len(text)):
            token = next(token for token in row["tokens"] if token["startIndex"] <= column < token["endIndex"])
            self.assertIn(scope, token["scopes"], (case, number, text, token))

    def test_arm_immediates_have_their_own_scope_inside_rom(self):
        self.assert_scope("thumb_rom", 0, "arm", "storage.type.ooc")
        self.assert_scope("thumb_rom", 1, ".word", "keyword.control.directive.arm.ooc")
        self.assert_scope("thumb_rom", 2, "reset", "entity.name.label.ooc")
        self.assert_scope("thumb_rom", 2, "MOVS", "keyword.operator.instruction.arm.ooc")
        self.assert_scope("thumb_rom", 2, "#5", "constant.numeric.arm.ooc")
        self.assert_scope("thumb_rom", 2, "// immediate", "comment.line.double-slash.ooc")
        self.assert_scope("thumb_rom", 4, "# legacy comment", "comment.line.number-sign.ooc")

    def test_clear_blocks_use_their_keyword_scope_without_coloring_quoted_text(self):
        self.assert_scope('clear_block', 0, 'clear', 'keyword.control.clear.ooc')
        self.assert_scope('clear_block', 0, 'flags', 'variable.other.readwrite.ooc')
        self.assert_scope('clear_block', 0, '3', 'constant.numeric.ooc')
        self.assert_scope('clear_block', 1, 'clear', 'comment.line.double-slash.ooc')
        self.assert_scope('clear_block', 3, 'clear', 'string.quoted.double.ooc')

    def test_operator_families_have_specific_scopes_with_longest_token_precedence(self):
        for row, family, operators in (
            (0, 'comparison', ('<', '>', '<=', '>=', '==', '!=')),
            (1, 'logical', ('&&', '||', '!')),
            (2, 'bitwise', ('&', '|', '^', '~')),
            (3, 'shift', ('<<', '>>')),
            (4, 'arithmetic', ('+', '-', '*', '/', '%', '++', '--')),
            (5, 'assignment', ('=', '+=', '-=', '*=', '/=', '%=', '&=', '|=', '^=', '<<=', '>>=')),
        ):
            for operator in operators:
                with self.subTest(operator=operator):
                    self.assert_scope('operator_families', row, operator, f'keyword.operator.{family}.ooc')
                    self.assert_scope('operator_families', row, operator, 'keyword.operator.ooc')

    def test_multiline_ternary_colons_keep_ranges_and_labels_as_punctuation(self):
        self.assert_scope('operator_families', 6, '?', 'keyword.operator.ternary.ooc')
        self.assert_scope('operator_families', 6, ':', 'punctuation.separator.ooc')
        self.assert_scope('operator_families', 7, ':', 'punctuation.separator.ooc')
        self.assert_scope('operator_families', 8, ':', 'keyword.operator.ternary.ooc')
        self.assert_scope('operator_families', 8, ':', 'keyword.operator.ternary.ooc', occurrence=1)
        self.assert_scope('operator_families', 8, ':', 'punctuation.separator.ooc', occurrence=2)

    def test_operator_scopes_do_not_enter_comments_or_quoted_metadata(self):
        self.assert_scope('operator_families', 9, '&&', 'comment.line.double-slash.ooc')
        for row, operators in ((10, ('&&',)), (11, ('?', ':', '>=', '|=', '<<='))):
            for operator in operators:
                self.assert_scope('operator_families', row, operator, 'string.quoted.double.ooc')

    def test_named_and_legacy_declarations_have_semantic_color_roles(self):
        for row, direction, names in ((0, 'input', ('input_value', 'other_value', 'compare_zero')),
                                      (1, 'output', ('next_value',)), (2, 'wire', ('temporary',)),
                                      (3, 'input', ('old_input',)), (4, 'output', ('old_output',)),
                                      (5, 'wire', ('old_wire',))):
            self.assert_scope('named_ports', row, direction, f'meta.declaration.{direction}.ooc')
            self.assert_scope('named_ports', row, direction, 'storage.modifier.pin-direction.ooc',
                              occurrence=int(row >= 3))
            for name in names:
                self.assert_scope('named_ports', row, name, f'meta.declaration.{direction}.ooc')
                self.assert_scope('named_ports', row, name, 'variable.other.readwrite.port.ooc')
        self.assert_scope('named_ports', 0, 'bus_addr', 'entity.name.section.ooc')
        self.assert_scope('named_ports', 1, 'fault', 'entity.name.section.ooc')
        self.assert_scope('named_ports', 0, '32', 'constant.numeric.ooc')
        for token in self.rows['named_ports'][6]['tokens']:
            self.assertNotIn('variable.other.readwrite.port.ooc', token['scopes'])

    def test_quoted_multiline_pin_lists_are_uniform_strings(self):
        for row in (0, 1, 2):
            for text in (('first', '32') if row == 0 else
                         ('UPPER', 'lower', '0x20', '// literal') if row == 1 else
                         ('input', 'fake', 'PC', '8')):
                self.assert_scope('quoted_lists', row, text, 'string.quoted.double.ooc')
        self.assert_scope('quoted_lists', 3, 'input', 'storage.modifier.pin-direction.ooc')
        self.assert_scope('quoted_lists', 3, 'actual', 'variable.other.readwrite.port.ooc')

    def test_arm_palette_scopes_include_targets_case_insensitive_opcodes_and_comments(self):
        self.assert_scope('arm_palette', 1, '.word', 'keyword.control.directive.arm.ooc')
        self.assert_scope('arm_palette', 7, '.byte', 'keyword.control.directive.arm.ooc')
        self.assert_scope('arm_palette', 2, 'lDr', 'keyword.operator.instruction.arm.ooc')
        self.assert_scope('arm_palette', 2, 'R0', 'variable.language.register.arm.ooc')
        self.assert_scope('arm_palette', 2, 'literal', 'entity.name.label.reference.arm.ooc')
        self.assert_scope('arm_palette', 3, '#5', 'constant.numeric.arm.ooc')
        self.assert_scope('arm_palette', 3, '@ alternate', 'comment.line.at-sign.arm.ooc')
        self.assert_scope('arm_palette', 5, 'reset', 'entity.name.label.reference.arm.ooc')
        self.assert_scope('arm_palette', 6, 'r7', 'variable.language.register.arm.ooc')
        self.assert_scope('arm_palette', 9, '# outside', 'comment.line.number-sign.ooc')

    def test_fpga_declarations_color_names_widths_and_direction(self):
        self.assert_scope("fpga", 0, "chip", "storage.type.ooc")
        self.assert_scope("fpga", 0, "HYBRID01", "entity.name.function.ooc")
        self.assert_scope("fpga", 1, "SW0", "variable.other.readwrite.ooc")
        self.assert_scope("fpga", 1, "input", "storage.modifier.pin-direction.ooc")
        self.assert_scope("fpga", 1, "output", "storage.modifier.pin-direction.ooc")
        self.assert_scope("fpga", 2, "const", "storage.modifier.ooc")
        self.assert_scope("fpga", 2, "STEP", "constant.other.ooc")
        self.assert_scope("fpga", 2, "32", "constant.numeric.ooc")
        self.assert_scope("fpga", 2, "24_999_999", "constant.numeric.ooc")
        self.assert_scope("fpga", 3, "phase", "variable.other.readwrite.ooc")
        self.assert_scope("fpga", 3, "flag", "variable.other.readwrite.ooc")

    def test_typed_diode_names_are_components_and_contacts_are_wire_names(self):
        self.assert_scope('diodes', 0, 'D.Schot1', 'entity.name.function.component.ooc')
        self.assert_scope('diodes', 1, 'D.Zener1', 'entity.name.function.component.ooc')

    def test_grouped_port_headers_members_and_nested_generation_have_scopes(self):
        self.assert_scope('port_groups', 1, 'input', 'storage.modifier.pin-direction.ooc')
        self.assert_scope('port_groups', 7, 'output', 'storage.modifier.pin-direction.ooc')
        for row, name in ((2, 'CLK'), (2, 'RESET'), (2, 'SW'), (4, 'word'),
                          (8, 'PC'), (8, 'MSP'), (9, 'IR'), (9, 'NZCV'),
                          (10, 'BOOTED'), (10, 'FAULT')):
            self.assert_scope('port_groups', row, name, 'variable.other.readwrite.ooc')
        self.assert_scope('port_groups', 2, ':', 'punctuation.separator.ooc')
        self.assert_scope('port_groups', 3, 'for', 'keyword.control.ooc')
        self.assert_scope('port_groups', 4, '${i}', 'meta.interpolation.ooc')
        self.assert_scope('port_groups', 8, '32', 'constant.numeric.ooc')
        self.assert_scope('port_groups', 14, 'input', 'comment.line.double-slash.ooc')
        self.assertTrue(all('meta.declaration.ports.ooc' not in token['scopes']
                            for token in self.rows['port_groups'][12]['tokens']))

    def test_seven_segment_function_and_vector_operations(self):
        for line in (0, 1, 4):
            self.assert_scope('sevenseg', line, 'sevenseg', 'support.function.ooc')
        for line, operators in ((1, ('>>', '!=')), (2, ('<<=', '>>=')),
                                (3, ('&=', '|=', '^=')), (4, ('<<',))):
            for operator in operators:
                self.assert_scope('sevenseg', line, operator, 'keyword.operator.ooc')
        self.assert_scope('sevenseg', 6, 'sevenseg', 'variable.other.readwrite.ooc')
        self.assert_scope('sevenseg', 7, 'sevenseg', 'comment.line.number-sign.ooc')

    def test_pin_banks_color_ranges_and_directions_without_changing_vector_syntax(self):
        self.assert_scope('pin_banks', 0, 'HYBRID01', 'entity.name.function.ooc')
        for row in (0, 1, 2):
            self.assert_scope('pin_banks', row, '9', 'constant.numeric.ooc')
            self.assert_scope('pin_banks', row, ':', 'punctuation.separator.ooc')
        self.assert_scope('pin_banks', 1, 'input', 'storage.modifier.pin-direction.ooc')
        self.assert_scope('pin_banks', 2, 'output', 'storage.modifier.pin-direction.ooc')
        self.assert_scope('pin_banks', 3, 'wire', 'storage.modifier.pin-direction.ooc')

    def test_inline_computed_wires_and_literal_state_keep_expression_scopes(self):
        for row, name, width in ((2, 'requests', '8'), (3, 'winner', '8'), (4, 'phase', '32')):
            self.assert_scope('inline_wires', row, name, 'variable.other.readwrite.ooc')
            self.assert_scope('inline_wires', row, width, 'constant.numeric.ooc')
            self.assert_scope('inline_wires', row, '=', 'keyword.operator.ooc')
        self.assert_scope('inline_wires', 2, 'SW', 'variable.other.readwrite.ooc')
        self.assert_scope('inline_wires', 2, ':', 'punctuation.separator.ooc')
        self.assert_scope('inline_wires', 3, '?', 'keyword.operator.ooc')
        self.assert_scope('inline_wires', 4, '0', 'constant.numeric.ooc')
        self.assert_scope('inline_wires', 5, '3', 'constant.numeric.ooc')

    def test_timed_blocks_expressions_and_bit_reads(self):
        self.assert_scope("fpga", 4, "clock", "keyword.control.ooc")
        self.assert_scope("fpga", 4, "clk", "variable.other.readwrite.ooc")
        self.assert_scope("fpga", 4, "50", "constant.numeric.ooc")
        self.assert_scope("fpga", 4, "MHz", "constant.other.unit.ooc")
        self.assert_scope("fpga", 5, "+=", "keyword.operator.ooc")
        self.assert_scope("fpga", 6, "every", "keyword.control.ooc")
        self.assert_scope("fpga", 6, "ms", "constant.other.unit.ooc")
        self.assert_scope("fpga", 7, "phase", "variable.other.readwrite.ooc")
        self.assert_scope("fpga", 7, "31", "constant.numeric.ooc")
        self.assert_scope("fpga", 9, "[", "punctuation.section.brackets.begin.ooc")
        self.assert_scope("fpga", 9, "]", "punctuation.section.brackets.end.ooc")

    def test_generated_signal_names_retain_interpolation_and_width_scopes(self):
        self.assert_scope("generation", 0, "main", "keyword.declaration.main.ooc")
        self.assert_scope("generation", 1, "for", "keyword.control.ooc")
        self.assert_scope("generation", 1, "int", "storage.type.ooc")
        self.assert_scope("generation", 2, "link", "variable.other.readwrite.ooc")
        self.assert_scope("generation", 2, "${", "punctuation.section.interpolation.begin.ooc")
        self.assert_scope("generation", 2, "${i}", "meta.interpolation.ooc")
        self.assert_scope("generation", 2, "8", "constant.numeric.ooc")
        self.assert_scope("generation", 2, "wire", "storage.modifier.pin-direction.ooc")
        self.assert_scope("generation", 3, "Copy", "entity.name.function.ooc")

    def test_indexed_loops_slices_and_ternary_operators(self):
        self.assert_scope("bus_logic", 0, "for", "keyword.control.ooc")
        self.assert_scope("bus_logic", 0, "int", "storage.type.ooc")
        self.assert_scope("bus_logic", 0, "++", "keyword.operator.ooc")
        self.assert_scope("bus_logic", 1, "LEDR", "variable.other.readwrite.ooc")
        self.assert_scope("bus_logic", 1, "i", "variable.other.readwrite.ooc")
        self.assert_scope("bus_logic", 1, "[i]", "meta.brackets.ooc")
        self.assert_scope("bus_logic", 3, "[7:0]", "meta.brackets.ooc")
        self.assert_scope("bus_logic", 3, "7", "constant.numeric.ooc")
        self.assert_scope("bus_logic", 3, ":", "punctuation.separator.ooc")
        for line in (4, 5):
            self.assert_scope("bus_logic", line, "?", "keyword.operator.ooc")
            self.assert_scope("bus_logic", line, ":", "keyword.operator.ternary.ooc")
            self.assert_scope("bus_logic", line, "EARLY", "variable.other.readwrite.ooc")
        self.assert_scope("bus_logic", 6, "for", "keyword.control.ooc")
        self.assert_scope("bus_logic", 6, "i", "variable.other.readwrite.ooc")
        self.assert_scope("bus_logic", 6, "+", "keyword.operator.ooc")

    def test_switch_case_default_and_inline_break_are_control_keywords(self):
        self.assert_scope("switch_logic", 0, "switch", "keyword.control.ooc")
        self.assert_scope("switch_logic", 0, "fsm", "variable.other.readwrite.ooc")
        self.assert_scope("switch_logic", 1, "case", "keyword.control.ooc")
        self.assert_scope("switch_logic", 1, "EARLY", "variable.other.readwrite.ooc")
        self.assert_scope("switch_logic", 2, "break", "keyword.control.ooc")
        self.assert_scope("switch_logic", 3, "break", "keyword.control.ooc")
        self.assert_scope("switch_logic", 4, "default", "keyword.control.ooc")
        self.assert_scope("switch_logic", 4, "break", "keyword.control.ooc")
        self.assert_scope("switch_logic", 6, "switch(fsm)", "comment.line.number-sign.ooc")

    def test_analog_components_calls_comments_and_strings_keep_their_scopes(self):
        self.assert_scope("analog", 0, "R1", "entity.name.function.component.ooc")
        self.assert_scope("analog", 0, "C1", "entity.name.function.component.ooc")
        self.assert_scope("analog", 0, "between", "keyword.other.placement.ooc")
        self.assert_scope("analog", 0, "100", "constant.numeric.ooc")
        self.assert_scope("analog", 1, "transQ1", "support.function.ooc")
        self.assert_scope("analog", 2, "chips/clock.ckt", "string.quoted.double.ooc")
        self.assert_scope("analog", 2, "phase[31] += 50MHz", "comment.line.number-sign.ooc")

    def test_incomplete_selectors_do_not_color_the_next_line_as_a_selector(self):
        self.assert_scope("incomplete", 0, "unfinished selector", "comment.line.number-sign.ooc")
        self.assert_scope("incomplete", 1, "phase", "variable.other.readwrite.ooc")
        self.assert_scope("incomplete", 1, "Hz", "constant.other.unit.ooc")
        self.assert_scope("incomplete", 2, "true", "constant.language.boolean.ooc")
        self.assertTrue(all("meta.brackets.ooc" not in token["scopes"] for token in self.rows["incomplete"][1]["tokens"]))

    def test_potentiometer_scope_requires_placement_and_preserves_percent_unit(self):
        self.assert_scope("potentiometer", 0, "P1", "entity.name.function.component.ooc")
        self.assert_scope("potentiometer", 0, "25", "constant.numeric.ooc")
        self.assert_scope("potentiometer", 0, "%", "constant.other.unit.ooc")
        self.assert_scope("potentiometer", 1, "Pbare", "entity.name.function.component.ooc")
        self.assert_scope("potentiometer", 2, "P", "variable.other.readwrite.ooc")
        self.assert_scope("potentiometer", 3, "P0", "variable.other.readwrite.ooc")
        self.assert_scope("potentiometer", 3, "P1", "variable.other.readwrite.ooc")
        for row in self.rows["potentiometer"][2:]:
            self.assertTrue(all("entity.name.function.component.ooc" not in token["scopes"] for token in row["tokens"]))

    def test_advanced_fpga_examples_scope_declared_clocks_and_signals(self):
        if not self.example_names:
            self.skipTest("Optional advanced examples are absent; inline syntax cases still run.")
        for name in self.example_names:
            with self.subTest(example=name):
                rows = self.rows[name]
                for clock, row in enumerate(rows):
                    if row['line'].strip().startswith('clock('):
                        self.assert_scope(name, clock, "clock", "keyword.control.ooc")
                        self.assert_scope(name, clock, "MHz", "constant.other.unit.ooc")
                declaration = next(index for index, row in enumerate(rows)
                                   if re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\[[0-9]+\]", row["line"]))
                variable = rows[declaration]["line"].strip().split("[", 1)[0]
                self.assert_scope(name, declaration, variable, "variable.other.readwrite.ooc")


if __name__ == "__main__":
    unittest.main()
