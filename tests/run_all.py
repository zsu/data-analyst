"""Run every offline test: core tests in tests/ and each pack's tests in tests/sources/<id>/.

    python -I tests/run_all.py            # all
    python -I tests/run_all.py cdc bls    # only these packs (plus core)
"""
import os
import sys

sys.dont_write_bytecode = True  # keep __pycache__ out of the skill folder while testing
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "plugins", "data-analyst", "skills", "data-analyst")
sys.path[:0] = [os.path.join(SKILL, "scripts"), os.path.join(ROOT, "tests")]


def main():
    only = set(sys.argv[1:])
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    suite.addTests(loader.discover(os.path.join(ROOT, "tests"), pattern="test_*.py", top_level_dir=os.path.join(ROOT, "tests")))
    src = os.path.join(ROOT, "tests", "sources")          # pack tests live outside the skill folder
    for pid in sorted(os.listdir(src)):
        t = os.path.join(src, pid)
        if os.path.isdir(t) and (not only or pid in only):
            suite.addTests(loader.discover(t, pattern="test_*.py", top_level_dir=t))
    res = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.exit(0 if res.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
