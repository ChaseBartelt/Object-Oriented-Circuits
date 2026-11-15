const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const test = require("node:test");
const { newer, candidate, startLocalUpdates } = require("../local-updates");

async function fixture(t, version = "0.6.0") {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), "ooc-update-"));
    t.after(() => fs.rm(root, { recursive: true, force: true }));
    const dir = path.join(root, "ide", "vscode", "dist");
    await fs.mkdir(dir, { recursive: true });
    const bytes = Buffer.from("complete package test bytes");
    const manifest = {
        id: "echoingfarts.ooc-language", version, file: `ooc-language-${version}.vsix`,
        sha256: createHash("sha256").update(bytes).digest("hex"),
    };
    await fs.writeFile(path.join(dir, manifest.file), bytes);
    const publish = () => fs.writeFile(path.join(dir, "latest.json"), JSON.stringify(manifest));
    await publish();
    return { root, dir, manifest, publish };
}

test("version comparison upgrades numerically and never downgrades or reinstalls the same version", () => {
    assert.equal(newer("0.10.0", "0.9.9"), true);
    assert.equal(newer("1.0.0", "0.99.0"), true);
    for (const version of ["0.4.0", "0.5.0", "../other", "0.6.0-beta", "1"]) {
        assert.equal(newer(version, "0.5.0"), false);
    }
});

test("only matching complete local releases are accepted", async (t) => {
    const f = await fixture(t);
    assert.equal((await candidate(f.root, "0.5.0")).version, "0.6.0");
    assert.equal(await candidate(f.root, "0.6.0"), undefined);
    f.manifest.id = "someone.else";
    await f.publish();
    assert.equal(await candidate(f.root, "0.5.0"), undefined);
    f.manifest.id = "echoingfarts.ooc-language";
    f.manifest.file = "../other.vsix";
    await f.publish();
    await assert.rejects(candidate(f.root, "0.5.0"), /manifest/);
    f.manifest.file = "ooc-language-0.6.0.vsix";
    await f.publish();
    await fs.writeFile(path.join(f.dir, f.manifest.file), "partial");
    await assert.rejects(candidate(f.root, "0.5.0"), /incomplete/);
});

function host(t, root, { trusted = true, enabled = true, fail = false, applyRuntime } = {}) {
    const calls = [], messages = [], logs = [];
    let activeRoot;
    const context = { extension: { packageJSON: { version: "0.5.0" } }, subscriptions: [] };
    const vscode = {
        workspace: { isTrusted: trusted, getConfiguration: () => ({ get: () => enabled }) },
        Uri: { file: (file) => ({ fsPath: file }) },
        commands: {
            registerCommand: () => ({ dispose() {} }),
            async executeCommand(...args) {
                calls.push(args);
                if (fail) throw new Error("installation refused");
            },
        },
        window: {
            async showInformationMessage(...args) { messages.push(args); },
            showErrorMessage(message) { messages.push([message]); },
            setStatusBarMessage(message) { logs.push(message); },
        },
    };
    const updater = startLocalUpdates(vscode, context, () => activeRoot, { appendLine: (s) => logs.push(s) }, applyRuntime);
    activeRoot = root;
    t.after(() => context.subscriptions.forEach((item) => item.dispose()));
    return { updater, calls, messages, logs, vscode };
}

test("installs once, offers reload, and never forces a window reload", async (t) => {
    const f = await fixture(t);
    const h = host(t, f.root);
    await h.updater.check();
    await h.updater.check();
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0][0], "workbench.extensions.installExtension");
    assert.equal(h.calls[0][1].fsPath, path.join(f.dir, f.manifest.file));
    assert.equal(h.messages[0][1], "Reload Window");
});

test("verified compatible runtime is applied with no reload command or dialog", async (t) => {
    const f = await fixture(t);
    const bytes = Buffer.from(JSON.stringify({ api: 1, version: "0.6.0", compatibility: "stable", files: {} }));
    f.manifest.runtime = { file: "ooc-runtime-0.6.0.json", sha256: createHash("sha256").update(bytes).digest("hex") };
    await fs.writeFile(path.join(f.dir, f.manifest.runtime.file), bytes);
    await f.publish();
    let applied;
    const h = host(t, f.root, { applyRuntime: (bundle) => { applied = bundle; return true; } });
    await h.updater.check();
    assert.equal(applied.version, "0.6.0");
    assert.equal(h.messages.length, 0);
    assert.equal(h.calls.length, 1);
    await fs.writeFile(path.join(f.dir, f.manifest.runtime.file), "changed");
    await assert.rejects(candidate(f.root, "0.5.0"), /checksum/);
});

test("disabled updates and restricted workspaces never auto-install", async (t) => {
    const f = await fixture(t);
    const disabled = host(t, f.root, { enabled: false });
    await disabled.updater.check();
    assert.equal(disabled.calls.length, 0);
    await disabled.updater.check(true);
    assert.equal(disabled.calls.length, 1);
    const restricted = host(t, f.root, { trusted: false });
    await restricted.updater.check(true);
    assert.equal(restricted.calls.length, 0);
});

test("installation failures remain retryable and do not report success", async (t) => {
    const f = await fixture(t);
    const h = host(t, f.root, { fail: true });
    await h.updater.check();
    await h.updater.check();
    assert.equal(h.calls.length, 2);
    assert.equal(h.messages.length, 0);
    assert.equal(h.logs.length, 1);
});
