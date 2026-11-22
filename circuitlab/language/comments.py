"""Recognize source comments without changing statement positions."""

import re

from circuitlab.language.thumb_rom import ARM_ROM


_STRING = r'"(?:\\[^\r\n]|[^"\\\r\n])*"?'
# Pin-list metadata permits a closed quoted value across physical lines. Match
# that declaration before ordinary single-line strings so comment markers stay
# literal data and reach the member-name validator. Requiring a real closing
# quote at a statement boundary leaves malformed strings on the old path.
_ARGUMENT_LIST_STRING = (
    r'\b[A-Za-z_][A-Za-z0-9_]*(?:[$*]\{(?:(?!//|/\*|\#)[^{}\r\n])*\}[A-Za-z0-9_]*)*'
    r'\s*\[[^\]\r\n]*:[^\]\r\n]*\]\s*=(?!=)\s*'
    r'"(?:\\[^\r\n]|[^"\\])*"(?=[ \t]*(?:;|\}|\r?\n|\Z))'
)
_BLOCK = r'/\*[\s\S]*?(?:\*/|\Z)'
_TOKENS = re.compile(rf'{_ARGUMENT_LIST_STRING}|{_STRING}|(?P<comment>{_BLOCK}|(?://|\#)[^\r\n]*)')
_WITH_ARM = re.compile(rf'{_TOKENS.pattern}|(?P<arm>(?i:{ARM_ROM.pattern}))')
_IN_ARM = re.compile(rf'{_STRING}|(?P<comment>{_BLOCK}|//[^\r\n]*)|(?P<brace>[{{}}])')
_NOT_NEWLINE = re.compile(r"[^\r\n]")


def mask_comments(source):
    """Replace comment characters with spaces, preserving strings and newlines."""
    def mask(match):
        return _NOT_NEWLINE.sub(" ", match[0]) if match["comment"] is not None else match[0]

    if ARM_ROM.search(source) is None:
        return _TOKENS.sub(mask, source)

    # The Thumb immediate marker is a literal # inside a ROM. OOC's slash
    # comments work in either context, and legacy # comments work outside ROMs.
    pattern, pieces, offset, depth = _WITH_ARM, [], 0, 0
    while match := pattern.search(source, offset):
        pieces.append(source[offset:match.start()])
        pieces.append(mask(match))
        if pattern is _WITH_ARM and match["arm"] is not None:
            pattern = _IN_ARM
            depth = 1
        elif pattern is _IN_ARM and match["brace"] is not None:
            depth += 1 if match["brace"] == "{" else -1
            if not depth:
                pattern = _WITH_ARM
        offset = match.end()
    return "".join(pieces) + source[offset:]
