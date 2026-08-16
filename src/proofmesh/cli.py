from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .runtime import build_runtime
from .workflow import RefundWorkflowRequest, WorkflowStatus
from .workflow_verifier import verify_workflow_proof


def _print(value) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="ProofMesh Agent action control plane")
    parser.add_argument("--home", default=str(Path(__file__).resolve().parents[2]))
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo-refund", help="Run the deterministic local refund smoke path")
    demo.add_argument("ticket_id", choices=["TKT-LOW-001", "TKT-HIGH-001", "TKT-SAGA-001"])
    demo.add_argument("--tenant", default="acme-cn")
    demo.add_argument("--requester", default="operator-local")

    approve = sub.add_parser("approve", help="Record a scoped approval without executing it")
    approve.add_argument("workflow_id")
    approve.add_argument("--approver", required=True)
    approve.add_argument("--reason", required=True)
    approve.add_argument("--approval-assertion-file", required=True)

    challenge = sub.add_parser("approval-challenge", help="Export the exact external approval challenge")
    challenge.add_argument("workflow_id")
    challenge.add_argument("--output", type=Path, required=True)

    resume = sub.add_parser("resume", help="Run executor, verifier and memory steps after authorization")
    resume.add_argument("workflow_id")

    status = sub.add_parser("status", help="Read a workflow summary and task receipts")
    status.add_argument("workflow_id")

    verify = sub.add_parser("verify-proof", help="Verify a sealed proof with external trust and policy")
    verify.add_argument("proof_path")
    verify.add_argument("--trust-bundle", default="config/trust/action-issuers.json")
    verify.add_argument("--policy", default="data/policies/refund_policy.json")

    args = parser.parse_args()
    home = Path(args.home).resolve()
    if args.command == "verify-proof":
        report = verify_workflow_proof(
            (home / args.proof_path).resolve(),
            trust_bundle_path=(home / args.trust_bundle).resolve(),
            pinned_policy_path=(home / args.policy).resolve(),
        )
        _print(report.as_dict())
        if not report.valid:
            raise SystemExit(1)
        return

    runtime = build_runtime(args.home)
    control = runtime.control_plane
    if args.command == "demo-refund":
        summary = control.run_until_gate_or_terminal(
            RefundWorkflowRequest(
                ticket_id=args.ticket_id,
                tenant_id=args.tenant,
                requester=args.requester,
            )
        )
        _print(summary)
        if summary.status == WorkflowStatus.WAITING_APPROVAL:
            print(
                "\n下一步先导出外部审批 challenge：\n"
                f"proofmesh --home . approval-challenge {summary.workflow_id} "
                "--output /tmp/proofmesh-approval-challenge.json\n"
                "将 challenge 交给独立审批服务后，再按 docs/deployment.md 提交 assertion；"
                "审批私钥不得进入 ProofMesh home 或 runtime。"
            )
    elif args.command == "approve":
        if os.getenv("PROOFMESH_ENV", "development").lower() == "production":
            parser.error(
                "the local --approver CLI is disabled in production; use the authenticated approval API "
                "and configure an external IdP/approval-service assertion"
            )
        waiting = control.get(args.workflow_id)
        _print(
            control.approve(
                args.workflow_id,
                expected_revision=waiting.revision,
                approver=args.approver,
                reason=args.reason,
                approval_assertion=Path(args.approval_assertion_file).read_text(encoding="utf-8").strip(),
            )
        )
    elif args.command == "approval-challenge":
        value = control.approval_challenge(args.workflow_id)
        args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _print({"output": str(args.output.resolve()), "challenge_digest": value["challenge_digest"]})
    elif args.command == "resume":
        current = control.get(args.workflow_id)
        _print(control.resume_authorized_locally(args.workflow_id, expected_revision=current.revision))
    elif args.command == "status":
        _print(control.get_state(args.workflow_id))


if __name__ == "__main__":
    main()
