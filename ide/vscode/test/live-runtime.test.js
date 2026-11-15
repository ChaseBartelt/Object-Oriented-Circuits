const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const { LiveRuntime } = require("../live-runtime");

function bundle(value, fail = false) {
    return { api: 1, version: `0.${value}.0`, compatibility: "stable", files: {
        "structure.js": `module.exports = ${value};`, "hover-client.js": "",
        "runtime.js": `const vscode = require('vscode');
const value = require('./structure');
exports.activate = (context, shared) => {
    vscode.commands.registerCommand('ooc.test', () => [value, shared.schematicWindows.get('window')]);
    vscode.workspace.onDidSaveTextDocument(() => shared.saved = value);
    ${fail ? "throw new Error('broken update');" : ""}
};`,
    } };
}

function host() {
    const commands = new Map(), saves = new Set();
    const vscode = Object.freeze({
        commands: Object.freeze({ registerCommand(name, callback) {
            assert.equal(commands.has(name), false, "must not double-register a command");
            commands.set(name, callback);
            return { dispose() { commands.delete(name); } };
        } }),
        workspace: Object.freeze({ onDidSaveTextDocument(callback) {
            saves.add(callback);
            return { dispose() { saves.delete(callback); } };
        } }),
    });
    const context = { get subscriptions() { return []; } };
    const connection = { pid: 123, unsavedLayout: true };
    const shared = { schematicWindows: new Map([["window", connection]]), runningFiles: new Map([["root", "file.ckt"]]) };
    const live = new LiveRuntime(vscode, context, shared, bundle(6));
    return { live, shared, connection, commands, saves };
}

test("live feature update preserves the schematic process and replaces handlers exactly once", () => {
    const h = host();
    assert.equal(h.commands.get("ooc.test")()[0], 6);
    assert.equal(h.live.apply(bundle(7)), true);
    assert.deepEqual(h.commands.get("ooc.test")(), [7, h.connection]);
    assert.equal(h.saves.size, 1);
    for (const fn of h.saves) fn();
    assert.equal(h.shared.saved, 7);
    assert.equal(h.shared.runningFiles.get("root"), "file.ckt");
    assert.equal(h.connection.unsavedLayout, true);
    h.live.dispose();
    assert.equal(h.commands.size, 0);
    assert.equal(h.saves.size, 0);
});

test("partial activation failure rolls back to working commands and save listener", () => {
    const h = host();
    assert.throws(() => h.live.apply(bundle(7, true)), /broken update/);
    assert.equal(h.commands.get("ooc.test")()[0], 6);
    assert.equal(h.saves.size, 1);
    assert.equal(h.live.bundle.version, "0.6.0");
    h.live.dispose();
});

test("syntax failures and incompatible changes leave the active runtime untouched", () => {
    const h = host();
    const bad = bundle(7);
    bad.files["structure.js"] = "not javascript !!";
    assert.throws(() => h.live.apply(bad), SyntaxError);
    assert.equal(h.live.apply({ ...bundle(7), compatibility: "new grammar" }), false);
    assert.equal(h.commands.get("ooc.test")()[0], 6);
    h.live.dispose();
});

test("the real OOC runtime retains Run and save-refresh connections across replacement", async () => {
    const commands = new Map(), saves = new Set(), logs = [], writes = [];
    const root = path.resolve(__dirname, "../../..");
    const file = path.join(root, "examples/circuit_method.ckt");
    const document = { languageId: "ooc", uri: { scheme: "file", fsPath: file } };
    const disposable = () => ({ dispose() {} });
    const vscode = {
        SymbolKind: {},
        window: { activeTextEditor: { document } },
        commands: { registerCommand(name, callback) {
            assert.equal(commands.has(name), false);
            commands.set(name, callback);
            return { dispose() { commands.delete(name); } };
        } },
        workspace: {
            isTrusted: true,
            getConfiguration: () => ({ get: (key) => key === "projectRoot" ? root : key === "pythonPath" ? "test-python" : true }),
            getWorkspaceFolder: () => undefined,
            onDidCloseTextDocument: disposable,
            onDidSaveTextDocument(callback) { saves.add(callback); return { dispose() { saves.delete(callback); } }; },
        },
        languages: {
            createDiagnosticCollection: disposable, registerHoverProvider: disposable,
            registerDefinitionProvider: disposable, registerFoldingRangeProvider: disposable,
            registerDocumentSymbolProvider: disposable,
        },
    };
    const shared = {
        output: { appendLine: (text) => logs.push(text) }, runningFiles: new Map(),
        schematicWindows: new Map([[JSON.stringify(["test-python", root]), {
            exitCode: null, stdin: { write(data, encoding, done) { writes.push(JSON.parse(data)); done(); } },
        }]]),
    };
    const original = { api: 1, version: "0.6.0", compatibility: "actual", files: Object.fromEntries(
        ["runtime.js", "structure.js", "hover-client.js"].map((name) => [name, fs.readFileSync(path.join(__dirname, "..", name), "utf8")]),
    ) };
    const live = new LiveRuntime(vscode, { subscriptions: [] }, shared, original);
    try {
        await commands.get("ooc.openSchematic")();
        const next = { ...original, version: "0.7.0", files: { ...original.files,
            "runtime.js": original.files["runtime.js"].replace("Reloading schematic:", "Live update schematic:"),
        } };
        assert.equal(live.apply(next), true);
        await commands.get("ooc.openSchematic")();
        assert.ok(logs.some((line) => line.startsWith("Live update schematic:")));
        assert.equal(saves.size, 1);
        for (const save of saves) save(document);
        await new Promise((resolve) => setTimeout(resolve, 360));
        assert.deepEqual(writes, [{ file, activate: true }, { file, activate: true }, { file, activate: false }]);
        assert.equal(shared.schematicWindows.size, 1);
    } finally { live.dispose(); }
});
