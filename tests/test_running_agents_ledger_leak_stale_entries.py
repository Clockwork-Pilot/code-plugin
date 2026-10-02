"""Regression test for running_agents ledger leak: stale entries reporting phantom agents.

Tests that completed sub-agents don't leave stale entries in the running_agents ledger,
and that eviction is governed by a single hard grace period
(config.RUNNING_AGENT_GRACE_SECONDS) applied to the max of an entry's own
started_at/updated_at/finished_at timestamps -- no other running_agents staleness
signal (file mtime, live-agent-id cross-referencing, etc.) exists.
"""

from datetime import datetime, timezone, timedelta

import pytest

from src.ledger import PersistentLedgerTracker
import src.config as config


def _iso_timestamp(offset_seconds=0):
    """Return an ISO 8601 timestamp, optionally offset from now."""
    dt = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return dt.isoformat()


class TestRunningAgentsGraceEviction:
    """running_agents records are hard-evicted once RUNNING_AGENT_GRACE_SECONDS has
    elapsed since the max of (started_at, updated_at, finished_at)."""

    @pytest.fixture
    def tracker(self, tmp_path):
        return PersistentLedgerTracker(path=tmp_path / "session_main.json")

    def test_grace_period_is_one_hour(self):
        assert config.RUNNING_AGENT_GRACE_SECONDS == 60 * 60

    def test_entry_past_grace_is_evicted(self, tracker):
        tracker.set_running_agents({
            "stopped": {
                "started_at": _iso_timestamp(-7200),
                "updated_at": _iso_timestamp(-7200),
                "finished_at": _iso_timestamp(-3700),
            },
        })

        evicted = tracker._evict_expired_running_agents()

        assert evicted == ["stopped"]
        assert "stopped" not in tracker.get_running_agents()

    def test_entry_within_grace_survives(self, tracker):
        tracker.set_running_agents({
            "working": {"started_at": _iso_timestamp(-700), "updated_at": _iso_timestamp(-700)},
        })

        evicted = tracker._evict_expired_running_agents()

        assert evicted == []
        assert "working" in tracker.get_running_agents()

    def test_recent_updated_at_keeps_old_started_at_alive(self, tracker):
        """A long-running agent with a recent updated_at is not evicted, even though
        started_at is well past the grace period -- eviction uses the MAX timestamp."""
        tracker.set_running_agents({
            "long_running": {
                "started_at": _iso_timestamp(-7200),
                "updated_at": _iso_timestamp(-5),
            },
        })

        evicted = tracker._evict_expired_running_agents()

        assert evicted == []
        assert "long_running" in tracker.get_running_agents()

    def test_timestampless_entry_is_evicted_immediately(self, tracker):
        tracker.set_running_agents({"no_stamps": {}})

        evicted = tracker._evict_expired_running_agents()

        assert evicted == ["no_stamps"]
        assert "no_stamps" not in tracker.get_running_agents()

    def test_finished_at_past_grace_is_evicted(self, tracker):
        tracker.set_running_agents({
            "stopped": {
                "started_at": _iso_timestamp(-7200),
                "updated_at": _iso_timestamp(-7200),
                "finished_at": _iso_timestamp(-3601),
            },
        })

        evicted = tracker._evict_expired_running_agents()

        assert evicted == ["stopped"]
        assert "stopped" not in tracker.get_running_agents()

    def test_finished_at_within_grace_survives(self, tracker):
        tracker.set_running_agents({
            "recently_stopped": {
                "started_at": _iso_timestamp(-300),
                "updated_at": _iso_timestamp(-5),
                "finished_at": _iso_timestamp(-3),
            },
        })

        evicted = tracker._evict_expired_running_agents()

        assert evicted == []
        assert "recently_stopped" in tracker.get_running_agents()

    def test_evict_expired_running_agents_persists_to_disk(self, tracker):
        """The public evict_expired_running_agents() writes the eviction to disk."""
        tracker.set_running_agents({
            "stopped": {"started_at": _iso_timestamp(-60), "updated_at": _iso_timestamp(-60)},
        })
        tracker._write(tracker._cache)  # fresh entry survives this write's inline evict

        # Age the entry past grace directly in the cache, simulating time passing
        # without another ledger write happening in between.
        stale_agents = tracker.get_running_agents()
        stale_agents["stopped"]["started_at"] = _iso_timestamp(-3700)
        stale_agents["stopped"]["updated_at"] = _iso_timestamp(-3700)
        stale_agents["stopped"]["finished_at"] = _iso_timestamp(-3700)
        tracker.set_running_agents(stale_agents)

        evicted = tracker.evict_expired_running_agents()
        assert evicted == ["stopped"]

        reloaded = PersistentLedgerTracker(path=tracker.path)
        assert "stopped" not in reloaded.get_running_agents()
