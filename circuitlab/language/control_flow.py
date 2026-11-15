"""Elaborate static integer and Boolean control flow into electrical source rows."""

import operator
import re
from collections import ChainMap
from dataclasses import dataclass

from circuitlab.model.graph import CircuitError

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_DECLARATION = re.compile(rf"(let|int|bool)\s+({_NAME})\s*=\s*(.+)", re.I)
_DECLARATION_START = re.compile(rf"(?:let|int|bool)\s+{_NAME}\s*=", re.I)
_ASSIGN = re.compile(rf"({_NAME})\s*(\+=|-=|\*=|/=|%=|=(?!=))\s*(.+)")
_INCREMENT = re.compile(rf"(?:(\+\+|--)\s*({_NAME})|({_NAME})\s*(\+\+|--))")
_MUTATION = re.compile(rf"({_NAME})\s*(?:[+\-*/%]=|\+\+|--)")
_HEADER = re.compile(r"(if|else|for|while|do)(?=[\s({]|$)", re.I)
_CONDITIONAL = re.compile(r"(if|else\s+if|for|while)\s*\((.*)\)\s*\{", re.I)
_DO = re.compile(r"do\s*\{", re.I)
_DO_TEST = re.compile(r"while\s*\((.*)\)\s*;", re.I)
_ELSE = re.compile(r"else\s*\{", re.I)
_DEVICE_BLOCK = re.compile(r"(?:battery|transistor)\s", re.I)
_INTERPOLATION_START = "${"
_INTERPOLATION = re.compile(r"\$\{([^{}]*)\}")
_TOKEN = re.compile(rf"[0-9]+|{_NAME}|\|\||&&|==|!=|<=|>=|\+\+|--|[+\-*/%!<>()]")

_BINARY_PRECEDENCE = {
    "||": 1, "&&": 2, "==": 3, "!=": 3, "<": 4, "<=": 4, ">": 4, ">=": 4,
    "+": 5, "-": 5, "*": 6, "/": 6, "%": 6,
}
_BINARY_EVALUATORS = {
    "==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le,
    ">": operator.gt, ">=": operator.ge, "+": operator.add, "-": operator.sub,
    "*": operator.mul,
}
_MIN_INTEGER = -(1 << 63)
_MAX_INTEGER = (1 << 63) - 1
_MAX_EXPRESSION_TOKENS = 256
_MAX_BLOCK_DEPTH = 64
MAX_ITERATIONS = 10_000
MAX_EXPANDED_ROWS = 100_000


def is_control_block(text):
    """Recognize a braced control header without reserving bare method calls."""
    return text.endswith("{") and bool(_HEADER.match(text))


def is_control_statement(text):
    """Identify rows that opt in to compile-time control flow."""
    statement = text.removesuffix(";").rstrip()
    return bool(
        is_control_block(statement)
        or _DECLARATION_START.match(statement)
        or _INCREMENT.fullmatch(statement)
        or _MUTATION.match(statement)
        or _INTERPOLATION_START in statement
    )


def _value_error(row, message):
    _, line, origin = row
    return CircuitError(message, line, path=origin.path)


@dataclass
class ExpansionBudget:
    """Bound the work performed across all control-flow blocks in a compilation."""

    iterations: int = 0
    output_rows: int = 0

    def iteration(self, row):
        self.iterations += 1
        if self.iterations > MAX_ITERATIONS:
            raise _value_error(row, f"Compile-time control flow exceeded {MAX_ITERATIONS:,} loop iterations.")

    def emitted(self, row):
        self.output_rows += 1
        if self.output_rows > MAX_EXPANDED_ROWS:
            raise _value_error(row, f"Compile-time control flow exceeded {MAX_EXPANDED_ROWS:,} generated statements.")
        return row


def _checked_integer(value, row):
    if not _MIN_INTEGER <= value <= _MAX_INTEGER:
        raise _value_error(row, "Compile-time integers must fit in a signed 64-bit value.")
    return value


class _Expression:
    def __init__(self, source, row):
        self.row = row
        self.tokens = []
        self.index = 0
        position = 0
        source = source.strip()
        while position < len(source):
            if source[position].isspace():
                position += 1
                continue
            token = _TOKEN.match(source, position)
            if not token:
                raise _value_error(row, f'Unexpected token "{source[position]}" in compile-time expression.')
            if token[0] in {"++", "--"}:
                raise _value_error(row, "Use ++ and -- as standalone updates.")
            self.tokens.append(token[0])
            if len(self.tokens) > _MAX_EXPRESSION_TOKENS:
                raise _value_error(row, "Compile-time expression is too long.")
            position = token.end()

    def parse(self):
        result = self.binary(1)
        if self.peek() is not None:
            raise _value_error(self.row, f'Unexpected token "{self.peek()}" in compile-time expression.')
        return result

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def read(self):
        token = self.peek()
        self.index += 1
        return token

    def binary(self, minimum):
        left = self.primary()
        while True:
            op = self.peek()
            precedence = _BINARY_PRECEDENCE.get(op, 0)
            if precedence < minimum:
                return left
            self.read()
            left = (op, left, self.binary(precedence + 1))

    def primary(self):
        token = self.read()
        if token in {"!", "+", "-"}:
            return (token, self.primary())
        if token == "(":
            expression = self.binary(1)
            if self.read() != ")":
                raise _value_error(self.row, "Close the compile-time expression with ).")
            return expression
        if token is None or not (token[0].isalnum() or token.startswith("_")):
            raise _value_error(self.row, "Expected an integer, Boolean, variable, or parenthesized expression.")
        if token.isdecimal():
            digits = token.lstrip("0") or "0"
            if len(digits) > 19:
                raise _value_error(self.row, "Compile-time integers must fit in a signed 64-bit value.")
            return int(digits)
        if token.casefold() in {"true", "false"}:
            return token.casefold() == "true"
        return token


@dataclass
class _Variable:
    type: str
    value: int | bool

    def store(self, value, row):
        if self.type == "int" and type(value) is not int:
            raise _value_error(row, "An int variable requires an integer value.")
        if self.type == "bool" and not isinstance(value, bool):
            raise _value_error(row, "A bool variable requires true or false.")
        self.value = value


def _binary(op, left, right, row):
    if op in {"/", "%"}:
        if not right:
            raise _value_error(row, "Division by zero in compile-time expression.")
        quotient = abs(left) // abs(right)
        if (left < 0) != (right < 0):
            quotient = -quotient
        value = quotient if op == "/" else left - quotient * right
    else:
        value = _BINARY_EVALUATORS[op](left, right)
    return _checked_integer(value, row)


def _evaluate(expression, scope, row):
    if isinstance(expression, int):
        return _checked_integer(expression, row)
    if isinstance(expression, str):
        if expression not in scope:
            raise _value_error(row, f'Unknown compile-time variable "{expression}".')
        return scope[expression].value
    op, first, *remaining = expression
    if op == "-" and not remaining and type(first) is int:
        return _checked_integer(-first, row)
    left = _evaluate(first, scope, row)
    if not remaining:
        if op == "!":
            return not left
        return _checked_integer(+left if op == "+" else -left, row)
    if op == "&&":
        return bool(left) and bool(_evaluate(remaining[0], scope, row))
    if op == "||":
        return bool(left) or bool(_evaluate(remaining[0], scope, row))
    return _binary(op, left, _evaluate(remaining[0], scope, row), row)


@dataclass(frozen=True)
class _Update:
    name: str
    operator: str
    expression: object
    declaration: str | None = None

    def apply(self, scope, row, *, declare_missing=False):
        value = _evaluate(self.expression, scope, row)
        if self.declaration or (declare_missing and self.name not in scope):
            if self.name.casefold() in {"true", "false"}:
                raise _value_error(row, f'"{self.name}" is a reserved Boolean literal.')
            if self.name in scope.maps[0]:
                raise _value_error(row, f'Declare compile-time variable "{self.name}" only once per block.')
            variable = _Variable(self.declaration or "let", value)
            variable.store(value, row)
            scope[self.name] = variable
            return
        if self.name not in scope:
            raise _value_error(row, f'Unknown compile-time variable "{self.name}".')
        variable = scope[self.name]
        if self.operator != "=":
            value = _binary(self.operator[0], variable.value, value, row)
        variable.store(value, row)


def _parse_update(text, row, *, declarations=True):
    declaration = _DECLARATION.fullmatch(text) if declarations else None
    if declaration:
        kind, name, raw = declaration.groups()
        return _Update(name, "=", _Expression(raw, row).parse(), kind.casefold())
    assign = _ASSIGN.fullmatch(text)
    if assign:
        name, op, raw = assign.groups()
        return _Update(name, op, _Expression(raw, row).parse())
    increment = _INCREMENT.fullmatch(text)
    if increment:
        before, first_name, last_name, after = increment.groups()
        return _Update(first_name or last_name, "+=" if (before or after) == "++" else "-=", 1)
    raise _value_error(row, "Use a compile-time variable declaration or assignment.")


@dataclass(frozen=True)
class _Branch:
    row: tuple
    condition: object
    body: list


@dataclass(frozen=True)
class _If:
    branches: list[_Branch]


@dataclass(frozen=True)
class _Loop:
    row: tuple
    condition: object
    body: list
    first: _Update | None = None
    next: _Update | None = None
    after_body: bool = False
    condition_row: tuple | None = None


@dataclass(frozen=True)
class _LoopControl:
    breaks: bool


@dataclass(frozen=True)
class _Electrical:
    start: tuple
    body: list
    end: tuple
    device: bool


class _Blocks:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.next = next(self.rows, None)

    def take(self):
        row = self.next
        self.next = next(self.rows, None)
        return row

    def parse(self):
        statements, _ = self.body(0, 0)
        return statements

    def body(self, depth, loops, opener=None):
        if depth > _MAX_BLOCK_DEPTH:
            raise _value_error(opener, f"Use at most {_MAX_BLOCK_DEPTH} nested compile-time blocks.")
        statements = []
        while self.next:
            row = self.take()
            text = row[0]
            if text == "}":
                if opener is None:
                    raise _value_error(row, "Unexpected } in compile-time statements.")
                return statements, row
            keyword = text.removesuffix(";").rstrip().casefold()
            if loops and keyword in {"break", "continue"}:
                statements.append(_LoopControl(breaks=keyword == "break"))
            elif not text.endswith("{"):
                statements.append(row)
            elif is_control_block(text):
                statements.append(self.control(row, depth + 1, loops))
            elif _DEVICE_BLOCK.match(text):
                fields = []
                while self.next and self.next[0] != "}":
                    fields.append(self.take())
                if self.next is None:
                    raise _value_error(row, "Close the electrical block with }.")
                statements.append(_Electrical(row, fields, self.take(), device=True))
            else:
                body, closer = self.body(depth + 1, loops, row)
                statements.append(_Electrical(row, body, closer, device=False))
        if opener:
            raise _value_error(opener, "Close the compile-time block with }.")
        return statements, None

    def branch(self, row, raw, depth, loops):
        condition = _Expression(raw, row).parse() if raw is not None else None
        body, _ = self.body(depth, loops, row)
        return _Branch(row, condition, body)

    def control(self, row, depth, loops):
        text = row[0]
        header = _CONDITIONAL.fullmatch(text)
        if _DO.fullmatch(text):
            body, _ = self.body(depth, loops + 1, row)
            tail = _DO_TEST.fullmatch(self.next[0]) if self.next else None
            if not tail:
                raise _value_error(self.next or row, 'Finish a do block with "while (condition);".')
            tail_row = self.take()
            return _Loop(row, _Expression(tail[1], tail_row).parse(), body, after_body=True, condition_row=tail_row)
        if _HEADER.match(text)[1].casefold() == "else":
            raise _value_error(row, "Use else or else if immediately after an if block.")
        if not header:
            raise _value_error(row, "Use a braced if, for, while, or do statement.")
        keyword, raw = header.groups()
        if keyword.casefold() == "if":
            branches = [self.branch(row, raw, depth, loops)]
            while self.next and is_control_block(self.next[0]):
                additional = self.next[0]
                if _ELSE.fullmatch(additional):
                    branches.append(self.branch(self.take(), None, depth, loops))
                    break
                conditional = _CONDITIONAL.fullmatch(additional)
                if not conditional or not conditional[1].casefold().startswith("else"):
                    break
                branches.append(self.branch(self.take(), conditional[2], depth, loops))
            return _If(branches)
        first = update = None
        if keyword.casefold() == "for":
            fields = raw.split(";")
            if len(fields) != 3:
                raise _value_error(row, "Use for (initializer; condition; update) { ... }.")
            initial, raw, step = map(str.strip, fields)
            if initial:
                first = _parse_update(initial, row)
                if first.operator != "=":
                    raise _value_error(row, "Initialize a compile-time variable in the for header.")
            update = _parse_update(step, row, declarations=False) if step else None
            raw = raw or "true"
        condition = _Expression(raw, row).parse()
        body, _ = self.body(depth, loops + 1, row)
        return _Loop(row, condition, body, first=first, next=update)


class _LoopSignal(Exception):
    def __init__(self, breaks):
        self.breaks = breaks


def _interpolate(row, scope):
    text, line, binding = row

    def expand(match):
        value = _evaluate(_Expression(match[1], row).parse(), scope, row)
        return str(value).lower()

    text = _INTERPOLATION.sub(expand, text)
    if _INTERPOLATION_START in text:
        raise _value_error(row, "Close the compile-time interpolation with }.")
    return text, line, binding


def _emit_loop(loop, scope, budget):
    local = scope.new_child()
    if loop.first:
        loop.first.apply(local, loop.row, declare_missing=True)
    condition_row = loop.condition_row or loop.row
    first_iteration = True
    while (first_iteration and loop.after_body) or _evaluate(loop.condition, local, condition_row):
        first_iteration = False
        budget.iteration(loop.row)
        try:
            yield from _emit(loop.body, local.new_child(), budget)
        except _LoopSignal as signal:
            if signal.breaks:
                break
        if loop.next:
            loop.next.apply(local, loop.row)


def _emit(statements, scope, budget):
    for statement in statements:
        if isinstance(statement, _If):
            for branch in statement.branches:
                if branch.condition is None or _evaluate(branch.condition, scope, branch.row):
                    yield from _emit(branch.body, scope.new_child(), budget)
                    break
        elif isinstance(statement, _Loop):
            yield from _emit_loop(statement, scope, budget)
        elif isinstance(statement, _LoopControl):
            raise _LoopSignal(statement.breaks)
        elif isinstance(statement, _Electrical):
            yield budget.emitted(_interpolate(statement.start, scope))
            if statement.device:
                for row in statement.body:
                    yield budget.emitted(_interpolate(row, scope))
            else:
                try:
                    yield from _emit(statement.body, scope.new_child(), budget)
                except _LoopSignal:
                    yield budget.emitted(statement.end)
                    raise
            yield budget.emitted(statement.end)
        else:
            text = statement[0].removesuffix(";").rstrip()
            assignment = _ASSIGN.fullmatch(text)
            if (
                _DECLARATION_START.match(text)
                or _INCREMENT.fullmatch(text)
                or _MUTATION.match(text)
                or (assignment and assignment[1] in scope)
            ):
                _parse_update(text, statement).apply(scope, statement)
            else:
                yield budget.emitted(_interpolate(statement, scope))


def expand_control_flow(rows, *, budget=None):
    """Expand flow in a method invocation and preserve source locations."""
    rows = list(rows)
    if not any(is_control_statement(row[0]) for row in rows):
        yield from rows
        return
    yield from _emit(_Blocks(rows).parse(), ChainMap(), budget or ExpansionBudget())
