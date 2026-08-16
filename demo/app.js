const DATA_URL = "data/scenarios.json";

const state = {
  data: null,
  scenarioIndex: 0,
  stepIndex: 0,
};

const byId = (id) => document.getElementById(id);

function textNode(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
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
      render();
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

function renderEvidence(scenario) {
  const step = scenario.steps[state.stepIndex];
  byId("evidence-json").textContent = JSON.stringify(
    {
      scenario: scenario.id,
      step: state.stepIndex + 1,
      name: step.name,
      ...step.evidence,
    },
    null,
    2,
  );
  const proofLink = byId("proof-link");
  proofLink.href = scenario.proof;
  proofLink.textContent = "查看冻结 Proof";
  byId("integrity-badge").textContent = "正在复核 SHA-256";
  verifyProofHash(scenario);
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

function renderScenario() {
  const scenario = state.data.scenarios[state.scenarioIndex];
  byId("case-id").textContent = scenario.case_id;
  byId("scenario-title").textContent = scenario.title;
  byId("scenario-summary").textContent = scenario.summary;
  byId("terminal-badge").textContent = scenario.terminal;
  renderFacts(scenario);
  renderTimeline(scenario);
  renderEvidence(scenario);

  const nextButton = byId("next-button");
  const isLast = state.stepIndex === scenario.steps.length - 1;
  nextButton.textContent = isLast ? "回到第一步" : "下一步";
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

function render() {
  renderTabs();
  renderScenario();
}

async function copyEvidence() {
  const text = byId("evidence-json").textContent;
  try {
    await navigator.clipboard.writeText(text);
    byId("copy-button").textContent = "已复制";
    window.setTimeout(() => { byId("copy-button").textContent = "复制这一步"; }, 1600);
  } catch (_error) {
    byId("copy-button").textContent = "浏览器未允许复制";
  }
}

async function load() {
  try {
    const response = await fetch(DATA_URL, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    document.querySelectorAll("[data-repo-link]").forEach((link) => {
      link.href = state.data.repository;
    });
    render();
    renderMetrics();
  } catch (error) {
    byId("demo").replaceChildren(
      textNode("p", `证据数据加载失败：${error.message}。请通过 HTTP 服务打开本页面，而不是直接双击文件。`),
    );
  }
}

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
