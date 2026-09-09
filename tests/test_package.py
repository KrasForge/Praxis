from praxis import __version__
from praxis import api, executors, kernel


def test_installed_package_boundaries():
    assert __version__ == "0.1.0"
    assert len({api.__name__, executors.__name__, kernel.__name__}) == 3
