"""Loading HOOK_CLI_CALLS from the single config file at config.HOOK_CLI_CALLS_FILE.

The path is a constant inside the plugin checkout, so there is no environment seam to
patch: the shipped-default cases read the module's own map, and the parsing cases call
the loader with an explicit path -- the same code path a real hook subprocess takes at
import, minus the fixed location.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import src.config as config


class TestShippedDefault:
    def test_default_file_ships_in_the_checkout(self):
        assert config.HOOK_CLI_CALLS_FILE == config.PLUGIN_ROOT / "cw-plugin-cli-hooks.json"
        assert config.HOOK_CLI_CALLS_FILE.exists()

    def test_shipped_file_wires_every_event(self):
        """Every event is wired -- a command one to an absolute path, a literal one to a
        payload.

        For a command, existence is deliberately not asserted, nor WHICH tree the path is
        in: a shipped wiring may name the plugin's own binary or the consuming project's,
        and in a real install those are different checkouts. Most commands are absolute
        after load-time expansion; the cwpilot binary is intentionally deferred to call
        time so CWPILOT_BIN can select it. A literal has no path to expand -- being
        present IS being wired.
        """
        assert set(config.HOOK_CLI_CALLS) == set(config.HOOK_CLI_EVENTS)
        for event, entry in config.HOOK_CLI_CALLS.items():
            if "policy" in entry:
                assert entry["policy"], f"{event} is wired to an empty literal"
                continue
            argv = entry["cmd"]
            assert argv, f"{event} is unwired in the shipped default"
            assert argv[0] == "{cwpilot_bin}" or Path(argv[0]).is_absolute(), (
                f"{event}: {argv[0]} is not absolute or a deferred binary placeholder"
            )

    def test_session_start_receives_resolved_plugin_data_path(self):
        assert config.HOOK_CLI_ENV["CLAUDE_PLUGIN_DATA"] == str(config.PLUGIN_DATA.resolve())
        assert config.HOOK_CLI_ENV["CWPILOT_PLUGIN_DATA"] == str(config.PLUGIN_DATA.resolve())
        assert config.HOOK_CLI_ENV["CWPILOT_PROJECT_ROOT"] == str(config.PROJECT_ROOT)
        assert config.HOOK_CLI_ENV["CWPILOT_PLUGIN_VERSION"]

    def test_load_time_placeholders_are_expanded(self):
        """No template survives the load. Which ROOT it expanded to is the wiring's
        business -- asserting a prefix here made this test fail on every install where
        the plugin and the project are separate checkouts, which is the normal case.

        Literal entries are skipped rather than exempted: _expand_roots is deliberately
        not run over them (a policy carried verbatim must not have its own values
        rewritten), so there is no expansion here to assert. The cwpilot binary is the
        one intentional call-time placeholder, because the environment may select it."""
        for entry in config.HOOK_CLI_CALLS.values():
            if "policy" in entry:
                continue
            argv = entry["cmd"]
            assert "{plugin_root}" not in argv[0]
            assert "{project_root}" not in argv[0]
            assert argv[0] == "{cwpilot_bin}" or Path(argv[0]).is_absolute()

    def test_cwpilot_binary_is_deferred_to_call_time(self):
        for event, entry in config.HOOK_CLI_CALLS.items():
            if "policy" not in entry:
                assert entry["cmd"][0] == "{cwpilot_bin}"

    def test_call_time_placeholders_survive_the_load(self):
        """{run_id}/{agent_id} belong to hook_cli_calls._fill, not to this loader.

        The placeholder is the whole assertion. Whatever the binding wraps it in --
        a bare argv element, a `key=` prefix, some other fact name entirely -- is the
        binding's own data, and pinning that spelling here only makes this test fail
        the next time the data changes without the loader changing at all."""
        def _has(event, placeholder):
            return any(placeholder in arg
                       for arg in config.HOOK_CLI_CALLS[event]["cmd"])

        assert _has("subagent_sync_call_on_bash", "{run_id}")
        assert _has("subagent_call_on_run_id_resolved", "{agent_id}")

    def test_a_bare_argv_list_is_refused(self, tmp_path):
        """The pre-release shape. Refused outright rather than tolerated: a config still
        written as bare lists would otherwise half-work, running every call on the default
        timeout while the timeouts it declares are silently ignored."""
        legacy = tmp_path / "legacy.json"
        legacy.write_text(json.dumps(
            {"hook_cli_calls": {"call_on_stop_hook": ["/bin/true", "--hook"]}}),
            encoding="utf-8")
        assert config._load_hook_cli_calls(legacy) == {}

    def test_an_unusable_timeout_falls_back_to_the_default(self, tmp_path):
        """None means "use CLI_CALL_TIMEOUT_SECONDS". A call that runs on the default
        beats an exception in a module every hook subprocess imports."""
        odd = tmp_path / "odd.json"
        odd.write_text(json.dumps(
            {"hook_cli_calls": {"call_on_stop_hook": {"cmd": ["/bin/true"],
                                                      "timeout": "soon"}}}),
            encoding="utf-8")
        assert config._load_hook_cli_calls(odd)["call_on_stop_hook"]["timeout"] is None

    def test_the_path_is_not_environment_overridable(self, monkeypatch, tmp_path):
        """The wiring belongs to the installed plugin, not to a session's env."""
        import importlib  # pylint: disable=import-outside-toplevel

        monkeypatch.setenv("HOOK_CLI_CALLS_FILE", str(tmp_path / "elsewhere.json"))
        cfg = importlib.reload(config)
        try:
            assert cfg.HOOK_CLI_CALLS_FILE == cfg.PLUGIN_ROOT / "cw-plugin-cli-hooks.json"
            assert set(cfg.HOOK_CLI_CALLS) == set(cfg.HOOK_CLI_EVENTS)
        finally:
            monkeypatch.delenv("HOOK_CLI_CALLS_FILE", raising=False)
            importlib.reload(config)


def _load(tmp_path, text):
    f = tmp_path / "calls.json"
    f.write_text(text)
    return config._load_hook_cli_calls(f)  # pylint: disable=protected-access


class TestFileContent:
    def test_json_file_defines_the_map(self, tmp_path):
        calls = _load(tmp_path, json.dumps({
            "hook_cli_calls": {"call_on_stop_hook":
                               {"cmd": ["mytool", "hooks", "stop", "--json"]}}
        }))
        assert calls["call_on_stop_hook"]["cmd"] == ["mytool", "hooks", "stop", "--json"]

    def test_a_declared_timeout_is_carried_through(self, tmp_path):
        calls = _load(tmp_path, json.dumps({
            "hook_cli_calls": {"call_on_stop_hook": {"cmd": ["mytool"], "timeout": 90}}
        }))
        assert calls["call_on_stop_hook"]["timeout"] == 90

    def test_events_the_file_omits_are_unwired_not_inherited(self, tmp_path):
        """The file is the whole configuration -- omission means no call, never a
        silent fallback to the shipped stub."""
        calls = _load(tmp_path, json.dumps(
            {"hook_cli_calls": {"call_on_stop_hook": {"cmd": ["mytool"]}}}))
        assert set(calls) == {"call_on_stop_hook"}

    def test_flat_mapping_without_the_section_key_is_accepted(self, tmp_path):
        calls = _load(tmp_path, json.dumps({"call_on_stop_hook": {"cmd": ["mytool", "stop"]}}))
        assert calls["call_on_stop_hook"]["cmd"] == ["mytool", "stop"]

    def test_null_value_leaves_an_event_unwired(self, tmp_path):
        calls = _load(tmp_path, json.dumps({"hook_cli_calls": {"call_on_stop_hook": None}}))
        assert calls.get("call_on_stop_hook") is None

    def test_an_entry_without_cmd_leaves_the_event_unwired(self, tmp_path):
        """A timeout with nothing to run is not a call."""
        calls = _load(tmp_path, json.dumps(
            {"hook_cli_calls": {"call_on_stop_hook": {"timeout": 30}}}))
        assert calls.get("call_on_stop_hook") is None

    def test_placeholders_are_expanded(self, tmp_path):
        calls = _load(tmp_path, json.dumps({
            "hook_cli_calls": {"call_on_stop_hook": {"cmd": ["{plugin_root}/bin/x"]}}
        }))
        assert calls["call_on_stop_hook"]["cmd"] == [f"{config.PLUGIN_ROOT}/bin/x"]


class TestMalformedConfigDegrades:
    """config.py is imported by every hook subprocess: a bad file must degrade to
    'nothing wired', never raise and break the observed tool call. The loader does no
    validation, so a rejected entry is simply absent."""

    @pytest.mark.parametrize("text", [
        json.dumps({"hook_cli_calls": [1, 2]}),   # section not a mapping
        json.dumps(["just", "a list"]),           # top level not a mapping
        "{ unterminated\n",                       # unparseable
    ])
    def test_malformed_file_degrades_to_unwired(self, tmp_path, text):
        assert _load(tmp_path, text) == {}

    def test_missing_file_degrades_to_unwired(self, tmp_path):
        assert config._load_hook_cli_calls(tmp_path / "nope.json") == {}  # pylint: disable=protected-access

    def test_unknown_event_is_loaded_verbatim(self, tmp_path):
        """No schema check: a name the plugin never asks for is simply never called."""
        calls = _load(tmp_path, json.dumps({"hook_cli_calls": {"call_on_stop_hok": ["typo"]}}))
        assert calls.get("call_on_stop_hook") is None

    def test_a_cold_import_never_raises(self):
        """The real failure mode: a hook subprocess imports config cold."""
        subprocess.run(
            [sys.executable, "-c", "import src.config"],
            cwd=str(config.PLUGIN_ROOT), capture_output=True, text=True, check=True)


def _load_env(tmp_path, text):
    f = tmp_path / "calls.json"
    f.write_text(text)
    return config._load_hook_cli_env(f)  # pylint: disable=protected-access


class TestEnvBlock:
    def test_roots_expand_at_load_time(self, tmp_path):
        env = _load_env(tmp_path, json.dumps({
            "env": {"CWPILOT_PROJECT_ROOT": "{project_root}"}
        }))
        assert env["CWPILOT_PROJECT_ROOT"] == str(config.PROJECT_ROOT)

    def test_absent_block_is_empty(self, tmp_path):
        assert _load_env(tmp_path, json.dumps({"hook_cli_calls": {}})) == {}

    def test_call_time_placeholders_are_left_alone(self, tmp_path):
        """Not expanded here -- see _load_hook_cli_env on why they don't belong in env."""
        env = _load_env(tmp_path, json.dumps({"env": {"X": "{run_id}"}}))
        assert env["X"] == "{run_id}"

    @pytest.mark.parametrize("text", [
        json.dumps({"env": ["not", "a", "mapping"]}),
        json.dumps({"env": {"X": 5}}),          # non-string value dropped
        "{ not json",
    ])
    def test_unusable_env_degrades_to_empty(self, tmp_path, text):
        assert _load_env(tmp_path, text) == {}

    def test_unreadable_file_is_empty(self, tmp_path):
        assert config._load_hook_cli_env(tmp_path / "nope.json") == {}  # pylint: disable=protected-access
