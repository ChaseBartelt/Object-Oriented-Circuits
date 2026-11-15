const fs = require("node:fs/promises");
const path = require("node:path");
const { createHash } = require("node:crypto");

function newer(candidate, installed) {
    const valid = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
    if (!valid.test(candidate) || !valid.test(installed)) return false;
    const a = candidate.split(".").map(Number), b = installed.split(".").map(Number);
    for (let i = 0; i < 3; i++) {
        if (a[i] !== b[i]) return a[i] > b[i];
    }
    return false;
}

async function candidate(root, installed) {
    const directory = path.join(root, "ide", "vscode", "dist");
    let release;
    try { release = JSON.parse(await fs.readFile(path.join(directory, "latest.json"), "utf8")); }
    catch (error) { if (error.code === "ENOENT") return; throw error; }
    if (release.id !== "echoingfarts.ooc-language" || !newer(release.version, installed)) return;
    if (release.file !== `ooc-language-${release.version}.vsix` || !/^[a-f0-9]{64}$/.test(release.sha256)) {
        throw new Error("Invalid OOC local release manifest.");
    }
    const file = path.join(directory, release.file);
    const info = await fs.lstat(file);
    if (!info.isFile() || info.isSymbolicLink() || info.size > 32 * 1024 * 1024) {
        throw new Error("Invalid OOC update package.");
    }
    const bytes = await fs.readFile(file);
    if (createHash("sha256").update(bytes).digest("hex") !== release.sha256) {
        throw new Error("OOC update is incomplete or changed; waiting for a complete package.");
    }
    let bundle;
    if (release.runtime) {
        if (release.runtime.file !== `ooc-runtime-${release.version}.json`
            || !/^[a-f0-9]{64}$/.test(release.runtime.sha256)) throw new Error("Invalid OOC runtime manifest.");
        const runtime = path.join(directory, release.runtime.file);
        const info = await fs.lstat(runtime);
        if (!info.isFile() || info.isSymbolicLink() || info.size > 8 * 1024 * 1024) throw new Error("Invalid OOC runtime package.");
        const data = await fs.readFile(runtime);
        if (createHash("sha256").update(data).digest("hex") !== release.runtime.sha256) throw new Error("OOC runtime checksum mismatch.");
        bundle = JSON.parse(data);
        if (bundle.version !== release.version) throw new Error("OOC runtime version mismatch.");
    }
    return { file, version: release.version, bundle };
}

function startLocalUpdates(vscode, context, getRoot, output, applyRuntime, checkInstalled) {
    let installed = context.extension.packageJSON.version;
    let busy = false, disposed = false, lastError;
    async function check(manual = false) {
        if (busy || disposed || !vscode.workspace.isTrusted) return;
        if (!manual && !vscode.workspace.getConfiguration("ooc").get("autoUpdate", true)) return;
        const root = getRoot();
        if (!root) {
            if (checkInstalled) return checkInstalled(manual);
            if (manual) vscode.window.showInformationMessage("Open your OOC project or set OOC: Project Root to check for local updates.");
            return;
        }
        busy = true;
        try {
            const update = await candidate(root, installed);
            if (!update) {
                if (manual) vscode.window.showInformationMessage("OOC has no newer local update ready.");
                return;
            }
            if (disposed || !vscode.workspace.isTrusted
                || (!manual && !vscode.workspace.getConfiguration("ooc").get("autoUpdate", true))) return;
            await vscode.commands.executeCommand("workbench.extensions.installExtension", vscode.Uri.file(update.file));
            installed = update.version;
            lastError = undefined;
            if (applyRuntime && update.bundle) {
                try {
                    if (await applyRuntime(update.bundle)) {
                        output.appendLine(`OOC ${installed} applied live. The schematic connection is preserved.`);
                        vscode.window.setStatusBarMessage(`OOC updated to ${installed}`, 5000);
                        return;
                    }
                } catch (error) {
                    output.appendLine(`OOC kept the previous runtime after an update error: ${error.message}`);
                }
            }
            output.appendLine(`Installed OOC ${installed} from ${root}. Reload VS Code to activate it.`);
            const choice = await vscode.window.showInformationMessage(
                `OOC ${installed} needs a reload for loader or editor-registration changes. Reload when ready.`, "Reload Window",
            );
            if (choice === "Reload Window") await vscode.commands.executeCommand("workbench.action.reloadWindow");
        } catch (error) {
            if (error.message !== lastError) {
                output.appendLine(`OOC update: ${error.message}`);
                lastError = error.message;
            }
            if (manual) vscode.window.showErrorMessage(`OOC update failed: ${error.message}`);
        } finally { busy = false; }
    }
    const timer = setInterval(() => { void check(); }, 30000);
    timer.unref?.();
    context.subscriptions.push(
        { dispose() { disposed = true; clearInterval(timer); } },
        vscode.commands.registerCommand("ooc.checkForUpdates", () => check(true)),
    );
    void check();
    return { check };
}

module.exports = { newer, candidate, startLocalUpdates };
