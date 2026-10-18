"""Test-data builders shared by unit and integration tests (generated test data only)."""


def generator_dict(namespace="test-ns", seed=1, out="gen-out", ingest_out="ingest-out", **over):
    cfg = {
        "namespace": namespace, "seed": seed, "output_dir": out, "ingest_output_dir": ingest_out,
        "currency": {"code": "USD", "exponent": 2}, "amount_format": "major",
        "time_window": {"start": "2026-01-01T00:00:00Z", "days": 10},
        "ordinary_activity": {"accounts": 12, "transactions": 30,
                              "amount_minor": {"min": 100, "max": 50000}, "channels": ["a", "b"]},
        "id_prefixes": {"account": "A", "transaction": "T", "alert": "AL"},
        "patterns": [
            {"type": "circular", "count": 1, "cycle_length": {"min": 3, "max": 4},
             "amount_minor": {"min": 10000, "max": 20000}, "retention_bps": 9900,
             "delay_seconds": {"min": 10, "max": 600}, "shared_account_fraction_percent": 0},
            {"type": "pass_through", "count": 1, "hops": {"min": 2, "max": 3},
             "amount_minor": {"min": 10000, "max": 20000}, "retention_bps": 9500,
             "delay_seconds": {"min": 10, "max": 600}, "shared_account_fraction_percent": 0},
            {"type": "fan_in", "count": 1, "sources": {"min": 3, "max": 4},
             "amount_minor": {"min": 1000, "max": 5000},
             "delay_seconds": {"min": 0, "max": 3600}, "shared_account_fraction_percent": 0},
            {"type": "fan_out", "count": 1, "recipients": {"min": 3, "max": 4},
             "amount_minor": {"min": 1000, "max": 5000},
             "delay_seconds": {"min": 0, "max": 3600}, "shared_account_fraction_percent": 0},
        ],
        "seed_alerts": {"on_pattern_transactions": 3, "on_ordinary_transactions": 2,
                        "alert_type": "seed", "severity": "unspecified"},
    }
    cfg.update(over)
    return cfg


def detection_dict(**over):
    """A detection config matched to the small test datasets produced by generator_dict()."""
    cfg = {
        "rule_set": {"name": "test-rules", "version": "1"},
        "limits": {"max_candidate_paths": 5000, "max_detections_per_detector": 200, "max_anchor_transactions": 100,
                   "max_evidence_transactions": 50, "query_timeout_ms": 15000},
        "detectors": {
            "circular": {"enabled": True, "min_hops": 3, "max_hops": 5, "max_gap_ms": 3_600_000,
                         "max_total_window_ms": 21_600_000, "allow_repeated_accounts": False,
                         "amount_retention": {"min_bps": 9000, "max_bps": 10000}},
            "pass_through": {"enabled": True, "min_hops": 2, "max_hops": 4, "max_gap_ms": 3_600_000,
                             "max_total_window_ms": 21_600_000, "allow_repeated_accounts": False,
                             "retention": {"min_bps": 9000, "max_bps": 10000}},
            "fan_in": {"enabled": True, "min_distinct_counterparties": 3, "min_transactions": 3,
                       "window_ms": 7_200_000, "min_total_amount": None},
            "fan_out": {"enabled": True, "min_distinct_counterparties": 3, "min_transactions": 3,
                        "window_ms": 7_200_000, "min_total_amount": None},
        },
        "persistence": {"hypothesis_findings": True},
        "evaluation": {"match": {"min_gt_coverage_percent": 100, "min_detection_purity_percent": 100}},
    }
    for k, v in over.items():
        cfg[k] = v
    return cfg
