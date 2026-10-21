"""Minimal stand-ins for the parts of Klipper the extras touch."""
import importlib.util
import sys
import types

from conftest import EXTRAS


def load_extra(name, package="extras", sibling_modules=None):
    """Import extras/<name>.py as <package>.<name> without a Klipper tree.

    sibling_modules maps module names under `package` (e.g. "fan") to fake
    modules so relative imports (``from . import fan``) resolve.
    """
    if package not in sys.modules:
        pkg = types.ModuleType(package)
        pkg.__path__ = []
        sys.modules[package] = pkg
    for mod_name, mod in (sibling_modules or {}).items():
        sys.modules["%s.%s" % (package, mod_name)] = mod
        setattr(sys.modules[package], mod_name, mod)
    full = "%s.%s" % (package, name)
    spec = importlib.util.spec_from_file_location(full, EXTRAS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


class ConfigError(Exception):
    pass


_REQUIRED = object()


def _check(name, v, minval, maxval, above, below):
    if (minval is not None and v < minval or maxval is not None and v > maxval
            or above is not None and v <= above
            or below is not None and v >= below):
        raise ConfigError("%s out of range" % name)
    return v


class FakeConfig:
    """Config section backed by a dict; mirrors ConfigWrapper's defaults."""
    error = ConfigError

    def __init__(self, values=None, name="section", printer=None):
        self.values = dict(values or {})
        self.name = name
        self.printer = printer

    def get_printer(self):
        return self.printer

    def get_name(self):
        return self.name

    def get(self, option, default=_REQUIRED, **kw):
        if option in self.values:
            return self.values[option]
        if default is _REQUIRED:
            raise ConfigError("Option '%s' must be specified" % option)
        return default

    def getfloat(self, option, default=_REQUIRED, minval=None, maxval=None,
                 above=None, below=None):
        if option not in self.values:
            if default is _REQUIRED:
                raise ConfigError("Option '%s' must be specified" % option)
            return default
        return _check(option, float(self.values[option]), minval, maxval,
                      above, below)

    def getint(self, option, default=_REQUIRED, minval=None, maxval=None):
        if option not in self.values:
            if default is _REQUIRED:
                raise ConfigError("Option '%s' must be specified" % option)
            return default
        return _check(option, int(self.values[option]), minval, maxval,
                      None, None)

    def getboolean(self, option, default=_REQUIRED):
        if option not in self.values:
            if default is _REQUIRED:
                raise ConfigError("Option '%s' must be specified" % option)
            return default
        return bool(self.values[option])

    def getlist(self, option, default=_REQUIRED):
        if option not in self.values:
            if default is _REQUIRED:
                raise ConfigError("Option '%s' must be specified" % option)
            return list(default)
        v = self.values[option]
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return list(v)


class FakeGcmd:
    error = ConfigError

    def __init__(self, params=None):
        self.params = {k.upper(): str(v) for k, v in (params or {}).items()}
        self.info = []
        self.raw = []

    def get(self, name, default=_REQUIRED, **kw):
        if name in self.params:
            return self.params[name]
        if default is _REQUIRED:
            raise ConfigError("Error on command: missing %s" % name)
        return default

    def get_float(self, name, default=_REQUIRED, minval=None, maxval=None,
                  above=None, below=None):
        if name not in self.params:
            if default is _REQUIRED:
                raise ConfigError("Error on command: missing %s" % name)
            return default
        return _check(name, float(self.params[name]), minval, maxval, above,
                      below)

    def get_int(self, name, default=_REQUIRED, minval=None, maxval=None):
        if name not in self.params:
            if default is _REQUIRED:
                raise ConfigError("Error on command: missing %s" % name)
            return default
        return _check(name, int(self.params[name]), minval, maxval, None, None)

    def respond_info(self, msg, log=True):
        self.info.append(msg)

    def respond_raw(self, msg):
        self.raw.append(msg)


class FakeGcode:
    def __init__(self):
        self.commands = {}
        self.scripts = []
        self.infos = []

    def register_command(self, name, func, desc=None, **kw):
        self.commands[name] = func

    def register_mux_command(self, cmd, key, value, func, desc=None):
        self.commands[(cmd, value)] = func

    def run_script_from_command(self, script):
        self.scripts.append(script)

    run_script = run_script_from_command

    def respond_info(self, msg, log=True):
        self.infos.append(msg)


class FakeReactor:
    NOW = 0.
    NEVER = 9999999999999999.

    def __init__(self):
        self.now = 1000.
        self.timers = []

    def monotonic(self):
        return self.now

    def register_timer(self, cb, when):
        self.timers.append(cb)
        return cb

    def register_callback(self, cb):
        cb(self.now)

    def pause(self, t):
        self.now = max(self.now, t)
        return self.now


class FakePwm:
    def __init__(self):
        self.calls = []

    def set_pwm(self, print_time, value):
        self.calls.append((print_time, value))

    def get_mcu(self):
        return "mcu"


class FakeHeater:
    def __init__(self, max_temp=70., temp=25., target=0.):
        self.max_temp = max_temp
        self.temp = temp
        self.target = target
        self.mcu_pwm = FakePwm()
        self.next_pwm_time = 123.

    def get_temp(self, eventtime):
        return self.temp, self.target


class FakeHeaters:
    def __init__(self, **heaters):
        self.heaters = heaters
        self.set_calls = []

    def lookup_heater(self, name):
        if name not in self.heaters:
            raise ConfigError("Unknown heater '%s'" % name)
        return self.heaters[name]

    def set_temperature(self, heater, temp, wait=False):
        heater.target = temp
        self.set_calls.append((heater, temp))


class FakePrinter:
    config_error = ConfigError

    def __init__(self):
        self.objects = {}
        self.handlers = {}
        self.reactor = FakeReactor()
        self.gcode = FakeGcode()
        self.objects["gcode"] = self.gcode
        self.shutdown = False

    def get_reactor(self):
        return self.reactor

    def is_shutdown(self):
        return self.shutdown

    def lookup_object(self, name, default=_REQUIRED):
        if name in self.objects:
            return self.objects[name]
        if default is _REQUIRED:
            raise ConfigError("Unknown config object '%s'" % name)
        return default

    def load_object(self, config, section, default=_REQUIRED):
        return self.lookup_object(section, default)

    def register_event_handler(self, event, cb):
        self.handlers.setdefault(event, []).append(cb)

    def send_event(self, event, *args):
        for cb in self.handlers.get(event, []):
            cb(*args)
