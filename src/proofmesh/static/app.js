"use strict";

const CASES = [
  {
    id: "TKT-LOW-001",
    name: "低风险自动退款",
    meta: "¥80.00 · 自动策略",
    tone: "safe",
    description: "策略允许后自动授权，仍需独立执行、回查与封存。",
  },
  {
    id: "TKT-HIGH-001",
    name: "高风险人工审批",
    meta: "¥129.00 · 审批门禁",
    tone: "review",
    description: "计划冻结后暂停，外部签名 assertion 只授权，不隐藏执行。",
  },
  {
    id: "TKT-SAGA-001",
    name: "下游故障与补偿",
    meta: "¥90.00 · Saga",
    tone: "saga",
    description: "退款后关单故障，触发补偿并由 Verifier 回查恢复状态。",
  },
];

const CREDENTIALS = [
  ["orchestrator", "Case Orchestrator"],
  ["intake", "Ticket Intake"],
  ["investigator", "Context Investigator"],
  ["policy", "Risk Policy Sentinel"],
  ["approver", "External Assertion Approver"],
  ["executor", "Action Executor"],
  ["verifier", "Outcome Verifier"],
  ["memory", "Memory Curator"],
  ["auditor", "Independent Auditor"],
];

const JOURNEY = [
  ["create_case", "01", "Orchestrator", "创建案件"],
  ["normalize_case", "02", "Intake", "规范化输入"],
  ["gather_context", "03", "Investigator", "读取真实上下文"],
  ["evaluate_policy", "04", "Policy", "冻结策略与计划"],
  ["record_human_approval", "05", "Human", "限定审批"],
  ["execute_authorized", "06", "Executor", "执行 / 补偿"],
  ["verify_outcome", "07", "Verifier", "独立回查"],
  ["curate_memory", "08", "Memory", "脱敏封存"],
];

const STEP_CALLS = {
  normalize_case: ["intake", "normalize", "intake"],
  gather_context: ["investigator", "context", "context"],
  evaluate_policy: ["policy", "policy", "policy"],
  execute_authorized: ["executor", "execute", "execute"],
  verify_outcome: ["verifier", "verify", "verify"],
  curate_memory: ["memory", "memory", "memory"],
};

const $ = (id) => document.getElementById(id);
let selectedCase = CASES[0];
let workflow = null;
let busy = false;

function isTerminal(summary) {
  if (!summary) return false;
  return summary.status === "COMPLETED"
    || summary.status === "BLOCKED"
    || (summary.status === "COMPENSATED" && !summary.next_step);
}

function escapeText(value) {
  return String(value ?? "");
}

function money(minor, currency) {
  if (minor === null || minor === undefined) return "—";
  const unit = currency === "CNY" ? "¥" : `${currency || ""} `;
  return `${unit}${(Number(minor) / 100).toFixed(2)}`;
}

function shortDigest(value, size = 12) {
  if (!value) return "—";
  return `${String(value).slice(0, size)}…${String(value).slice(-6)}`;
}

function token(role) {
  return sessionStorage.getItem(`proofmesh.token.${role}`) || "";
}

function authHeaders(role, json = true) {
  const headers = {};
  if (json) headers["Content-Type"] = "application/json";
  const value = token(role);
  if (value) headers.Authorization = `Bearer ${value}`;
  return headers;
}

function bestReadRole() {
  if (token("auditor")) return "auditor";
  return CREDENTIALS.map(([role]) => role).find((role) => token(role)) || "orchestrator";
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof payload === "object" ? payload.detail ?? payload.error ?? payload : payload;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload;
}

function showToast(message, kind = "info") {
  const node = $("toast");
  node.textContent = message;
  node.className = `toast show ${kind}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { node.className = "toast"; }, 3600);
}

function setBusy(value, label) {
  busy = value;
  document.body.classList.toggle("busy", value);
  $("createButton").disabled = value || Boolean(workflow && !isTerminal(workflow));
  $("advanceButton").disabled = value || !workflow || isTerminal(workflow) || workflow.next_step === "record_human_approval";
  $("refreshButton").disabled = value || !workflow;
  $("approveButton").disabled = value || !workflow || workflow.next_step !== "record_human_approval";
  if (label) $("advanceButton").textContent = label;
  else $("advanceButton").textContent = "推进至下一门禁";
}

function renderCases() {
  const list = $("caseList");
  list.replaceChildren();
  CASES.forEach((item) => {
    const button = document.createElement("button");
    button.className = `case-option ${item.id === selectedCase.id ? "selected" : ""}`;
    button.type = "button";
    button.setAttribute("role", "radio");
    button.setAttribute("aria-checked", item.id === selectedCase.id ? "true" : "false");
    const marker = document.createElement("span");
    marker.className = `case-marker ${item.tone}`;
    const copy = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = item.name;
    const meta = document.createElement("small");
    meta.textContent = item.meta;
    copy.append(title, meta);
    button.append(marker, copy);
    button.title = item.description;
    button.addEventListener("click", () => {
      if (workflow || busy) return;
      selectedCase = item;
      renderCases();
    });
    list.append(button);
  });
}

function renderCredentials() {
  const root = $("credentialFields");
  root.replaceChildren();
  CREDENTIALS.forEach(([role, label]) => {
    const wrapper = document.createElement("label");
    wrapper.className = "credential-field";
    const caption = document.createElement("span");
    caption.textContent = label;
    const input = document.createElement("input");
    input.type = "password";
    input.autocomplete = "off";
    input.placeholder = `${role} bearer token`;
    input.value = token(role);
    input.addEventListener("input", () => {
      if (input.value) sessionStorage.setItem(`proofmesh.token.${role}`, input.value);
      else sessionStorage.removeItem(`proofmesh.token.${role}`);
      updateCredentialCount();
    });
    wrapper.append(caption, input);
    root.append(wrapper);
  });
  updateCredentialCount();
}

function updateCredentialCount() {
  const count = CREDENTIALS.filter(([role]) => token(role)).length;
  $("credentialCount").textContent = `${count} / ${CREDENTIALS.length}`;
  $("credentialCount").classList.toggle("ready", count === CREDENTIALS.length);
}

function completedJourneyIndex(summary) {
  if (!summary) return -1;
  const last = JOURNEY.findIndex(([step]) => step === summary.last_step);
  return last;
}

function renderJourney() {
  const root = $("journey");
  root.replaceChildren();
  const completed = completedJourneyIndex(workflow);
  JOURNEY.forEach(([step, number, actor, label], index) => {
    const item = document.createElement("li");
    const isSkippedApproval = workflow && !workflow.approval_required && step === "record_human_approval" && completed >= index;
    const isDone = completed >= index && !isSkippedApproval;
    const isCurrent = workflow && workflow.next_step === step;
    item.className = `${isDone ? "done" : ""} ${isCurrent ? "current" : ""} ${isSkippedApproval ? "skipped" : ""}`.trim();
    const badge = document.createElement("span");
    badge.className = "journey-number";
    badge.textContent = isDone ? "✓" : number;
    const text = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = label;
    const meta = document.createElement("small");
    meta.textContent = isSkippedApproval ? "策略自动授权" : actor;
    text.append(title, meta);
    item.append(badge, text);
    root.append(item);
  });
}

function statusClass(status) {
  if (["COMPLETED", "VERIFIED"].includes(status)) return "success";
  if (status === "COMPENSATED") return "compensated";
  if (["WAITING_APPROVAL", "AUTHORIZED", "EXECUTED"].includes(status)) return "attention";
  if (status === "BLOCKED") return "blocked";
  return "running";
}

function renderWorkflow() {
  const empty = !workflow;
  $("workflowStatus").textContent = empty ? "NOT STARTED" : workflow.status;
  $("workflowStatus").className = `status ${empty ? "idle" : statusClass(workflow.status)}`;
  $("workflowId").textContent = empty ? "—" : workflow.workflow_id;
  $("projectId").textContent = empty ? "—" : workflow.project_id;
  $("revision").textContent = empty ? "0" : String(workflow.revision);
  $("nextStep").textContent = empty ? "create_case" : workflow.next_step || "terminal";
  $("finalMessage").textContent = empty
    ? "选择案件并配置角色凭证后，创建一条全新的可审计工作流。"
    : workflow.final_message;
  $("amount").textContent = empty ? "—" : money(workflow.amount_minor, workflow.currency);
  $("currency").textContent = workflow?.currency || "未冻结";
  $("riskScore").textContent = workflow?.risk_score === null || workflow?.risk_score === undefined
    ? "—" : Number(workflow.risk_score).toFixed(2);
  $("taskReceipts").textContent = empty ? "0" : String(workflow.task_receipt_count);
  $("gatewayReceipts").textContent = empty ? "0" : String(workflow.gateway_receipt_count);
  $("agentCount").textContent = empty ? "0" : String(workflow.agent_count);
  $("copyIdButton").disabled = empty;

  const risk = $("riskBadge");
  if (empty || workflow.risk_score === null) {
    risk.textContent = "—";
    risk.className = "risk-badge neutral";
  } else if (workflow.approval_required) {
    risk.textContent = "HUMAN GATE";
    risk.className = "risk-badge review";
  } else {
    risk.textContent = "POLICY ALLOW";
    risk.className = "risk-badge safe";
  }

  const approval = workflow?.next_step === "record_human_approval";
  $("approvalPanel").classList.toggle("hidden", !approval);
  $("planDigest").textContent = approval ? workflow.plan_digest : "—";
  $("approvalScope").textContent = approval ? workflow.approval_scope.join(" + ") : "—";
  $("verifyButton").disabled = !workflow?.proof_bundle;
  $("createButton").textContent = empty
    ? "创建隔离案件"
    : isTerminal(workflow) ? "开始另一案件" : "案件进行中";
  renderJourney();
  setBusy(false);
}

function eventRole(event) {
  return event.payload?.authenticated_role || event.payload?.role || event.agent || "system";
}

function renderEvents(payload) {
  const events = payload?.events || [];
  $("eventCount").textContent = `${events.length} events`;
  $("ledgerState").textContent = payload?.chain?.valid ? "VALID" : events.length ? "INVALID" : "—";
  $("ledgerState").className = payload?.chain?.valid ? "valid" : events.length ? "invalid" : "";
  $("ledgerHead").textContent = payload?.chain?.head ? shortDigest(payload.chain.head, 10) : "尚未生成";
  const root = $("eventStream");
  root.replaceChildren();
  if (!events.length) {
    root.className = "event-stream empty-state";
    root.textContent = "尚无事件。案件创建后，每次状态转换都会写入哈希链。";
    return;
  }
  root.className = "event-stream";
  [...events].reverse().forEach((event) => {
    const row = document.createElement("div");
    row.className = "event-row";
    const seq = document.createElement("span");
    seq.className = "event-seq";
    seq.textContent = String(event.seq ?? "").padStart(2, "0");
    const body = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = escapeText(event.event_type);
    const meta = document.createElement("span");
    meta.textContent = `${eventRole(event)} · ${event.ts || ""}`;
    body.append(title, meta);
    const digest = document.createElement("code");
    digest.textContent = shortDigest(event.event_hash, 8);
    row.append(seq, body, digest);
    root.append(row);
  });
}

async function fetchEvents() {
  if (!workflow) return;
  try {
    const result = await api(`/api/v1/refund-workflows/${workflow.workflow_id}/events`, {
      headers: authHeaders(bestReadRole(), false),
    });
    renderEvents(result);
  } catch (error) {
    $("ledgerState").textContent = "UNAVAILABLE";
    $("ledgerState").className = "invalid";
    showToast(`事件读取失败：${error.message}`, "error");
  }
}

async function refreshWorkflow(silent = false) {
  if (!workflow) return;
  try {
    workflow = await api(`/api/v1/refund-workflows/${workflow.workflow_id}`, {
      headers: authHeaders(bestReadRole(), false),
    });
    renderWorkflow();
    await fetchEvents();
    if (!silent) showToast("状态与证据链已刷新", "success");
  } catch (error) {
    showToast(`刷新失败：${error.message}`, "error");
  }
}

async function createWorkflow() {
  if (busy) return;
  if (workflow && isTerminal(workflow)) {
    workflow = null;
    renderWorkflow();
    renderEvents({events: [], chain: {valid: false, head: null}});
    $("verifierResult").classList.add("hidden");
    $("verifierResult").replaceChildren();
    $("verifierEmpty").classList.remove("hidden");
    showToast("已清空本地视图；可选择另一业务用例。", "info");
    return;
  }
  if (workflow) return;
  setBusy(true);
  try {
    workflow = await api("/api/v1/refund-workflows", {
      method: "POST",
      headers: authHeaders("orchestrator"),
      body: JSON.stringify({ticket_id: selectedCase.id, tenant_id: $("tenantId").value.trim()}),
    });
    renderWorkflow();
    await fetchEvents();
    showToast(`案件 ${selectedCase.id} 已创建；尚未执行任何业务副作用。`, "success");
  } catch (error) {
    showToast(`创建失败：${error.message}`, "error");
    setBusy(false);
  }
}

async function executeNextStep() {
  const next = workflow?.next_step;
  if (!next || next === "record_human_approval") return false;
  const spec = STEP_CALLS[next];
  if (!spec) throw new Error(`控制台不认识下一步骤：${next}`);
  const [role, endpoint, taskName] = spec;
  workflow = await api(`/api/v1/refund-workflows/${workflow.workflow_id}/steps/${endpoint}`, {
    method: "POST",
    headers: authHeaders(role),
    body: JSON.stringify({
      expected_revision: workflow.revision,
      task_id: `${workflow.workflow_id}:${String(workflow.revision + 1).padStart(2, "0")}-${taskName}`,
    }),
  });
  renderWorkflow();
  await fetchEvents();
  return true;
}

async function advanceToGate() {
  if (busy || !workflow) return;
  setBusy(true, "角色任务执行中…");
  try {
    let steps = 0;
    while (workflow.next_step && workflow.next_step !== "record_human_approval" && !isTerminal(workflow)) {
      if (steps >= 8) throw new Error("状态机超过单次推进上限");
      await executeNextStep();
      steps += 1;
    }
    if (workflow.next_step === "record_human_approval") {
      showToast("计划已冻结：项目停在外部 assertion 审批门禁，没有副作用被执行。", "review");
    } else if (workflow.status === "COMPENSATED") {
      showToast("故障已补偿并由独立角色验真，证明包已封存。", "review");
    } else if (workflow.status === "COMPLETED") {
      showToast("业务终态与签名证明均已封存。", "success");
    } else if (workflow.status === "BLOCKED") {
      showToast("Fail-closed：工作流已阻断。", "error");
    }
  } catch (error) {
    showToast(`推进失败：${error.message}`, "error");
    await refreshWorkflow(true);
  } finally {
    setBusy(false);
  }
}

async function approveWorkflow() {
  if (busy || workflow?.next_step !== "record_human_approval") return;
  const reason = $("approvalReason").value.trim();
  const approvalAssertion = $("approvalAssertion").value.trim();
  setBusy(true);
  try {
    workflow = await api(`/api/v1/refund-workflows/${workflow.workflow_id}/approve`, {
      method: "POST",
      headers: authHeaders("approver"),
      body: JSON.stringify({
        expected_revision: workflow.revision,
        reason,
        approval_assertion: approvalAssertion,
        task_id: `${workflow.workflow_id}:approval`,
      }),
    });
    renderWorkflow();
    await fetchEvents();
    showToast("审批已原子记录；执行仍由独立 Executor 领取。", "success");
  } catch (error) {
    showToast(`审批失败：${error.message}`, "error");
  } finally {
    setBusy(false);
  }
}

function renderVerifier(result) {
  $("verifierEmpty").classList.add("hidden");
  const root = $("verifierResult");
  root.classList.remove("hidden");
  root.replaceChildren();
  const header = document.createElement("div");
  header.className = `verifier-verdict ${result.valid ? "pass" : "fail"}`;
  const title = document.createElement("strong");
  title.textContent = result.valid ? "PROOF VALID" : "PROOF REJECTED";
  const subtitle = document.createElement("span");
  subtitle.textContent = result.valid ? "外部信任与跨证据语义一致" : "至少一项验证失败";
  header.append(title, subtitle);
  root.append(header);
  const details = result.checks || result.details || result;
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(details, null, 2);
  root.append(pre);
}

async function verifyProof() {
  if (!workflow?.proof_bundle || busy) return;
  setBusy(true);
  try {
    const result = await api(`/api/v1/refund-workflows/${workflow.workflow_id}/verify`, {
      headers: authHeaders("auditor", false),
    });
    renderVerifier(result);
    showToast(result.valid ? "证明通过独立语义验真" : "证明被拒绝", result.valid ? "success" : "error");
  } catch (error) {
    showToast(`验真失败：${error.message}`, "error");
  } finally {
    setBusy(false);
  }
}

async function checkHealth() {
  try {
    const health = await api("/health");
    $("healthDot").className = "dot healthy";
    $("healthText").textContent = `${health.data_plane} · ${health.trust_anchor} trust`;
  } catch (_error) {
    $("healthDot").className = "dot unhealthy";
    $("healthText").textContent = "控制面不可达";
  }
}

$("createButton").addEventListener("click", createWorkflow);
$("advanceButton").addEventListener("click", advanceToGate);
$("refreshButton").addEventListener("click", () => refreshWorkflow(false));
$("approveButton").addEventListener("click", approveWorkflow);
$("verifyButton").addEventListener("click", verifyProof);
$("copyIdButton").addEventListener("click", async () => {
  if (!workflow) return;
  await navigator.clipboard.writeText(workflow.workflow_id);
  showToast("Workflow ID 已复制", "success");
});

renderCases();
renderCredentials();
renderWorkflow();
checkHealth();
