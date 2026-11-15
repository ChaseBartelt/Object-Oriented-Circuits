const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

function harness({ dirty = false, save = true, trusted = true, failure = false, refresh = true } = {}) {
    const calls = [], messages = [], logs = [], writes = [], commands = new Map();
    const child = new EventEmitter();
    child.exitCode = null;
    child.stdin = new EventEmitter();
    child.stdin.write = (data, encoding, done) => { writes.push(JSON.parse(data)); done(); };
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    const root = path.resolve(__dirname, "../../..");
    const file = path.join(root, "examples", "circuit with spaces.ckt");
    const document = {
        languageId: "ooc", uri: { scheme: "file", fsPath: file }, isDirty: dirty,
        async save() { calls.push("save"); onSave?.(document); return save; },
    };
    let onSave, hover;
    const hoverRequests = [];
    const output = { append: (s) => logs.push(s), appendLine: (s) => logs.push(s), show() {} };
    const vscode = {
        SymbolKind: {},
        window: {
            activeTextEditor: { document }, createOutputChannel: () => output,
            showInformationMessage: (s) => messages.push(s), showErrorMessage: (s) => messages.push(s),
        },
        workspace: {
            isTrusted: trusted, getConfiguration: () => ({ get: (key) =>
                key === "projectRoot" ? root : key === "refreshOnSave" ? refresh : "test-python" }),
            getWorkspaceFolder: () => undefined, onDidCloseTextDocument() {},
            onDidSaveTextDocument(fn) { onSave = fn; },
        },
        languages: {
            createDiagnosticCollection: () => ({}), registerDocumentSymbolProvider() {},
            registerFoldingRangeProvider() {}, registerDefinitionProvider() {},
            registerHoverProvider(language, provider) { hover = provider; },
        },
        MarkdownString: class {
            value = "";
            appendText(text) { this.value += text; }
            appendMarkdown(text) { this.value += text; }
        },
        Hover: class { constructor(contents, range) { this.contents = contents; this.range = range; } },
        commands: { registerCommand: (name, fn) => commands.set(name, fn) },
    };
    const module = { exports: {} };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../runtime.js"), "utf8"), {
        module, process, setTimeout, clearTimeout,
        require(name) {
            if (name === "vscode") return vscode;
            if (name === "./structure") return require("../structure");
            if (name === "./local-updates") return { startLocalUpdates() {} };
            if (name === "./hover-client") return { HoverClient: class {
                async request(payload) { hoverRequests.push(payload); return { text: "Wire: out", image: "YWJj" }; }
                dispose() {}
            } };
            if (name === "node:child_process") return {
                spawn(...args) {
                    calls.push(args);
                    process.nextTick(() => child.emit(failure ? "error" : "spawn", new Error("missing interpreter")));
                    return child;
                },
            };
            return require(name);
        },
    });
    module.exports.activate({ subscriptions: [] });
    return { run: commands.get("ooc.openSchematic"), calls, messages, logs, child, file, root, writes, document,
        onSave, hover, hoverRequests };
}

test("Run saves and launches the exact active file without waiting for window close", async () => {
    const h = harness({ dirty: true });
    assert.equal(await h.run(), true);
    assert.equal(h.calls[0], "save");
    const [python, args, options] = h.calls[1];
    assert.equal(python, "test-python");
    assert.deepEqual(Array.from(args), ["-X", "utf8", "-m", "circuitlab", "--editor", h.file]);
    assert.equal(options.cwd, h.root);
    assert.equal(options.windowsHide, true);
    assert.equal(options.shell, undefined);
    h.child.stderr.emit("data", Buffer.from("diagnostic"));
    assert.ok(h.logs.includes("diagnostic"));
});

test("repeated Run and another circuit reuse the window; closing it allows a fresh launch", async () => {
    const h = harness();
    await h.run();
    await h.run();
    h.document.uri.fsPath = path.join(h.root, "examples", "other circuit.ckt");
    await h.run();
    assert.equal(h.calls.length, 1);
    assert.deepEqual(h.writes, [{ file: h.file, activate: true }, { file: h.document.uri.fsPath, activate: true }]);
    h.child.emit("exit", 0);
    await h.run();
    assert.equal(h.calls.length, 2);
});

const settleSave = () => new Promise((resolve) => setTimeout(resolve, 360));

test("saving refreshes the running entry once, including a saved dependency, without focus", async () => {
    const h = harness({ dirty: true });
    await h.run();
    await settleSave();
    assert.equal(h.writes.length, 0); // Run's own save must not cause a second refresh.
    const dependency = { uri: { scheme: "file", fsPath: path.join(h.root, "included.ckt") } };
    h.onSave(h.document);
    h.onSave(dependency);
    await settleSave();
    assert.deepEqual(h.writes, [{ file: h.file, activate: false }]);
    assert.equal(h.calls.filter(Array.isArray).length, 1);
    h.child.emit("exit", 0);
    h.onSave(h.document);
    await settleSave();
    assert.equal(h.calls.filter(Array.isArray).length, 1);
});

test("save never opens a window before Run and respects refresh and trust settings", async () => {
    for (const options of [{}, { refresh: false }, { trusted: false }]) {
        const h = harness(options);
        if (options.refresh === false) await h.run();
        h.onSave(h.document);
        await settleSave();
        assert.equal(h.writes.length, 0);
        assert.equal(h.calls.length, options.refresh === false ? 1 : 0);
    }
});

test("hover sends current editor text and returns an untrusted image tooltip", async () => {
    const h = harness();
    h.document.version = 3;
    h.document.getWordRangeAtPosition = () => ({ start: 0, end: 3 });
    h.document.getText = () => "R1(1k) between(in, out);";
    const hover = await h.hover.provideHover(h.document, { line: 0, character: 20 }, {});
    assert.equal(h.hoverRequests[0].source, h.document.getText());
    assert.equal(hover.contents.isTrusted, false);
    assert.match(hover.contents.value, /Wire: out/);
    assert.match(hover.contents.value, /data:image\/png;base64/);
    assert.equal(await h.hover.provideHover(h.document, {}, { isCancellationRequested: true }), undefined);
});

test("a broken reload pipe reports an error without crashing the extension", async () => {
    const h = harness();
    await h.run();
    h.child.stdin.write = (data, encoding, done) => {
        const error = new Error("pipe closed");
        done(error);
        h.child.stdin.emit("error", error);
    };
    assert.equal(await h.run(), false);
    assert.ok(h.messages.some((s) => s.includes("could not reload")));
});

test("cancelled save and untrusted workspace do not launch a process", async () => {
    const cancelled = harness({ dirty: true, save: false });
    assert.equal(await cancelled.run(), false);
    assert.deepEqual(cancelled.calls, ["save"]);
    const restricted = harness({ trusted: false });
    await restricted.run();
    assert.deepEqual(restricted.calls, []);
});

test("launch and later GUI startup failures are visible", async () => {
    const failed = harness({ failure: true });
    assert.equal(await failed.run(), false);
    assert.ok(failed.messages.some((s) => s.includes("Could not open OOC")));
    const h = harness();
    await h.run();
    h.child.emit("exit", 1);
    assert.ok(h.messages.some((s) => s.includes("could not run")));
});

test("editor play button selects the schematic command while Solve remains available", () => {
    const manifest = require("../package.json");
    assert.equal(manifest.contributes.menus["editor/title"][0].command, "ooc.openSchematic");
    assert.ok(manifest.contributes.commands.some((c) => c.command === "ooc.solve"));
});
