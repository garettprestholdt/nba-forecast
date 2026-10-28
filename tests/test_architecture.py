"""The package layers in docs/architecture.md: lower layers never import higher ones, and the
sibling packages on top of the pipeline (agents, research, games) don't import each other."""

import ast
from pathlib import Path

import nbaforecast

LAYER = {
    "config": 0, "metrics": 0, "attribution": 0,
    "warehouse": 1, "ingest": 1, "models": 1,
    "pipeline": 2,
    "agents": 3, "research": 3, "games": 3,
    "web": 4,
    "cli": 5,
}  # fmt: skip
ROOT = Path(nbaforecast.__file__).parent


def imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("nbaforecast")
        ):
            parts = node.module.split(".")
            yield from ([parts[1]] if len(parts) > 1 else [a.name for a in node.names])


def test_every_package_has_a_layer():
    modules = {p.stem for p in ROOT.glob("*.py") if p.stem != "__init__"}
    packages = {p.name for p in ROOT.iterdir() if (p / "__init__.py").exists()}
    tops = modules | packages
    assert tops <= set(LAYER), f"add new packages to LAYER: {sorted(tops - set(LAYER))}"


def test_imports_point_down_the_layers():
    bad = []
    for path in ROOT.rglob("*.py"):
        me = path.relative_to(ROOT).parts[0].removesuffix(".py")
        if me == "__init__":
            continue
        for target in imports(path):
            if target not in LAYER:
                continue
            if LAYER[target] > LAYER[me] or (LAYER[target] == LAYER[me] == 3 and target != me):
                bad.append(f"{path.relative_to(ROOT)} imports {target}")
    assert not bad, "\n".join(sorted(bad))
