"""Check OOC scopes with the same TextMate engine shipped by VS Code."""

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

    def assert_scope(self, case, number, text, scope):
        row = self.rows[case][number]
        start = row["line"].index(text)
        for column in range(start, start + len(text)):
            token = next(token for token in row["tokens"] if token["startIndex"] <= column < token["endIndex"])
            self.assertIn(scope, token["scopes"], (case, number, text, token))

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

    def test_pin_banks_color_ranges_and_directions_without_changing_vector_syntax(self):
        self.assert_scope('pin_banks', 0, 'HYBRID01', 'entity.name.function.ooc')
        for row in (0, 1, 2):
            self.assert_scope('pin_banks', row, '9', 'constant.numeric.ooc')
            self.assert_scope('pin_banks', row, ':', 'punctuation.separator.ooc')
        self.assert_scope('pin_banks', 1, 'input', 'storage.modifier.pin-direction.ooc')
        self.assert_scope('pin_banks', 2, 'output', 'storage.modifier.pin-direction.ooc')
        self.assert_scope('pin_banks', 3, 'wire', 'storage.modifier.pin-direction.ooc')

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
            self.assert_scope("bus_logic", line, ":", "punctuation.separator.ooc")
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
