"""Command-line entry point for the execution runtime."""

import argparse
from collections.abc import Sequence

from praxis import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="praxis", description="Praxis execution runtime")
    parser.add_argument("--version", action="version", version=f"praxis {__version__}")
    parser.parse_args(argv)
    return 0
