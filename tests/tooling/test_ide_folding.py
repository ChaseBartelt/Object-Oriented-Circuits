"""Exercise the registered VS Code folding provider and pin decorations."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUN_EDITOR = r"""
const fs = require('node:fs');
const Module = require('node:module');
const scanner = require(process.argv[1]);
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const providers = {}, decorations = [], applied = new Map();
const disposable = () => ({dispose() {}});
const event = () => disposable();
const document = {
    languageId: 'ooc', version: 1, uri: {scheme: 'file', fsPath: 'folding.ckt'},
    setText(text) {
        this.text = text;
        this.starts = [0];
        for (const match of text.matchAll(/\r\n|[\r\n]/g)) this.starts.push(match.index + match[0].length);
    },
    getText() { return this.text; },
    positionAt(offset) {
        let line = this.starts.length - 1;
        while (line && this.starts[line] > offset) line--;
        return {line, character: offset - this.starts[line]};
    },
    offsetAt(position) { return this.starts[position.line] + position.character; },
};
document.setText(payload.source);
const editor = {document, setDecorations(type, ranges) { applied.set(type, ranges); }};
const vscode = {
    SymbolKind: new Proxy({}, {get: (_, name) => name}),
    DecorationRangeBehavior: {ClosedClosed: 0},
    FoldingRangeKind: {Region: 'region', Comment: 'comment', Imports: 'imports'},
    Range: class { constructor(start, end) { this.start = start; this.end = end; } },
    FoldingRange: class { constructor(start, end, kind) { Object.assign(this, {start, end, kind}); } },
    window: {
        visibleTextEditors: [editor],
        createTextEditorDecorationType(options) {
            const item = {...options, disposed: false, dispose() { this.disposed = true; }};
            decorations.push(item); return item;
        },
        onDidChangeVisibleTextEditors: event,
        createOutputChannel: () => ({appendLine() {}}),
    },
    workspace: {
        isTrusted: false, onDidChangeTextDocument: event, onDidOpenTextDocument: event,
        onDidSaveTextDocument: event, onDidCloseTextDocument: event,
        getConfiguration: () => ({get: (_, fallback) => fallback}),
    },
    languages: {
        createDiagnosticCollection: disposable,
        registerHoverProvider: event,
        registerDocumentSymbolProvider: event,
        registerDefinitionProvider: event,
        registerFoldingRangeProvider(_, provider) { providers.folds = provider; return disposable(); },
    },
    commands: {registerCommand: event},
};
const load = Module._load;
Module._load = function(name, ...args) { return name === 'vscode' ? vscode : load.call(this, name, ...args); };
const context = {subscriptions: []};
require(process.argv[2]).activate(context);
const result = scanner.analyzeSource(payload.source);
result.editorFolds = providers.folds.provideFoldingRanges(document);
result.colors = {};
for (const [decoration, ranges] of applied) {
    const words = ranges.map(range => document.text.slice(document.offsetAt(range.start), document.offsetAt(range.end)));
    (result.colors[decoration.color] ||= []).push(...words);
}
result.masked = scanner.maskComments(payload.source);
if (payload.replacement !== undefined) {
    document.setText(payload.replacement);
    document.version++;
    result.updatedFolds = providers.folds.provideFoldingRanges(document);
}
for (const item of context.subscriptions) item.dispose();
result.disposedColors = decorations.filter(item => item.disposed).map(item => item.color);
process.stdout.write(JSON.stringify(result));
"""


class EditorFoldingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node.js is unavailable for editor-provider checks.")

    def scan(self, source, replacement=None):
        payload = {"source": source}
        if replacement is not None:
            payload["replacement"] = replacement
        result = subprocess.run(
            [self.node, "-e", RUN_EDITOR, str(ROOT / "ide/vscode/structure.js"),
             str(ROOT / "ide/vscode/runtime.js")],
            input=json.dumps(payload), capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def ranges(parsed):
        return {(item["start"], item["end"], item["kind"]) for item in parsed["editorFolds"]}

    def test_multiline_pin_aliases_fold_on_the_declaration_line(self):
        source = '\n'.join((
            'main Board {',
            '    memory_pins[3:0] = "CLK, RESET,',
            '        read_data,',
            '        write_data";',
            '    core_pins[2:0] =',
            '        "CLK,',
            '        PC,',
            '        MSP";',
            '}',
        ))
        parsed = self.scan(source)
        self.assertEqual(self.ranges(parsed), {(0, 8, 'region'), (1, 3, 'region'), (4, 7, 'region')})
        self.assertFalse(parsed['references'])
        self.assertEqual(parsed['structure'][0]['name'], 'Board')

    def test_actual_cortex_pin_lists_and_crlf_have_the_same_arrows(self):
        source = (ROOT / 'examples/advanced/CortexM7/CortexM7_Firmware.ckt').read_text(encoding='utf-8')
        parsed = self.scan(source)
        ranges = self.ranges(parsed)
        for name in ('memory_pins', 'core_pins'):
            alias = re.search(rf'(?m)^\s*{name}\[\d+:\d+\]\s*=\s*"[^"]*"', source)
            self.assertIsNotNone(alias, name)
            start = source.count('\n', 0, source.index(name, alias.start()))
            end = source.count('\n', 0, alias.end() - 1)
            self.assertIn((start, end, 'region'), ranges)
        self.assertEqual(ranges, self.ranges(self.scan(source.replace('\n', '\r\n'))))

    def test_headers_groups_control_and_clear_blocks_remain_navigable(self):
        source = '\n'.join((
            'chip Example(', '    CLK,', '    result', ')', '{',
            '    input pins {', '        CLK;', '    }',
            '    output { result; }',
            '    wire interconnects {', '        bus[8];', '    }',
            '    clock(CLK, 10Hz) {', '        if (CLK) {',
            '            clear {', '                bus;', '            }',
            '        } else {', '            bus = 1;', '        }', '    }',
            '    for (int i = 0; i < 2; i++) {', '        bus = i;', '    }',
            '    arm(address, word) {', '        MOVS r0, #5;', '    }', '}',
            'main {', '    Example(', '        CLK,', '        result', '    );', '}',
        ))
        parsed = self.scan(source)
        expected = {(0, 27), (5, 7), (9, 11), (12, 20), (13, 16), (17, 19),
                    (14, 16), (21, 23), (24, 26), (28, 33), (29, 32)}
        self.assertTrue(expected.issubset({(start, end) for start, end, _ in self.ranges(parsed)}))
        self.assertEqual([(node['name'], node['kind']) for node in parsed['structure']],
                         [('Example', 'chip'), ('main', 'main')])
        self.assertEqual([reference['name'] for reference in parsed['references']], ['Example'])
        chip = parsed['structure'][0]
        self.assertEqual(chip['children'][0]['name'], 'pins')
        self.assertEqual(chip['children'][0]['children'][0]['name'], 'CLK')

    def test_multiline_calls_do_not_hide_following_code_on_the_closing_line(self):
        source = 'main {\n    Memory(\n        CLK,\n        data\n    ); Next();\n}'
        parsed = self.scan(source)
        self.assertIn((1, 3, 'region'), self.ranges(parsed))
        self.assertNotIn((1, 4, 'region'), self.ranges(parsed))
        self.assertEqual([item['name'] for item in parsed['references']], ['Memory', 'Next'])

    def test_include_sections_and_legacy_blocks_keep_their_fold_ranges(self):
        source = '\n'.join((
            'include("registers.ckt");', 'include(', '    "alu.ckt"', ');',
            '(series)', '    R1 = 1k;', '    R2 = 2k;', '(end series)',
        ))
        parsed = self.scan(source)
        self.assertIn((0, 3, 'imports'), self.ranges(parsed))
        self.assertIn((1, 3, 'region'), self.ranges(parsed))
        self.assertIn((4, 7, 'region'), self.ranges(parsed))
        self.assertEqual([(item['name'], item['kind']) for item in parsed['references']],
                         [('registers.ckt', 'include'), ('alu.ckt', 'include')])

    def test_quotes_comments_sections_and_arm_immediates_do_not_create_false_blocks(self):
        source = '\n'.join((
            '/* Hardware notes', 'chip Phantom(a) {', '*/',
            '// #region ALU', 'analog {', '    V(out) = V(in);', '}', '// #endregion',
            'arm(address, word) {', '    MOVS r0, #5;', '    ADDS r0, #2;', '}',
            'memory_pins[2:0] = "CLK, // not a comment {', '    /* not a block */ result";',
            '# legacy note', '# another note',
        ))
        parsed = self.scan(source)
        ranges = self.ranges(parsed)
        self.assertIn((0, 2, 'comment'), ranges)
        self.assertIn((3, 7, 'region'), ranges)
        self.assertIn((4, 6, 'region'), ranges)  # Unknown block kinds still fold.
        self.assertIn((8, 11, 'region'), ranges)
        self.assertIn((12, 13, 'region'), ranges)
        self.assertIn((14, 15, 'comment'), ranges)
        self.assertNotIn('Phantom', [item['name'] for item in parsed['structure']])
        self.assertIn('MOVS r0, #5;', parsed['masked'])
        self.assertIn('ADDS r0, #2;', parsed['masked'])
        self.assertIn('// not a comment {', parsed['masked'])

    def test_incomplete_editor_text_and_document_changes_remain_foldable(self):
        for source in ('main {\n    wire state {\n        accumulator[32];',
                       'chip Pending(\n    CLK,\n    result,',
                       'memory_pins[3:0] = "CLK,\n    RESET,',
                       '/* unfinished comment\n    notes',
                       'memory_pins[2:0] = "CLK,\n    escaped\\"'):
            with self.subTest(source=source):
                parsed = self.scan(source)
                self.assertTrue(parsed['editorFolds'])
                self.assertTrue(all(item['end'] < len(source.splitlines()) for item in parsed['editorFolds']))
        updated = self.scan('wire { a; }', 'wire state {\n a;\n b;\n}')
        self.assertEqual(updated['editorFolds'], [])
        self.assertEqual(updated['updatedFolds'], [{'start': 0, 'end': 3, 'kind': 'region'}])

    def test_only_keywords_and_chip_header_ports_receive_direction_colors(self):
        source = '\n'.join((
            'input inputs { CLK; }', 'output outputs { PC[32]; }',
            'wire links {', '    bus[8];', '    for (int i = 0; i < 2; i++) { lane${i}[8]; }', '}',
            'legacy[8] = wire;', '// wire ignored { fake; }',
            'chip Unit(CLK,', ' PC) { input { CLK; } output { PC[32]; } }',
            'chip Other(CLK, PC) { CLK = output; PC = input; }',
            'memory_pins[1:0] = "wire hidden, CLK";',
        ))
        parsed = self.scan(source)
        self.assertEqual(set(parsed['colors']['#d4d4d4']), {'inputs', 'outputs', 'links'})
        self.assertEqual(set(parsed['colors']['#4ec9b0']), {'wire'})
        self.assertEqual(set(parsed['colors']['#f6a652']), {'input', 'CLK', 'PC'})
        self.assertEqual(set(parsed['colors']['#659cd7']), {'output', 'PC', 'CLK'})
        for word in ('8', '${i}', 'hidden', 'fake', 'ignored'):
            self.assertNotIn(word, parsed['colors']['#4ec9b0'])
        self.assertIn('#4ec9b0', parsed['disposedColors'])


if __name__ == '__main__':
    unittest.main()
