const DATA_URL = "data/scenarios.json";

const state = {
  data: null,
  scenarioIndex: 0,
  stepIndex: 0,
};

const domainSpecs = {
  refund: {
    label: "客服退款",
    tenant: "retail-demo",
    actor: "action-executor",
    tool: "payments.issue_refund",
    resource: "refund-case/ref-demo-042",
    scope: "refund:issue",
    arguments: { amount_bucket: "100-500", currency: "CNY" },
    downstream: "crm.close_ticket",
    completed: "COMPLETED",
    compensated: "COMPENSATED",
    successText: "退款完成，工单状态随后得到核对。",
    recoveryText: "支付动作已发生；系统使用原 operation id 对账，没有再次退款。",
    compensationText: "支付成功但 CRM 失败；系统执行补偿，并重新核对余额与工单状态。",
  },
  operations: {
    label: "生产变更",
    tenant: "platform-demo",
    actor: "change-executor",
    tool: "deployment.apply_change",
    resource: "service/catalog-api",
    scope: "change:apply",
    arguments: { risk_units: 35, environment: "staging" },
    downstream: "health.verify_release",
    completed: "COMPLETED",
    compensated: "ROLLED_BACK_VERIFIED",
    successText: "变更完成，健康检查与目标版本一致。",
    recoveryText: "发布已经提交；系统查询发布平台后确认原操作完成，没有重复创建发布。",
    compensationText: "变更已应用但健康检查失败；系统回滚并重新核对服务版本。",
  },
};

const mutationSpecs = {
  none: { label: "合同未变化", mismatch: null },
  arguments: { label: "执行参数已变化", mismatch: "arguments_digest_mismatch" },
  tool: { label: "执行工具已变化", mismatch: "tool_mismatch" },
  context: { label: "业务上下文已变化", mismatch: "context_digest_mismatch" },
};

const byId = (id) => document.getElementById(id);

function textNode(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function formValue(name) {
  return document.querySelector(`input[name="${name}"]:checked`)?.value;
}

function decisionStep(title, detail, tone = "neutral") {
  return { title, detail, tone };
}

function buildPassport(domain, risk, approval) {
  const spec = domainSpecs[domain];
  return {
    issuer: "proofmesh-policy-issuer",
    subject: spec.actor,
    tenant: spec.tenant,
    tool: spec.tool,
    resource: spec.resource,
    scope: [spec.scope],
    arguments: spec.arguments,
    policy: risk === "high" ? "human-gated" : "automatic-reference",
    approval: risk === "high" && approval === "signed" ? "external-signed-assertion" : risk === "low" ? "AUTOMATIC" : "MISSING",
    calls: 1,
    ttl_seconds: 90,
  };
}

function evaluateLab(input) {
  const spec = domainSpecs[input.domain];
  const mutation = mutationSpecs[input.mutation];
  const passport = buildPassport(input.domain, input.risk, input.approval);

  if (input.risk === "high" && input.approval !== "signed") {
    return {
      badge: "WAITING_APPROVAL",
      tone: "amber",
      title: "先停下来，等独立审批",
      summary: `这次${spec.label}属于高风险动作。系统只冻结计划和审批挑战，不调用业务工具。`,
      dispatches: 0,
      duplicates: 0,
      terminal: "WAITING_APPROVAL",
      passport,
      steps: [
        decisionStep("计划已经冻结", "工具、业务对象、参数、策略和上下文都进入本次动作摘要。", "done"),
        decisionStep("审批断言缺失", "Human 需要通过独立审批服务签发短时断言；控制面不能替他签名。", "warn"),
        decisionStep("Gateway 不派发", "审批前写副作用保持为 0。", "blocked"),
      ],
    };
  }

  if (mutation.mismatch) {
    return {
      badge: "REJECTED",
      tone: "red",
      title: "合同发生变化，Gateway 拒绝执行",
      summary: `${mutation.label}。许可证只批准原工具、原参数和原上下文，因此请求在到达业务系统前被拦截。`,
      dispatches: 0,
      duplicates: 0,
      terminal: "REJECTED",
      passport,
      steps: [
        decisionStep("许可证有效", "签名、签发者、时效和一次性调用次数通过检查。", "done"),
        decisionStep("精确绑定不一致", `拒绝原因：${mutation.mismatch}。`, "blocked"),
        decisionStep("业务系统没有收到请求", "支付、CRM 或发布平台调用次数为 0。", "blocked"),
      ],
    };
  }

  const common = [
    decisionStep("许可证通过", "身份、租户、工具、业务对象、参数、策略、审批和有效期逐项一致。", "done"),
    decisionStep("建立逻辑操作", "稳定 operation id 与本次凭证分开保存，重试不会重新扣预算。", "done"),
  ];

  if (input.outcome === "success") {
    return {
      badge: "COMPLETED",
      tone: "green",
      title: "本次动作可以完成",
      summary: spec.successText,
      dispatches: 1,
      duplicates: 0,
      terminal: spec.completed,
      passport,
      steps: [...common,
        decisionStep("调用业务工具", "上游返回成功，Gateway 封存结果摘要和签名回执。", "done"),
        decisionStep("重新读取终态", "Verifier 核对业务状态与批准的计划一致。", "done"),
      ],
    };
  }

  if (input.outcome === "timeout_committed") {
    return {
      badge: "RECOVERED",
      tone: "cyan",
      title: "响应丢了，先对账再继续",
      summary: spec.recoveryText,
      dispatches: 1,
      duplicates: 0,
      terminal: "RECOVERED_COMPLETED",
      passport,
      steps: [...common,
        decisionStep("上游响应超时", "超时发生在提交之后，系统不把它解释成“没有执行”。", "warn"),
        decisionStep("按 operation id 对账", "上游确认原操作已经成功。", "done"),
        decisionStep("封存原结果", "不再派发第二次写操作，继续验证最终状态。", "done"),
      ],
    };
  }

  if (input.outcome === "downstream_failure") {
    return {
      badge: "COMPENSATED",
      tone: "cyan",
      title: "前一步已发生，系统执行精确恢复",
      summary: spec.compensationText,
      dispatches: 2,
      duplicates: 0,
      terminal: spec.compensated,
      passport,
      steps: [...common,
        decisionStep("第一步已经成功", "系统保留原回执，不把后续失败解释成整条流程都没执行。", "done"),
        decisionStep("下游检查失败", `${spec.downstream} 返回失败，工作流进入恢复分支。`, "warn"),
        decisionStep("执行补偿或回滚", "只撤销已经确认发生的动作，并重新读取业务终态。", "done"),
      ],
    };
  }

  return {
    badge: "UNKNOWN_MANUAL",
    tone: "amber",
    title: "结果无法确认，停止自动执行",
    summary: "对账仍无法判断上游是否执行。系统保留现场并转人工，不用一次冒险重试换取表面上的成功。",
    dispatches: 1,
    duplicates: 0,
    terminal: "UNKNOWN_MANUAL",
    passport,
    steps: [...common,
      decisionStep("上游响应不确定", "执行结果没有可靠回执。", "warn"),
      decisionStep("对账仍无结论", "系统无法证明已执行，也无法证明未执行。", "warn"),
      decisionStep("停止自动重派", "案件进入人工队列，保留 operation、租约和审计轨迹。", "blocked"),
    ],
  };
}

function renderPassport(passport) {
  const fields = byId("passport-fields");
  fields.replaceChildren();
  const summaryFields = {
    执行主体: passport.subject,
    业务租户: passport.tenant,
    允许工具: passport.tool,
    业务对象: passport.resource,
    审批来源: passport.approval,
    有效时间: `${passport.ttl_seconds} 秒`,
  };
  Object.entries(summaryFields).forEach(([label, value]) => {
    const row = document.createElement("div");
    row.append(textNode("dt", label));
    row.append(textNode("dd", value));
    fields.append(row);
  });
  byId("passport-json").textContent = JSON.stringify(passport, null, 2);
}

function renderLab(result) {
  const panel = document.querySelector(".decision-panel");
  panel.dataset.tone = result.tone;
  byId("decision-title").textContent = result.title;
  byId("decision-badge").textContent = result.badge;
  byId("decision-summary").textContent = result.summary;
  byId("dispatch-count").textContent = String(result.dispatches);
  byId("duplicate-count").textContent = String(result.duplicates);
  byId("terminal-state").textContent = result.terminal;

  const list = byId("decision-steps");
  list.replaceChildren();
  result.steps.forEach((step) => {
    const item = document.createElement("li");
    item.className = step.tone;
    item.append(textNode("strong", step.title));
    item.append(textNode("span", step.detail));
    list.append(item);
  });
  renderPassport(result.passport);
}

function runLab(event) {
  event?.preventDefault();
  renderLab(evaluateLab({
    domain: formValue("domain"),
    risk: byId("risk-input").value,
    approval: byId("approval-input").value,
    outcome: byId("outcome-input").value,
    mutation: formValue("mutation"),
  }));
}

function syncApprovalControl() {
  const approval = byId("approval-input");
  const isLow = byId("risk-input").value === "low";
  approval.disabled = isLow;
  if (isLow) approval.value = "missing";
}

function renderTabs() {
  const tabs = byId("scenario-tabs");
  tabs.replaceChildren();
  state.data.scenarios.forEach((scenario, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "scenario-tab";
    button.role = "tab";
    button.id = `tab-${scenario.id}`;
    button.setAttribute("aria-controls", "scenario-title");
    button.setAttribute("aria-selected", String(index === state.scenarioIndex));
    button.append(textNode("strong", scenario.tab));
    button.append(textNode("span", scenario.terminal));
    button.addEventListener("click", () => {
      state.scenarioIndex = index;
      state.stepIndex = 0;
      renderReplay();
    });
    tabs.append(button);
  });
}

function renderFacts(scenario) {
  const facts = byId("fact-list");
  facts.replaceChildren();
  Object.entries(scenario.facts).forEach(([label, value]) => {
    const wrapper = document.createElement("div");
    wrapper.append(textNode("dt", label));
    wrapper.append(textNode("dd", value));
    facts.append(wrapper);
  });
}

function renderTimeline(scenario) {
  const timeline = byId("timeline");
  timeline.replaceChildren();
  scenario.steps.forEach((step, index) => {
    const item = document.createElement("li");
    if (index < state.stepIndex) item.className = "done";
    if (index === state.stepIndex) item.className = "current";
    item.append(textNode("strong", step.name));
    item.append(textNode("span", step.detail));
    timeline.append(item);
  });
}

async function verifyProofHash(scenario) {
  const badge = byId("integrity-badge");
  try {
    const response = await fetch(scenario.proof, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const bytes = await response.arrayBuffer();
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    const actual = [...new Uint8Array(digest)]
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
    const currentScenario = state.data.scenarios[state.scenarioIndex];
    if (currentScenario.id !== scenario.id) return;
    badge.textContent = actual === scenario.proof_sha256
      ? `SHA-256 已匹配 · ${actual.slice(0, 10)}…`
      : "SHA-256 不匹配";
  } catch (_error) {
    badge.textContent = "SHA-256 无法复核";
  }
}

function renderEvidence(scenario) {
  const step = scenario.steps[state.stepIndex];
  byId("evidence-json").textContent = JSON.stringify(
    { scenario: scenario.id, step: state.stepIndex + 1, name: step.name, ...step.evidence },
    null,
    2,
  );
  byId("proof-link").href = scenario.proof;
  byId("integrity-badge").textContent = "正在复核 SHA-256";
  verifyProofHash(scenario);
}

function renderScenario() {
  const scenario = state.data.scenarios[state.scenarioIndex];
  byId("case-id").textContent = scenario.case_id;
  byId("scenario-title").textContent = scenario.title;
  byId("scenario-summary").textContent = scenario.summary;
  byId("terminal-badge").textContent = scenario.terminal;
  renderFacts(scenario);
  renderTimeline(scenario);
  renderEvidence(scenario);
  byId("next-button").textContent = state.stepIndex === scenario.steps.length - 1 ? "回到第一步" : "下一步";
}

function renderMetrics() {
  const grid = byId("metric-grid");
  grid.replaceChildren();
  state.data.metrics.forEach((metric) => {
    const card = document.createElement("article");
    card.className = "metric-card";
    card.append(textNode("p", metric.question, "question"));
    card.append(textNode("strong", metric.value));
    card.append(textNode("p", metric.unit));
    const detail = document.createElement("p");
    detail.textContent = metric.detail;
    const link = document.createElement("a");
    link.href = metric.link;
    link.textContent = "查看证据";
    link.className = "text-link";
    detail.append(" ", link);
    card.append(detail);
    grid.append(card);
  });
}

function renderReplay() {
  renderTabs();
  renderScenario();
}

async function copyEvidence() {
  try {
    await navigator.clipboard.writeText(byId("evidence-json").textContent);
    byId("copy-button").textContent = "已复制";
    window.setTimeout(() => { byId("copy-button").textContent = "复制这一步"; }, 1600);
  } catch (_error) {
    byId("copy-button").textContent = "浏览器未允许复制";
  }
}

async function load() {
  syncApprovalControl();
  runLab();
  try {
    const response = await fetch(DATA_URL, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    document.querySelectorAll("[data-repo-link]").forEach((link) => { link.href = state.data.repository; });
    renderReplay();
    renderMetrics();
  } catch (error) {
    byId("replay").replaceChildren(
      textNode("p", `冻结证据加载失败：${error.message}。请通过 HTTP 服务打开本页面。`),
    );
  }
}

byId("lab-form").addEventListener("submit", runLab);
byId("risk-input").addEventListener("change", syncApprovalControl);
byId("next-button").addEventListener("click", () => {
  const scenario = state.data.scenarios[state.scenarioIndex];
  state.stepIndex = (state.stepIndex + 1) % scenario.steps.length;
  renderScenario();
});
byId("reset-button").addEventListener("click", () => {
  state.stepIndex = 0;
  renderScenario();
});
byId("copy-button").addEventListener("click", copyEvidence);

load();
