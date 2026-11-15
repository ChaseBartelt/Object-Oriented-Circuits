const { execFile, spawn } = require("node:child_process");
const { existsSync, readFileSync, realpathSync } = require("node:fs");
const path = require("node:path");
const vscode = require("vscode");
const { analyzeSource } = require("./structure");
const { HoverClient } = require("./hover-client");

const language = { language: "ooc" };
const methodKinds = new Set(["circuit", "chip", "gate", "series", "parallel"]);
const symbolKinds = {
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

function documentSymbol(document, node) {
    const symbol = new vscode.DocumentSymbol(
        node.name, node.detail || "", symbolKinds[node.kind] ?? vscode.SymbolKind.Operator,
        range(document, node.start, node.end),
        range(document, node.selectionStart, node.selectionEnd),
    );
    symbol.children = node.children.map((child) => documentSymbol(document, child));
    return symbol;
}

function addFolds(document, nodes, folds) {
    for (const node of nodes) {
        const start = document.positionAt(node.start).line;
        const end = document.positionAt(node.end - 1).line;
        if (end > start) {
            folds.push(new vscode.FoldingRange(start, end, vscode.FoldingRangeKind.Region));
        }
        addFolds(document, node.children, folds);
    }
}

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
        if (existsSync(path.join(directory, "circuitlab", "cli.py"))) {
            return directory;
        }
        const parent = path.dirname(directory);
        if (parent === directory) {
            return undefined;
        }
        directory = parent;
    }
}

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

function includeTarget(document, filename) {
    if (document.uri.scheme !== "file" || path.isAbsolute(filename) || /^[A-Za-z]:/.test(filename)
        || ![".ckt", ".txt"].includes(path.extname(filename).toLowerCase())) {
        return undefined;
    }
    if (filename.startsWith("@chips/") || filename.startsWith("@chips\\")) {
        const root = checkoutRoot(document);
        const library = root ? path.join(root, "circuitlab", "libraries", "chips") : installedRuntime()?.library;
        if (!library) {
            return undefined;
        }
        const target = path.resolve(library, filename.slice("@chips/".length));
        const relative = path.relative(library, target);
        return relative === ".." || relative.startsWith(".." + path.sep) ? undefined : target;
    }
    return path.resolve(path.dirname(document.uri.fsPath), filename);
}

function compilerProcess(python, cwd, action, file) {
    return new Promise((resolve) => {
        execFile(python, ["-X", "utf8", "-m", "circuitlab.cli", action, file], {
            cwd, windowsHide: true, encoding: "utf8", maxBuffer: 8 * 1024 * 1024,
        }, (error, stdout, stderr) => resolve({ error, stdout, stderr }));
    });
}

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
        child.stdout.on("data", (data) => output.append(data.toString()));
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

function activate(context, shared = {}) {
    const schematicWindows = shared.schematicWindows || new Map();
    const runningFiles = shared.runningFiles || new Map();
    let disposed = false;
    const saveTimers = new Map();
    const saving = new Set();
    const hoverClients = new Map();
    const cache = new WeakMap();
    function sourceInfo(document) {
        let info = cache.get(document);
        if (info?.version !== document.version) {
            info = { version: document.version, ...analyzeSource(document.getText()) };
            cache.set(document, info);
        }
        return info;
    }

    const output = shared.output || vscode.window.createOutputChannel("OOC");
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
            if (saving.has(document) || !vscode.workspace.isTrusted || document.uri.scheme !== "file"
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
                const folds = [];
                addFolds(document, sourceInfo(document).structure, folds);
                return folds;
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
    );
}

module.exports = { activate, checkoutRoot, installedRuntime, includeTarget, runtime };
