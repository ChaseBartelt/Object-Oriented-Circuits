const { execFile } = require("node:child_process");
const { existsSync } = require("node:fs");
const path = require("node:path");
const vscode = require("vscode");
const { analyzeSource } = require("./structure");

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

function includeTarget(document, filename) {
    if (document.uri.scheme !== "file" || path.isAbsolute(filename) || /^[A-Za-z]:/.test(filename)
        || ![".ckt", ".txt"].includes(path.extname(filename).toLowerCase())) {
        return undefined;
    }
    if (filename.startsWith("@chips/") || filename.startsWith("@chips\\")) {
        const root = checkoutRoot(document);
        if (!root) {
            return undefined;
        }
        const library = path.join(root, "circuitlab", "libraries", "chips");
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

function activate(context) {
    const cache = new WeakMap();
    function sourceInfo(document) {
        let info = cache.get(document);
        if (info?.version !== document.version) {
            info = { version: document.version, ...analyzeSource(document.getText()) };
            cache.set(document, info);
        }
        return info;
    }

    const output = vscode.window.createOutputChannel("OOC");
    const diagnostics = vscode.languages.createDiagnosticCollection("ooc");
    let latestRun = 0;
    async function runCircuit(action) {
        const document = vscode.window.activeTextEditor?.document;
        if (!document || document.languageId !== "ooc" || document.uri.scheme !== "file" || document.isDirty) {
            vscode.window.showInformationMessage("Open and save an OOC .ckt file before running the circuit.");
            return undefined;
        }
        if (!vscode.workspace.isTrusted) {
            vscode.window.showInformationMessage("OOC circuit commands run the local compiler in a trusted workspace.");
            return undefined;
        }
        const root = checkoutRoot(document);
        const config = vscode.workspace.getConfiguration("ooc", document.uri);
        const windows = process.platform === "win32";
        const interpreter = root && path.join(root, ".venv", windows ? "Scripts/python.exe" : "bin/python");
        const python = config.get("pythonPath", "").trim()
            || (interpreter && existsSync(interpreter) ? interpreter : windows ? "python" : "python3");
        const invocation = ++latestRun;
        const version = document.version;
        const result = await vscode.window.withProgress(
            { location: vscode.ProgressLocation.Window, title: action === "check" ? "Checking OOC circuit" : "Solving OOC circuit" },
            () => compilerProcess(python, root || path.dirname(document.uri.fsPath), action, document.uri.fsPath),
        );
        if (invocation !== latestRun) {
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
        output, diagnostics,
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
    );
}

module.exports = { activate };
