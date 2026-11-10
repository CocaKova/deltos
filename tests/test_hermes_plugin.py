"""The Hermes plugin around the provider (adapters/hermes/deltos): where it finds the
library and the store, a new store's starter settings, `hermes memory setup`, /deltos,
`hermes deltos`, and what plugin.yaml declares. An existing store is never touched.
Hermes isn't installed here: its two imports are stubbed."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import tomllib
import types
import unittest
from pathlib import Path
from unittest import mock

from deltos import Memory, demo
from deltos.memory import tools_for

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "adapters" / "hermes" / "deltos"
# The library the plugin loads: its own copy (lib/, in a public cut) or this checkout.
LIBRARY = PLUGIN / "lib" if (PLUGIN / "lib" / "deltos").is_dir() else REPO
TODAY = "2026-11-10"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_adapter(folder: Path = PLUGIN):
    mp = types.ModuleType("agent.memory_provider")
    mp.MemoryProvider = type("MemoryProvider", (), {})
    reg = types.ModuleType("tools.registry")
    reg.tool_error = lambda msg: json.dumps({"error": msg})
    stubs = {"agent": types.ModuleType("agent"), "agent.memory_provider": mp,
             "tools": types.ModuleType("tools"), "tools.registry": reg}
    added = [k for k in stubs if k not in sys.modules]
    sys.modules.update({k: stubs[k] for k in added})
    try:
        return load(folder / "__init__.py", "deltos_hermes_plugin_under_test")
    finally:
        for k in added:
            sys.modules.pop(k, None)


def yaml_list(text: str, key: str) -> list[str]:
    """The items under `key:` in a flat YAML file (no YAML library here)."""
    out, inside = [], False
    for ln in text.splitlines():
        if ln.startswith(f"{key}:"):
            inside = True
            if ln.strip().endswith("[]"):
                return []
        elif inside and ln.startswith("  - "):
            out.append(ln[4:].strip())
        elif inside:
            break
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.tmp = Path(self.d.name)
        self.home = self.tmp / "hermes-home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home), "HOME": str(self.tmp / "home")})
        env.start()
        self.addCleanup(env.stop)
        for k in [k for k in os.environ if k.startswith("DELTOS_")]:
            os.environ.pop(k)
        path = mock.patch.object(sys, "path", list(sys.path))   # the adapter puts its library first
        path.start()
        self.addCleanup(path.stop)


class WhereTest(Base):
    def setUp(self):
        super().setUp()
        self.where = load(PLUGIN / "where.py", "deltos_where_under_test")

    def test_library_is_the_plugins_copy_or_the_checkout(self):
        self.assertEqual(self.where.src(), str(LIBRARY))
        self.assertEqual(self.where.available(), "")

    def test_library_shipped_inside_the_plugin_wins_over_a_checkout(self):
        plugin = self.tmp / "a" / "b" / "deltos"
        (plugin / "lib" / "deltos").mkdir(parents=True)
        shutil.copy(PLUGIN / "where.py", plugin / "where.py")
        w = load(plugin / "where.py", "deltos_where_installed")
        self.assertEqual(w.src(), str(plugin / "lib"))
        self.assertEqual(w.available(), "")
        os.environ["DELTOS_SRC"] = "~/elsewhere"
        self.assertEqual(w.src(), str(self.tmp / "home" / "elsewhere"))
        self.assertIn("is not in", w.available())   # says what is missing, for a person

    def test_no_library_anywhere(self):
        plugin = self.tmp / "x" / "y" / "z" / "deltos"
        plugin.mkdir(parents=True)
        shutil.copy(PLUGIN / "where.py", plugin / "where.py")
        w = load(plugin / "where.py", "deltos_where_bare")
        self.assertIn("reinstall the plugin", w.available())

    def test_the_store_folder(self):
        w = self.where
        self.assertIsNone(w.root())                       # nothing set: the library's default
        (self.home / "deltos.json").write_text(json.dumps({"root": "~/mem"}))
        self.assertEqual(w.root(), str(self.tmp / "home" / "mem"))
        self.assertEqual(w.root(self.home), str(self.tmp / "home" / "mem"))
        other = self.tmp / "other-home"
        other.mkdir()
        self.assertIsNone(w.root(other))                  # another profile's home: its own answer
        os.environ["DELTOS_ROOT"] = str(self.tmp / "env")
        self.assertEqual(w.root(), str(self.tmp / "env"))  # the environment wins

    def test_broken_settings_are_no_settings(self):
        for text in ("{not json", "[1, 2]", ""):
            (self.home / "deltos.json").write_text(text)
            self.assertEqual(self.where.settings(), {})
            self.assertIsNone(self.where.root())


class ProviderTest(Base):
    def setUp(self):
        super().setUp()
        self.adapter = load_adapter()
        self.p = self.adapter.DeltosProvider()
        self.root = self.tmp / "store"

    def setup_answer(self, root: Path | str) -> None:
        self.p.save_config({"root": str(root)}, str(self.home))

    def test_setup_asks_one_thing_and_saves_it(self):
        schema = self.p.get_config_schema()
        self.assertEqual([f["key"] for f in schema], ["root"])
        self.assertFalse(any(f.get("secret") or f.get("env_var") for f in schema))   # no key, no account
        (self.home / "deltos.json").write_text(json.dumps({"kept": 1}))
        self.setup_answer(f"  {self.root}  ")
        self.assertEqual(json.loads((self.home / "deltos.json").read_text()), {"kept": 1, "root": str(self.root)})
        self.p.save_config({"root": ""}, str(self.home))   # a blank answer keeps the folder
        self.assertEqual(json.loads((self.home / "deltos.json").read_text())["root"], str(self.root))

    def test_setup_by_name_asks_too(self):
        # `hermes memory setup deltos` runs only post_setup: it asks, saves and activates
        saved = []
        cfg = types.ModuleType("hermes_cli.config")
        cfg.save_config = saved.append
        mods = {"hermes_cli": types.ModuleType("hermes_cli"), "hermes_cli.config": cfg}
        for answer, want in ((f"{self.root}\n", str(self.root)), ("\n", str(self.root))):   # Enter keeps it
            config = {"memory": None, "other": 1}
            with mock.patch.dict(sys.modules, mods), mock.patch.object(sys, "stdin", io.StringIO(answer)), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.p.post_setup(str(self.home), config)
            self.assertEqual(json.loads((self.home / "deltos.json").read_text())["root"], want)
            self.assertEqual(config, {"memory": {"provider": "deltos"}, "other": 1})
            self.assertIs(saved[-1], config)
            self.assertIn("Folder to keep memory in", out.getvalue())
            self.assertIn(want, out.getvalue())
        (self.home / "deltos.json").unlink()
        with mock.patch.dict(sys.modules, mods), mock.patch.object(sys, "stdin", io.StringIO("")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.p.post_setup(str(self.home), {})   # nothing typed, nothing saved before: the default
        self.assertEqual(json.loads((self.home / "deltos.json").read_text())["root"], "~/deltos")

    def test_a_new_store_gets_the_starter_settings(self):
        self.setup_answer(self.root)
        self.p.initialize("s1", hermes_home=str(self.home), platform="cli")
        m = self.p._m()
        self.assertEqual(Path(m.root), self.root)
        cfg = tomllib.loads((self.root / "deltos.toml").read_text())
        self.assertEqual(cfg, {"recall": {"doubt_days": 21}, "checks": {"on": True}})
        self.assertEqual((m.doubt_days, m.checks), (21, True))
        self.assertIn("memory_checks", [t["name"] for t in self.p.get_tool_schemas()])
        self.assertFalse((self.tmp / "home" / "deltos").exists())   # not the default folder too

    def test_a_store_without_settings_keeps_none(self):
        # notes and no deltos.toml: someone runs it that way
        Memory(self.root).call("name_thing", {"thing": "Box-7", "kind": "machine"})
        self.setup_answer(self.root)
        m = self.p._m()
        self.assertFalse((self.root / "deltos.toml").exists())
        self.assertEqual((m.doubt_days, m.checks), (0, False))
        self.assertNotIn("memory_checks", [t["name"] for t in self.p.get_tool_schemas()])

    def test_settings_that_exist_are_left_alone(self):
        self.root.mkdir()
        (self.root / "deltos.toml").write_text('owner = "Ada"\n')
        self.setup_answer(self.root)
        self.p._m()
        self.assertEqual((self.root / "deltos.toml").read_text(), 'owner = "Ada"\n')
        elsewhere = self.tmp / "settings.toml"
        elsewhere.write_text("[recall]\ndoubt_days = 5\n")
        os.environ["DELTOS_CONFIG"] = str(elsewhere)
        os.environ["DELTOS_ROOT"] = str(self.tmp / "fresh")
        p = self.adapter.DeltosProvider()
        self.assertEqual(p._m().doubt_days, 5)
        self.assertFalse((self.tmp / "fresh" / "deltos.toml").exists())

    def test_the_environment_still_names_the_store(self):
        self.setup_answer(self.tmp / "from-setup")
        os.environ["DELTOS_ROOT"] = str(self.root)
        self.assertEqual(Path(self.p._m().root), self.root)
        self.assertFalse((self.tmp / "from-setup").exists())

    def test_each_hermes_home_has_its_own_store(self):
        self.setup_answer(self.root)
        other = self.tmp / "profile-home"
        other.mkdir()
        self.p.save_config({"root": str(self.tmp / "store-2")}, str(other))
        self.p.initialize("s1", hermes_home=str(self.home))
        self.assertEqual(Path(self.p._m().root), self.root)
        first = self.p._m()
        self.p.initialize("s2", hermes_home=str(self.home))
        self.assertIs(self.p._m(), first)                 # the same home: the same Memory
        self.p.initialize("s3", hermes_home=str(other))
        self.assertEqual(Path(self.p._m().root), self.tmp / "store-2")

    def test_slash_command(self):
        demo.seed(self.root, TODAY)
        (self.root / "deltos.toml").write_text("[recall]\ndoubt_days = 21\n")
        self.setup_answer(self.root)
        self.p._m().today = TODAY   # the demo's dates are counted back from this day
        self.assertIn("7 things, 23 entries, 16 past values kept", self.p.command(""))
        self.assertIn("- **Host:** vps-2 (since 2026-09-01; not confirmed in 2 months)", self.p.command("the shop"))
        self.assertIn("Changed in the last 7 days:", self.p.command("changes"))
        self.assertIn("no thing called 'the moon'", self.p.command("the moon"))
        self.assertIn("**Memory**", self.p.command(None))

    def test_slash_command_never_raises(self):
        with mock.patch.object(self.adapter.DeltosProvider, "_m", side_effect=OSError("disk gone")):
            self.assertEqual(self.p.command("orchard"), "Memory could not be read: disk gone")

    def test_register(self):
        ctx = mock.MagicMock()
        self.adapter.register(ctx)
        (provider,), _ = ctx.register_memory_provider.call_args
        self.assertEqual(provider.name, "deltos")
        (name, handler), kw = ctx.register_command.call_args
        self.assertEqual(name, "deltos")
        self.assertEqual(handler, provider.command)
        self.assertTrue(kw["description"] and kw["args_hint"])

    def test_a_hermes_without_plugin_commands_still_gets_memory(self):
        class Old:
            provider = None

            def register_memory_provider(self, p):
                self.provider = p

        ctx = Old()
        self.adapter.register(ctx)
        self.assertEqual(ctx.provider.name, "deltos")

    def test_status(self):
        demo.seed(self.root, TODAY)
        self.setup_answer(self.root)
        got = self.p.get_status_config({})
        self.assertEqual(got["store"], str(self.root))
        self.assertEqual(got["library"], str(LIBRARY))
        self.assertIn("7 things, 23 entries", got["holds"])
        self.assertEqual(got["checks"], "off")
        with mock.patch.object(self.adapter.DeltosProvider, "_m", side_effect=OSError("disk gone")):
            self.assertEqual(self.p.get_status_config({})["error"], "disk gone")


class CliTest(Base):
    def run_cli(self, *words: str) -> tuple[int, str]:
        cli = load(PLUGIN / "cli.py", "deltos_plugin_cli_under_test")
        ap = argparse.ArgumentParser(prog="hermes")
        sub = ap.add_subparsers(dest="command")
        cli.register_cli(sub.add_parser("deltos"))
        args = ap.parse_args(["deltos", *words])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = args.func(args)
        return code, out.getvalue()

    def test_hermes_deltos_runs_deltos_on_the_setup_store(self):
        root = self.tmp / "store"
        demo.seed(root)
        (self.home / "deltos.json").write_text(json.dumps({"root": str(root)}))
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("7 things, 23 entries", out)             # no words: the overview
        self.assertIn("**Orchard** (web app)", self.run_cli("view", "the", "shop")[1])
        self.assertIn("vps-1", self.run_cli("show", "Orchard", "--history")[1])
        self.assertIn("# Orchard on 2026-", self.run_cli("show", "Orchard", "--as-of", "2026-08-15")[1])
        other = self.tmp / "other"
        demo.seed(other)
        Memory(other).call("name_thing", {"thing": "Only here", "kind": "note"})
        self.assertIn("8 things", self.run_cli("--root", str(other), "view")[1])   # a folder named here wins


class ManifestTest(unittest.TestCase):
    def test_plugin_yaml_declares_what_register_registers(self):
        # Hermes' validator compares these with what register() registers through the context:
        # no tools (the memory provider hands the agent its own) and no hooks. The README names
        # every tool the agent gets.
        text = (PLUGIN / "plugin.yaml").read_text()
        self.assertEqual(yaml_list(text, "provides_tools"), [])
        self.assertEqual(yaml_list(text, "provides_hooks"), [])
        readme = (PLUGIN / "README.md").read_text()
        for t in tools_for("plain", True):
            self.assertIn(f"`{t['name']}`", readme)
        self.assertIn("\nname: deltos\n", "\n" + text)

    def test_one_version_everywhere(self):
        import re
        want = re.search(r'^version: "([^"]+)"', (PLUGIN / "plugin.yaml").read_text(), re.M).group(1)
        self.assertEqual(json.loads((PLUGIN / "dashboard" / "manifest.json").read_text())["version"], want)
        self.assertIn(f'PLUGIN_VERSION = "{want}"', (PLUGIN / "dashboard" / "plugin_api.py").read_text())
        pane = PLUGIN / "desktop" / "plugin.js"
        if pane.exists():
            self.assertIn(f"PLUGIN_VERSION = '{want}'", pane.read_text())

    def test_a_shipped_copy_of_the_library_is_the_library(self):
        lib = PLUGIN / "lib" / "deltos"
        if not lib.is_dir():
            self.skipTest("no lib/ here: the export puts it in")
        for f in sorted((REPO / "deltos").rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                self.assertEqual((lib / f.relative_to(REPO / "deltos")).read_bytes(), f.read_bytes(), f.name)


if __name__ == "__main__":
    unittest.main()
