const vm = require("node:vm");
const FILES = ["runtime.js", "structure.js", "hover-client.js"];

function compile(bundle) {
    if (bundle?.api !== 1 || !bundle.files || FILES.some((file) => typeof bundle.files[file] !== "string")) {
        throw new Error("Invalid OOC runtime bundle.");
    }
    const scripts = new Map();
    for (const file of FILES) {
        scripts.set(file, new vm.Script(`(function(require, module, exports) {\n${bundle.files[file]}\n})`, {
            filename: `ooc-${bundle.version}/${file}`,
        }));
    }
    return scripts;
}

function disposeAll(items) {
    for (const item of [...new Set(items)].reverse()) {
        try { item?.dispose?.(); } catch { /* Continue removing the remaining registrations. */ }
    }
}

class LiveRuntime {
    constructor(vscode, context, shared, bundle) {
        this.vscode = vscode;
        this.context = context;
        this.shared = shared;
        this.compatibility = bundle.compatibility;
        this.bundle = bundle;
        this.current = this.start(bundle, compile(bundle));
    }
    get module() { return this.current.module; }

    start(bundle, scripts) {
        const subscriptions = [];
        // Capture registrations immediately, including partial activation failures.
        const track = (api) => new Proxy({}, {
            get(target, property) {
                const value = api[property];
                if (typeof value !== "function" || !/^(register|onDid|createDiagnosticCollection)/.test(String(property))) return value;
                return (...args) => {
                    const result = value.apply(api, args);
                    subscriptions.push(result);
                    return result;
                };
            },
        });
        const api = new Proxy({}, {
            get: (target, key) => {
                return ["commands", "languages", "workspace"].includes(key) ? track(this.vscode[key]) : this.vscode[key];
            },
        });
        const modules = new Map();
        const load = (file) => {
            if (modules.has(file)) return modules.get(file).exports;
            const module = { exports: {} };
            modules.set(file, module);
            const localRequire = (name) => {
                if (name === "vscode") return api;
                if (name.startsWith("node:")) return require(name);
                const dependency = name.startsWith("./") ? name.slice(2) + ".js" : "";
                if (!scripts.has(dependency)) throw new Error(`Unsupported OOC runtime dependency: ${name}`);
                return load(dependency);
            };
            scripts.get(file).runInThisContext()(localRequire, module, module.exports);
            return module.exports;
        };
        try {
            const module = load("runtime.js");
            if (typeof module.activate !== "function") throw new Error("OOC runtime has no activation function.");
            const context = Object.create(this.context);
            Object.defineProperty(context, "subscriptions", { value: subscriptions });
            module.activate(context, this.shared);
            return { module, subscriptions };
        } catch (error) {
            disposeAll(subscriptions);
            throw error;
        }
    }

    apply(bundle) {
        if (!bundle || bundle.compatibility !== this.compatibility || bundle.api !== 1) return false;
        const scripts = compile(bundle);
        const previous = this.bundle;
        disposeAll(this.current.subscriptions);
        try {
            this.current = this.start(bundle, scripts);
            this.bundle = bundle;
        } catch (error) {
            this.current = this.start(previous, compile(previous));
            throw error;
        }
        return true;
    }
    dispose() { disposeAll(this.current.subscriptions); }
}

module.exports = { LiveRuntime, compile };
