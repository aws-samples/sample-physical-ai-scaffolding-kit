"""Make the Lambda handler importable without packaging it.

It is called `handler.py` and is not in a package: Lambda puts the bundle root
on `sys.path`, which is how `import transport` resolves inside the bundle.
Reproducing that here means loading it by path under its own module name, so
that a second function's `handler.py` can be loaded alongside it.

Lives outside `submit/` so it stays out of the deployed zip —
`build.py:_copy_sources` takes the top-level `*.py` of a source dir plus
`slurm/`, and nothing else.
"""

import importlib.util
import pathlib
import sys

import pytest

LAMBDA_DIR = pathlib.Path(__file__).resolve().parent.parent


def load_handler(name: str, source_dir: pathlib.Path):
    """Import `<source_dir>/handler.py` as `name`, with its siblings importable."""
    # Prepended, not appended: `transport` must resolve to the one beside this
    # handler rather than anything of that name earlier on the path.
    source = source_dir / "handler.py"
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {source} as a module")

    sys.path.insert(0, str(source_dir))
    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(source_dir))


@pytest.fixture(scope="session")
def submit():
    return load_handler("submit_handler", LAMBDA_DIR / "submit")
