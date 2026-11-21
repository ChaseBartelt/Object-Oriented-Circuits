"""Recognize source comments without changing statement positions."""

import re


_TOKENS = re.compile(
    r'"(?:\\[^\r\n]|[^"\\\r\n])*"?'
    r'|(?P<comment>/\*[\s\S]*?(?:\*/|\Z)|(?://|#)[^\r\n]*)'
)
_NOT_NEWLINE = re.compile(r"[^\r\n]")


def mask_comments(source):
    """Replace comment characters with spaces, preserving strings and newlines."""
    def mask(match):
        return _NOT_NEWLINE.sub(" ", match[0]) if match["comment"] is not None else match[0]

    return _TOKENS.sub(mask, source)
