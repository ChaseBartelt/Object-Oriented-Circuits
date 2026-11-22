// Navigation scans offsets in unfinished text; successful scanning is not compiler validation.
// Quoted strings, comments, interpolation, and legacy delimiters must not create false blocks
// or method references in document symbols, folds, and definition links.
"use strict";

// Tolerant source scanning for editor navigation. Compilation still decides
// whether the statements and their electrical connections are valid.
const TOKEN = /"(?:\\[\s\S]|[^"\\])*"?|[$*]\{[^}\r\n]*(?:\}|(?=\r|\n|$))|\r\n|[\r\n(){};]/g;
const PROTECTED = /"(?:\\[\s\S]|[^"\\])*"?|\/\/[^\r\n]*|\/\*[\s\S]*?(?:\*\/|$)|#[^\r\n]*/g;
const ARM_ROM = /\barm\s*\(\s*[A-Za-z_]\w*\s*,\s*[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)?\s*\)\s*\{/i;
const OUTSIDE_ARM = new RegExp(`(?<arm>${ARM_ROM.source})|${PROTECTED.source}`, "gi");
const INSIDE_ARM = /(?<brace>[{}])|"(?:\\[\s\S]|[^"\\])*"?|\/\/[^\r\n]*|\/\*[\s\S]*?(?:\*\/|$)/g;
const DECLARATION = /^(circuit|chip|gate|series|parallel)[ \t]+([A-Za-z_][A-Za-z0-9_]*)(?:[ \t]*\(.*\))?$/id;
const DEVICE_BLOCK = /^(battery|transistor)[ \t]+([A-Za-z](?:[A-Za-z0-9_]|[$*]\{[^}]*\})*)$/id;
const MAIN = /^(main)(?:[ \t]+([A-Za-z_][A-Za-z0-9_]*))?(?:[ \t]*\([ \t]*\))?$/id;
const INLINE = /^(series|parallel)(?:[ \t]*\(.*\))?$/id;
const BRANCH = /^(else[ \t]+if|if|for|while|clock|every|switch|arm)[ \t]*\(.*\)$/id;
const KEYWORD_BLOCK = /^(else|do|clear)$/id;
const PORT_BLOCK = /^(input|output|wire)(?:[ \t]+([A-Za-z_][A-Za-z0-9_]*))?$/id;
const PORT_DIRECTION = /^(.+?)[ \t]*=[ \t]*(input|output|wire)$/id;
const PORT_NAME = String.raw`[A-Za-z_][A-Za-z0-9_]*(?:[$*]\{[^}\r\n]*\}[A-Za-z0-9_]*)*`;
const PORT_ITEM = new RegExp(`(${PORT_NAME})([ \\t]*\\[[^\\]\\r\\n]+\\])?`, "gd");
const PORT_LIST = new RegExp(`^(?:${PORT_NAME}(?:[ \\t]*\\[[^\\]\\r\\n]+\\])?)(?:[ \\t]*,[ \\t]*${PORT_NAME}(?:[ \\t]*\\[[^\\]\\r\\n]+\\])?)*$`);
const LEGACY = /\([ \t]*(?:(end)[ \t]+)?(?:(series|parallel)|(battery)(?:[ \t]+([^()=#\r\n]+?))?|(opamp)(?:[ \t]+([A-Za-z][A-Za-z0-9_]*))?)[ \t]*\)/idy;
const INCLUDE = /^include\s*\(\s*"([^"\r\n]+)"\s*\)$/id;
const CALL = /^([A-Za-z][A-Za-z0-9_]*)[ \t]*\((?:[^()]|\([^()]*\))*\)$/id;
const DO_TAIL = /^while[ \t]*\(.*\)$/i;
const NON_METHODS = new Set(["include", "connector"]);

// Preserve UTF-16 offsets and line breaks for editor ranges and statement bounds.
function maskComments(text, maskStrings = false, comments) {
    const mask = (token, offset) => {
        if (!token.startsWith('"')) comments?.push({ start: offset, end: offset + token.length, text: token });
        return !maskStrings && token.startsWith('"') ? token : token.replace(/[^\r\n]/g, " ");
    };
    if (!ARM_ROM.test(text)) {
        return text.replace(PROTECTED, mask);
    }
    const pieces = [];
    let cursor = 0;
    let depth = 0;
    let tokens = OUTSIDE_ARM;
    for (;;) {
        tokens.lastIndex = cursor;
        const match = tokens.exec(text);
        if (!match) {
            break;
        }
        const boundary = match.groups.arm || match.groups.brace;
        pieces.push(text.slice(cursor, match.index), boundary ? match[0] : mask(match[0], match.index));
        cursor = tokens.lastIndex;
        if (match.groups.arm) {
            depth = 1;
            tokens = INSIDE_ARM;
        } else if (match.groups.brace) {
            depth += match[0] === "{" ? 1 : -1;
            if (!depth) {
                tokens = OUTSIDE_ARM;
            }
        }
    }
    return pieces.join("") + text.slice(cursor);
}

function nodeFor(match, nameGroup, kindGroup, start, end) {
    const [from, to] = match.indices[nameGroup];
    const name = match[nameGroup];
    const kind = match[kindGroup].toLowerCase().replace(/[ \t]+/g, " ");
    return {
        name,
        kind,
        start,
        end,
        selectionStart: start + from,
        selectionEnd: start + to,
        children: [],
    };
}

// Keep display ranges distinct from selected-name ranges so navigation highlights the name
// while a fold or outline item still spans the complete structural block.
function bracedNode(header, start, end) {
    // Flatten whitespace without changing UTF-16 offsets within a multiline header.
    header = header.replace(/[\r\n]/g, " ");
    const ports = PORT_BLOCK.exec(header);
    if (ports) {
        const node = nodeFor(ports, ports[2] ? 2 : 1, 1, start, end);
        if (ports[2]) node.detail = node.kind;
        return node;
    }
    const main = MAIN.exec(header);
    if (main) {
        const node = nodeFor(main, main[2] ? 2 : 1, 1, start, end);
        node.detail = "main";
        return node;
    }
    const named = DECLARATION.exec(header) || DEVICE_BLOCK.exec(header);
    if (named) {
        const node = nodeFor(named, 2, 1, start, end);
        const argumentsText = header.slice(named.indices[2][1]).trim();
        node.detail = argumentsText ? node.kind + " " + argumentsText : node.kind;
        return node;
    }
    const unnamed = INLINE.exec(header)
        || BRANCH.exec(header) || KEYWORD_BLOCK.exec(header);
    if (unnamed) {
        const node = nodeFor(unnamed, 1, 1, start, end);
        const argumentsText = header.slice(unnamed.indices[1][1]).trim();
        if (argumentsText) {
            node.detail = argumentsText;
        }
        return node;
    }
    return null;
}

function legacyMarker(text, start, previousEnd) {
    LEGACY.lastIndex = start;
    const marker = LEGACY.exec(text);
    if (!marker) {
        return null;
    }
    const prefix = text.slice(previousEnd, start).trim();
    // A marker can follow a legacy assignment on the same line. A word
    // followed by parentheses can also be an ordinary method call.
    if (prefix && !prefix.includes("=")) {
        return null;
    }
    return marker;
}

// Use one token walk for structure and references so both features agree on source offsets.
// The frame stack keeps nested braced/legacy blocks and do/while tails attached correctly.
function scanDocument(text) {
    const original = text;
    const comments = [];
    text = maskComments(text, false, comments);
    const structure = [];
    const references = [];
    const portTokens = [];
    const pairs = [];
    const includes = [];
    const parentheses = [];
    const frames = [{ children: structure }];
    let previousEnd = 0;
    let skipUntil = 0;
    let parens = 0;
    let doTail = null;

    function portName(name, start, direction) {
        // Interpolation retains its own colors; only the literal name pieces
        // carry the declaration role, never widths or loop expressions.
        const pieces = /[$*]\{[^}]*\}|[A-Za-z_][A-Za-z0-9_]*/g;
        for (const piece of name.matchAll(pieces)) {
            if (!piece[0].startsWith("${") && !piece[0].startsWith("*{")) {
                portTokens.push({ start: start + piece.index, end: start + piece.index + piece[0].length, direction, kind: "member" });
            }
        }
    }

    function open(closer, node, start = node?.start) {
        const parent = frames[frames.length - 1];
        const { children } = parent;
        if (node) {
            children.push(node);
            if (["input", "output", "wire"].includes(node.kind)) {
                portTokens.push({ start: node.start, end: node.start + node.kind.length, direction: node.kind, kind: "keyword" });
                if (node.selectionStart > node.start) {
                    portTokens.push({ start: node.selectionStart, end: node.selectionEnd,
                        direction: node.kind, kind: "label" });
                }
            }
        }
        frames.push({ closer, node, foldStart: start, children: node ? node.children : children,
            portDirection: node && ["input", "output", "wire"].includes(node.kind) ? node.kind : parent.portDirection });
    }

    function close(closer, end) {
        doTail = null;
        const frame = frames[frames.length - 1];
        if (frame.closer !== closer) {
            return;
        }
        frames.pop();
        if (frame.foldStart !== undefined) pairs.push({ start: frame.foldStart, end });
        if (frame.node) {
            frame.node.end = end;
            if (frame.node.kind === "do") {
                doTail = frame.node;
            }
        }
    }

    // Only complete recognized include/call statements become references; ordinary assignments
    // and control-flow tails must not navigate as if they were reusable methods.
    function finishStatement(to, end = to) {
        const raw = text.slice(previousEnd, to);
        const statement = raw.trim();
        if (!statement) {
            return;
        }
        if (doTail && DO_TAIL.test(statement.replace(/[\r\n]/g, " "))) {
            doTail.end = end;
            pairs.push({ start: doTail.start, end });
            doTail = null;
            return;
        }
        doTail = null;
        const frame = frames[frames.length - 1];
        const logicalStatement = statement.replace(/[\r\n]/g, " ");
        if (frame.portDirection && PORT_LIST.test(logicalStatement)) {
            const statementStart = previousEnd + raw.search(/\S/);
            for (const port of logicalStatement.matchAll(PORT_ITEM)) {
                const [from, to] = port.indices[1];
                portName(port[1], statementStart + from, frame.portDirection);
                frame.children.push({
                    name: port[1], kind: "pin", detail: frame.portDirection + (port[2] ? " " + port[2].trim() : ""),
                    start: statementStart + port.index,
                    end: statementStart + port.index + port[0].length,
                    selectionStart: statementStart + from, selectionEnd: statementStart + to,
                    children: [],
                });
            }
            return;
        }
        const declaration = PORT_DIRECTION.exec(logicalStatement);
        if (declaration && PORT_LIST.test(declaration[1].trim())) {
            const statementStart = previousEnd + raw.search(/\S/);
            const direction = declaration[2].toLowerCase();
            for (const port of declaration[1].matchAll(PORT_ITEM)) {
                portName(port[1], statementStart + port.indices[1][0], direction);
            }
            const [from, to] = declaration.indices[2];
            portTokens.push({ start: statementStart + from, end: statementStart + to, direction, kind: "keyword" });
        }
        const include = INCLUDE.exec(statement);
        const reference = include || CALL.exec(statement);
        if (reference && (include || !NON_METHODS.has(reference[1].toLowerCase()))) {
            const [start, stop] = reference.indices[1];
            const statementStart = previousEnd + raw.search(/\S/);
            references.push({
                name: reference[1],
                kind: include ? "include" : "call",
                start: statementStart + start,
                end: statementStart + stop,
            });
            if (include) includes.push({ start: statementStart, end });
        }
    }

    for (const match of text.matchAll(TOKEN)) {
        const token = match[0];
        const at = match.index;
        if (at < skipUntil) {
            continue;
        }
        const after = at + token.length;
        if (token.startsWith('"')) {
            // Pin-list aliases are strings, so their contents stay opaque to
            // block/navigation scanning. The arrow belongs to the declaration.
            const raw = text.slice(previousEnd, at);
            const assignment = /^(?:const\s+)?[A-Za-z_]\w*(?:\s*\[[^\]]+\])?\s*=\s*$/.test(raw.trim());
            pairs.push({ start: assignment ? previousEnd + raw.search(/\S/) : at,
                end: after, incomplete: !/^"(?:\\[\s\S]|[^"\\])*"$/.test(token) });
        } else if (token[0] === "\r" || token[0] === "\n") {
            // A newline inside a call/header or after a continuation does not
            // end the statement. Braces may also start on the following line.
            const pending = parens || /[,=][ \t\r\n]*$/.test(text.slice(previousEnd, at))
                || /^[ \t\r\n]*\{/.test(text.slice(after));
            if (!pending) {
                finishStatement(at);
                previousEnd = after;
            }
        } else if (token === "(") {
            const marker = parens === 0 ? legacyMarker(text, at, previousEnd) : null;
            if (marker) {
                const kindGroup = marker[2] ? 2 : marker[3] ? 3 : 5;
                const nameGroup = marker[4] ? 4 : marker[6] ? 6 : kindGroup;
                const kind = marker[kindGroup].toLowerCase();
                const end = at + marker[0].length;
                finishStatement(at);
                if (marker[1]) {
                    close(kind, end);
                } else {
                    // Sticky matches carry absolute document offsets.
                    const [start, stop] = marker.indices[nameGroup];
                    const node = {
                        name: marker[nameGroup],
                        kind,
                        start: at,
                        end: text.length,
                        selectionStart: start,
                        selectionEnd: stop,
                        children: [],
                    };
                    if (nameGroup !== kindGroup) {
                        node.detail = kind;
                    }
                    open(kind, node);
                }
                previousEnd = end;
                skipUntil = end;
            } else {
                const raw = text.slice(previousEnd, at);
                parentheses.push({ start: parens ? at : previousEnd + (raw.search(/\S/) < 0 ? raw.length : raw.search(/\S/)) });
                parens += 1;
            }
        } else if (token === ")") {
            const frame = parentheses.pop();
            if (frame) pairs.push({ start: frame.start, end: after });
            parens = Math.max(0, parens - 1);
        } else if (parens === 0 && token === "{") {
            const raw = text.slice(previousEnd, at);
            const header = raw.trim();
            const start = previousEnd + raw.search(/\S/);
            doTail = null;
            open("}", header ? bracedNode(header, start, text.length) : null, header ? start : at);
            previousEnd = after;
        } else if (parens === 0 && token === "}") {
            finishStatement(at);
            close("}", after);
            previousEnd = after;
        } else if (parens === 0 && token === ";") {
            finishStatement(at, after);
            previousEnd = after;
        }
    }
    finishStatement(text.length);
    const end = original.trimEnd().length;
    for (const frame of [...frames.slice(1), ...parentheses]) {
        pairs.push({ start: frame.foldStart ?? frame.start, end, incomplete: true });
    }
    // Declaration members supply direction metadata only. Apply their colors
    // to the owning chip's formal parameters, never the declaration body.
    function headerColors(nodes) {
        for (const node of nodes) {
            if (node.kind === "chip") {
                const headerEnd = text.indexOf("{", node.start);
                const header = text.slice(node.start, headerEnd);
                const opening = header.indexOf("(");
                if (opening >= 0) {
                    const roles = new Map(portTokens.filter(token => token.kind === "member"
                        && token.start > headerEnd && token.end <= node.end
                        && token.direction !== "wire")
                        .map(token => [text.slice(token.start, token.end), token.direction]));
                    for (const name of header.slice(opening + 1).matchAll(/[A-Za-z_][A-Za-z0-9_]*/g)) {
                        const direction = roles.get(name[0]);
                        if (direction) {
                            const start = node.start + opening + 1 + name.index;
                            portTokens.push({ start, end: start + name[0].length, direction, kind: "header" });
                        }
                    }
                }
            }
            headerColors(node.children);
        }
    }
    headerColors(structure);
    return { structure, references, portTokens: portTokens.filter(token => token.kind !== "member"), folds: foldingRanges(original, text, pairs, comments, includes) };
}

// Folding is broader than the outline: strings, calls and anonymous blocks
// still need arrows even when they are not navigable hardware definitions.
function foldingRanges(original, code, pairs, comments, includes) {
    const starts = [0];
    for (const match of original.matchAll(/\r\n|[\r\n]/g)) starts.push(match.index + match[0].length);
    function lineAt(offset) {
        let low = 0, high = starts.length;
        while (low + 1 < high) {
            const middle = (low + high) >> 1;
            if (starts[middle] <= offset) low = middle;
            else high = middle;
        }
        return low;
    }
    const ranges = [];
    function add({ start, end, incomplete }, kind = "region") {
        if (start === undefined || end <= start) return;
        const first = lineAt(start);
        let last = lineAt(end - 1);
        // Do not hide unrelated code sharing a closing delimiter's line.
        const tail = code.slice(end, starts[last + 1] ?? code.length);
        if (!incomplete && /[^\s;]/.test(tail)) last--;
        if (last > first) ranges.push({ start: first, end: last, kind });
    }
    for (const pair of pairs) add(pair);

    const regions = [];
    let run = null;
    function finishComments() {
        if (run) add(run, "comment");
        run = null;
    }
    for (const comment of comments) {
        const line = lineAt(comment.start);
        const standalone = !original.slice(starts[line], comment.start).trim();
        if (comment.text.startsWith("/*")) {
            finishComments();
            add({ ...comment, incomplete: !comment.text.endsWith("*/") }, "comment");
        } else if (standalone) {
            const marker = /^(?:\/\/[ \t]*#|#)[ \t]*(end)?region\b/i.exec(comment.text);
            if (marker) {
                finishComments();
                if (marker[1]) {
                    const start = regions.pop();
                    if (start !== undefined) add({ start, end: comment.end });
                } else regions.push(comment.start);
            } else {
                if (!run || line !== lineAt(run.end - 1) + 1) finishComments();
                run = run ? { start: run.start, end: comment.end } : comment;
            }
        } else finishComments();
    }
    finishComments();
    for (const start of regions) add({ start, end: original.trimEnd().length, incomplete: true });

    let imports = null;
    for (const item of includes) {
        if (!imports || code.slice(imports.end, item.start).trim()) {
            if (imports) add(imports, "imports");
            imports = item;
        } else imports = { start: imports.start, end: item.end };
    }
    if (imports) add(imports, "imports");

    // VS Code shows one arrow per line and requires ranges to be disjoint or
    // nested. Prefer the complete block over its multiline argument header.
    ranges.sort((a, b) => a.start - b.start || b.end - a.end);
    const result = [], stack = [];
    for (const range of ranges) {
        while (stack.length && range.start > stack[stack.length - 1].end) stack.pop();
        const parent = stack[stack.length - 1];
        if (parent && (range.start === parent.start || range.end > parent.end)) continue;
        result.push(range);
        stack.push(range);
    }
    return result;
}

function parseStructure(text) {
    return scanDocument(text).structure;
}

function parseReferences(text) {
    return scanDocument(text).references;
}

module.exports = { analyzeSource: scanDocument, parseStructure, parseReferences, maskComments };
