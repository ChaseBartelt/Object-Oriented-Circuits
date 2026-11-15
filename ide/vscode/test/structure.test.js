"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { analyzeSource, parseStructure, parseReferences } = require("../structure");

function labels(nodes) {
    return nodes.map(({ kind, name }) => [kind, name]);
}

function checkSelection(source, nodes) {
    for (const node of nodes) {
        assert.equal(source.slice(node.selectionStart, node.selectionEnd), node.name);
        assert.ok(node.start <= node.selectionStart && node.selectionEnd <= node.end);
        checkSelection(source, node.children);
    }
}

test("recognizes the circuit method example and its call", () => {
    const source = fs.readFileSync(path.join(__dirname, "../../../examples/circuit_method.ckt"), "utf8");
    const { structure, references } = analyzeSource(source);
    assert.deepEqual(labels(structure), [
        ["battery", "test"], ["circuit", "divider"], ["main", "main"],
    ]);
    assert.equal(structure[1].detail, "circuit (input, output, ground)");
    assert.equal(source.slice(structure[0].start, structure[0].end).split(/\r?\n/).at(-1), "(end battery)");
    assert.equal(source.slice(structure[1].start, structure[1].end).at(-1), "}");
    assert.equal(references.length, 1);
    assert.equal(references[0].name, "divider");
    assert.equal(source.slice(references[0].start, references[0].end), "divider");
    checkSelection(source, structure);
});

test("nests conditional branches and loops with exact CRLF offsets", () => {
    const source = [
        "# 🎛 gate commented() {}",
        'include("library/{core}.ckt");',
        "circuit Build(input, out) {",
        "  for (int i = 0; i < 3; i++) { # ignore }",
        "    if ((i >= 1) && (i != 2)) {",
        "      R${i}(1k) between(input, n${(i + 1)});",
        "    } else if (i == 2) { foo(input); } else {",
        "      transistor Q${i} { base = input; collector = out; emitter = 0; type = npn; }",
        "    }",
        "    do { i++; }",
        "    while (i < 4);",
        "  }",
        "}",
        "main() { Build(input, out); }",
    ].join("\r\n");

    const { structure, references } = analyzeSource(source);
    assert.deepEqual(labels(structure), [["circuit", "Build"], ["main", "main"]]);
    const [loop] = structure[0].children;
    assert.deepEqual(labels(loop.children), [
        ["if", "if"], ["else if", "else if"], ["else", "else"], ["do", "do"],
    ]);
    assert.deepEqual(labels(loop.children[2].children), [["transistor", "Q${i}"]]);
    assert.equal(source.slice(loop.children[3].start, loop.children[3].end),
        "do { i++; }\r\n    while (i < 4);");
    assert.deepEqual(references.map(({ kind, name }) => [kind, name]), [
        ["include", "library/{core}.ckt"], ["call", "foo"], ["call", "Build"],
    ]);
    for (const reference of references) {
        assert.equal(source.slice(reference.start, reference.end), reference.name);
    }
    checkSelection(source, structure);
});

test("mixes inline legacy markers, named sections, and anonymous groups", () => {
    const source = [
        "battery Input { V = 5; wire = vin; }",
        "gate withGroups(a, b) {",
        "  parallel(a, b) {",
        "    (series) R1 = 1k (end series)",
        "    (opamp Filter) in+ = a out = b (end opamp)",
        "  }",
        "  while (false) {}",
        "}",
        "series Sample { R1 = 1k; }",
        "parallel Shunt(a, b) { R2 = 2k; }",
        "chip Logic(a, out) { a = input; out = output; out = a; }",
    ].join("\n");

    const structure = parseStructure(source);
    assert.deepEqual(labels(structure), [
        ["battery", "Input"], ["gate", "withGroups"], ["series", "Sample"],
        ["parallel", "Shunt"], ["chip", "Logic"],
    ]);
    const [parallel, loop] = structure[1].children;
    assert.equal(parallel.kind, "parallel");
    assert.equal(loop.kind, "while");
    assert.deepEqual(labels(parallel.children), [["series", "series"], ["opamp", "Filter"]]);
    assert.equal(source.slice(parallel.children[0].start, parallel.children[0].end),
        "(series) R1 = 1k (end series)");
    checkSelection(source, structure);
});

test("keeps incomplete blocks and unterminated lines safe for editing", () => {
    const source = [
        "}",
        "circuit Waiting(a, b) {",
        "  if (true) {",
        "    R${i(1k) between(a, b); # unfinished interpolation",
        '    include("missing }',
    ].join("\n");
    const { structure, references } = analyzeSource(source);
    assert.equal(structure[0].end, source.length);
    assert.equal(structure[0].children[0].end, source.length);
    assert.deepEqual(references, []);
    checkSelection(source, structure);
});

test("avoids treating properties, placements, and comment text as method calls", () => {
    const source = [
        '# include("ignored.ckt")',
        'include("boards/chip 1.ckt"); # ignoredCall()',
        "connector(a, b);",
        "main {",
        "  R1(1k) between(a, b);",
        "  foo(series);",
        "  foo((a, b));",
        "  op1(a, b, out);",
        "}",
    ].join("\n");
    assert.deepEqual(parseReferences(source).map(({ kind, name }) => [kind, name]), [
        ["include", "boards/chip 1.ckt"], ["call", "foo"], ["call", "foo"], ["call", "op1"],
    ]);
    assert.deepEqual(labels(parseStructure(source)), [["main", "main"]]);
});

test("preserves legacy method calls whose names also start control blocks", () => {
    const source = [
        "circuit if(a, b) { R1(1k) between(a, b); }",
        "circuit for(a, b) { R2(1k) between(a, b); }",
        "circuit while(a, b) { R3(1k) between(a, b); }",
        "main {",
        "  if(a, b); for(a, b); while(a, b);",
        "  if (true) { do { i++; } while (i < 2); }",
        "}",
    ].join("\n");
    assert.deepEqual(parseReferences(source).map(({ name }) => name), ["if", "for", "while"]);
});
