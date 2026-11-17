// Run with VS Code's --extensionTestsPath and the packaged extension staged
// under --extensionDevelopmentPath, using an isolated VS Code profile.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const vscode = require("vscode");

async function run() {
    const repo = path.resolve(__dirname, "../../..");
    const divider = vscode.Uri.file(path.join(repo, "examples/circuit_method.ckt"));
    const document = await vscode.workspace.openTextDocument(divider);
    await vscode.window.showTextDocument(document);
    assert.equal(document.languageId, "ooc");

    const symbols = await vscode.commands.executeCommand("vscode.executeDocumentSymbolProvider", divider);
    assert.deepEqual(symbols.map(({ name }) => name), ["test", "divider", "main"]);

    const folds = await vscode.commands.executeCommand("vscode.executeFoldingRangeProvider", divider);
    for (const [start, end] of [[1, 4], [6, 9], [11, 14]]) {
        assert.ok(folds.some((fold) => fold.start === start && fold.end === end));
    }

    const call = document.positionAt(document.getText().indexOf("divider(vin,"));
    const definitions = await vscode.commands.executeCommand("vscode.executeDefinitionProvider", divider, call);
    const definition = definitions[0];
    const targetUri = definition.uri || definition.targetUri;
    const targetRange = definition.range || definition.targetSelectionRange;
    assert.equal(targetUri.toString(), divider.toString());
    assert.equal(document.getText(targetRange), "divider");
    assert.equal(targetRange.start.line, 6);

    assert.equal(await vscode.commands.executeCommand("ooc.check"), true);
    assert.equal(await vscode.commands.executeCommand("ooc.solve"), true);

    // Use the actual compiler and actual VS Code language providers against
    // every new behavioral example. Run/save pipe reuse is covered by launch.test.js
    // so this isolated host does not create persistent viewer windows.
    for (const [filename, top, chip] of [
        ['01_byte_selector.ckt', 'ByteSelector', 'SelectByte'],
        ['02_counter_bank.ckt', 'CounterBank', 'CountBy50'],
        ['03_hybrid01.ckt', 'HYBRID01', 'Tone'],
        ['04_switch_bank.ckt', 'HYBRID01', 'Follow'],
    ]) {
        const uri = vscode.Uri.file(path.join(repo, 'examples/behavioral', filename));
        const source = await vscode.workspace.openTextDocument(uri);
        await vscode.window.showTextDocument(source);
        assert.equal(source.languageId, 'ooc', filename);
        const outline = await vscode.commands.executeCommand('vscode.executeDocumentSymbolProvider', uri);
        assert.ok(outline.some(symbol => symbol.name === top && symbol.kind === vscode.SymbolKind.Module), filename);
        assert.ok(outline.some(symbol => symbol.name === chip && symbol.kind === vscode.SymbolKind.Class), filename);
        const assembly = outline.find(symbol => symbol.name === top && symbol.kind === vscode.SymbolKind.Module);
        const regions = await vscode.commands.executeCommand('vscode.executeFoldingRangeProvider', uri);
        assert.ok(regions.some(region => region.start === assembly.range.start.line && region.end === assembly.range.end.line), filename);
        const invocation = source.positionAt(source.getText().lastIndexOf(chip + '('));
        const targets = await vscode.commands.executeCommand('vscode.executeDefinitionProvider', uri, invocation);
        assert.ok(targets.length, filename);
        const target = targets[0];
        assert.equal((target.uri || target.targetUri).toString(), uri.toString(), filename);
        assert.equal(source.getText(target.range || target.targetSelectionRange), chip, filename);
        assert.equal(await vscode.commands.executeCommand('ooc.check'), true, filename);
        assert.equal(vscode.languages.getDiagnostics(uri).filter(issue => issue.source === 'OOC').length, 0, filename);
    }

    const temporary = await fs.mkdtemp(path.join(__dirname, "ooc-host-"));
    const invalid = path.join(temporary, "invalid.ckt");
    try {
        await fs.writeFile(invalid, "V1(5) between(in, 0);\ninvalid op\n");
        const invalidUri = vscode.Uri.file(invalid);
        await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(invalidUri));
        assert.equal(await vscode.commands.executeCommand("ooc.check"), false);
        const issues = vscode.languages.getDiagnostics(invalidUri);
        assert.ok(issues.some((issue) => issue.source === "OOC" && issue.range.start.line === 1));
    } finally {
        await fs.unlink(invalid);
        await fs.rmdir(temporary);
    }
}

module.exports = { run };
