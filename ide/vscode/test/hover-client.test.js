const assert = require("node:assert/strict");
const path = require("node:path");
const { existsSync } = require("node:fs");
const test = require("node:test");
const { HoverClient } = require("../hover-client");

const root = path.resolve(__dirname, "../../..");
const python = process.env.OOC_PYTHON || path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");

test("real hover worker returns gate and wire PNGs over the same process", {
    skip: !existsSync(python), timeout: 60000,
}, async () => {
    const messages = [];
    const client = new HoverClient(python, root, (message) => messages.push(message));
    try {
        const file = path.join(root, "examples", "preview.ckt");
        const gate = await client.request({ file, source: "nor(vcc,gnd,a,b,y);", line: 0, column: 1 });
        assert.ok(gate?.image, messages.join("\n"));
        const child = client.child;
        const source = "V1(5) between(in,0);\nR1(1k) between(in,out);\nR2(1k) between(out,0);";
        const wire = await client.request({ file, source, line: 1, column: 18 });
        assert.equal(client.child, child);
        assert.deepEqual(wire.nets, ["out"]);
        assert.equal(Buffer.from(wire.image, "base64").subarray(1, 4).toString(), "PNG");
        const comment = await client.request({ file, source: "# nor(vcc,gnd,a,b,y);", line: 0, column: 3 });
        assert.equal(comment, null);
        assert.equal(client.pending.size, 0);
    } finally { client.dispose(); }
});
