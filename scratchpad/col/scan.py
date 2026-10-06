import ast, sys, pathlib
for p in sorted(pathlib.Path("tests/unit/scripts").glob("*.py")):
    t = ast.parse(p.read_text())
    hits = []
    for n in t.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)): continue
        nodes = []
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nodes = n.decorator_list
            if isinstance(n, ast.ClassDef):
                for m in n.body:
                    if isinstance(m,(ast.FunctionDef,)): nodes += m.decorator_list
                    elif not isinstance(m,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)): nodes.append(m)
        else:
            nodes = [n]
        for x in nodes:
            for c in ast.walk(x):
                if isinstance(c, ast.Call):
                    s = ast.unparse(c.func)
                    if s.startswith(("Path", "pytest.mark", "re.compile", "frozenset", "set", "dict", "tuple", "list", "pytest.param", "str", "field","os.environ.get")): continue
                    if s.endswith(("resolve","parents")): continue
                    hits.append(f"{c.lineno}:{s}")
    if hits: print(p.name, " ".join(sorted(set(hits), key=lambda h:int(h.split(':')[0]))))
