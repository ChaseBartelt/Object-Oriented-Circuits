// Installed-runtime discovery and extension synchronization share the installer's integrity
// metadata.
// An available release is offered explicitly; partial/corrupt artifacts never become an editor
// update.
// Complete installed application updates; local development keeps its existing feed.
const { execFile } = require("node:child_process");
const fs = require("node:fs/promises");
const { createHash } = require("node:crypto");
const { newer } = require("./local-updates");

function invoke(installation, command, version) {
    const args = ["-X", "utf8", "-m", "circuitlab.tooling.updates", command, "--root", installation.root];
    if (version) args.push("--expected-version", version);
    return new Promise((resolve, reject) => execFile(installation.python, args, {
        cwd: installation.root, windowsHide: true, encoding: "utf8", timeout: command === "apply" ? 900000 : 70000,
        maxBuffer: 1024 * 1024,
    }, (error, stdout, stderr) => {
        let reply;
        try { reply = JSON.parse(stdout); } catch { return reject(new Error(stderr || error?.message || "Invalid OOC update response.")); }
        if (error || reply.error) reject(new Error(reply.error || error.message));
        else resolve(reply);
    }));
}

// Validate bounded file content against its recorded digest before parsing or installing it.
async function verified(file, checksum, limit) {
    if (!/^[a-f0-9]{64}$/.test(checksum || "")) throw new Error("Invalid installed extension checksum.");
    const info = await fs.stat(file);
    if (!info.isFile() || info.size > limit) throw new Error("Invalid installed extension file.");
    const data = await fs.readFile(file);
    if (createHash("sha256").update(data).digest("hex") !== checksum) throw new Error("Installed extension checksum mismatch; repair OOC Setup.");
    return data;
}

// Serialize checks and synchronize the bundled editor integration before offering an application
// update. Recheck disposal/trust after asynchronous boundaries before applying results.
function installedUpdater(vscode, context, getInstallation, output, applyRuntime, run = invoke) {
    let busy = false, disposed = false, lastCheck = 0;
    let extensionVersion = context.extension.packageJSON.version;
    const offered = new Set();
    context.subscriptions.push({ dispose() { disposed = true; } });

    async function syncExtension(installation) {
        if (!newer(installation.extensionVersion, extensionVersion)) return;
        await verified(installation.extension, installation.extensionSha256, 32 * 1024 * 1024);
        const data = await verified(installation.bundle, installation.bundleSha256, 8 * 1024 * 1024);
        const bundle = JSON.parse(data);
        if (bundle.version !== installation.extensionVersion) throw new Error("Installed extension version mismatch.");
        if (disposed || !vscode.workspace.isTrusted) return;
        await vscode.commands.executeCommand("workbench.extensions.installExtension", vscode.Uri.file(installation.extension));
        extensionVersion = installation.extensionVersion;
        if (await applyRuntime(bundle)) return;
        const answer = await vscode.window.showInformationMessage("OOC's editor integration was updated. Reload when ready.", "Reload Window");
        if (answer === "Reload Window") await vscode.commands.executeCommand("workbench.action.reloadWindow");
    }

    return async function check(manual = false) {
        if (busy || disposed || !vscode.workspace.isTrusted) return;
        if (!manual && !vscode.workspace.getConfiguration("ooc").get("autoUpdate", true)) return;
        const installation = getInstallation();
        if (!installation) {
            if (manual) vscode.window.showInformationMessage("Run OOC Setup, or open your OOC development checkout, to configure updates.");
            return;
        }
        busy = true;
        try {
            await syncExtension(installation);
            if (!manual && Date.now() - lastCheck < 6 * 60 * 60 * 1000) return;
            lastCheck = Date.now();
            const result = await run(installation, "check");
            if (disposed || !vscode.workspace.isTrusted) return;
            if (!result.available) {
                if (manual) vscode.window.showInformationMessage(result.message || `OOC ${installation.version} is up to date.`);
                return;
            }
            const version = result.release.version;
            if (!manual && offered.has(version)) return;
            offered.add(version);
            const answer = await vscode.window.showInformationMessage(
                `OOC ${version} is available for the compiler, viewer, library and extension.`, "Install update", "Later");
            if (answer !== "Install update" || disposed || !vscode.workspace.isTrusted) return;
            await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: "Updating OOC…" },
                () => run(installation, "apply", version));
            if (disposed) return;
            await syncExtension(getInstallation());
            vscode.window.showInformationMessage(`OOC ${version} installed. Close the previous viewer before running again.`);
        } catch (error) {
            output.appendLine(`OOC update: ${error.message}`);
            if (manual) vscode.window.showErrorMessage(`OOC update failed: ${error.message}`);
        } finally { busy = false; }
    };
}

module.exports = { installedUpdater, invoke, verified };
