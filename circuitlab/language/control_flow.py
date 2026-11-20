"""Elaborate static integer and Boolean control flow into electrical source rows."""
# Electrical loops and conditions execute during circuit generation, not during simulation.
# Only the supported integer/Boolean expression grammar is evaluated, and expansion budgets
# bound both iteration count and emitted statements to catch nonterminating input.

import operator
import re
from collections import ChainMap
from dataclasses import dataclass

from circuitlab.model.graph import CircuitError

# Recognize generation-time variables and updates separately from ordinary
# electrical rows. The emitter decides whether a plain name=value assignment
# refers to a declared static variable or should pass to the circuit builder.
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_DECLARATION = re.compile(rf"(let|int|bool)\s+({_NAME})\s*=\s*(.+)", re.I)
_DECLARATION_START = re.compile(rf"(?:let|int|bool)\s+{_NAME}\s*=", re.I)
# The negative lookahead on plain = prevents == from being mistaken for
# assignment. Increment/decrement have separate standalone-update syntax.
_ASSIGN = re.compile(rf"({_NAME})\s*(\+=|-=|\*=|/=|%=|=(?!=))\s*(.+)")
_INCREMENT = re.compile(rf"(?:(\+\+|--)\s*({_NAME})|({_NAME})\s*(\+\+|--))")
_MUTATION = re.compile(rf"({_NAME})\s*(?:[+\-*/%]=|\+\+|--)")
# A control keyword must end at a real word boundary such as whitespace or
# an opening parenthesis/brace. Names such as else_count remain ordinary
# identifiers; is_control_block() additionally requires an opening brace.
_HEADER = re.compile(r"(if|else|for|while|do)(?=[\s({]|$)", re.I)
_CONDITIONAL = re.compile(r"(if|else\s+if|for|while)\s*\((.*)\)\s*\{", re.I)
_DO = re.compile(r"do\s*\{", re.I)
_DO_TEST = re.compile(r"while\s*\((.*)\)\s*;", re.I)
_ELSE = re.compile(r"else\s*\{", re.I)
_DEVICE_BLOCK = re.compile(r"(?:(?:battery|transistor|opamp)\s|(?:analog|digital)\s*(?::\s*math)?\s*[\s{])", re.I)
_INTERPOLATION_START = "${"
_INTERPOLATION = re.compile(r"\$\{([^{}]*)\}")
# Expressions contain only decimal integers, identifiers, supported
# operators, and parentheses. There are no function calls, attributes,
# electrical unit suffixes, or evaluation through Python source text.
_TOKEN = re.compile(rf"[0-9]+|{_NAME}|\|\||&&|==|!=|<=|>=|\+\+|--|[+\-*/%!<>()]")

# Higher numbers bind more tightly. Logical OR/AND are evaluated specially
# for short-circuit behavior; division/remainder need explicit signed
# integer semantics rather than Python's default / and % behavior.
_BINARY_PRECEDENCE = {
    "||": 1, "&&": 2, "==": 3, "!=": 3, "<": 4, "<=": 4, ">": 4, ">=": 4,
    "+": 5, "-": 5, "*": 6, "/": 6, "%": 6,
}
_BINARY_EVALUATORS = {
    "==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le,
    ">": operator.gt, ">=": operator.ge, "+": operator.add, "-": operator.sub,
    "*": operator.mul,
}
# These bounds apply to the static expression arithmetic, not to hardware
# register widths. Token/block limits bound parsing; separate iteration
# and output budgets bound how much circuit text execution can generate.
_MIN_INTEGER = -(1 << 63)
_MAX_INTEGER = (1 << 63) - 1
_MAX_EXPRESSION_TOKENS = 256
_MAX_BLOCK_DEPTH = 64
MAX_ITERATIONS = 10_000
MAX_EXPANDED_ROWS = 100_000


def expand_digital_loops(rows):
    """Unroll bounded for loops, leaving signal-dependent if/clock behavior intact."""
    # This entry point uses (text, line) digital rows. A temporary Binding lets
    # it reuse the static expression/update helpers; returned rows keep their
    # original line numbers for the later HDL validators.
    from circuitlab.language.syntax import Binding
    # If there is no digital for header, leave the body untouched. In
    # particular, do not evaluate signal-dependent if/clock behavior here.
    if not any(re.match(r"for\s*\(", text) for text, _ in rows):
        return rows
    budget = ExpansionBudget()
    # Make simple decimal const declarations available as static bounds.
    # Remove digit separators and limit numeric text conversion; other HDL
    # declarations still belong to their normal parsing/width-validation pass.
    constants = {}
    for text, line in rows:
        match = re.fullmatch(r"const\s+(\w+)(?:\s*\[[^]]+\])?\s*=\s*([0-9_]+)", text)
        if match and len(match[2].replace("_", "").lstrip("0")) <= 19:
            constants[match[1]] = _Variable("int", int(match[2].replace("_", "")))

    # Only for blocks are removed by this pass. Other headers/rows, including
    # clocked and signal-dependent conditions, remain for HDL lowering.
    # variables records active loop indexes that need whole-word substitution.
    def expand(block, scope, variables=(), depth=0):
        if depth > 64:
            raise CircuitError("Digital loops may nest at most 64 levels.")
        output, index = [], 0
        while index < len(block):
            text, line = block[index]
            row = text, line, Binding()
            index += 1
            match = re.fullmatch(r"for\s*\((.*)\)\s*\{", text)
            if match:
                pieces = match[1].split(";")
                if len(pieces) != 3:
                    raise CircuitError("Use for (i = 0; i < count; i += 1) { ... }.", line)
                first = _parse_update(pieces[0].strip(), row)
                update = _parse_update(pieces[2].strip(), row, declarations=False)
                # The digital loop header must update the same index it initializes.
                # Bounds must be evaluable from this static scope, not live signal values.
                if first.name != update.name:
                    raise CircuitError("Update the same loop variable declared by for.", line)
                # Use a loop-local declaration map layered over enclosing indexes/constants.
                # A missing initializer target can be declared implicitly; explicit
                # declarations can shadow, while assignments can update an existing record.
                local = scope.new_child()
                first.apply(local, row, declare_missing=True)
                condition = _Expression(pieces[1], row).parse()
                # Find the matching closing brace while counting all nested structural
                # blocks, not just nested for loops. The copied body excludes the for braces.
                start, nesting = index, 1
                while index < len(block) and nesting:
                    nesting += int(block[index][0].endswith("{")) - int(block[index][0] == "}")
                    index += 1
                if nesting:
                    raise CircuitError("Close the for loop with }.", line)
                body = block[start:index - 1]
                # Track the nearest for/switch owner of each break. A switch-owned break
                # must survive for later digital switch lowering; a for-owned break and
                # any continue are rejected by this restricted unroller.
                owners = ["for"]
                for nested, number in body:
                    if nested == "continue" or (nested == "break" and
                            next(owner for owner in reversed(owners) if owner in {"for", "switch"}) == "for"):
                        raise CircuitError("Use static for bounds instead of break/continue in chip loop bodies.", number)
                    if nested.endswith("{"):
                        owners.append("switch" if re.match(r"switch\s*\(", nested) else
                                      "for" if re.match(r"for\s*\(", nested) else "block")
                    elif nested == "}":
                        owners.pop()
                # Re-evaluate the static bound after each index update, sharing one budget
                # through nested expansion. Even an empty nonterminating body consumes
                # iterations and eventually reports an error at the loop header.
                while _evaluate(condition, local, row):
                    budget.iteration(row)
                    output.extend(expand(body, local, (*variables, first.name), depth + 1))
                    update.apply(local, row)
                continue
            # Replace complete index identifiers with their current decimal values;
            # do not replace a matching substring inside a longer signal name.
            for variable in variables:
                text = re.sub(rf"\b{re.escape(variable)}\b", str(scope[variable].value), text)
            if variables:
                # Try folding arithmetic in selector bounds after index substitution,
                # including both sides of a slice. If it is not statically evaluable,
                # retain the original selector for the normal validator's diagnostic.
                def index_value(match):
                    try:
                        values = [str(_evaluate(_Expression(value, row).parse(), scope, row))
                                  for value in match[1].split(":")]
                    except CircuitError:
                        return match[0]  # The normal selector validator reports dynamic indices.
                    return "[" + ":".join(values) + "]"
                text = re.sub(r"\[([^]]+)\]", index_value, text)
            # Count each forwarded row, including structural headers/closers, while
            # retaining its original source line in the returned digital body.
            budget.emitted(row)
            output.append((text, line))
        return output

    return expand(rows, ChainMap(constants))


# Requiring { avoids reserving a legacy method call solely because its
# name resembles a loop/control keyword.
def is_control_block(text):
    """Recognize a braced control header without reserving bare method calls."""
    return text.endswith("{") and bool(_HEADER.match(text))


# Declarations, mutations, braced controls, and ${...} interpolation opt
# a body into static expansion. A plain assignment alone is ambiguous
# with electrical quantities, so it is classified later using scope.
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


# Electrical SourceRows are (text, original_line, binding). Preserve that
# binding's file origin in errors instead of referring to an unrolled row index.
def _value_error(row, message):
    _, line, origin = row
    return CircuitError(message, line, path=origin.path)


@dataclass
class ExpansionBudget:
    """Bound the work performed across all control-flow blocks in a compilation."""

    # Counters are cumulative for this budget object, including nested loops
    # and any separate method invocations that the caller gives the same budget.
    iterations: int = 0
    output_rows: int = 0

    # Charge for every executed loop body, even if it produces no components.
    # This catches while(true) {} as well as loops that emit too much text.
    def iteration(self, row):
        self.iterations += 1
        if self.iterations > MAX_ITERATIONS:
            raise _value_error(row, f"Compile-time control flow exceeded {MAX_ITERATIONS:,} loop iterations.")

    # Count generated source rows, not just components. Returning the row
    # unchanged makes this usable directly in a generator's yield expression.
    def emitted(self, row):
        self.output_rows += 1
        if self.output_rows > MAX_EXPANDED_ROWS:
            raise _value_error(row, f"Compile-time control flow exceeded {MAX_EXPANDED_ROWS:,} generated statements.")
        return row


# Check numeric range after literal evaluation and arithmetic. Return the
# value unchanged, so Boolean comparison results retain their bool type
# (Python bool is an int subclass) instead of becoming 0/1 integers.
def _checked_integer(value, row):
    if not _MIN_INTEGER <= value <= _MAX_INTEGER:
        raise _value_error(row, "Compile-time integers must fit in a signed 64-bit value.")
    return value


# Parse to a small expression tree before evaluation. Integer/Boolean
# literals are leaf values, variable names are string leaves, and tuples
# encode unary/binary operations. Parsing does not read variable values.
class _Expression:
    def __init__(self, source, row):
        self.row = row
        self.tokens = []
        self.index = 0
        position = 0
        source = source.strip()
        # Match tokens exactly at the current position. Unsupported characters
        # cannot be skipped by searching forward; whitespace is the only ignored text.
        while position < len(source):
            if source[position].isspace():
                position += 1
                continue
            token = _TOKEN.match(source, position)
            if not token:
                raise _value_error(row, f'Unexpected token "{source[position]}" in compile-time expression.')
            # Updates have statement semantics only; x++ cannot be embedded in a
            # condition or interpolation with an ambiguous before/after value.
            if token[0] in {"++", "--"}:
                raise _value_error(row, "Use ++ and -- as standalone updates.")
            self.tokens.append(token[0])
            if len(self.tokens) > _MAX_EXPRESSION_TOKENS:
                raise _value_error(row, "Compile-time expression is too long.")
            position = token.end()

    # Parse only the supported expression grammar; user text is never passed to Python eval.
    def parse(self):
        result = self.binary(1)
        # Require complete consumption: a valid prefix must not hide trailing
        # tokens such as an unsupported call or a second adjacent operand.
        if self.peek() is not None:
            raise _value_error(self.row, f'Unexpected token "{self.peek()}" in compile-time expression.')
        return result

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    # Advance the lookahead cursor even at EOF; primary()/parenthesis checks
    # turn a missing operand or closer into the appropriate source error.
    def read(self):
        token = self.peek()
        self.index += 1
        return token

    # Precedence climbing: parse the right side above the current precedence.
    # Equal-precedence operators therefore group left-to-right, as in
    # 8-3-1 == (8-3)-1, while multiplication binds inside an addition.
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
        # Unary operators bind to the next primary before any binary operation.
        # Parentheses restart binary parsing at the lowest precedence.
        if token in {"!", "+", "-"}:
            return (token, self.primary())
        if token == "(":
            expression = self.binary(1)
            if self.read() != ")":
                raise _value_error(self.row, "Close the compile-time expression with ).")
            return expression
        if token is None or not (token[0].isalnum() or token.startswith("_")):
            raise _value_error(self.row, "Expected an integer, Boolean, variable, or parenthesized expression.")
        # Strip leading zeros and bound conversion length before int(). Exact
        # signed-range checking happens during evaluation, allowing the positive
        # magnitude 2**63 to appear specifically beneath unary minus for -2**63.
        if token.isdecimal():
            digits = token.lstrip("0") or "0"
            if len(digits) > 19:
                raise _value_error(self.row, "Compile-time integers must fit in a signed 64-bit value.")
            return int(digits)
        if token.casefold() in {"true", "false"}:
            return token.casefold() == "true"
        return token


# Scope entries are mutable records. Child ChainMaps add declaration maps
# but share ancestor records, so assigning an existing outer variable
# updates that variable instead of creating an accidental local copy.
@dataclass
class _Variable:
    type: str
    value: int | bool

    # Preserve the declared integer/Boolean type when compile-time variables are updated.
    def store(self, value, row):
        # int and bool declarations keep strict types: bool is intentionally
        # excluded from int despite Python's subclass relationship. let imposes
        # neither of these restrictions and can hold either expression result.
        if self.type == "int" and type(value) is not int:
            raise _value_error(row, "An int variable requires an integer value.")
        if self.type == "bool" and not isinstance(value, bool):
            raise _value_error(row, "A bool variable requires true or false.")
        self.value = value


# Evaluate eager binary operators, checking every result against the
# static integer range. Logical &&/|| are handled by _evaluate instead
# so their right-hand expression is not evaluated unnecessarily.
def _binary(op, left, right, row):
    if op in {"/", "%"}:
        if not right:
            raise _value_error(row, "Division by zero in compile-time expression.")
        # Compute quotient with integer arithmetic, then apply its sign to
        # truncate toward zero. Remainder follows left=quotient*right+remainder:
        # for example -7/3=-2 and -7%3=-1, unlike Python floor division/modulo.
        quotient = abs(left) // abs(right)
        if (left < 0) != (right < 0):
            quotient = -quotient
        value = quotient if op == "/" else left - quotient * right
    else:
        value = _BINARY_EVALUATORS[op](left, right)
    return _checked_integer(value, row)


def _evaluate(expression, scope, row):
    # This includes bool literals while preserving their Boolean type.
    # Variables are looked up from static records, never from circuit voltages
    # or live HDL state.
    if isinstance(expression, int):
        return _checked_integer(expression, row)
    if isinstance(expression, str):
        if expression not in scope:
            raise _value_error(row, f'Unknown compile-time variable "{expression}".')
        return scope[expression].value
    op, first, *remaining = expression
    # Negate a raw integer literal before checking its positive magnitude.
    # This admits the minimum signed value -9223372036854775808 while still
    # rejecting that magnitude as an ordinary positive static literal.
    if op == "-" and not remaining and type(first) is int:
        return _checked_integer(-first, row)
    left = _evaluate(first, scope, row)
    if not remaining:
        if op == "!":
            return not left
        return _checked_integer(+left if op == "+" else -left, row)
    # Use Python's short-circuit and/or on explicit Boolean conversions.
    # A skipped RHS can contain an otherwise invalid division or unknown name
    # without error because it is not needed for this condition.
    if op == "&&":
        return bool(left) and bool(_evaluate(remaining[0], scope, row))
    if op == "||":
        return bool(left) or bool(_evaluate(remaining[0], scope, row))
    return _binary(op, left, _evaluate(remaining[0], scope, row), row)


# An immutable parsed action, separate from the mutable variable it updates.
# declaration is int/bool/let for a declaration and None for an assignment.
@dataclass(frozen=True)
class _Update:
    name: str
    operator: str
    expression: object
    declaration: str | None = None

    def apply(self, scope, row, *, declare_missing=False):
        # Evaluate before installing a new local declaration, so a shadowing
        # initializer can still read an outer variable of the same name.
        value = _evaluate(self.expression, scope, row)
        # Declarations occupy only the current ChainMap layer. A for initializer
        # may also create a missing target implicitly; it does not redeclare an
        # already visible target unless an explicit declaration was written.
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
        # Lookup returns the existing record from any enclosing map. Compound
        # updates reuse the checked integer operator, then store through that
        # record's type rule rather than replacing its declared type.
        variable = scope[self.name]
        if self.operator != "=":
            value = _binary(self.operator[0], variable.value, value, row)
        variable.store(value, row)


# Normalize declarations, assignments, compound operators, and both
# prefix/postfix increments into one action. The two increment forms
# are equivalent here because updates do not yield an expression value.
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


# A branch keeps its condition's source row for evaluation errors.
# condition=None represents the unconditional final else branch.
@dataclass(frozen=True)
class _Branch:
    row: tuple
    condition: object
    body: list


# Preserve branch priority: emission chooses the first true branch only.
@dataclass(frozen=True)
class _If:
    branches: list[_Branch]


# Normalize for/while/do loops into one record. first/next are optional
# for actions; after_body selects do-while timing. A separate condition_row
# points do-while errors at the trailing while rather than the opening do.
@dataclass(frozen=True)
class _Loop:
    row: tuple
    condition: object
    body: list
    first: _Update | None = None
    next: _Update | None = None
    after_body: bool = False
    condition_row: tuple | None = None


# Represent a parsed break/continue inside a loop. Emission turns it into
# an internal unwind signal caught by the nearest executing loop.
@dataclass(frozen=True)
class _LoopControl:
    breaks: bool


# Retain structural electrical braces around expanded contents. Device
# blocks (battery/transistor) instead carry raw field rows, which receive
# interpolation but are not interpreted as static assignments.
@dataclass(frozen=True)
class _Electrical:
    start: tuple
    body: list
    end: tuple
    device: bool


# One-row lookahead parser for the outer statement structure. All control
# headers/braces are parsed before execution, while electrical rows stay
# available for the downstream graph builder rather than being solved here.
class _Blocks:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.next = next(self.rows, None)

    # Consume the buffered row and fetch the next. This lets an if collect
    # immediately following else/else-if blocks and a do consume its tail.
    def take(self):
        row = self.next
        self.next = next(self.rows, None)
        return row

    def parse(self):
        statements, _ = self.body(0, 0)
        return statements

    # depth bounds nested parsed blocks; loops counts enclosing loop contexts.
    # Return the closing row with the statements so electrical wrappers can
    # later be emitted with their original source identity.
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
            # Only reserve break/continue inside a loop. Outside loops these words
            # can remain legacy bare wire/tap names and pass to the electrical parser.
            if loops and keyword in {"break", "continue"}:
                statements.append(_LoopControl(breaks=keyword == "break"))
            elif not text.endswith("{"):
                statements.append(row)
            elif is_control_block(text):
                statements.append(self.control(row, depth + 1, loops))
            # Device field assignments such as base=... or V=... are electrical data.
            # Collect them opaquely to their closing brace instead of consuming them
            # as declarations/updates in the surrounding static scope.
            elif _DEVICE_BLOCK.match(text):
                fields, field_depth = [], 0
                # Analog properties blocks are nested electrical data too.
                # Their close must not end the surrounding model or expose
                # following local assignments to the compile-time scope.
                while self.next:
                    if self.next[0] == "}":
                        if not field_depth:
                            break
                        field_depth -= 1
                    elif self.next[0].endswith("{"):
                        field_depth += 1
                    fields.append(self.take())
                if self.next is None:
                    raise _value_error(row, "Close the electrical block with }.")
                statements.append(_Electrical(row, fields, self.take(), device=True))
            else:
                # Other braced electrical structures retain their wrapper but may contain
                # nested static flow. Their contents get a lexical scope when emitted.
                body, closer = self.body(depth + 1, loops, row)
                statements.append(_Electrical(row, body, closer, device=False))
        if opener:
            raise _value_error(opener, "Close the compile-time block with }.")
        return statements, None

    # Parse branch syntax now; defer looking up variables and choosing a path
    # until emission. Even unselected branches must have valid control structure.
    def branch(self, row, raw, depth, loops):
        condition = _Expression(raw, row).parse() if raw is not None else None
        body, _ = self.body(depth, loops, row)
        return _Branch(row, condition, body)

    def control(self, row, depth, loops):
        text = row[0]
        header = _CONDITIONAL.fullmatch(text)
        # A do body executes at least once and requires an immediately following
        # while(condition); row. Preserve that tail's location for condition errors.
        if _DO.fullmatch(text):
            body, _ = self.body(depth, loops + 1, row)
            tail = _DO_TEST.fullmatch(self.next[0]) if self.next else None
            if not tail:
                raise _value_error(self.next or row, 'Finish a do block with "while (condition);".')
            tail_row = self.take()
            return _Loop(row, _Expression(tail[1], tail_row).parse(), body, after_body=True, condition_row=tail_row)
        # An else encountered as a new control statement has no adjacent owning
        # if. Valid alternatives are consumed by the if-chain parser below.
        if _HEADER.match(text)[1].casefold() == "else":
            raise _value_error(row, "Use else or else if immediately after an if block.")
        if not header:
            raise _value_error(row, "Use a braced if, for, while, or do statement.")
        keyword, raw = header.groups()
        # Collect an ordered if/else-if chain, optionally ending with else.
        # Do not evaluate conditions while parsing: scope values can change between
        # loop iterations that execute this same parsed tree.
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
        # A for header separates initializer, condition, and update. Empty fields
        # are permitted; an empty condition means true, so for(;;) relies on a
        # body break or eventually reaches the shared iteration budget.
        if keyword.casefold() == "for":
            fields = raw.split(";")
            if len(fields) != 3:
                raise _value_error(row, "Use for (initializer; condition; update) { ... }.")
            initial, raw, step = map(str.strip, fields)
            if initial:
                first = _parse_update(initial, row)
                # The electrical for initializer must initialize/assign, not perform a
                # compound mutation before its loop variable has been established.
                if first.operator != "=":
                    raise _value_error(row, "Initialize a compile-time variable in the for header.")
            update = _parse_update(step, row, declarations=False) if step else None
            raw = raw or "true"
        condition = _Expression(raw, row).parse()
        body, _ = self.body(depth, loops + 1, row)
        return _Loop(row, condition, body, first=first, next=update)


# Internal control transfer, not a circuit error. Propagation through
# nested branch/electrical emitters skips the remainder of the body;
# the closest _emit_loop decides whether to break or continue.
class _LoopSignal(Exception):
    def __init__(self, breaks):
        self.breaks = breaks


# Substitute generated names using the current static scope while preserving the row origin.
def _interpolate(row, scope):
    text, line, binding = row

    # Evaluate only the explicit ${expression} placeholder. Lowercase output
    # renders Boolean literals as true/false; integer values use decimal text.
    def expand(match):
        value = _evaluate(_Expression(match[1], row).parse(), scope, row)
        return str(value).lower()

    text = _INTERPOLATION.sub(expand, text)
    # The supported placeholders are not nested. A remaining ${ after the
    # single substitution pass indicates an unfinished/unsupported placeholder
    # and must not leak into generated component or net names.
    if _INTERPOLATION_START in text:
        raise _value_error(row, "Close the compile-time interpolation with }.")
    # Every generated variant retains the template's original line and binding,
    # so errors and schematic edits still identify the source template row.
    return text, line, binding


# Share one expansion budget across nested loops so nesting cannot evade the work limits.
def _emit_loop(loop, scope, budget):
    # Keep header variables for the life of this loop. Each body execution
    # gets another fresh child map below, allowing body-local declarations to
    # repeat each iteration while updates to outer records remain visible.
    local = scope.new_child()
    if loop.first:
        loop.first.apply(local, loop.row, declare_missing=True)
    condition_row = loop.condition_row or loop.row
    first_iteration = True
    # do-while skips the first test; other loops test before entering. Subsequent
    # tests use updated static values and the correct header/tail source row.
    while (first_iteration and loop.after_body) or _evaluate(loop.condition, local, condition_row):
        first_iteration = False
        budget.iteration(loop.row)
        try:
            yield from _emit(loop.body, local.new_child(), budget)
        # break exits before the for update; continue falls through to that update
        # and then retests the condition. Both unwind any nested if/structural body
        # before reaching this nearest loop handler.
        except _LoopSignal as signal:
            if signal.breaks:
                break
        if loop.next:
            loop.next.apply(local, loop.row)


def _emit(statements, scope, budget):
    for statement in statements:
        # Execute only the first selected branch in a fresh lexical map. Its
        # declarations are local, but assignments to existing enclosing records
        # persist through ChainMap lookup.
        if isinstance(statement, _If):
            for branch in statement.branches:
                if branch.condition is None or _evaluate(branch.condition, scope, branch.row):
                    yield from _emit(branch.body, scope.new_child(), budget)
                    break
        elif isinstance(statement, _Loop):
            yield from _emit_loop(statement, scope, budget)
        elif isinstance(statement, _LoopControl):
            raise _LoopSignal(statement.breaks)
        # Control wrappers disappear, but an electrical block must keep its opening
        # and closing rows. Count them in the expansion budget along with its contents.
        elif isinstance(statement, _Electrical):
            yield budget.emitted(_interpolate(statement.start, scope))
            # Interpolate raw device fields only. A field name matching a static variable
            # must not accidentally update that variable instead of reaching the builder.
            if statement.device:
                for row in statement.body:
                    yield budget.emitted(_interpolate(row, scope))
            else:
                try:
                    yield from _emit(statement.body, scope.new_child(), budget)
                # If break/continue escapes this structural block, emit its closing row
                # before propagating the signal. Otherwise a partially emitted series/
                # parallel block would leave the generated electrical syntax unbalanced.
                except _LoopSignal:
                    yield budget.emitted(statement.end)
                    raise
            yield budget.emitted(statement.end)
        else:
            text = statement[0].removesuffix(";").rstrip()
            assignment = _ASSIGN.fullmatch(text)
            # Explicit declarations/mutations are static actions. A plain assignment
            # is static only when its target already exists in scope; otherwise keep
            # it as electrical source, quantity, or component text for downstream parsing.
            if (
                _DECLARATION_START.match(text)
                or _INCREMENT.fullmatch(text)
                or _MUTATION.match(text)
                or (assignment and assignment[1] in scope)
            ):
                _parse_update(text, statement).apply(scope, statement)
            else:
                yield budget.emitted(_interpolate(statement, scope))


# Each method call starts with its own variable scope, preventing one invocation from
# leaking compile-time variable values into another instance.
def expand_control_flow(rows, *, budget=None):
    """Expand flow in a method invocation and preserve source locations."""
    # Materialize once for opt-in detection. The fast path leaves ordinary
    # legacy rows unchanged when no static feature is present.
    rows = list(rows)
    if not any(is_control_statement(row[0]) for row in rows):
        yield from rows
        return
    # Each invocation starts with a fresh variable environment. A supplied
    # budget may span many method calls, so independent variable scopes cannot
    # bypass the total work limit by resetting the counters.
    yield from _emit(_Blocks(rows).parse(), ChainMap(), budget or ExpansionBudget())
