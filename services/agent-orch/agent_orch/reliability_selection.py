"""Read the blocking runner's manual quarantine selectors without executing tests."""
from __future__ import annotations

import ast
import configparser
from pathlib import Path
import re


def effective_tests(checkout):
    root = Path(checkout)
    daemon = root / "services/chat-stream-v2"
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(daemon / "pytest.ini")
    selector = (root / "scripts/lib/test-runner.js").read_text()
    table = re.search(r"const QUARANTINED_TESTS = \{\n(.*?)\n\};", selector, re.S)
    if (ini.get("pytest", "addopts", fallback="") != '-m "not soak and not quarantined"'
            or not table or "--test-skip-pattern=${quarantined}" not in selector):
        raise ValueError("effective blocking selector unavailable")
    tests = set()
    for row in table[1].strip().splitlines():
        match = re.fullmatch(r"\s*'([^']+)': '\^([A-Za-z0-9 ,_-]+)\$',?", row)
        if not match:
            raise ValueError("unrecognized web quarantine selector")
        tests.add(match[1] + "::" + match[2])
    for path in (daemon / "tests").rglob("test_*.py"):
        data = path.read_text()
        if "pytest.mark.quarantined" not in data:
            continue
        tree = ast.parse(data)
        marks = {id(node) for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                 and ast.unparse(node) == "pytest.mark.quarantined"}
        for function in (n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            name = f"{path.relative_to(daemon)}::{function.name}"
            for decorator in function.decorator_list:
                if id(decorator) in marks:
                    tests.add(name)
                    marks.remove(id(decorator))
                elif isinstance(decorator, ast.Call) and ast.unparse(decorator.func) == "pytest.mark.parametrize":
                    for call in ast.walk(decorator):
                        if not isinstance(call, ast.Call) or ast.unparse(call.func) != "pytest.param":
                            continue
                        for kw in call.keywords:
                            if kw.arg == "marks" and id(kw.value) in marks:
                                if len(call.args) != 1 or not isinstance(call.args[0], ast.Constant) or len(call.keywords) != 1:
                                    raise ValueError("ambiguous parametrized quarantine")
                                tests.add(f"{name}[{call.args[0].value}]")
                                marks.remove(id(kw.value))
        if marks:
            raise ValueError("unrecognized daemon quarantine selector")
    return tests
