"""Plugin-side tests: schema, migration, config change. Run: python3 tests/test_plugin.py (or pytest)"""
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DEV = os.environ.get("PI_HUB_DEV") or os.path.join(HERE, "..", "..", "..", "pi-hub-dev")
if DEV not in sys.path:
    sys.path.insert(0, DEV)

from pi_hub.plugins import contrib  # noqa: E402


def load_module():
    path = os.path.join(HERE, "..", "pi_hub_plugins", "model-hub", "__init__.py")
    spec = importlib.util.spec_from_file_location("model_hub_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = load_module()


class Ctx:
    def __init__(self, cfg):
        self.cfg = cfg

    def get_config(self):
        return self.cfg


def make(cfg=None):
    p = mod.ModelHubPlugin()
    p.load(Ctx(cfg or {}))
    return p


CORE_READY = "url" in getattr(contrib, "FIELD_TYPES", ())


def test_schema_validates_and_defaults_coerce():
    if not CORE_READY:
        print("skip: core contrib lacks url/list field types (8.3.0 not built yet)")
        return
    schema = make().get_config_schema()
    checked = contrib.validate_config_schema(schema)
    assert [f["name"] for f in checked] == [f["name"] for f in schema]
    defaults = {f["name"]: f["default"] for f in schema if "default" in f}
    values, err = contrib.coerce_config_values(checked, defaults, {})
    assert err is None


def test_schema_shape():
    schema = make().get_config_schema()
    names = [f["name"] for f in schema]
    assert len(names) == len(set(names))
    assert all(f.get("section") for f in schema)


def test_migrate_old_config():
    old = {"agent_url": " http://gpu.example.local:9872/ ", "token": " t ", "host_id": "gpu"}
    new = make().migrate_config("1.1.1", old)
    assert new["agent_url"] == "http://gpu.example.local:9872"
    assert new["token"] == "t" and new["host_id"] == "gpu"
    assert make().migrate_config("1.1.1", {}) == {}


def test_numbers_use_defaults_and_clamp():
    p = make({})
    assert p._num("action_timeout_s") == 15
    p = make({"status_timeout_s": 9999, "status_ttl_s": "x"})
    assert p._num("status_timeout_s") == 60
    assert p._num("status_ttl_s") == 2.0


def test_on_config_change_clears_cache():
    p = make({})
    p._cache = (1e12, {"x": 1})
    p.on_config_change({}, {})
    assert p._cache is None


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_"):
            _fn()
            print("ok  ", _name)
