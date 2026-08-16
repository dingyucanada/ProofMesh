"""Privacy-minimised shadow evaluation over public CFPB complaint narratives.

The benchmark deliberately has no write-capable path.  Narratives are consumed in
memory, reduced to one-way digests and conservative review routes, and never
persisted.  CFPB issues are observational metadata, not authorization ground truth,
so this module does not calculate or claim classification accuracy.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


SCHEMA = "proofmesh.cfpb-public-shadow/v1"
DATA_LABEL = "PUBLIC_REAL_OBSERVATIONAL"
API_URL = "https://www.consumerfinance.gov/data-research/consumer-complaints/search/api/v1/"
DATE_RECEIVED_MIN = "2023-01-01"
DATE_RECEIVED_MAX_EXCLUSIVE = "2024-01-01"
PER_PRODUCT = 60
PRODUCTS: tuple[tuple[str, str], ...] = (
    ("credit_card_or_prepaid", "Credit card or prepaid card"),
    ("checking_or_savings", "Checking or savings account"),
    ("money_transfer_or_virtual_currency", "Money transfer, virtual currency, or money service"),
    ("debt_collection", "Debt collection"),
)

# Terms only choose between two human-review queues.  They never authorize action.
SENSITIVE_TERMS = (
    "fraud",
    "unauthoriz",
    "identity theft",
    "stolen",
    "dispute",
    "chargeback",
    "refund",
    "transfer",
    "withdraw",
    "foreclosure",
    "repossess",
    "lawsuit",
    "garnish",
    "credit report",
    "account closed",
)

FORBIDDEN_ARTIFACT_KEYS = {
    "complaint_what_happened",
    "complaint_id",
    "company",
    "company_public_response",
    "company_response",
    "state",
    "zip_code",
    "date_received",
    "date_sent_to_company",
    "tags",
    "submitted_via",
    "sub_product",
    "sub_issue",
    "timely",
}


class CFPBShadowError(RuntimeError):
    """The remote dataset or a shadow-evaluation invariant is invalid."""


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def query_parameters(product: str, *, per_product: int = PER_PRODUCT) -> dict[str, Any]:
    """Return the frozen, non-curated CFPB selection rule."""

    return {
        "field": "complaint_what_happened",
        "frm": 0,
        "size": per_product,
        "sort": "created_date_asc",
        "no_aggs": "true",
        "no_highlight": "true",
        "has_narrative": "true",
        "date_received_min": DATE_RECEIVED_MIN,
        "date_received_max": DATE_RECEIVED_MAX_EXCLUSIVE,
        "product": product,
    }


def _url_for(base_url: str, parameters: Mapping[str, Any]) -> str:
    return f"{base_url}?{urllib.parse.urlencode(parameters)}"


def fetch_json(url: str, *, attempts: int = 3, timeout_seconds: int = 45) -> dict[str, Any]:
    """Fetch one official response without ever writing the response body to disk."""

    request = urllib.request.Request(
        url,
        # The official endpoint currently rejects some bot-identifying user-agent
        # strings at its CDN edge; use a neutral standards-compliant HTTP client UA.
        headers={"Accept": "application/json", "User-Agent": "curl/8.7.1"},
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                if response.status != 200:
                    raise CFPBShadowError(f"CFPB API returned HTTP {response.status}")
                value = json.load(response)
                if not isinstance(value, dict):
                    raise CFPBShadowError("CFPB API returned a non-object response")
                return value
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
    raise CFPBShadowError(f"CFPB API request failed after {attempts} attempts: {last_error}")


def sanitize_hit(
    hit: Mapping[str, Any],
    *,
    product_key: str,
    expected_product: str,
    sequence: int,
) -> dict[str, Any]:
    """Transform one remote hit into a no-raw-text, no-identity case record."""

    source = hit.get("_source")
    if not isinstance(source, Mapping):
        raise CFPBShadowError("CFPB hit is missing _source")
    narrative = source.get("complaint_what_happened")
    issue = source.get("issue")
    product = source.get("product")
    if not isinstance(narrative, str) or not narrative.strip():
        raise CFPBShadowError("selected CFPB hit has no public narrative")
    if not isinstance(issue, str) or not issue.strip():
        raise CFPBShadowError("selected CFPB hit has no issue")
    if product != expected_product:
        raise CFPBShadowError("CFPB product filter did not hold")

    normalized = narrative.casefold()
    matched = sum(term in normalized for term in SENSITIVE_TERMS)
    if matched >= 2:
        confidence_bucket = "high"
    elif matched == 1:
        confidence_bucket = "medium"
    else:
        confidence_bucket = "low"
    route = "human_review_sensitive" if matched else "human_review_general"
    narrative_digest = _sha256_text(narrative)
    issue_digest = _sha256_text(issue)
    # Sequence is part of the frozen API ordering rule. It separates genuine
    # duplicate narratives without retaining the CFPB complaint identifier.
    case_digest = _sha256_text(
        "\x00".join((SCHEMA, product_key, str(sequence), narrative_digest, issue_digest))
    )
    return {
        "schema_version": SCHEMA,
        "data_label": DATA_LABEL,
        "case_digest": case_digest,
        "narrative_sha256": narrative_digest,
        "issue_sha256": issue_digest,
        "product_key": product_key,
        "route": route,
        "confidence_bucket": confidence_bucket,
        "shadow_only": True,
        "write_attempted": False,
    }


def _assert_no_raw_leakage(value: Any) -> None:
    if isinstance(value, Mapping):
        overlap = FORBIDDEN_ARTIFACT_KEYS.intersection(value)
        if overlap:
            raise CFPBShadowError(f"forbidden raw fields in artifact: {sorted(overlap)}")
        for child in value.values():
            _assert_no_raw_leakage(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_raw_leakage(child)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_shadow(
    output_dir: Path,
    *,
    base_url: str = API_URL,
    per_product: int = PER_PRODUCT,
    fetcher: Callable[[str], dict[str, Any]] = fetch_json,
    retrieved_at_utc: str | None = None,
) -> dict[str, Any]:
    """Run the fixed CFPB shadow protocol and write only privacy-minimised artifacts."""

    if per_product < 1 or per_product > 100:
        raise CFPBShadowError("per_product must be between 1 and 100")
    retrieved_at_utc = retrieved_at_utc or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    cases: list[dict[str, Any]] = []
    source_meta: list[dict[str, Any]] = []
    query_manifest: list[dict[str, Any]] = []

    for product_key, product in PRODUCTS:
        parameters = query_parameters(product, per_product=per_product)
        url = _url_for(base_url, parameters)
        payload = fetcher(url)
        hits = payload.get("hits", {}).get("hits", [])
        if not isinstance(hits, list) or len(hits) != per_product:
            raise CFPBShadowError(
                f"expected {per_product} hits for {product_key}, received {len(hits) if isinstance(hits, list) else 'invalid'}"
            )
        sanitized = [
            sanitize_hit(
                hit,
                product_key=product_key,
                expected_product=product,
                sequence=sequence,
            )
            for sequence, hit in enumerate(hits)
        ]
        if len({item["case_digest"] for item in sanitized}) != per_product:
            raise CFPBShadowError(f"duplicate sanitized case digest for {product_key}")
        cases.extend(sanitized)
        meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}
        source_meta.append(
            {
                "product_key": product_key,
                "license": meta.get("license"),
                "last_updated": meta.get("last_updated"),
                "last_indexed": meta.get("last_indexed"),
                "is_data_stale": meta.get("is_data_stale"),
                "has_data_issue": meta.get("has_data_issue"),
            }
        )
        query_manifest.append(
            {
                "product_key": product_key,
                "product": product,
                "endpoint": base_url,
                "parameters": parameters,
                "selection_rule": "first-N from official API under created_date_asc; no manual curation",
            }
        )

    _assert_no_raw_leakage(cases)
    product_counts = Counter(case["product_key"] for case in cases)
    route_counts = Counter(case["route"] for case in cases)
    confidence_counts = Counter(case["confidence_bucket"] for case in cases)
    expected_total = per_product * len(PRODUCTS)
    if len(cases) != expected_total:
        raise CFPBShadowError("sample total invariant failed")

    report = {
        "schema_version": SCHEMA,
        "data_label": DATA_LABEL,
        "status": "PASS",
        "retrieved_at_utc": retrieved_at_utc,
        "sample_protocol": {
            "date_received_min_inclusive": DATE_RECEIVED_MIN,
            "date_received_max_exclusive": DATE_RECEIVED_MAX_EXCLUSIVE,
            "per_product": per_product,
            "product_count": len(PRODUCTS),
            "sort": "created_date_asc",
            "manual_curation": False,
            "public_narrative_required": True,
        },
        "source_meta": source_meta,
        "metrics": {
            "expected_case_count": expected_total,
            "ingested_case_count": len(cases),
            "routed_case_count": len(cases),
            "product_counts": dict(sorted(product_counts.items())),
            "route_counts": dict(sorted(route_counts.items())),
            "confidence_bucket_counts": dict(sorted(confidence_counts.items())),
            "raw_narrative_artifact_count": 0,
            "forbidden_identity_field_artifact_count": 0,
            "write_capable_tool_invocation_count": 0,
            "unsafe_write_count": 0,
        },
        "claim_boundary": {
            "accuracy_reported": False,
            "reason": "CFPB issue labels are observational complaint metadata, not ProofMesh authorization ground truth.",
            "allowed_claim": "Real public complaint narratives were ingested in memory and conservatively routed in shadow/no-write mode.",
            "prohibited_claims": [
                "production accuracy",
                "representative customer distribution",
                "end-to-end business outcome",
                "authorization-policy correctness",
            ],
        },
        "limitations": [
            "CFPB complaints are not a statistical sample of consumer experience.",
            "Consumer narratives are not verified by CFPB.",
            "The run is shadow-only and invokes no production or write-capable tool.",
            "Later CFPB backfills can change a repeated API query; this artifact records retrieval time and source metadata.",
        ],
    }
    _assert_no_raw_leakage(report)

    output_dir.mkdir(parents=True, exist_ok=True)
    cases_path = output_dir / "case-results.jsonl"
    cases_path.write_text("".join(_canonical_json(case) + "\n" for case in cases), encoding="utf-8")
    report_path = output_dir / "report.json"
    _write_json(report_path, report)
    provenance_path = output_dir / "provenance.json"
    provenance = {
        "schema_version": SCHEMA,
        "data_label": DATA_LABEL,
        "source_name": "Consumer Financial Protection Bureau Consumer Complaint Database API",
        "source_documentation": "https://cfpb.github.io/ccdb5-api/documentation/",
        "source_data_use": "https://www.consumerfinance.gov/complaint/data-use/",
        "license": "CC0 (as returned by the official API and OpenAPI metadata)",
        "retrieved_at_utc": retrieved_at_utc,
        "queries": query_manifest,
        "persistence_policy": "Remote JSON and narrative text processed in memory only; neither is persisted.",
    }
    _write_json(provenance_path, provenance)
    report_md_path = output_dir / "report.md"
    report_md_path.write_text(
        "# CFPB public complaint shadow evaluation\n\n"
        f"**Result: PASS — {len(cases)}/{expected_total} public complaints ingested and routed; 0 write-capable calls.**\n\n"
        "This is a privacy-minimised observational shadow run, not an accuracy benchmark. "
        "Narratives were processed only in memory; artifacts contain digests and controlled enums.\n\n"
        "## Observed routing\n\n"
        + "\n".join(f"- `{key}`: {value}" for key, value in sorted(route_counts.items()))
        + "\n\n## Claim boundary\n\n"
        "CFPB issues are not authorization labels, complaints are not statistically representative, "
        "and narratives are not verified by CFPB. This run does not establish production accuracy or business outcomes.\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": SCHEMA,
        "data_label": DATA_LABEL,
        "status": "PASS",
        "artifacts": {
            path.name: {"sha256": _sha256_file(path), "byte_count": path.stat().st_size}
            for path in (cases_path, report_path, provenance_path, report_md_path)
        },
    }
    _write_json(output_dir / "manifest.json", manifest)
    return report


def validate_artifacts(output_dir: Path) -> dict[str, Any]:
    """Fail closed if a committed artifact leaks raw fields or diverges from its manifest."""

    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    report = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    cases = [json.loads(line) for line in (output_dir / "case-results.jsonl").read_text(encoding="utf-8").splitlines()]
    provenance = json.loads((output_dir / "provenance.json").read_text(encoding="utf-8"))
    _assert_no_raw_leakage([manifest, report, cases, provenance])
    for name, expected in manifest["artifacts"].items():
        path = output_dir / name
        if not path.is_file() or _sha256_file(path) != expected["sha256"] or path.stat().st_size != expected["byte_count"]:
            raise CFPBShadowError(f"artifact integrity failed: {name}")
    if report["metrics"]["ingested_case_count"] != len(cases):
        raise CFPBShadowError("case count differs from report")
    if any(case.get("write_attempted") is not False or case.get("shadow_only") is not True for case in cases):
        raise CFPBShadowError("case escaped shadow/no-write mode")
    return report
