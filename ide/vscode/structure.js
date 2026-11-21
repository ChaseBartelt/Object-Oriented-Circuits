// Navigation scans offsets in unfinished text; successful scanning is not compiler validation.
// Quoted strings, comments, interpolation, and legacy delimiters must not create false blocks
// or method references in document symbols, folds, and definition links.
"use strict";

// Tolerant source scanning for editor navigation. Compilation still decides
// whether the statements and their electrical connections are valid.
const TOKEN = /"(?:\\[^\r\n]|[^"\\\r\n])*"?|\$\{[^}\r\n]*(?:\}|(?=\r|\n|$))|\r\n|[\r\n(){};]/g;
const PROTECTED = /"(?:\\[^\r\n]|[^"\\\r\n])*"?|\/\/[^\r\n]*|\/\*[\s\S]*?(?:\*\/|$)|#[^\r\n]*/g;
const DECLARATION = /^(circuit|chip|gate|series|parallel)[ \t]+([A-Za-z_][A-Za-z0-9_]*)(?:[ \t]*\(.*\))?$/id;
const DEVICE_BLOCK = /^(battery|transistor)[ \t]+([A-Za-z](?:[A-Za-z0-9_]|\$\{[^}]*\})*)$/id;
const MAIN = /^(main)(?:[ \t]+([A-Za-z_][A-Za-z0-9_]*))?(?:[ \t]*\([ \t]*\))?$/id;
const INLINE = /^(series|parallel)(?:[ \t]*\(.*\))?$/id;
const BRANCH = /^(else[ \t]+if|if|for|while|clock|every|switch)[ \t]*\(.*\)$/id;
const KEYWORD_BLOCK = /^(else|do)$/id;
const LEGACY = /\([ \t]*(?:(end)[ \t]+)?(?:(series|parallel)|(battery)(?:[ \t]+([^()=#\r\n]+?))?|(opamp)(?:[ \t]+([A-Za-z][A-Za-z0-9_]*))?)[ \t]*\)/idy;
const INCLUDE = /^include[ \t]*\([ \t]*"([^"\r\n]+)"[ \t]*\)$/id;
const CALL = /^([A-Za-z][A-Za-z0-9_]*)[ \t]*\((?:[^()]|\([^()]*\))*\)$/id;
const DO_TAIL = /^while[ \t]*\(.*\)$/i;
const NON_METHODS = new Set(["include", "connector"]);

// Preserve UTF-16 offsets and line breaks for editor ranges and statement bounds.
function maskComments(text, maskStrings = false) {
    return text.replace(PROTECTED, token => !maskStrings && token.startsWith('"')
        ? token : token.replace(/[^\r\n]/g, " "));
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
    text = maskComments(text);
    const structure = [];
    const references = [];
    const frames = [{ children: structure }];
    let previousEnd = 0;
    let skipUntil = 0;
    let parens = 0;
    let doTail = null;

    function open(closer, node) {
        const { children } = frames[frames.length - 1];
        if (node) {
            children.push(node);
        }
        frames.push({ closer, node, children: node ? node.children : children });
    }

    function close(closer, end) {
        doTail = null;
        const frame = frames[frames.length - 1];
        if (frame.closer !== closer) {
            return;
        }
        frames.pop();
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
        if (doTail && DO_TAIL.test(statement)) {
            doTail.end = end;
            doTail = null;
            return;
        }
        doTail = null;
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
        }
    }

    for (const match of text.matchAll(TOKEN)) {
        const token = match[0];
        const at = match.index;
        if (at < skipUntil) {
            continue;
        }
        const after = at + token.length;
        if (token[0] === "\r" || token[0] === "\n") {
            finishStatement(at);
            previousEnd = after;
            parens = 0;
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
                parens += 1;
            }
        } else if (token === ")") {
            parens = Math.max(0, parens - 1);
        } else if (parens === 0 && token === "{") {
            const raw = text.slice(previousEnd, at);
            const header = raw.trim();
            const start = previousEnd + raw.search(/\S/);
            doTail = null;
            open("}", header ? bracedNode(header, start, text.length) : null);
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
    return { structure, references };
}

function parseStructure(text) {
    return scanDocument(text).structure;
}

function parseReferences(text) {
    return scanDocument(text).references;
}

module.exports = { analyzeSource: scanDocument, parseStructure, parseReferences, maskComments };
