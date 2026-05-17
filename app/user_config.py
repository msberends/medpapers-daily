"""Shared helpers for reading and writing per-user YAML configuration files."""
from pathlib import Path

try:
    from ruamel.yaml import YAML as _RYAML
    _ry = _RYAML()
    _ry.default_flow_style = False
    _ry.allow_unicode = True
    _HAS_RUAMEL = True
except ImportError:
    import yaml as _pyyaml
    _HAS_RUAMEL = False

_BASE_DIR = Path(__file__).parent.parent


def load_user_cfg(username: str) -> dict:
    path = _BASE_DIR / "users" / f"{username}.yaml"
    if not path.exists():
        return {}
    if _HAS_RUAMEL:
        with open(path) as f:
            result = _ry.load(f)
        return dict(result) if result else {}
    import yaml
    with open(path) as f:
        return yaml.safe_load(f) or {}


def save_user_cfg(username: str, data: dict):
    path = _BASE_DIR / "users" / f"{username}.yaml"
    path.parent.mkdir(exist_ok=True)
    if _HAS_RUAMEL:
        # Load existing file to preserve comments; merge new data on top
        existing_cm = None
        if path.exists():
            with open(path) as f:
                existing_cm = _ry.load(f)
        if existing_cm is None:
            existing_cm = _ry.map()
        for k, v in data.items():
            existing_cm[k] = v
        # Remove keys that were deleted from data
        for k in list(existing_cm.keys()):
            if k not in data:
                del existing_cm[k]
        with open(path, "w") as f:
            _ry.dump(existing_cm, f)
    else:
        import yaml
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
