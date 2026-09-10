"""Build and qualify the wheel in a fresh environment using locked test dependencies."""
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    repo = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("VIRTUAL_ENV", None)
    def run(*args, quiet=False):
        subprocess.run(list(args), cwd=repo, env=environment, check=True,
                       stdout=subprocess.DEVNULL if quiet else None)
    with TemporaryDirectory(prefix="praxis-qualification-") as directory:
        root = Path(directory)
        run("uv", "build", "--out-dir", str(root / "dist"))
        wheel = next((root / "dist").glob("*.whl"))
        run("uv", "export", "--locked", "--no-emit-project", "--output-file", str(root / "requirements.txt"), quiet=True)
        run("uv", "venv", "--python", sys.executable, str(root / "venv"))
        python = str(root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
        run("uv", "pip", "install", "--python", python, "-r", str(root / "requirements.txt"), str(wheel))
        run(python, "-c", "import pathlib,sys,praxis; assert pathlib.Path(praxis.__file__).is_relative_to(sys.prefix); assert pathlib.Path(praxis.__file__).with_name('py.typed').is_file(); print('installed wheel:', praxis.__version__)")
        run(python, "-m", "pytest", "-q")
        run(python, "-m", "ruff", "check", ".")
        run(python, "-m", "mypy")
        run(python, "examples/demo.py")
        run(str(root / "venv" / ("Scripts/praxis.exe" if os.name == "nt" else "bin/praxis")), "--version")
    print("Clean wheel qualification passed.")


if __name__ == "__main__":
    main()
