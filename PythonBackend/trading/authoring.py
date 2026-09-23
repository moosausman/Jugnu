"""
authoring.py -- "buy BTC when RSI drops under 30, sell above 60" becomes a
real strategy file you can read, edit and backtest.

The model writes Python. That is only safe because nothing it writes is
trusted: every generated strategy is parsed and walked before it runs, and
anything outside a tiny allowed language is rejected with the reason. No
imports, no attribute access beyond the context object, no file or network
names, no unbounded loops, no dunders. Then it executes with a stripped
__builtins__ containing exactly the functions a strategy needs.

So the worst a bad generation can do is fail to parse, or return a silly
number. It cannot touch the machine.

Strategies land in `custom/` as ordinary files. They survive restarts, they
work in the CLI, the window and the tools, and you can open one and change
a number without asking anyone.
"""
from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

from . import strategies
from .strategies import Spec, rsi, sma, stdev

CUSTOM_DIR = Path(__file__).parent / "custom"

# The whole language a strategy may use.
ALLOWED_CALLS = {"sma", "rsi", "stdev", "max", "min", "abs", "len", "sum", "round", "float", "int",
                 "range", "sorted", "any", "all", "enumerate", "zip"}
ALLOWED_CTX = {"closes", "bars", "bar", "i", "position", "cash", "equity", "params", "param",
               "entry_price"}
ALLOWED_BAR = {"open", "high", "low", "close", "volume", "ts", "date", "stamp"}
ALLOWED_ATTRS = ALLOWED_CTX | ALLOWED_BAR

TEMPLATE = '''"""{description}

Written by Jugnu from a description, then checked before it ran.
Edit it freely -- it's an ordinary strategy file.
"""

DEFAULTS = {defaults}


def strategy(ctx):
{body}
'''

_PROMPT = """You write one trading strategy as a Python function. Nothing else.

The function signature is exactly:

    def strategy(ctx):

It returns a target weight: 0.0 means hold cash, 1.0 means fully invested,
and values between are partial positions. It is called once per bar.

What ctx gives you:
    ctx.closes      every close up to and including this bar (a sequence)
    ctx.bars        the bars themselves; ctx.bars[-1].high, .low, .open, .close, .volume
    ctx.bar         this bar
    ctx.i           index of this bar
    ctx.position    shares currently held (0 when in cash)
    ctx.entry_price average price paid for what you hold (0.0 when in cash) --
                    use it for stops: ctx.entry_price and price < ctx.entry_price * 0.98
    ctx.param(name, default)   a tunable parameter

Helpers available, already imported: sma(values, length), rsi(values, length),
stdev(values). Python builtins available: max, min, abs, len, sum, round, float,
int, range, sorted, any, all, enumerate, zip.

HARD RULES -- breaking any of these means your answer is discarded:
- no import statements of any kind
- no other function or attribute than the ones listed above
- no while loops, no classes, no lambdas assigned to names, no dunder names
- guard against short history: return 0.0 when there aren't enough bars
- every tunable number must come from ctx.param("name", default), not be hardcoded
- the strategy cannot look at bars after ctx.i -- it physically can't, so don't try

Answer with exactly two things, in this order, and nothing else:
1. a line "NAME: <one lowercase word>"
2. a fenced python code block containing only the function body (no def line),
   indented by four spaces

Description to implement: {description}
"""


class StrategyError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------

class _Inspector(ast.NodeVisitor):
    def __init__(self):
        self.problems: list[str] = []
        self.params: dict[str, float] = {}

    def fail(self, node, message: str) -> None:
        self.problems.append(f"line {getattr(node, 'lineno', '?')}: {message}")

    def visit_Import(self, node):           # noqa: N802
        self.fail(node, "imports aren't allowed in a strategy")

    def visit_ImportFrom(self, node):       # noqa: N802
        self.fail(node, "imports aren't allowed in a strategy")

    def visit_While(self, node):            # noqa: N802
        self.fail(node, "while loops aren't allowed (a strategy must always finish)")

    def visit_ClassDef(self, node):         # noqa: N802
        self.fail(node, "a strategy is one function, not a class")

    def visit_Attribute(self, node):        # noqa: N802
        if node.attr.startswith("_"):
            self.fail(node, f"'{node.attr}' isn't available to a strategy")
        elif node.attr not in ALLOWED_ATTRS:
            self.fail(node, f"'.{node.attr}' isn't available; you can use "
                            f"{', '.join(sorted(ALLOWED_CTX))}")
        self.generic_visit(node)

    def visit_Name(self, node):             # noqa: N802
        if node.id.startswith("__"):
            self.fail(node, f"'{node.id}' isn't allowed")
        self.generic_visit(node)

    def visit_Call(self, node):             # noqa: N802
        func = node.func
        if isinstance(func, ast.Name):
            if func.id not in ALLOWED_CALLS:
                self.fail(node, f"'{func.id}()' isn't available to a strategy")
        elif isinstance(func, ast.Attribute):
            if func.attr == "param":
                self._record_param(node)
            elif func.attr not in ALLOWED_ATTRS:
                self.fail(node, f"'.{func.attr}()' isn't available to a strategy")
        else:
            self.fail(node, "only plain function calls are allowed")
        self.generic_visit(node)

    def _record_param(self, node) -> None:
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            name = node.args[0].value
            default = 0.0
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                value = node.args[1].value
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    default = value
            self.params.setdefault(name, default)


def check(source: str) -> dict:
    """Parses and walks a generated strategy. Raises StrategyError with
    every problem, so a retry can fix them all at once."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise StrategyError(f"that isn't valid Python: {e.msg} (line {e.lineno})") from e
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if not any(f.name == "strategy" for f in functions):
        raise StrategyError("no function called 'strategy' in there")
    inspector = _Inspector()
    inspector.visit(tree)
    if inspector.problems:
        raise StrategyError("; ".join(inspector.problems[:6]))
    return inspector.params


def compile_strategy(source: str):
    """Runs the (already checked) module in a namespace with nothing in it
    but the helpers, and hands back the function."""
    namespace = {
        "__builtins__": {"max": max, "min": min, "abs": abs, "len": len, "sum": sum,
                         "round": round, "float": float, "int": int, "range": range,
                         "sorted": sorted, "any": any, "all": all, "enumerate": enumerate,
                         "zip": zip, "True": True, "False": False, "None": None},
        "sma": sma, "rsi": rsi, "stdev": stdev,
    }
    exec(compile(source, "<strategy>", "exec"), namespace)     # noqa: S102 -- checked above
    function = namespace.get("strategy")
    if not callable(function):
        raise StrategyError("no callable 'strategy' after compiling")
    return function, namespace.get("DEFAULTS", {})


# ---------------------------------------------------------------------------
# Writing one from a description
# ---------------------------------------------------------------------------

def _parse_reply(text: str) -> tuple[str, str]:
    name_match = re.search(r"NAME:\s*([A-Za-z][A-Za-z0-9_]*)", text)
    name = (name_match.group(1) if name_match else "custom").lower()[:20]
    blocks = re.findall(r"```(?:python)?\n(.*?)```", text, re.S)
    body = (blocks[0] if blocks else text.split("NAME:")[-1]).strip("\n")
    body = textwrap.dedent(body)
    if body.lstrip().startswith("def strategy"):
        body = "\n".join(body.split("\n")[1:])
        body = textwrap.dedent(body)
    body = textwrap.indent(textwrap.dedent(body), "    ")
    return name, body


def write(description: str, *, attempts: int = 2, on_status=None) -> Spec:
    """Description in, registered strategy out. Retries once with the
    checker's complaints, which is usually all it takes."""
    import llm_client

    problems = ""
    last_error = ""
    for attempt in range(attempts):
        if on_status:
            on_status("writing the strategy…" if attempt == 0 else "fixing it…")
        prompt = _PROMPT.format(description=description)
        if problems:
            prompt += f"\n\nYour last attempt was rejected: {problems}\nWrite it again, fixed."
        response = llm_client.chat(
            [{"role": "system", "content": "You write small, exact Python. No commentary."},
             {"role": "user", "content": prompt}], role="AGENT")
        name, body = _parse_reply(response["message"].content or "")
        if not body.strip():
            problems = last_error = "the reply had no code in it"
            continue
        source = TEMPLATE.format(description=description.replace('"""', "'''"),
                                 defaults="{}", body=body)
        try:
            params = check(source)
        except StrategyError as e:
            problems = last_error = str(e)
            continue
        source = TEMPLATE.format(description=description.replace('"""', "'''"),
                                 defaults=repr(params), body=body)
        try:
            function, defaults = compile_strategy(source)
        except Exception as e:
            problems = last_error = f"it didn't run: {e}"
            continue
        return install(name, source, function, defaults or params, description)
    raise StrategyError(last_error or "couldn't write a working strategy")


def install(name: str, source: str, function, defaults: dict, description: str) -> Spec:
    """Saves it and registers it under a name that isn't taken."""
    CUSTOM_DIR.mkdir(exist_ok=True)
    final = name if name not in strategies.REGISTRY else f"{name}2"
    suffix = 2
    while final in strategies.REGISTRY:
        suffix += 1
        final = f"{name}{suffix}"
    (CUSTOM_DIR / f"{final}.py").write_text(source)
    spec = Spec(final, function, description.strip()[:140],
                {k: v for k, v in (defaults or {}).items()},
                {k: (max(1, v * 0.5), v * 1.5, max(1, v * 0.25)) for k, v in (defaults or {}).items()
                 if isinstance(v, (int, float)) and v})
    strategies.register(spec)
    return spec


def load_saved() -> list[str]:
    """Re-registers everything in custom/ at startup. Files are re-checked,
    so an edited one that broke the rules fails loudly instead of running."""
    loaded = []
    if not CUSTOM_DIR.exists():
        return loaded
    for path in sorted(CUSTOM_DIR.glob("*.py")):
        try:
            source = path.read_text()
            params = check(source)
            function, defaults = compile_strategy(source)
            description = (ast.get_docstring(ast.parse(source)) or path.stem).split("\n")[0]
            spec = Spec(path.stem, function, description[:140], defaults or params,
                        {k: (max(1, v * 0.5), v * 1.5, max(1, v * 0.25))
                         for k, v in (defaults or params).items()
                         if isinstance(v, (int, float)) and v})
            strategies.register(spec)
            loaded.append(path.stem)
        except Exception:
            continue
    return loaded


def source_of(name: str) -> str | None:
    path = CUSTOM_DIR / f"{name}.py"
    return path.read_text() if path.exists() else None
