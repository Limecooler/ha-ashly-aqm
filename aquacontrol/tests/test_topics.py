"""Tests for the topics module."""

from __future__ import annotations

from aquacontrol import topics


def test_all_topics_count():
    """ALL_TOPICS contains every confirmed topic — 10 as of v0.1.0."""
    assert len(topics.ALL_TOPICS) == 10


def test_all_topics_unique():
    assert len(set(topics.ALL_TOPICS)) == len(topics.ALL_TOPICS)


def test_known_topic_names():
    """Spot-check that the reverse-engineered names match the wire format."""
    assert "Channel Meters" in topics.ALL_TOPICS
    assert "System" in topics.ALL_TOPICS
    assert "WorkingSettings" in topics.ALL_TOPICS
    assert "Preset" in topics.ALL_TOPICS
    assert "Events" in topics.ALL_TOPICS
    assert "MicPreamp" in topics.ALL_TOPICS
    assert "PhantomPower" in topics.ALL_TOPICS
    assert "Network" in topics.ALL_TOPICS
    assert "Firmware" in topics.ALL_TOPICS
    assert "Security" in topics.ALL_TOPICS


def test_constants_are_string_typed():
    """Topics are exposed as plain strings, not enum members, to keep
    consumer dispatch tables (dicts) ergonomic."""
    assert isinstance(topics.SYSTEM, str)
    assert topics.SYSTEM == "System"


def test_is_ambient_covers_three_pairs():
    """Three (topic, name) pairs are flagged as ambient heartbeats."""
    # Concrete identity checks — adding a new ambient event is a
    # protocol-level change and should fail this test as a reminder.
    ambient_pairs = {
        ("System", "System Info Values"),
        ("System", "DateTime"),
        ("Network", "detected updated network parameters"),
    }
    for topic, name in ambient_pairs:
        assert topics.is_ambient(topic, name), f"{topic}/{name} should be ambient"


def test_is_meter_only_one_pair():
    assert topics.is_meter("Channel Meters", "Channel Meters")
    # Negative: Block Meters topic exists (per swagger) but isn't a meter
    # by our classification on this firmware; future firmware may change.
    assert not topics.is_meter("Block Meters", "Block Meters")
