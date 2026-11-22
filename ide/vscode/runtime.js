// This replaceable runtime registers language features and launches the checkout or installed
// backend.
// Source-edit requests use exact before/after text and IDE acknowledgements; a diagram gesture
// does not grant permission to overwrite a document revision that has changed in the editor.
const { execFile, spawn } = require("node:child_process");
const { existsSync, readFileSync, readdirSync, realpathSync } = require("node:fs");
const path = require("node:path");
const vscode = require("vscode");
const { analyzeSource, maskComments } = require("./structure");
const { HoverClient } = require("./hover-client");

const language = { language: "ooc" };
const methodKinds = new Set(["circuit", "chip", "gate", "series", "parallel"]);
const symbolKinds = {
    input: vscode.SymbolKind.Namespace,
    output: vscode.SymbolKind.Namespace,
    wire: vscode.SymbolKind.Namespace,
    pin: vscode.SymbolKind.Variable,
    circuit: vscode.SymbolKind.Function,
    chip: vscode.SymbolKind.Class,
    gate: vscode.SymbolKind.Function,
    main: vscode.SymbolKind.Module,
    series: vscode.SymbolKind.Namespace,
    parallel: vscode.SymbolKind.Namespace,
    battery: vscode.SymbolKind.Object,
    transistor: vscode.SymbolKind.Object,
    opamp: vscode.SymbolKind.Object,
};

function range(document, start, end) {
    return new vscode.Range(document.positionAt(start), document.positionAt(end));
}

function toggleLineComment(editor) {
    if (editor?.document.languageId !== "ooc") return;
    const lines = new Set();
    for (const selection of editor.selections) {
        // A selection ending at column zero leaves that last line untouched.
        let end = selection.end.line;
        if (!selection.isEmpty && selection.end.character === 0) end--;
        for (let line = selection.start.line; line <= end; line++) lines.add(line);
    }
    const remove = [];
    let hasLegacy = false;
    for (const number of lines) {
        const line = editor.document.lineAt(number);
        if (line.isEmptyOrWhitespace) continue;
        const prefix = /^[ \t]*(#|\/\/) ?/.exec(line.text);
        if (!prefix) return vscode.commands.executeCommand("editor.action.commentLine");
        hasLegacy ||= prefix[1] === "#";
        remove.push(new vscode.Range(number, line.firstNonWhitespaceCharacterIndex, number, prefix[0].length));
    }
    // The built-in action handles all ordinary // toggles and preserves the
    // editor's spacing and multi-selection behavior.
    if (!hasLegacy) return vscode.commands.executeCommand("editor.action.commentLine");
    return editor.edit(edit => {
        for (const marker of remove) edit.delete(marker);
    });
}

function documentSymbol(document, node) {
    const symbol = new vscode.DocumentSymbol(
        node.name, node.detail || "", symbolKinds[node.kind] ?? vscode.SymbolKind.Operator,
        range(document, node.start, node.end),
        range(document, node.selectionStart, node.selectionEnd),
    );
    symbol.children = node.children.map((child) => documentSymbol(document, child));
    return symbol;
}

// Resolve an explicit project root first, otherwise walk parents of a saved file until the
// actual compiler entry point is found. Unsaved/non-file documents cannot supply that path.
function checkoutRoot(document) {
    const config = vscode.workspace.getConfiguration("ooc", document.uri);
    const configured = config.get("projectRoot", "").trim();
    if (configured) {
        const folder = vscode.workspace.getWorkspaceFolder(document.uri)?.uri.fsPath;
        return path.resolve(folder || path.dirname(document.uri.fsPath), configured);
    }
    if (document.uri.scheme !== "file") {
        return undefined;
    }
    let directory = path.dirname(document.uri.fsPath);
    while (true) {
        if (existsSync(path.join(directory, "circuitlab", "tooling", "cli.py"))) {
            return directory;
        }
        const parent = path.dirname(directory);
        if (parent === directory) {
            return undefined;
        }
        directory = parent;
    }
}

// Treat manifest paths as untrusted until resolved beneath the installation root, including
// symlink resolution, then return the matching interpreter/library/editor artifacts.
function installedRuntime() {
    if (process.platform !== "win32") return;
    const configured = vscode.workspace.getConfiguration("ooc").get("installationPath", "").trim();
    const root = configured || process.env.OOC_INSTALL_ROOT
        || (process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, "Programs", "OOC"));
    if (!root) return;
    try {
        const base = realpathSync(root);
        const record = JSON.parse(readFileSync(path.join(base, "install.json"), "utf8").replace(/^\uFEFF/, ""));
        if (record.schema !== 1 || record.product !== "object-oriented-circuits") return;
        const child = (relative) => {
            if (typeof relative !== "string" || path.isAbsolute(relative)) throw new Error("Invalid OOC path");
            const file = realpathSync(path.resolve(base, relative));
            const rel = path.relative(base, file);
            if (!rel || path.isAbsolute(rel) || rel === ".." || rel.startsWith(".." + path.sep)) throw new Error("Invalid OOC path");
            return file;
        };
        return { root: base, version: record.version, python: child(record.python), library: child(record.library),
            extension: child(record.extension), bundle: child(record.runtime_bundle),
            extensionVersion: record.extension_version, extensionSha256: record.extension_sha256,
            bundleSha256: record.runtime_sha256 };
    } catch { return undefined; }
}

// Navigation accepts supported relative circuit paths or short library names, not arbitrary
// absolute filesystem targets embedded in a source include.
function includeTarget(document, filename) {
    filename = filename.replace(/\\/g, "/");
    const shortName = /^[A-Za-z0-9_][A-Za-z0-9_-]*$/.test(filename);
    if (path.isAbsolute(filename) || /^[A-Za-z]:/.test(filename)
        || (!shortName && ![".ckt", ".txt"].includes(path.extname(filename).toLowerCase()))) {
        return undefined;
    }
    if (shortName || filename.startsWith("@chips/")) {
        const root = checkoutRoot(document);
        const library = root ? path.join(root, "circuitlab", "libraries", "chips") : installedRuntime()?.library;
        if (!library) {
            return undefined;
        }
        if (shortName) {
            const matches = new Set();
            const search = (directory) => {
                for (const entry of readdirSync(directory, { withFileTypes: true })) {
                    const file = path.join(directory, entry.name);
                    if (entry.isDirectory() && !entry.isSymbolicLink()) search(file);
                    else if (entry.isFile() && path.extname(entry.name).toLowerCase() === ".ckt"
                        && path.basename(entry.name, path.extname(entry.name)).toLowerCase() === filename.toLowerCase()) {
                        const resolved = realpathSync(file);
                        const relative = path.relative(realpathSync(library), resolved);
                        if (relative && !path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(".." + path.sep)) matches.add(resolved);
                    }
                }
            };
            try { search(library); } catch { return undefined; }
            return matches.size === 1 ? [...matches][0] : undefined;
        }
        const target = path.resolve(library, filename.slice("@chips/".length));
        const relative = path.relative(library, target);
        return relative === ".." || relative.startsWith(".." + path.sep) ? undefined : target;
    }
    if (document.uri.scheme !== "file") return undefined;
    return path.resolve(path.dirname(document.uri.fsPath), filename);
}

function compilerProcess(python, cwd, action, file) {
    return new Promise((resolve) => {
        execFile(python, ["-X", "utf8", "-m", "circuitlab.tooling.cli", action, file], {
            cwd, windowsHide: true, encoding: "utf8", maxBuffer: 8 * 1024 * 1024,
        }, (error, stdout, stderr) => resolve({ error, stdout, stderr }));
    });
}

// Translate the compiler's file/line messages to editor diagnostics without treating tool
// output as source code or guessing a different file location.
function compilerDiagnostics(document, stderr) {
    const groups = new Map();
    for (const outputLine of stderr.split(/\r?\n/)) {
        const match = /^(.*?)(?:\((\d+)\))?: (error|warning) (OOC\d+): (.*)$/.exec(outputLine);
        if (!match) {
            continue;
        }
        const [, filename, line, level, code, message] = match;
        const problems = groups.get(filename) || [];
        const index = Math.max(0, Number(line || 1) - 1);
        const current = filename === document.uri.fsPath && index < document.lineCount;
        const bounds = current ? document.lineAt(index).range : new vscode.Range(index, 0, index, 1);
        const problem = new vscode.Diagnostic(bounds, message, level === "error"
            ? vscode.DiagnosticSeverity.Error : vscode.DiagnosticSeverity.Warning);
        problem.source = "OOC";
        problem.code = code;
        problems.push(problem);
        groups.set(filename, problems);
    }
    return groups;
}

function runtime(document) {
    const root = checkoutRoot(document);
    const config = vscode.workspace.getConfiguration("ooc", document.uri);
    const windows = process.platform === "win32";
    const interpreter = root && path.join(root, ".venv", windows ? "Scripts/python.exe" : "bin/python");
    const installed = !root && installedRuntime();
    const python = config.get("pythonPath", "").trim()
        || (interpreter && existsSync(interpreter) ? interpreter : installed?.python || (windows ? "python" : "python3"));
    const folder = vscode.workspace.getWorkspaceFolder(document.uri)?.uri.fsPath;
    return { python, cwd: root || folder || path.dirname(document.uri.fsPath) };
}

const schematicSaving = new WeakSet();

// Apply one checked replacement through the IDE only when the target still matches the
// request's expected source; return an explicit acknowledgement to the viewer.
async function applySchematicEdit(request) {
    if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace before editing its circuit.');
    if (typeof request.path !== 'string' || !path.isAbsolute(request.path)
        || typeof request.before !== 'string' || typeof request.after !== 'string') {
        throw new Error('Invalid schematic source edit.');
    }
    const document = await vscode.workspace.openTextDocument(vscode.Uri.file(request.path));
    const normalize = (text) => text.replace(/\r\n/g, '\n');
    if (document.isDirty || normalize(document.getText()) !== normalize(request.before)) {
        throw new Error('The source has changed or has unsaved edits. Save in VS Code, refresh the schematic, then try again.');
    }
    const edit = new vscode.WorkspaceEdit();
    edit.replace(document.uri, new vscode.Range(document.positionAt(0), document.positionAt(document.getText().length)), request.after);
    if (!await vscode.workspace.applyEdit(edit)) throw new Error('VS Code refused the source edit.');
    // The viewer refreshes on acknowledgment. Sending it another save/reload
    // message can discard the drawing history it has just restored with Undo.
    schematicSaving.add(document);
    try {
        if (!await document.save()) throw new Error('The edit is in VS Code but could not be saved. Save it there, then refresh the schematic.');
    } finally {
        schematicSaving.delete(document);
    }
}

// Correlate source transactions and reserve the private protocol for structured responses.
function attachSchematicEdits(child, output) {
    if (!child.stdout) return;
    child.stdout.removeAllListeners('data');
    const decoder = new (require('node:string_decoder').StringDecoder)('utf8');
    let pendingOutput = '';
    child.stdout.on("data", (data) => {
        pendingOutput += decoder.write(data);
        let newline;
        while ((newline = pendingOutput.indexOf('\n')) >= 0) {
            const line = pendingOutput.slice(0, newline);
            pendingOutput = pendingOutput.slice(newline + 1);
            let request;
            try { request = JSON.parse(line); } catch { output.append(line + '\n'); continue; }
            if (!request || request.type !== 'ooc-source-edit') { output.append(line + '\n'); continue; }
            applySchematicEdit(request).then(
                () => ({ type: 'ooc-source-edit-result', id: request.id, ok: true }),
                (error) => ({ type: 'ooc-source-edit-result', id: request.id, ok: false, error: error.message }),
            ).then((reply) => {
                if (!child.stdin.destroyed) child.stdin.write(JSON.stringify(reply) + '\n', 'utf8', () => {});
            });
        }
    });
}

// Reuse the existing viewer for this source identity. Background save refreshes synchronize
// state without behaving like an explicit user request to raise or reopen the window.
function openSchematic(python, cwd, file, output, windows, automatic = false) {
    const key = JSON.stringify([python, cwd]);
    const existing = windows.get(key);
    if (existing && existing.exitCode === null && !existing.stdin.destroyed) {
        return new Promise((resolve) => {
            existing.stdin.write(JSON.stringify({ file, activate: !automatic }) + "\n", "utf8", (error) => {
                if (error) {
                    output.appendLine(error.message);
                    output.show(true);
                    vscode.window.showErrorMessage("OOC could not reload the circuit. Close its window and run again.");
                } else {
                    output.appendLine(`Reloading schematic: ${file}`);
                }
                resolve(!error);
            });
        });
    }
    if (automatic) return Promise.resolve(false);
    return new Promise((resolve) => {
        // Launch asynchronously: the command must not wait for the GUI to close.
        // Argument arrays preserve spaces and avoid interpreting circuit paths as shell code.
        const child = spawn(python, ["-X", "utf8", "-m", "circuitlab", "--editor", file], {
            cwd, windowsHide: true, stdio: ["pipe", "pipe", "pipe"],
        });
        windows.set(key, child);
        const forget = () => { if (windows.get(key) === child) windows.delete(key); };
        // A closed pipe must not become an unhandled Node error.
        child.stdin.on("error", forget);
        attachSchematicEdits(child, output);
        child.stderr.on("data", (data) => output.append(data.toString()));
        child.once("spawn", () => {
            output.appendLine(`Opening schematic: ${file}`);
            resolve(true);
        });
        child.once("error", (error) => {
            forget();
            output.appendLine(error.message);
            output.show(true);
            vscode.window.showErrorMessage("Could not open OOC. Check OOC: Python Path and OOC: Project Root in Settings.");
            resolve(false);
        });
        child.once("exit", (code) => {
            forget();
            if (code !== null && code !== 0) {
                output.appendLine(`OOC window exited with code ${code}.`);
                output.show(true);
                vscode.window.showErrorMessage("The OOC window could not run. See the OOC output for details.");
            }
        });
    });
}

// Register providers against the shared runtime state so compatible live replacement can
// dispose old registrations without losing the already-running schematic processes.
function activate(context, shared = {}) {
    const schematicWindows = shared.schematicWindows || new Map();
    const runningFiles = shared.runningFiles || new Map();
    let disposed = false;
    const saveTimers = new Map();
    const saving = new Set();
    const hoverClients = new Map();
    const cache = new WeakMap();

    // These keywords have explicit OOC colors; TextMate continues to theme
    // all other syntax. Decorations avoid changing the user's editor settings.
    const keywordColor = vscode.window.createTextEditorDecorationType({
        color: "#659cd7", rangeBehavior: vscode.DecorationRangeBehavior.ClosedClosed,
    });
    const overrideColor = vscode.window.createTextEditorDecorationType({
        color: "#f6a652", rangeBehavior: vscode.DecorationRangeBehavior.ClosedClosed,
    });
    const measureColor = vscode.window.createTextEditorDecorationType({
        color: "#aed6ff", rangeBehavior: vscode.DecorationRangeBehavior.ClosedClosed,
    });
    const inputColor = vscode.window.createTextEditorDecorationType({
        color: "#f6a652", rangeBehavior: vscode.DecorationRangeBehavior.ClosedClosed,
    });
    const outputColor = vscode.window.createTextEditorDecorationType({
        color: "#659cd7", rangeBehavior: vscode.DecorationRangeBehavior.ClosedClosed,
    });
    const wireColor = vscode.window.createTextEditorDecorationType({
        color: "#4ec9b0", rangeBehavior: vscode.DecorationRangeBehavior.ClosedClosed,
    });
    const groupNameColor = vscode.window.createTextEditorDecorationType({
        color: "#d4d4d4", rangeBehavior: vscode.DecorationRangeBehavior.ClosedClosed,
    });
    function refreshKeywordColors(document) {
        for (const editor of vscode.window.visibleTextEditors) {
            if (document && editor.document !== document) continue;
            const keywords = [], overrides = [], measurements = [], inputs = [], outputs = [], wires = [], groupNames = [];
            if (editor.document.languageId === "ooc") {
                // Mask quoted paths and comments without changing UTF-16 offsets.
                const code = maskComments(editor.document.getText(), true);
                const tokens = /@override\b|\b(?:analog|digital)\b(?=\s*(?::\s*math\b|\{|\(|[A-Za-z_]|$))/gim;
                for (const match of code.matchAll(tokens)) {
                    (match[0].startsWith("@") ? overrides : keywords).push(
                        range(editor.document, match.index, match.index + match[0].length),
                    );
                }
                const declarations = /(?:^|(?<=[;{}]))[ \t]*[A-Za-z_][A-Za-z0-9_]*[ \t]*=[ \t]*(count)\b(?=[ \t]*(?:[;}]|$))/gim;
                for (const match of code.matchAll(declarations)) {
                    const end = match.index + match[0].length;
                    keywords.push(range(editor.document, end - match[1].length, end));
                }
                const measure = /(?:^|(?<=[;{}:]))[ \t]*(measure)(?=[ \t]*(?:=(?!=)|$))/dgim;
                for (const match of code.matchAll(measure)) {
                    const [start, end] = match.indices[1];
                    measurements.push(range(editor.document, start, end));
                }
                for (const token of sourceInfo(editor.document).portTokens || []) {
                    const target = token.kind === "label" ? groupNames
                        : { input: inputs, output: outputs, wire: wires }[token.direction];
                    if (!target) continue;
                    target.push(
                        range(editor.document, token.start, token.end),
                    );
                }
            }
            editor.setDecorations(keywordColor, keywords);
            editor.setDecorations(overrideColor, overrides);
            editor.setDecorations(measureColor, measurements);
            editor.setDecorations(inputColor, inputs);
            editor.setDecorations(outputColor, outputs);
            editor.setDecorations(wireColor, wires);
            editor.setDecorations(groupNameColor, groupNames);
        }
    }
    context.subscriptions.push(
        keywordColor, overrideColor, measureColor, inputColor, outputColor, wireColor, groupNameColor,
        vscode.window.onDidChangeVisibleTextEditors(() => refreshKeywordColors()),
        vscode.workspace.onDidChangeTextDocument(event => refreshKeywordColors(event.document)),
        vscode.workspace.onDidOpenTextDocument(document => refreshKeywordColors(document)),
    );
    refreshKeywordColors();

    function sourceInfo(document) {
        let info = cache.get(document);
        if (info?.version !== document.version) {
            info = { version: document.version, ...analyzeSource(document.getText()) };
            cache.set(document, info);
        }
        return info;
    }

    const output = shared.output || vscode.window.createOutputChannel("OOC");
    for (const child of schematicWindows.values()) attachSchematicEdits(child, output);
    const diagnostics = vscode.languages.createDiagnosticCollection("ooc");
    let latestRun = 0;
    async function runCircuit(action) {
        const document = vscode.window.activeTextEditor?.document;
        if (!document || document.languageId !== "ooc" || document.uri.scheme !== "file") {
            vscode.window.showInformationMessage("Open and save an OOC .ckt file before running the circuit.");
            return undefined;
        }
        if (!vscode.workspace.isTrusted) {
            vscode.window.showInformationMessage("OOC circuit commands run the local compiler in a trusted workspace.");
            return undefined;
        }
        if (document.isDirty) {
            saving.add(document);
            try {
                if (!await document.save()) {
                    vscode.window.showInformationMessage("Save the circuit before running it in OOC.");
                    return false;
                }
            } finally { saving.delete(document); }
        }
        if (disposed) return;
        const { python, cwd } = runtime(document);
        if (action === "open") {
            const key = JSON.stringify([python, cwd]);
            clearTimeout(saveTimers.get(key));
            runningFiles.set(key, document.uri.fsPath);
            return openSchematic(python, cwd, document.uri.fsPath, output, schematicWindows);
        }
        const invocation = ++latestRun;
        const version = document.version;
        const result = await vscode.window.withProgress(
            { location: vscode.ProgressLocation.Window, title: action === "check" ? "Checking OOC circuit" : "Solving OOC circuit" },
            () => compilerProcess(python, cwd, action, document.uri.fsPath),
        );
        if (disposed || invocation !== latestRun) {
            return undefined;
        }
        output.clear();
        output.appendLine(document.uri.fsPath);
        if (result.stdout) {
            output.append(result.stdout);
        }
        if (result.stderr) {
            output.append(result.stderr);
        }
        const groups = compilerDiagnostics(document, result.stderr);
        diagnostics.clear();
        if (document.version === version) {
            for (const [filename, problems] of groups) {
                diagnostics.set(vscode.Uri.file(filename), problems);
            }
        }
        if (result.error && !groups.size) {
            output.appendLine(result.error.message);
            output.appendLine("Set OOC: Project Root and OOC: Python Path in VS Code settings if your OOC checkout is in a separate folder.");
        }
        output.show(true);
        return !result.error;
    }

    context.subscriptions.push(
        diagnostics,
        { dispose() {
            disposed = true;
            latestRun++;
            for (const timer of saveTimers.values()) clearTimeout(timer);
            for (const client of hoverClients.values()) client.dispose();
        } },
        vscode.workspace.onDidSaveTextDocument((document) => {
            if (saving.has(document) || schematicSaving.has(document) || !vscode.workspace.isTrusted || document.uri.scheme !== "file"
                || ![".ckt", ".txt"].includes(path.extname(document.uri.fsPath).toLowerCase())
                || !vscode.workspace.getConfiguration("ooc", document.uri).get("refreshOnSave", true)) return;
            const { python, cwd } = runtime(document);
            const key = JSON.stringify([python, cwd]);
            const file = runningFiles.get(key);
            if (!file || !schematicWindows.has(key)) return;
            clearTimeout(saveTimers.get(key));
            saveTimers.set(key, setTimeout(() => {
                saveTimers.delete(key);
                if (vscode.workspace.isTrusted) openSchematic(python, cwd, file, output, schematicWindows, true);
            }, 300));
        }),
        vscode.languages.registerHoverProvider(language, {
            async provideHover(document, position, token) {
                if (!vscode.workspace.isTrusted || document.uri.scheme !== "file"
                    || !vscode.workspace.getConfiguration("ooc", document.uri).get("hoverPreviews", true)) return;
                const word = document.getWordRangeAtPosition(position, /[A-Za-z_][\w]*|\d+/);
                if (!word || token.isCancellationRequested) return;
                const { python, cwd } = runtime(document);
                const key = JSON.stringify([python, cwd]);
                let client = hoverClients.get(key);
                if (!client) {
                    client = new HoverClient(python, cwd, (message) => output.appendLine(message));
                    hoverClients.set(key, client);
                }
                const version = document.version;
                const result = await client.request({
                    file: document.uri.fsPath, source: document.getText(),
                    line: position.line, column: position.character,
                });
                if (disposed || !result || token.isCancellationRequested || version !== document.version) return;
                const contents = new vscode.MarkdownString();
                contents.isTrusted = false;
                contents.appendText(result.text || "");
                if (result.image && /^[A-Za-z0-9+/=]+$/.test(result.image)) {
                    contents.appendMarkdown(`\n\n![OOC preview](data:image/png;base64,${result.image})`);
                }
                return new vscode.Hover(contents, word);
            },
        }),
        vscode.languages.registerDocumentSymbolProvider(language, {
            provideDocumentSymbols(document) {
                return sourceInfo(document).structure.map((node) => documentSymbol(document, node));
            },
        }),
        vscode.languages.registerFoldingRangeProvider(language, {
            provideFoldingRanges(document) {
                const kinds = { region: vscode.FoldingRangeKind.Region,
                    comment: vscode.FoldingRangeKind.Comment, imports: vscode.FoldingRangeKind.Imports };
                return sourceInfo(document).folds.map(({ start, end, kind }) =>
                    new vscode.FoldingRange(start, end, kinds[kind]));
            },
        }),
        vscode.languages.registerDefinitionProvider(language, {
            provideDefinition(document, position) {
                const info = sourceInfo(document);
                const offset = document.offsetAt(position);
                const reference = info.references.find(({ start, end }) => offset >= start && offset < end);
                if (!reference) {
                    return undefined;
                }
                if (reference.kind === "include") {
                    const target = includeTarget(document, reference.name);
                    return target && existsSync(target)
                        ? new vscode.Location(vscode.Uri.file(target), new vscode.Position(0, 0)) : undefined;
                }
                const name = reference.name.toLowerCase();
                const declaration = info.structure.find((node) =>
                    methodKinds.has(node.kind) && node.name.toLowerCase() === name);
                return declaration
                    ? new vscode.Location(document.uri, range(document, declaration.selectionStart, declaration.selectionEnd))
                    : undefined;
            },
        }),
        vscode.workspace.onDidCloseTextDocument((document) => diagnostics.delete(document.uri)),
        vscode.commands.registerCommand("ooc.check", () => runCircuit("check")),
        vscode.commands.registerCommand("ooc.solve", () => runCircuit("solve")),
        vscode.commands.registerCommand("ooc.openSchematic", () => runCircuit("open")),
        vscode.commands.registerCommand("ooc.toggleLineComment", () => toggleLineComment(vscode.window.activeTextEditor)),
    );
}

module.exports = { activate, checkoutRoot, installedRuntime, includeTarget, runtime, applySchematicEdit };
