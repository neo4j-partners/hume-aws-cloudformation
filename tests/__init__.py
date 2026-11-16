"""The offline tests. No test here makes an AWS call.

Importing this package puts the directories of the code under test on the
import path, so a test imports a script by its module name.

Run every test from the repository root:
    uv run --with pyyaml --with boto3 --with python-dotenv \
        python -m unittest discover -s tests -t .
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# The directories that hold the code under test. A missing one is harmless.
CODE_DIRS = ("stack", "verify", "demo/spine")

for _name in CODE_DIRS:
    _path = str(REPO_ROOT / _name)
    if _path not in sys.path:
        sys.path.insert(0, _path)
