import importlib.util
import sys
import types
from pathlib import Path

root = Path(r'D:\nvda-addons\nvda-mcp\addon\globalPlugins\nvdaMcp-bridge')
name = 'bridge_root'
root_mod = types.ModuleType(name)
root_mod.__path__ = [str(root)]
sys.modules[name] = root_mod

for pkg in ['core', 'server', 'tools', 'validation']:
    modname = f'{name}.{pkg}'
    pkgmod = types.ModuleType(modname)
    pkgmod.__path__ = [str(root / pkg)]
    sys.modules[modname] = pkgmod

logmod = types.ModuleType('logHandler')
logmod.log = types.SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
)
sys.modules['logHandler'] = logmod

queue_mod = types.ModuleType('queueHandler')
queue_mod.queueFunction = lambda func, *args, **kwargs: func(*args, **kwargs)
sys.modules['queueHandler'] = queue_mod

# Load package initializers before modules that use relative imports
for pkg in ['core', 'server', 'tools', 'validation']:
    init_path = root / pkg / '__init__.py'
    if init_path.exists():
        spec = importlib.util.spec_from_file_location(f'{name}.{pkg}', init_path)
        package = importlib.util.module_from_spec(spec)
        package.__package__ = f'{name}.{pkg}'
        package.__path__ = [str(root / pkg)]
        sys.modules[spec.name] = package
        spec.loader.exec_module(package)

path = root / 'server' / 'http_bridge.py'
spec = importlib.util.spec_from_file_location(f'{name}.server.http_bridge', path)
mod = importlib.util.module_from_spec(spec)
mod.__package__ = f'{name}.server'
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
print('loaded', mod.__name__, mod.PROTOCOL)
