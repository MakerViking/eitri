"""Import one overlay extra against a Klipper or Kalico tree (run with -I).

usage: python -I import_check.py <klipper|kalico> <tree-root> <extra.py>

The extra is loaded under the module name the host would give it
(``extras.<name>`` for Klipper, ``klippy.extras.<name>`` for Kalico) straight
from its file, so relative imports such as ``from . import fan`` resolve
against the tree under test and nothing inside the tree is modified.
"""
import importlib.util
import os
import sys


def main(argv):
    flavor, root, path = argv
    if flavor == "klipper":
        sys.path.insert(0, os.path.join(root, "klippy"))
        pkg = "extras"
    elif flavor == "kalico":
        sys.path.insert(0, root)
        pkg = "klippy.extras"
    else:
        raise SystemExit("unknown flavor %r" % (flavor,))
    name = "%s.%s" % (pkg, os.path.splitext(os.path.basename(path))[0])
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    hooks = [h for h in ("load_config", "load_config_prefix")
             if callable(getattr(module, h, None))]
    if not hooks:
        raise SystemExit("%s defines no load_config/load_config_prefix" % name)
    stub = bool(getattr(module, "STUB", False))
    if stub:
        check_stub_refuses(module)
    print("OK %s %s stub=%s" % (name, ",".join(hooks), stub))


class _ConfigError(Exception):
    pass


class _FakeConfig:
    error = _ConfigError


def check_stub_refuses(module):
    """A stub must fail loudly when someone configures it."""
    try:
        module.load_config(_FakeConfig())
    except _ConfigError as e:
        if "stub" not in str(e):
            raise SystemExit("stub error message does not say it is a stub")
    else:
        raise SystemExit("stub %s loaded without error" % module.__name__)


if __name__ == "__main__":
    main(sys.argv[1:])
