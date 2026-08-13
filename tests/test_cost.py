from raizuinu.cost import CostTracker

PRICING = {
    "input": 5.0,
    "output": 25.0,
    "cache_read": 0.5,
    "cache_write_5m": 6.25,
    "cache_write_1h": 10.0,
}


def make_tracker(tmp_path, limit=100.0, threshold=0.7):
    return CostTracker(
        state_dir=tmp_path / "state",
        limit_jpy=limit,
        alert_threshold=threshold,
        usd_jpy_rate=100.0,  # テストしやすい為替
        pricing_usd_per_mtok=PRICING,
        prompt_cache_ttl="1h",
    )


class TestEstimate:
    def test_estimate_includes_all_token_kinds(self, tmp_path):
        tracker = make_tracker(tmp_path)
        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_read_input_tokens": 1_000_000,
            "cache_creation_input_tokens": 1_000_000,
        }
        # (5 + 25 + 0.5 + 10) USD × 100円 = 4050円
        assert tracker.estimate_cost_jpy(usage) == 4050.0

    def test_missing_keys_treated_as_zero(self, tmp_path):
        assert make_tracker(tmp_path).estimate_cost_jpy({}) == 0.0


class TestAccumulation:
    def test_accumulates_and_persists(self, tmp_path):
        tracker = make_tracker(tmp_path)
        tracker.add_usage({"output_tokens": 100_000})  # 25×0.1×100 = 250...
        status = tracker.status()
        assert status.total_jpy == 250.0

        # 別インスタンスでも状態を引き継ぐ（ファイル永続化）
        tracker2 = make_tracker(tmp_path)
        assert tracker2.status().total_jpy == 250.0

    def test_over_limit_and_alert_flow(self, tmp_path):
        tracker = make_tracker(tmp_path, limit=500.0, threshold=0.7)
        status = tracker.add_usage({"output_tokens": 150_000})  # 375円 = 75%
        assert not status.over_limit
        assert tracker.needs_alert(status)

        tracker.mark_alert_sent()
        assert not tracker.needs_alert(tracker.status())

        status = tracker.add_usage({"output_tokens": 100_000})  # +250円 → 625円
        assert status.over_limit
        assert not status.stopped_notified
        tracker.mark_stopped_notified()
        assert tracker.status().stopped_notified
