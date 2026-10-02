"""per_playbook_subagent_denied_commands: the denials a project's own playbook declares
are enforced against a sub-agent's commands, and nothing else is.

`should_block_subagent_command` holds the enforcement RULE and never a list: it judges
whatever denylist it is handed. That list is the project's policy, read per bash call
(handler_bash.py's subagent_fetch_policy) and never stored -- so there is no plugin-side
set to union with, and no per-agent copy anywhere for these tests to stamp.

What this file used to assert -- a per-agent denylist written onto the session ledger by
set_denied_commands at first contact, unioned with a hardcoded FORBIDDEN_SUBAGENT_CALLS --
described the mechanism that WAS the defect: the union's fetched half arrived once and was
cached, so any gap in that chain left only the hardcoded half. Both halves are gone.
"""

from hooks.handler_bash import should_block_subagent_command

# Exactly what subagent_fetch_policy's binding literal carries under
# subagent_denied_commands, verbatim from the project's own
# playbooks/imports/subagent_handoff.yaml.
PROJECT_POLICY = ["play advance", "git"]


class TestEnforcesThePolicyItIsGiven:
    def test_a_declared_subcommand_is_denied(self):
        """The canonical invocation, prefixed with the CLI name as a sub-agent issues it."""
        assert should_block_subagent_command(
            "cwpilot play advance run_impl_abc123",
            agent_id="agent-1", denied=PROJECT_POLICY)

    def test_a_declared_subcommand_is_denied_through_a_path_prefixed_binary(self):
        assert should_block_subagent_command(
            "/plugin/bin/cwpilot play advance run_impl_abc123",
            agent_id="agent-1", denied=PROJECT_POLICY)

    def test_a_declared_bare_name_is_denied(self):
        assert should_block_subagent_command(
            "git commit -m wip", agent_id="agent-1", denied=PROJECT_POLICY)

    def test_a_command_the_policy_does_not_name_is_allowed(self):
        assert not should_block_subagent_command(
            "pytest -n4", agent_id="agent-1", denied=PROJECT_POLICY)

    def test_a_mention_inside_an_argument_is_not_an_invocation(self):
        """Reading about a denied command is not running it."""
        assert not should_block_subagent_command(
            "grep -rn 'cwpilot play advance' docs/",
            agent_id="agent-1", denied=PROJECT_POLICY)

    def test_a_project_specific_command_needs_no_plugin_side_entry(self):
        """Whatever the playbook declares is enforced -- this module knows no names."""
        assert should_block_subagent_command(
            "some-review-tool --check", agent_id="agent-1", denied=["some-review-tool"])


class TestNoPolicyMeansNoDenials:
    """Fail OPEN. The project owns what is denied; the plugin adds nothing of its own,
    so an empty or absent policy denies nothing -- a plugin-side default would be a
    second copy of a policy it does not own, and the two would disagree."""

    def test_an_empty_policy_denies_nothing(self):
        assert not should_block_subagent_command(
            "cwpilot play advance run_impl_abc123", agent_id="agent-1", denied=[])

    def test_no_policy_at_all_denies_nothing(self):
        assert not should_block_subagent_command(
            "git commit -m wip", agent_id="agent-1", denied=None)


class TestTheOrchestratorIsNeverGated:
    def test_agent_id_absent_is_the_main_session(self):
        """Advancing the run and committing are the orchestrator's own job."""
        assert not should_block_subagent_command(
            "cwpilot play advance run_impl_abc123",
            agent_id=None, denied=PROJECT_POLICY)
