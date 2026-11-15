// Stable loader: replace compatible OOC features without restarting VS Code.
const vscode = require("vscode");
const path = require("node:path");
const { existsSync } = require("node:fs");
const { startLocalUpdates } = require("./local-updates");
const { LiveRuntime } = require("./live-runtime");
const { installedUpdater } = require("./installed-updates");
const bundled = require("./runtime-bundle.json");

function activate(context) {
    const output = vscode.window.createOutputChannel("OOC");
    const shared = { output, schematicWindows: new Map(), runningFiles: new Map() };
    const live = new LiveRuntime(vscode, context, shared, bundled);
    context.subscriptions.push(output, live);
    const checkInstalled = installedUpdater(vscode, context, () => live.module.installedRuntime(), output,
        (bundle) => live.apply(bundle));
    startLocalUpdates(vscode, context, () => {
        const document = vscode.window.activeTextEditor?.document;
        if (document?.uri.scheme === "file") {
            const root = live.module.checkoutRoot(document);
            if (root) return root;
        }
        const configured = vscode.workspace.getConfiguration("ooc").get("projectRoot", "").trim();
        const folder = vscode.workspace.workspaceFolders?.find((item) => item.uri.scheme === "file")?.uri.fsPath;
        if (configured && (path.isAbsolute(configured) || folder)) return path.resolve(folder || "", configured);
        return vscode.workspace.workspaceFolders?.find((item) => item.uri.scheme === "file"
            && existsSync(path.join(item.uri.fsPath, "circuitlab", "cli.py")))?.uri.fsPath;
    }, output, (bundle) => live.apply(bundle), checkInstalled);
}

module.exports = { activate };
