"""Phase 5 — the server-side Tier-2 rate-cluster collector hook (detection → filtered Telegram forward).

Detection itself is covered exhaustively in test_rate_alerts.py; here we isolate the collector's
orchestration (per-pair detection off the ring, the rule filter, the forward cooldown, the no-notifier
short-circuit, the guard) by monkeypatching compute_rate_crossings / detect_rate_clusters.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from analytics.rate_alerts import Cluster
from services.live_collector import _update_rate_clusters, get_or_create_rate_ring_state


T = datetime(2026, 7, 20, 10, 0, 0)
T2 = datetime(2026, 7, 20, 10, 0, 5)
KEY_A = "13|IDX_I|2026-07-30"
_CLUSTER = Cluster(rule="oi", direction="up", option_type="CE", strikes=(22600.0, 22650.0, 22700.0))


class _FakeNotifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text):
        self.sent.append(text)


def _pair(view_key, *, strike_size=50.0, display_name="Nifty", expiry="2026-07-30"):
    request = SimpleNamespace(pair_key=SimpleNamespace(value=view_key), display_name=display_name, expiry=expiry)
    return SimpleNamespace(report={"strike_size": strike_size}, request=request)


def _result(*pairs):
    return SimpleNamespace(pair_results=list(pairs))


def _services(notifier=None):
    return SimpleNamespace(telegram_notifier=notifier)


def _seed_ring_and_detection(services, monkeypatch, clusters):
    ring = get_or_create_rate_ring_state(services)
    ring.rings[KEY_A] = {"22600|CE": [(1, 2.0, 3.0, 4.0)]}  # non-empty snapshot so detection is reached
    monkeypatch.setattr("services.live_collector.compute_rate_crossings", lambda snapshot, now: {})
    monkeypatch.setattr("services.live_collector.detect_rate_clusters", lambda crossings, strike_size: clusters)
    return ring


def test_update_rate_clusters_forwards_detected_cluster(tmp_path, monkeypatch):
    notifier = _FakeNotifier()
    services = _services(notifier)
    _seed_ring_and_detection(services, monkeypatch, [_CLUSTER])
    _update_rate_clusters(services, _result(_pair(KEY_A)), T)
    assert len(notifier.sent) == 1
    assert "Rate Clusters" in notifier.sent[0]


def test_update_rate_clusters_respects_rule_filter(tmp_path, monkeypatch):
    notifier = _FakeNotifier()
    services = _services(notifier)
    _seed_ring_and_detection(services, monkeypatch, [_CLUSTER])
    monkeypatch.setattr("services.live_collector.TELEGRAM_FORWARD_CONFIG", {"tier2": {"oi": False}})
    _update_rate_clusters(services, _result(_pair(KEY_A)), T)
    assert notifier.sent == []  # 'oi' rule disabled → default-deny, nothing forwarded


def test_update_rate_clusters_cooldown_blocks_repeat(tmp_path, monkeypatch):
    notifier = _FakeNotifier()
    services = _services(notifier)
    _seed_ring_and_detection(services, monkeypatch, [_CLUSTER])
    _update_rate_clusters(services, _result(_pair(KEY_A)), T)
    _update_rate_clusters(services, _result(_pair(KEY_A)), T2)  # same (key,rule,dir) within 60 s
    assert len(notifier.sent) == 1  # only the first forwarded


def test_update_rate_clusters_no_notifier_is_noop(tmp_path):
    services = _services(notifier=None)
    _update_rate_clusters(services, _result(_pair(KEY_A)), T)  # returns immediately, no error


def test_update_rate_clusters_guards_errors(tmp_path):
    notifier = _FakeNotifier()
    services = _services(notifier)
    bad = SimpleNamespace(pair_results=[SimpleNamespace(report={"strike_size": 50.0}, request=None)])
    _update_rate_clusters(services, bad, T)  # request=None → raises inside → guarded, must not propagate
    assert notifier.sent == []
