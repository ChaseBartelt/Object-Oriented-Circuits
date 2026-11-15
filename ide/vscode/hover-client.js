const { spawn } = require("node:child_process");

// One offscreen renderer per checkout, kept warm instead of starting Qt per hover.
class HoverClient {
    constructor(python, cwd, report) {
        this.python = python;
        this.cwd = cwd;
        this.report = report;
        this.pending = new Map();
        this.serial = 0;
        this.child = undefined;
    }

    start() {
        if (this.child) return;
        const child = spawn(this.python, ["-X", "utf8", "-m", "circuitlab.ide_hover"], {
            cwd: this.cwd, windowsHide: true, stdio: ["pipe", "pipe", "pipe"],
        });
        this.child = child;
        let buffer = "";
        child.stdout.setEncoding("utf8");
        child.stdout.on("data", (chunk) => {
            buffer += chunk;
            if (buffer.length > 8 * 1024 * 1024) { this.dispose(); return; }
            let end;
            while ((end = buffer.indexOf("\n")) >= 0) {
                const line = buffer.slice(0, end);
                buffer = buffer.slice(end + 1);
                try {
                    const response = JSON.parse(line);
                    const request = this.pending.get(response.id);
                    if (request) {
                        clearTimeout(request.timer);
                        this.pending.delete(response.id);
                        request.resolve(response.result);
                    }
                } catch { /* Ignore non-protocol stdout without executing it. */ }
            }
        });
        const failed = () => {
            if (this.child !== child) return;
            this.child = undefined;
            for (const request of this.pending.values()) {
                clearTimeout(request.timer);
                request.resolve(undefined);
            }
            this.pending.clear();
        };
        child.once("error", (error) => { this.report(`OOC hover: ${error.message}`); failed(); });
        child.once("exit", failed);
        child.stdin.on("error", failed);
        child.stderr.on("data", (data) => this.report(`OOC hover: ${data.toString().trim()}`));
    }

    request(payload) {
        if (payload.source.length > 2 * 1024 * 1024 || this.pending.size >= 4) return Promise.resolve(undefined);
        this.start();
        const id = ++this.serial;
        return new Promise((resolve) => {
            const timer = setTimeout(() => {
                this.report("OOC hover preview timed out; the renderer will restart on the next hover.");
                this.dispose();
            }, 20000);
            this.pending.set(id, { resolve, timer });
            this.child.stdin.write(JSON.stringify({ id, ...payload }) + "\n", "utf8", (error) => {
                if (error) this.dispose();
            });
        });
    }

    dispose() {
        const child = this.child;
        this.child = undefined;
        if (child) { child.stdin.end(); child.kill(); }
        for (const request of this.pending.values()) {
            clearTimeout(request.timer);
            request.resolve(undefined);
        }
        this.pending.clear();
    }
}

module.exports = { HoverClient };
