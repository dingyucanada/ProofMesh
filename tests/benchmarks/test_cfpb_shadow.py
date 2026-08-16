from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from proofmesh.benchmarks.cfpb_shadow import (
    DATA_LABEL,
    FORBIDDEN_ARTIFACT_KEYS,
    PRODUCTS,
    CFPBShadowError,
    run_shadow,
    validate_artifacts,
)


def _fixture_fetcher(url: str) -> dict:
    query = parse_qs(urlparse(url).query)
    product = query["product"][0]
    count = int(query["size"][0])
    assert query["sort"] == ["created_date_asc"]
    assert query["date_received_min"] == ["2023-01-01"]
    assert query["date_received_max"] == ["2024-01-01"]
    return {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "product": product,
                        "issue": f"issue-{index % 3}",
                        "complaint_what_happened": (
                            f"Public fixture narrative {product} {index} unauthorized transfer"
                            if index % 2
                            else f"Public fixture narrative {product} {index} service question"
                        ),
                        "complaint_id": str(10_000 + index),
                        "company": "must-not-leak",
                        "state": "XX",
                        "zip_code": "00000",
                    }
                }
                for index in range(count)
            ]
        },
        "_meta": {
            "license": "CC0",
            "last_updated": "2024-01-02T00:00:00Z",
            "last_indexed": "2024-01-02T01:00:00Z",
            "is_data_stale": False,
            "has_data_issue": False,
        },
    }


def test_shadow_run_is_balanced_private_and_no_write(tmp_path):
    report = run_shadow(
        tmp_path,
        per_product=3,
        fetcher=_fixture_fetcher,
        retrieved_at_utc="2026-08-14T00:00:00+00:00",
    )
    assert report["status"] == "PASS"
    assert report["data_label"] == DATA_LABEL
    assert report["metrics"]["ingested_case_count"] == 3 * len(PRODUCTS)
    assert report["metrics"]["write_capable_tool_invocation_count"] == 0
    assert report["claim_boundary"]["accuracy_reported"] is False
    assert validate_artifacts(tmp_path) == report

    artifact_text = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.iterdir())
    assert "must-not-leak" not in artifact_text
    assert "Public fixture narrative" not in artifact_text
    # The query manifest may name the searched narrative field, but no case or
    # report object may contain a forbidden raw source key.
    case_objects = [json.loads(line) for line in (tmp_path / "case-results.jsonl").read_text().splitlines()]
    report_object = json.loads((tmp_path / "report.json").read_text())
    assert all(not FORBIDDEN_ARTIFACT_KEYS.intersection(case) for case in case_objects)
    assert not FORBIDDEN_ARTIFACT_KEYS.intersection(report_object)


def test_shadow_fails_closed_on_short_product_page(tmp_path):
    def short_fetcher(url: str) -> dict:
        payload = _fixture_fetcher(url)
        payload["hits"]["hits"].pop()
        return payload

    with pytest.raises(CFPBShadowError, match="expected 3 hits"):
        run_shadow(tmp_path, per_product=3, fetcher=short_fetcher)


def test_validator_detects_tampering(tmp_path):
    run_shadow(tmp_path, per_product=2, fetcher=_fixture_fetcher)
    with (tmp_path / "case-results.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"complaint_id": "leak"}) + "\n")
    with pytest.raises(CFPBShadowError):
        validate_artifacts(tmp_path)
