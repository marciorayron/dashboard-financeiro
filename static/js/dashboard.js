/* =====================================================================
   dashboard.js
   Dashboard Financeiro Pessoal — Tema Escuro
   Lógica de frontend: upload com progresso, KPIs, gráficos Chart.js,
   histórico com detalhe e exclusão. Autenticação por sessão.
   ===================================================================== */

"use strict";

// O user_id NUNCA é enviado pelo cliente: a sessão autenticada no
// backend identifica o usuário automaticamente em todas as chamadas.

// Helpers utilitários -------------------------------------------------
function formatCurrency(value) {
  const num = Number(value) || 0;
  return num.toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  const div = document.createElement("div");
  div.textContent = String(str);
  return div.innerHTML;
}

async function safeParseJson(resp, fallback) {
  // Parse defensivo: se o corpo não for JSON válido (ex.: erro inesperado do
  // servidor ou valor Infinity/NaN), não propaga a exceção — loga um warning
  // e devolve a estrutura vazia padrão, evitando bloquear os event listeners.
  const fb = (fallback === undefined) ? {} : fallback;
  try {
    return await resp.json();
  } catch (err) {
    console.warn("[safeParseJson] Falha ao interpretar resposta JSON de " + resp.url + ": " + err.message, err);
    return fb;
  }
}

async function apiGet(url, fallback) {
  const resp = await fetch(url, { credentials: "same-origin" });
  if (resp.status === 401) {
    window.location.href = "/login";
    throw new Error("Não autenticado");
  }
  if (!resp.ok) throw new Error("Erro na API: " + resp.status);
  return safeParseJson(resp, fallback);
}

async function apiPostJson(url, body) {
  // POST com corpo JSON. Em erro, lança uma exceção com .status e .data
  // (para que os handlers de limite 402/429 abram o modal de upgrade).
  const resp = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await safeParseJson(resp, {});
  if (resp.status === 401) {
    window.location.href = "/login";
    throw new Error("Não autenticado");
  }
  if (!resp.ok) {
    const err = new Error(data.message || data.error || ("HTTP " + resp.status));
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

function showToast(message, type) {
  type = type || "success";
  const toastEl = document.getElementById("appToast");
  toastEl.className = "toast align-items-center text-bg-" + type + " border-0";
  document.getElementById("appToastMsg").textContent = message;
  const toast = bootstrap.Toast.getOrCreateInstance(toastEl);
  toast.show();
}

// ---------------------------------------------------------------
// Freemium: plano, medidor de uso e IA conversacional
// ---------------------------------------------------------------
function openUpgradeModal(message) {
  const msgEl = document.getElementById("upgradeModalMsg");
  if (msgEl && message) msgEl.textContent = message;
  const el = document.getElementById("upgradeModal");
  if (el && window.bootstrap) bootstrap.Modal.getOrCreateInstance(el).show();
}

// Explain ✨ (Explicação de cards com IA) ------------------------------
function explainCooldownText(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  if (!s) return "";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  const parts = [];
  if (h) parts.push(h + "h");
  if (m) parts.push(m + "min");
  parts.push(r + "s");
  return parts.join(" ");
}

function explainMarkdownHtml(md) {
  if (!md) return "";
  const lines = String(md).split(/\r?\n/).map(function (l) { return l.trim(); }).filter(Boolean);
  const items = [];
  lines.forEach(function (line) {
    let text = line;
    if (text.indexOf("- ") === 0) text = text.slice(2);
    text = escapeHtml(text).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    items.push("<li>" + text + "</li>");
  });
  return '<ul class="mb-0">' + items.join("") + "</ul>";
}

function setExplainCardLoading(loading) {
  const sk = document.getElementById("explainCardSkeleton");
  const bd = document.getElementById("explainCardBody");
  if (sk) sk.classList.toggle("d-none", !loading);
  if (bd) bd.classList.toggle("d-none", loading);
}

function showExplainCardModal() {
  const el = document.getElementById("explainCardModal");
  if (el && window.bootstrap) bootstrap.Modal.getOrCreateInstance(el).show();
}

function hideExplainCardModal() {
  const el = document.getElementById("explainCardModal");
  if (el && window.bootstrap) bootstrap.Modal.getInstance(el)?.hide();
}

// Escopo de filtro exato usado pelo payload de analytics do dashboard.
// Prefere `advancedState` (fonte da verdade das análises avançadas); se ainda
// vazio, lê os <select> ativos — ids canônicos `advFilterMonth/Company`, com
// tolerância para `#filter-month`/`#filter-company` (mesmo objetivo).
function currentExplainFilterScope() {
  const astate = (typeof advancedState !== "undefined") ? advancedState : null;
  let month = (astate && astate.month) || "";
  let company = (astate && astate.company) || "";

  const monthEl = document.getElementById("advFilterMonth") ||
                  document.getElementById("filter-month");
  const companyEl = document.getElementById("advFilterCompany") ||
                    document.getElementById("filter-company");
  if (!month && monthEl) month = monthEl.value || "";
  if (!company && companyEl) company = companyEl.value || "";
  return { month: month, company: company };
}

async function openExplainCard(cardId) {
  if (!cardId) return;
  setExplainCardTitle(cardId);
  setExplainCardLoading(true);
  showExplainCardModal();
  const scope = currentExplainFilterScope();
  const payload = {
    card_id: cardId,
    month: scope.month,
    company: scope.company,
  };
  try {
    const data = await apiPostJson("/api/analytics/explain-card", payload);
    const bodyEl = document.getElementById("explainCardBody");
    if (bodyEl) bodyEl.innerHTML = explainMarkdownHtml(data && data.markdown);
  } catch (err) {
    const bodyEl = document.getElementById("explainCardBody");
    if (bodyEl) bodyEl.innerHTML = "";
    if (err && err.status === 429) {
      const d = (err && err.data) || {};
      const wait = explainCooldownText(d.retry_after_seconds);
      hideExplainCardModal();
      openUpgradeModal(
        "Você atingiu o limite de consultas de IA para o seu plano." +
        (wait ? " Nova consulta disponível em " + wait + "." : "") +
        " Faça upgrade para o Plano Pró (15 consultas/hora)."
      );
    } else {
      showToast((err && err.message) || "Falha ao explicar o card.", "danger");
      hideExplainCardModal();
    }
  } finally {
    setExplainCardLoading(false);
  }
}

function initExplainButtons() {
  const btns = Array.prototype.slice.call(document.querySelectorAll(".explain-btn[data-card]"));
  btns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      openExplainCard(btn.getAttribute("data-card"));
    });
  });
}

// Rótulos human-readable (pt-BR) dos cards explicáveis. A chave técnica
// (snake_case) fica no data-card; o usuário só vê o título amigável.
const CARD_EXPLAIN_LABELS = {
  "recurrent_net": "Líquido Recorrente Efetivo",
  "salario_hora": "Salário-Hora Efetivo",
  "overtime_vulnerability": "Vulnerabilidade de Horas Extras",
  "effective_tax_rate": "Alíquota Efetiva de Retenção",
  "projecao_anual": "Projeção de Entrada Anual",
  "inconsistencias": "Inconsistências Identificadas",
};

function setExplainCardTitle(cardId) {
  const titleEl = document.getElementById("explainCardTitle");
  if (!titleEl) return;
  const label = CARD_EXPLAIN_LABELS[cardId];
  if (!label) { titleEl.innerHTML = '<i class="bi bi-stars me-2 text-primary"></i>Explicação da IA'; return; }
  titleEl.innerHTML = '<i class="bi bi-stars me-2 text-primary"></i>' +
    escapeHtml(label) + ' <span class="text-secondary fw-normal">· Explicação da IA</span>';
}

function showAIAnswer(text) {
  const el = document.getElementById("aiAnswer");
  if (!el) return;
  el.classList.remove("d-none");
  el.innerHTML = '<i class="bi bi-stars me-1 text-primary"></i>' + escapeHtml(text);
}

function showAIDrawer(text) {
  const drawer = document.getElementById("aiDrawer");
  const body = document.getElementById("aiDrawerBody");
  if (!drawer || !body) return;
  body.innerHTML = escapeHtml(text);
  drawer.classList.remove("d-none");
}

async function loadPlanInfo() {
  // Atualiza o medidor "Holerites cadastrados: X/3" e a nota de plano.
  try {
    const data = await apiGet("/api/plan");
    const count = Number(data.paystub_count) || 0;
    const limit = data.paystub_limit;
    safeText("paystubCount", count);
    if (typeof limit === "number") safeText("paystubLimit", limit);
    const note = document.getElementById("uploadPlanNote");
    if (note) {
      if (data.plan === "pro") {
        note.classList.remove("d-none");
        note.textContent = "Plano Pró — holerites ilimitados";
      } else if (typeof limit === "number") {
        note.classList.remove("d-none");
        note.textContent = "Plano Gratuito — limite de " + limit + " holerites";
      }
    }
  } catch (err) {
    console.error("[loadPlanInfo]", err);
  }
}

async function askAI(question) {
  question = String(question || "").trim();
  if (!question) { showToast("Digite uma pergunta.", "warning"); return; }
  if (question.length > 200) { showToast("Máximo de 200 caracteres.", "warning"); return; }
  const btn = document.getElementById("aiAskBtn");
  const original = btn ? btn.innerHTML : "";
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Pensando...'; }
  try {
    const data = await apiPostJson("/api/analytics/ask-ai", { question: question });
    // Normaliza a resposta de forma defensiva. Um HTTP 200 do ask-ai deve
    // trazer {"answer": "<texto>"}; porém, se o corpo vier vazio/não-objeto
    // (retorno inesperado/fallback do provedor) ou usar chave alternativa
    // ("response"/"message"), extraímos com segurança — SEMPRE sem lançar
    // exceção dentro deste bloco de sucesso (evita cair no toast de erro
    // genérico apesar do backend já ter respondido 200).
    const raw = (data && typeof data === "object")
      ? (data.answer ?? data.response ?? data.message)
      : "";
    const text = (raw === undefined || raw === null) ? "" : String(raw);
    showAIAnswer(text);
    const ansEl = document.getElementById("aiAnswer");
    if (ansEl) ansEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (err) {
    if (err.status === 402 || err.status === 429) {
      openUpgradeModal(err.data && err.data.message);
    } else {
      showToast(err.message || "Falha ao consultar a IA.", "danger");
    }
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = original; }
  }
}

async function explainAnomalyByIndex(index) {
  const flags = window._anomalyData || [];
  const anomaly = flags[index];
  if (!anomaly) return;
  const body = document.getElementById("aiDrawerBody");
  if (body) body.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Analisando...';
  try {
    const data = await apiPostJson("/api/analytics/explain-anomaly", anomaly);
    showAIDrawer(data.explanation || "");
  } catch (err) {
    if (err.status === 402 || err.status === 429) {
      openUpgradeModal(err.data && err.data.message);
    } else {
      showToast(err.message || "Falha ao explicar a anomalia.", "danger");
    }
  }
}

// Exportação de planilha (XLSX) via âncora de download -------------
function exportSpreadsheet() {
  const a = document.createElement("a");
  a.href = "/api/export/excel";
  a.download = "holerites_export.xlsx";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// KPIs ----------------------------------------------------------------


// Helpers de null-safety -------------------------------------------------
function safeText(id, value) {
  const el = document.getElementById(id);
  if (el && value !== undefined && value !== null) {
    el.textContent = privacyActive ? privacyMaskText(value) : value;
  }
}
function safeRender(fn) {
  try { fn(); } catch (err) { console.error("render error:", err); }
}



// ---- Chart.js -----------------------------------------------------
// Registry p/ destruir a instância anterior antes de recriar em um canvas.
const chartRegistry = {};

function isMobileViewport() {
  return typeof window !== "undefined"
    && (window.matchMedia ? window.matchMedia("(max-width: 767px)").matches
      : (window.innerWidth || 0) <= 767);
}

function getChartCanvas(id) {
  return document.getElementById(id);
}

function destroyChart(id) {
  const inst = chartRegistry[id];
  if (inst) {
    try { inst.destroy(); } catch (err) { /* noop */ }
    delete chartRegistry[id];
  }
}

// Cria (ou recria) um gráfico Chart.js sobre um canvas, destruindo o anterior.
function mountChart(id, config) {
  const canvas = typeof id === "string" ? getChartCanvas(id) : id;
  if (!canvas) { console.error("Chart.js: canvas não encontrado:", id); return null; }
  destroyChart(id);
  try {
    const ctx = canvas.getContext && canvas.getContext("2d");
    if (!ctx) { console.error("Chart.js: canvas sem contexto 2d:", id); return null; }
    const chart = new Chart(ctx, config);
    chartRegistry[id] = chart;
    // Mobile-first: adapta eixos/rotação de rótulos conforme a largura atual.
    try { applyChartMobileScales(chart); } catch (err) { /* noop */ }
    return chart;
  } catch (err) {
    console.error("Chart.js mount error:", err);
    return null;
  }
}

// Eixo em moeda compacta: R$ 15k / R$ 1,5M.
function chartAxisMoney(value) {
  const v = Number(value) || 0;
  const abs = Math.abs(v);
  if (abs >= 1e6) {
    return "R$ " + (v / 1e6).toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + "M";
  }
  if (abs >= 1e3) {
    return "R$ " + (v / 1e3).toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + "k";
  }
  return "R$ " + Math.round(v).toLocaleString("pt-BR");
}

// Opções de tooltip padrão (dark, limpo e legível no toque).
function chartTooltip(opts) {
  return Object.assign(
    {
      backgroundColor: "#0f172a",
      titleColor: "#f1f5f9",
      bodyColor: "#cbd5e1",
      borderColor: "#2b3a55",
      borderWidth: 1,
      padding: 10,
      displayColors: false,
      usePointStyle: true,
      titleFont: { weight: "600" },
    },
    opts || {}
  );
}

// Gradiente de preenchimento sob uma linha (transparente -> cor no topo).
function lineAreaGradient(rgbaTop) {
  return function (context) {
    const chart = context.chart;
    const area = chart.chartArea;
    if (!area) return rgbaTop;
    const g = chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
    g.addColorStop(0, rgbaTop);
    g.addColorStop(1, "rgba(0,0,0,0)");
    return g;
  };
}

// Tema padrão escuro do Chart.js (consistente com o dashboard).
if (typeof Chart !== "undefined") {
  Chart.defaults.color = "#cbd5e1";
  Chart.defaults.font.family =
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif";
  Chart.defaults.borderColor = "rgba(148,163,184,0.14)";
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.plugins.legend.labels.boxWidth = 8;
  Chart.defaults.plugins.legend.labels.boxHeight = 8;
  Chart.defaults.plugins.legend.labels.padding = 14;
  Chart.defaults.plugins.legend.labels.font = { size: 12 };
}

// Plugin global: desenha o marcador de férias (🌴) sobre o ponto da série.
const vacationMarkersPlugin = {
  id: "vacationMarkers",
  afterDatasetsDraw: function (chart) {
    const cfg = (chart.config.options.plugins && chart.config.options.plugins.vacationMarkers) || {};
    const indexes = cfg.indexes;
    if (!indexes) return;
    const meta = chart.getDatasetMeta(0);
    if (!meta || !meta.data) return;
    const ctx = chart.ctx;
    ctx.save();
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.font = (isMobileViewport() ? 15 : 18) + "px sans-serif";
    for (let i = 0; i < (chart.data.labels || []).length; i++) {
      if (!indexes[i]) continue;
      const pt = meta.data[i];
      if (!pt) continue;
      ctx.fillText("\u{1F334}", pt.x, (pt.y || 0) - 10);
    }
    ctx.restore();
  },
};
if (typeof Chart !== "undefined") {
  Chart.register(vacationMarkersPlugin);
}

// ---- Data labels (chartjs-plugin-datalabels) & Controles globais -----
let dataLabelsOn = true;      // rótulos de dados visíveis (toggle "🏷️ Rótulos")
let privacyActive = false;    // modo privacidade (máscara R$ ****)

// Registra o plugin de data labels (exposto como ChartDataLabels no UMD).
if (typeof Chart !== "undefined" && typeof ChartDataLabels !== "undefined") {
  Chart.register(ChartDataLabels);
}

// Redesenha todos os gráficos ativos (usado após trocar o estado de rótulos).
function refreshAllCharts() {
  Object.keys(chartRegistry).forEach(function (id) {
    const chart = chartRegistry[id];
    if (chart) {
      try { chart.update(); } catch (err) { /* noop */ }
    }
  });
}

// ---- Mobile-first (Chart.js) --------------------------------------------
// Breakpoint móvel dos gráficos: telas < 640px.
function isChartMobile() {
  if (typeof window === "undefined") return false;
  if (window.matchMedia) return !window.matchMedia("(min-width: 640px)").matches;
  return (window.innerWidth || 0) < 640;
}

// Data labels só aparecem em telas >= 640px (e se o toggle "🏷️ Rótulos" estiver ON).
// Em telas pequenas os valores continuam acessíveis via tooltip (toque/click),
// evitando sobreposição de textos flutuantes nos gráficos.
function dataLabelsAllowed() {
  return dataLabelsOn && !isChartMobile();
}

// Ajusta os eixos dos gráficos para telas pequenas:
// rótulos de categoria no eixo X rotacionados em 45° (evita sobreposição de
// títulos como "Baseline 12m", "13º Salário", competências, etc.).
// Barras horizontais e donuts não são rotacionadas (categoria vai no eixo Y).
function applyChartMobileScales(chart) {
  if (!chart || !chart.options || !chart.options.scales) return;
  const mobile = isChartMobile();
  const type = chart.config && chart.config.type;
  const indexAxis = chart.options.indexAxis;
  const isVerticalCategory = type === "bar" && indexAxis !== "y";
  const isLineChart = type === "line";
  if (!isVerticalCategory && !isLineChart) return;
  const ticks = chart.options.scales.x && chart.options.scales.x.ticks;
  if (!ticks) return;
  if (mobile) {
    ticks.maxRotation = 45;
    ticks.minRotation = 45;
    ticks.autoSkip = true;
    ticks.autoSkipPadding = 6;
  } else {
    ticks.maxRotation = 0;
    ticks.minRotation = 0;
    ticks.autoSkip = true;
    ticks.autoSkipPadding = 24;
  }
}

// Reaplica as adaptações mobile a todos os gráficos ativos (ex.: ao cruzar o
// breakpoint de 640px redimensionando/janela ou girando o aparelho).
function syncChartsWithViewport() {
  Object.keys(chartRegistry).forEach(function (id) {
    const chart = chartRegistry[id];
    if (!chart) return;
    try {
      applyChartMobileScales(chart);
      chart.update();
    } catch (err) { /* noop */ }
  });
}

// Debounce simples para evitar recálculos em cascata durante o redimensionar.
let _mobileResizeTimer = null;
if (typeof window !== "undefined") {
  window.addEventListener("resize", function () {
    clearTimeout(_mobileResizeTimer);
    _mobileResizeTimer = setTimeout(syncChartsWithViewport, 150);
  });
}

// Texto mascarado por padrão de privacidade.
function privacyMaskText(text) {
  const t = String(text == null ? "" : text).trim();
  if (/R\$\s?[\d.,]+/.test(t)) return "R$ ****";
  if (/^[-+]?[\d][\d.,]*$/.test(t)) return "****";
  return String(text);
}

// 👁️ Visibilidade: mascara/esfumaça números em cards e gráficos.
function setPrivacy(on) {
  privacyActive = !!on;
  document.body.classList.toggle("privacy-mode", privacyActive);
  const all = document.querySelectorAll("body *");
  for (let i = 0; i < all.length; i++) {
    const el = all[i];
    if (el.children && el.children.length) continue;
    const tag = el.tagName;
    if (/^(SCRIPT|STYLE|CANVAS|INPUT|TEXTAREA|BUTTON|SELECT|OPTION|A)$/.test(tag)) continue;
    const raw = (el.textContent || "").trim();
    if (!raw) continue;
    if (!/R\$\s?[\d.,]+/.test(raw) && !/^[-+]?[\d][\d.,]*$/.test(raw)) continue;
    if (privacyActive) {
      if (el.dataset.privacyOrigin === undefined) el.dataset.privacyOrigin = el.textContent;
      el.textContent = privacyMaskText(el.dataset.privacyOrigin);
    } else if (el.dataset.privacyOrigin !== undefined) {
      el.textContent = el.dataset.privacyOrigin;
      delete el.dataset.privacyOrigin;
    }
  }
  const btn = document.getElementById("privacyToggle");
  if (btn) {
    btn.classList.toggle("active", privacyActive);
    btn.classList.toggle("btn-outline-light", !privacyActive);
    btn.classList.toggle("btn-outline-warning", privacyActive);
    btn.setAttribute("aria-pressed", String(privacyActive));
    const ic = btn.querySelector("i");
    if (ic) ic.className = privacyActive ? "bi bi-eye me-1" : "bi bi-eye-slash me-1";
  }
  const hint = document.getElementById("viewHint");
  if (hint) {
    hint.textContent = privacyActive
      ? "Modo privacidade ativo — valores mascarados e gráficos desfocados."
      : "";
  }
}

// 🏷️ Rótulos: alterna a visibilidade dos data labels (chart.update()).
function setLabelsVisible(on) {
  dataLabelsOn = !!on;
  const btn = document.getElementById("labelsToggle");
  if (btn) {
    btn.classList.toggle("active", dataLabelsOn);
    btn.classList.toggle("btn-outline-info", dataLabelsOn);
    btn.classList.toggle("btn-outline-secondary", !dataLabelsOn);
    btn.setAttribute("aria-pressed", String(dataLabelsOn));
    const st = btn.querySelector(".lbl-state");
    if (st) {
      st.textContent = dataLabelsOn ? "ON" : "OFF";
      st.classList.toggle("text-bg-info", dataLabelsOn);
      st.classList.toggle("text-bg-secondary", !dataLabelsOn);
    }
  }
  const hint = document.getElementById("viewHint");
  if (hint && !privacyActive) {
    hint.textContent = dataLabelsOn ? "" : "Rótulos de dados ocultos nos gráficos.";
  }
  refreshAllCharts();
}

// Liga os controles da barra superior (privacidade & rótulos).
function initViewToggles() {
  const privacyBtn = document.getElementById("privacyToggle");
  if (privacyBtn) {
    privacyBtn.addEventListener("click", function () { setPrivacy(!privacyActive); });
  }
  const labelsBtn = document.getElementById("labelsToggle");
  if (labelsBtn) {
    labelsBtn.addEventListener("click", function () { setLabelsVisible(!dataLabelsOn); });
  }
  // Estado inicial consistente com a marcação do HTML (rótulos ON, dados visíveis).
  setLabelsVisible(true);
  setPrivacy(false);
}
// KPIs orientados à decisão -----------------------------------------

// Salário-Hora Efetivo — visões Mensal / Acumulado do Período ----------
let effHourlyState = { active: "monthly", views: null };

// Espelha o round2 do services/savings_service.py (centavos determinísticos).
function round2(value) { return Math.round((Number(value) || 0) * 100) / 100; }

// Fallback para quando o backend ainda não retornou `effective_hourly`:
// deriva as duas visões aprovadas a partir dos componentes da API.
function buildEffectiveViews(meta, overtime, wh) {
  meta = meta || {}; overtime = overtime || {}; wh = wh || {};
  const recurrent = Number(meta.recurrent_net_pay) || 0;
  const extraNet = Number(overtime.net) ||
    ((Number(overtime.total) || 0) + (Number(overtime.dsr_overtime) || 0)) *
      (1 - (Number(meta.effective_tax_rate) || 0) / 100);
  const monthlyHours = Number(meta.monthly_hours) || 220;
  const extraHours = Number(wh.extra_hours_total != null
    ? wh.extra_hours_total : overtime.extra_hours) || 0;
  const periodMonths = Math.max(Number(wh.period_months) || 1, 1);
  const contractual = Number(wh.contractual_hours_total) || (monthlyHours * periodMonths);

  const mNet = recurrent + extraNet / periodMonths;             // visão mensal
  const mHours = monthlyHours + extraHours / periodMonths;      // visão mensal
  const pNet = recurrent * periodMonths + extraNet;             // acumulado
  const pHours = contractual + extraHours;                      // acumulado

  return {
    period_months: periodMonths,
    monthly: { net: round2(mNet), hours: round2(mHours), rate: mHours > 0 ? mNet / mHours : 0 },
    period:  { net: round2(pNet), hours: round2(pHours), rate: pHours > 0 ? pNet / pHours : 0 },
  };
}

function renderEffectiveHourly() {
  const st = effHourlyState;
  const views = st.views;
  const view = views && views[st.active];
  if (!view) return;
  const period = (views && views.period_months) || 0;
  const isPeriod = st.active === "period";
  const label = isPeriod
    ? "acumulado do período" + (period ? " (" + period + " meses)" : "")
    : "média mensal";
  safeText("kpiEffHourly", formatCurrency(view.rate));
  safeText("kpiEffHourlySub",
    formatCurrency(view.net) + " / " + (Number(view.hours) || 0).toFixed(1) + "h · " + label);
}

function initEffHourlyToggle() {
  const group = document.getElementById("effHourlyToggle");
  if (!group) return;
  const btns = Array.prototype.slice.call(group.querySelectorAll("[data-view]"));
  btns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      const v = btn.getAttribute("data-view");
      if (v !== "monthly" && v !== "period") return;
      effHourlyState.active = v;
      btns.forEach(function (b) { b.classList.toggle("active", b === btn); });
      renderEffectiveHourly();
    });
  });
}

// Planejador de Poupança — arredondamento estrito em centavos -----------
// Regra aprovada (espelha services/savings_service.py): baseSavings e bonus
// são arredondados a 2 casas ANTES da soma; total == soma dos exibidos.
function savingsPlanCalc(opts) {
  opts = opts || {};
  const monthlyNet = Number(opts.monthlyNet) || 0;
  const ratePct = Number(opts.ratePct) || 0;
  const months = Math.max(Number(opts.months) || 1, 1);
  const includeBonus = opts.includeBonus !== false;

  const monthlyRaw = monthlyNet * (ratePct / 100);
  const monthly = round2(monthlyRaw);
  const baseSavings = round2(monthlyRaw * months);

  let bonus = 0;
  if (includeBonus) {
    const bonusRaw = ((Number(opts.thirteenth) || 0) + (Number(opts.vacation) || 0)) *
      (ratePct / 100);
    bonus = round2(bonusRaw);
  }
  const total = round2(baseSavings + bonus);
  return {
    monthly: monthly, baseSavings: baseSavings, bonus: bonus, total: total, months: months,
  };
}

function applyAdvancedAnalytics(data) {
  data = data || {};
  const overtime = data.overtime || {};
  const taxes = data.tax_rates || {};
  const meta = data.meta || {};
  const pt = data.period_totals || {};
  const inssRate = Number(taxes.inss && taxes.inss.rate) || 0;
  const irrfRate = Number(taxes.irrf && taxes.irrf.rate) || 0;

  // Totais acumulados do período filtrado (bruto/líquido recebidos).
  safeText("kpiPeriodGross", formatCurrency(pt.gross));
  safeText("kpiPeriodNet", formatCurrency(pt.net));
  const ptLabel = buildPeriodTotalsLabel(pt);
  safeText("kpiPeriodGrossSub", ptLabel);
  safeText("kpiPeriodNetSub", ptLabel);

  safeText("kpiRecurrentNet", formatCurrency(meta.recurrent_net_pay));
  safeText("kpiRecurrentNetSub", "média de " + (meta.recurrent_net_months || 0) + " meses completos");

  safeText("kpiOvertimeVuln", formatPercent(overtime.ratio));
  safeText("kpiOvertimeVulnSub", formatCurrency((Number(overtime.total) || 0) + (Number(overtime.dsr_overtime) || 0)) + " em H.E./DSR");

  // Salário-Hora Efetivo — alterna entre "Visão Mensal" e "Acumulado do Período".
  effHourlyState.views = data.effective_hourly ||
    buildEffectiveViews(meta, overtime, data.work_hours || {});
  renderEffectiveHourly();

  safeText("kpiRetentionRate", formatPercent(inssRate + irrfRate));
  safeText("kpiRetentionSub", "INSS " + formatPercent(inssRate) + " + IRRF " + formatPercent(irrfRate));

  safeRender(function () { renderOvertimeBreakdown(overtime); });
  safeRender(function () { renderWorkHours(data); });
}

// Rótulo de contexto do período filtrado para os cartões de total
// (ex.: "competência 2026-04" ou "3 competência(s) · Empresa X").
function buildPeriodTotalsLabel(pt) {
  pt = pt || {};
  const parts = [];
  if (pt.month) parts.push("competência " + pt.month);
  else if (pt.months) parts.push(String(pt.months) + " competência(s)");
  if (pt.company) parts.push(pt.company);
  return parts.join(" · ") || "período filtrado";
}

// Jornada de Trabalho & Horas Extras --------------------------------
function renderWorkHours(data) {
  data = data || {};
  const wh = data.work_hours || {};
  const split = wh.overtime_split || {};
  const hoursFmt = function (v) {
    return Number(v || 0).toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + " h";
  };

  safeText("whContractualHours", hoursFmt(wh.contractual_hours_total));
  safeText("whContractualHoursSub",
    (Number(wh.monthly_hours) || 0) + " h/mês × " + (wh.period_months || 0) + " meses");
  safeText("whExtraHours", hoursFmt(wh.extra_hours_total));
  safeText("whExtraHoursSub", "H.E. + DSR no período");

  const tier1 = Number(split.tier1_pct) || 0;
  const tier2 = Number(split.tier2_pct) || 0;
  safeText("whTier1Label", split.tier1_label || "50/70%");
  safeText("whTier1Pct", formatPercent(tier1));
  safeText("whTier2Label", split.tier2_label || "100%");
  safeText("whTier2Pct", formatPercent(tier2));

  const bar1 = document.getElementById("whTier1Bar");
  const bar2 = document.getElementById("whTier2Bar");
  if (bar1) bar1.style.width = tier1 + "%";
  if (bar2) bar2.style.width = tier2 + "%";
}



function renderAnnualProjection(d) {
  if (!d || d.error) return;
  const incl = !!(d.includes_overtime) || (Number(d.avg_overtime_dsr) > 0);
  const t13 = d.thirteenth || {};
  const v13 = d.vacation_bonus || {};
  const useYtd = !!(d.use_ytd_model);

  const total = useYtd
    ? (Number(d.total_annual_projected) || Number(d.total_annual_take_home_incl_ot) || 0)
    : (Number(d.total_annual_take_home_incl_ot) || Number(d.total_annual_take_home) || 0);
  safeText("kpiAnnualInflow", formatCurrency(total));

  let sub;
  if (useYtd) {
    const yr = d.projection_year || String(new Date().getFullYear());
    sub = "Já recebido (" + yr + "): " + formatCurrency(d.realized_net_ytd) +
      " + " + (d.remaining_months || 0) + " meses restantes";
  } else {
    sub = "baseline " + formatCurrency(d.baseline_annual) + "/12m · Acumulado Global";
  }
  if (incl && Number(d.avg_overtime_dsr) > 0) sub += " · incl. H.E./DSR";
  safeText("kpiAnnualInflowSub", sub);

  // Alimenta o Planejador de Poupança com o líquido base e bônus (13º + férias).
  savingsData.monthlyNet = Number(d.monthly_net) || 0;
  savingsData.thirteenth = Number(t13.net_incl_ot) || Number(t13.net) || 0;
  savingsData.vacation = Number(v13.net_incl_ot) || Number(v13.net) || 0;
  if (savingsUpdater) savingsUpdater();

  // Nota explicativa: 13º/férias já com média de H.E./DSR (CLT).
  const noteEl = document.getElementById("annualChartNote");
  if (noteEl) {
    if (incl && Number(d.avg_overtime_dsr) > 0) {
      noteEl.classList.remove("d-none");
      noteEl.textContent =
        "13º e férias já incluem média de H.E./DSR (" + formatCurrency(d.avg_overtime_dsr) + "/mês)" +
        (d.retention_has_data ? " · líquido por alíquota real de retenção" : "");
    } else {
      noteEl.classList.add("d-none");
    }
  }

  safeRender(function () { renderAnnualWaterfall(d); });
}

// Projeção Tributária Anual (INSS & IRRF) ----------------------------
const TAX_METHOD_LABELS = {
  average: "Média Histórica",
  trend: "Tendência (Últimos 3M)",
  last_month: "Último Mês (Run-Rate)",
};

function renderTaxProjection(d) {
  d = d || {};
  const ytd = d.ytd || {};
  const projected = d.projected || {};
  const annual = d.annual || {};
  const monthly = d.monthly || {};
  const capped = !!(d.inss_capped || monthly.inss_capped);
  safeText("taxInssYtd", formatCurrency(ytd.inss));
  safeText("taxIrrfYtd", formatCurrency(ytd.irrf));
  safeText("taxInssProj", formatCurrency(projected.inss));
  safeText("taxIrrfProj", formatCurrency(projected.irrf));
  // Aviso explícito quando o teto anual de INSS foi atingido.
  const noteEl = document.getElementById("taxInssNote");
  if (noteEl) noteEl.textContent = capped ? "Teto do INSS atingido" : "";
  const methodLabel = TAX_METHOD_LABELS[d.method] || TAX_METHOD_LABELS.average;
  const inssNote = capped ? " · INSS já no teto anual (projeção zerada)" : "";
  safeText("taxSub",
    methodLabel + " · Mês " + (d.month_now || 0) + "/12 · " +
    (d.remaining_months || 0) + " meses restantes · " +
    "INSS mensal " + formatCurrency(monthly.inss) + " + IRRF mensal " + formatCurrency(monthly.irrf) +
    inssNote +
    " · Total anual INSS " + formatCurrency(annual.inss) + " / IRRF " + formatCurrency(annual.irrf));
}

async function loadTaxProjection() {
  const sel = document.getElementById("taxMethodSelect");
  const method = sel ? sel.value : "average";
  try {
    const d = await apiGet("/api/analytics/tax-projection?method=" + encodeURIComponent(method), {});
    renderTaxProjection(d);
  } catch (err) {
    console.error(err);
  }
}

function initTaxProjection() {
  const sel = document.getElementById("taxMethodSelect");
  if (!sel) return;
  sel.addEventListener("change", function () { loadTaxProjection(); });
}


function renderAnnualWaterfall(d) {
  const id = "annual-projection-chart";
  if (!getChartCanvas(id)) return;
  const t13 = d.thirteenth || {};
  const v13 = d.vacation_bonus || {};

  const realized = Number(d.realized_net_ytd) != null ? (Number(d.realized_net_ytd) || 0) : (Number(d.baseline_annual) || 0);
  const remaining = Number(d.remaining_projection_net) || 0;
  const thirteenth = Number(t13.net_incl_ot) || Number(t13.net) || 0;
  const vacation = Number(v13.net_incl_ot) || Number(v13.net) || 0;
  const total = Number(d.total_annual_projected) ||
    Number(d.total_annual_take_home_incl_ot) ||
    Number(d.total_annual_take_home) || 0;

  // ---- Audit / log da decomposição de `total_annual_projected` ----
  // total = Realizado YTD (meses 1..mês atual; NÃO é duplicado na projeção) +
  //         Projeção Restante ((12 − mês atual) × média mensal líquida) +
  //         13º Net + Férias (1/3) Net
  console.log("[AnnualProjection] breakdown", {
    realized_net_ytd: Number(d.realized_net_ytd) || 0,
    remaining_months: Number(d.remaining_months) || 0,
    monthly_projected_net: Number(d.monthly_projected_net) || 0,
    remaining_projection_net: Number(d.remaining_projection_net) || 0,
    thirteenth_net_incl_ot: Number(d.thirteenth && d.thirteenth.net_incl_ot) || 0,
    vacation_net_incl_ot: Number(d.vacation_bonus && d.vacation_bonus.net_incl_ot) || 0,
    total_annual_projected: Number(d.total_annual_projected) || 0,
  });

  const labels = ["Realizado YTD", "Projeção Restante", "13º Salário", "Férias (1/3)", "Total Anual Projetado"];
  const deltas = [realized, remaining, thirteenth, vacation, total];
  const floats = [
    [0, realized],
    [realized, realized + remaining],
    [realized + remaining, realized + remaining + thirteenth],
    [realized + remaining + thirteenth, realized + remaining + thirteenth + vacation],
    [0, total],
  ];
  const colors = ["#3b82f6", "#38bdf8", "#22c55e", "#22c55e", "#8b5cf6"];
  const isMobile = isChartMobile();

  mountChart(id, {
    type: "bar",
    data: {
      labels: labels,
      datasets: [{
        label: "Valor anual",
        data: floats,
        backgroundColor: colors,
        borderColor: colors,
        borderWidth: 1,
        borderRadius: 6,
        barPercentage: 0.5,
        categoryPercentage: 0.7,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      // Datalabel do topo (ex.: "+R$ 59.726,83") não pode ser cortado.
      layout: { padding: { top: 28 } },
      plugins: {
        legend: { display: false },
        datalabels: {
          display: function () { return dataLabelsAllowed(); },
          anchor: "end",
          align: "end",
          offset: 2,
          color: "#e2e8f0",
          clamp: true,
          font: { size: isMobile ? 9 : 11, weight: "700" },
          formatter: function (value, ctx) {
            if (ctx.dataIndex === 4) return formatCurrency(deltas[4]);
            if (ctx.dataIndex === 0) return "";
            return "+" + formatCurrency(deltas[ctx.dataIndex]);
          },
        },
        tooltip: chartTooltip({
          callbacks: {
            label: function (ctx) {
              const idx = ctx.dataIndex;
              if (idx === 4) return "Total: " + formatCurrency(deltas[4]);
              if (idx === 0) return "Realizado YTD: " + formatCurrency(deltas[0]);
              return labels[idx] + ": +" + formatCurrency(deltas[idx]);
            },
          },
        }),
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            // Exibe TODAS as 5 categorias sob suas barras (sem auto-skip).
            autoSkip: false,
            maxRotation: 45,
            minRotation: 0,
            autoSkipPadding: 4,
            font: { size: 11 },
          },
        },
        y: {
          beginAtZero: true,
          // grace extra p/ o datalabel do topo não encostar/cortar na borda.
          grace: "15%",
          grid: { color: "rgba(148,163,184,0.08)" },
          ticks: { callback: chartAxisMoney },
        },
      },
    },
  });
}


// Gráficos ------------------------------------------------------------
// Nomes/abreviações dos meses em PT-BR — usados nos rótulos do eixo X.
const PT_MONTHS_SHORT = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"];
const PT_MONTHS_FULL = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"];

function parseYmParts(value) {
  const m = String(value || "").trim().match(/^(\d{4})-(\d{2})/);
  if (!m) return null;
  return { year: parseInt(m[1], 10), monthIndex: parseInt(m[2], 10) - 1 };
}

// '2026-09' -> 'Set/26' (rótulo compacto do eixo X)
function monthShortLabel(value) {
  const p = parseYmParts(value);
  if (!p) return String(value || "");
  return PT_MONTHS_SHORT[p.monthIndex] + "/" + String(p.year).slice(2);
}

// '2026-09' -> 'Setembro/2026' (usado no tooltip, sem perder o ano)
function monthFullLabel(value) {
  const p = parseYmParts(value);
  if (!p) return String(value || "");
  return PT_MONTHS_FULL[p.monthIndex] + "/" + p.year;
}

function renderMonthlyTrend(series) {
  const id = "monthly-trend-chart";
  if (!getChartCanvas(id)) return;
  series = Array.isArray(series) ? series : [];
  const isMobile = isMobileViewport();

  // Rótulos curtos no eixo X; tooltip guarda mês/ano por extenso.
  const labels = series.map(function (s) { return monthShortLabel(s.mes); });
  const fullLabels = series.map(function (s) { return monthFullLabel(s.mes); });
  const gross = series.map(function (s) { return Number(s.gross) || 0; });
  const net = series.map(function (s) { return Number(s.net) || 0; });
  const isVacation = series.map(function (s) { return !!(s && s.is_vacation_month === true); });

  // Dataset de linha: curva suave (tension), gradiente sob a linha e pontos
  // arredondados — legível no desktop e no toque mobile.
  function lineDataset(label, color, colorFill, data) {
    return {
      label: label,
      data: data,
      borderColor: color,
      backgroundColor: lineAreaGradient(colorFill),
      fill: true,
      tension: 0.4,
      borderWidth: 3,
      pointRadius: isMobile ? 4 : 5,
      pointHoverRadius: isMobile ? 7 : 8,
      pointBackgroundColor: color,
      pointBorderColor: "#0f172a",
      pointBorderWidth: 1,
      pointHoverBackgroundColor: color,
      pointHoverBorderColor: "#0f172a",
    };
  }

  mountChart(id, {
    type: "line",
    data: {
      labels: labels,
      datasets: [
        lineDataset("Bruto", "#3b82f6", "rgba(59,130,246,0.35)", gross),
        lineDataset("Líquido", "#22c55e", "rgba(34,197,94,0.30)", net),
      ],
    },
    options: {
      responsive: true,              // redimensiona desktop <-> mobile
      maintainAspectRatio: false,
      interaction: { mode: "nearest", intersect: false },
      plugins: {
        legend: {
          position: "top",
          align: isMobile ? "center" : "end",
          labels: { color: "#e2e8f0", boxWidth: 10, padding: 12 },
        },
        vacationMarkers: { indexes: isVacation },   // 🌴 marcador de férias
        datalabels: {
          display: function () { return dataLabelsAllowed(); },
          color: "#94a3b8",
          anchor: "center",
          align: "top",
          offset: 6,
          font: { size: 10, weight: "600" },
          clamp: true,
          formatter: function (value) { return chartAxisMoney(value); },
        },
        tooltip: chartTooltip({
          intersect: false,
          displayColors: true,
          callbacks: {
            title: function (items) {
              const i = items.length ? items[0].dataIndex : 0;
              return fullLabels[i] + (isVacation[i] ? " \u{1F334}" : "");
            },
            label: function (ctx) {
              const v = Number(ctx.parsed.y) || 0;
              return ctx.dataset.label + ": " + formatCurrency(v);
            },
            afterBody: function (items) {
              const i = items.length ? items[0].dataIndex : -1;
              if (i < 0 || !isVacation[i]) return [];
              const s = series[i];
              return [
                "Líquido do Holerite: " + formatCurrency(s.net),
                "Adiantamento Quinzenal: " + formatCurrency(s.month_adiantamento_net),
                "Férias Antecipadas do Ciclo: " + formatCurrency(s.vacation_net_prepayment),
                "",
                "Fluxo de Caixa Total no Bolso: " + formatCurrency(s.total_effective_cashflow),
                "Líquido reduzido por abate de adiantamento de férias no ciclo",
              ];
            },
          },
        }),
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            maxRotation: 0,
            minRotation: 0,
            autoSkip: true,
            autoSkipPadding: 24,
            color: "#94a3b8",
          },
        },
        y: {
          beginAtZero: true,
          grid: { color: "rgba(148,163,184,0.10)" },
          ticks: { callback: chartAxisMoney, color: "#94a3b8" },
        },
      },
    },
  });
}

function renderDeductions(items) {
  items = Array.isArray(items) ? items : [];
  // Exclui /B02 (adiantamento), férias-imp (compensação) e valores zero.
  const isVacationClearing = /(f[ée]rias.*imp|abono.*imp|provis[aã]o\s*adia|provis[aã]o\s*cont)/i;
  const filtered = (items || []).filter(function (i) {
    return Number(i.total || 0) > 0
      && !/(b02|adto|quinzenal)/i.test(i.descricao || "")
      && !isVacationClearing.test(i.descricao || "");
  });
  const total = filtered.reduce(function (s, i) { return s + Number(i.total || 0); }, 0);

  // Benefícios principais sempre exibidos individualmente (mesmo que < 2%).
  const isCoreBenefit = /(inss|irrf|fretado|refeit|sa[úu]de|odonto|seguro de vida|parc cred|consignado|empre|taxa)/i;
  const MIN_PCT = 2.0;

  const shown = [];
  const small = [];
  filtered.forEach(function (i) {
    const pct = total ? (Number(i.total) / total * 100) : 0;
    if (pct >= MIN_PCT || isCoreBenefit.test(i.descricao || "")) {
      shown.push(i);
    } else {
      small.push(i);
    }
  });

  // Ordena do maior para o menor (topo -> base com autorange reverso).
  shown.sort(function (a, b) { return Number(b.total) - Number(a.total); });
  const smallSum = small.reduce(function (s, i) { return s + Number(i.total || 0); }, 0);
  if (smallSum > 0) shown.push({ descricao: "Outros", total: smallSum });

  const labels = shown.map(function (i) { return i.descricao; });
  const values = shown.map(function (i) { return Number(i.total || 0); });

  mountChart("deductions-breakdown-chart", {
    type: "bar",
    data: {
      labels: labels,
      datasets: [{
        label: "Desconto",
        data: values,
        backgroundColor: "#ef4444",
        hoverBackgroundColor: "#f87171",
        borderRadius: 6,
        barPercentage: 0.7,
        categoryPercentage: 0.8,
      }],
    },
    options: {
      indexAxis: "y",               // barras horizontais
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        datalabels: {
          display: function () { return dataLabelsAllowed(); },
          anchor: "end",
          align: "end",
          offset: 5,
          color: "#fda4af",
          font: { size: 11, weight: "700" },
          formatter: function (value) { return formatCurrency(value); },
        },
        tooltip: chartTooltip({
          callbacks: {
            label: function (ctx) {
              const v = Number(ctx.parsed.x) || 0;
              const pct = total ? (v / total * 100) : 0;
              return formatCurrency(v) + " (" + formatPercent(pct) + " do total de descontos)";
            },
          },
        }),
      },
      scales: {
        x: {
          beginAtZero: true,
          grid: { color: "rgba(148,163,184,0.08)" },
          ticks: { callback: chartAxisMoney },
        },
        y: { grid: { display: false } },
      },
    },
  });
}

// Tabela de holerites -------------------------------------------------
function renderHoleritesTable(items) {
  items = Array.isArray(items) ? items : [];
  const tbody = document.getElementById("holeritesTableBody");
  tbody.innerHTML = "";

  // Atualiza o contador no cabeçalho colapsável.
  const countEl = document.getElementById("holeriteCount");
  if (countEl) countEl.textContent = (items || []).length;

  if (!items.length) {
    tbody.innerHTML = [
      "<tr><td colspan=\"6\" class=\"text-center text-secondary py-4\">",
      "Nenhum holerite importado ainda. Envie um PDF acima.",
      "</td></tr>",
    ].join("");
    return;
  }

  items.forEach(function (h) {
    const totals = h.totals || {};
    const liquid = totals.net_value ? formatCurrency(totals.net_value) : "-";
    const gross = totals.total_earnings ? formatCurrency(totals.total_earnings) : "-";
    const tr = document.createElement("tr");
    tr.innerHTML = [
      "<td>" + escapeHtml(h.company_name) + "</td>",
      "<td>" + escapeHtml(h.mes_referencia || "-") + "</td>",
      "<td><span class=\"badge text-bg-secondary\">" + escapeHtml(h.tipo_documento || "-") + "</span></td>",
      "<td>" + liquid + "</td>",
      "<td>" + gross + "</td>",
      "<td class=\"text-end\">",
      "  <button type=\"button\" class=\"btn btn-sm btn-outline-primary me-1 btn-detail\" data-id=\"" + h.id + "\" title=\"Analisar holerite\">",
      "    <i class=\"bi bi-search me-1\"></i>Analisar",
      "  </button>",
      "  <button class=\"btn btn-sm btn-outline-danger btn-delete\" data-id=\"" + h.id + "\">",
      "    <i class=\"bi bi-trash\"></i>",
      "  </button>",
      "</td>",
    ].join("");
    tbody.appendChild(tr);
  });

  // Eventos de detalhe/exclusão (delegação simples).
  tbody.querySelectorAll(".btn-detail").forEach(function (btn) {
    btn.addEventListener("click", function () {
      openDetail(btn.getAttribute("data-id"));
    });
  });
  tbody.querySelectorAll(".btn-delete").forEach(function (btn) {
    btn.addEventListener("click", function () {
      deleteHolerite(btn.getAttribute("data-id"));
    });
  });
}

// Carregamento dos dados do dashboard --------------------------------


async function loadDashboard() {
  try {
    const requests = [
      apiGet("/api/monthly", []),
      apiGet("/api/descontos", []),
      apiGet("/api/holerites", []),
      apiGet("/api/analytics/projection", {}),
      apiGet("/api/analytics/audit", []),
    ];
    const results = await Promise.all(requests);
    // Cada render é isolado: um gráfico com erro não derruba os demais.
    safeRender(function () { renderMonthlyTrend(results[0]); });
    safeRender(function () { renderDeductions(results[1]); });
    safeRender(function () { renderHoleritesTable(results[2]); });
    safeRender(function () { populateAdvancedFilters(results[2]); });
    safeRender(function () { renderAnnualProjection(results[3]); });
    safeRender(function () { renderAnomalies(results[4]); });
    await loadTaxProjection(); // usa o método ativo no seletor (média por padrão)
    loadAdvancedAnalytics(advancedState.month, advancedState.company);
  } catch (err) {
    console.error(err);
    if (err.message !== "Não autenticado") {
      showToast("Falha ao carregar o dashboard.", "danger");
    }
  }
}



// Detalhe do holerite ------------------------------------------------
// Holerite ativo no modal de detalhe (para a IA por competência).
let currentDetailHoleriteId = null;

// "Entenda seu Holerite com IA": pergunta contextual de UM holerite.
function initPaystubAI() {
  const input = document.getElementById("paystubAiInput");
  const btn = document.getElementById("paystubAiAsk");
  if (!input || !btn) return;

  const ans = document.getElementById("paystubAiAnswer");
  const setBusy = function (busy) {
    if (!btn) return;
    btn.disabled = busy;
    btn.innerHTML = busy
      ? '<span class="spinner-border spinner-border-sm me-1"></span>Analisando...'
      : '<i class="bi bi-send me-1"></i>Perguntar';
  };

  async function submit() {
    const q = String(input.value || "").trim();
    if (!q) { showToast("Digite uma pergunta.", "warning"); return; }
    if (!currentDetailHoleriteId) { showToast("Selecione um holerite.", "warning"); return; }
    if (ans) {
      ans.classList.remove("d-none");
      ans.classList.add("text-secondary");
      ans.textContent = "Consultando a IA sobre esta competência...";
    }
    setBusy(true);
    try {
      const data = await apiPostJson("/api/analytics/explain-paystub", {
        holerite_id: currentDetailHoleriteId,
        question: q,
      });
      if (ans) {
        ans.classList.remove("text-secondary");
        ans.textContent = (data && data.answer) || "Sem resposta.";
      }
    } catch (err) {
      if (ans) {
        ans.classList.remove("d-none");
        ans.classList.add("text-secondary");
        ans.textContent = "";
      }
      if (err.status === 402 || err.status === 429) {
        openUpgradeModal(err.data && err.data.message);
      } else {
        showToast(err.message || "Falha ao consultar a IA.", "danger");
      }
    } finally {
      setBusy(false);
    }
  }

  btn.addEventListener("click", submit);
  input.addEventListener("keydown", function (e) { if (e.key === "Enter") submit(); });
}

async function openDetail(id) {
  currentDetailHoleriteId = id;
  const payAns = document.getElementById("paystubAiAnswer");
  if (payAns) { payAns.classList.add("d-none"); payAns.textContent = ""; }
  const payInput = document.getElementById("paystubAiInput");
  if (payInput) payInput.value = "";
  try {
    const data = await apiGet("/api/holerites/" + id);

    document.getElementById("detailModalTitle").textContent =
      "Holerite — " + (data.company_name || "Empresa") + " (" + (data.mes_referencia || "-") + ")";

    // Cards de totais.
    const t = data.totals || {};
    const totalsHtml = [
      totalCard("Proventos", t.total_earnings, ""),
      totalCard("Descontos", t.total_deductions, "danger"),
      totalCard("Líquido", t.net_value, "success"),
      totalCard("Base", t.base_salary, ""),
    ].join("");
    document.getElementById("detailTotals").innerHTML = totalsHtml;

    // Rubricas.
    const items = data.line_items || [];
    const tb = document.getElementById("detailLineItems");
    tb.innerHTML = items.length
      ? items.map(function (it) {
          const badge = String(it.tipo || "").toUpperCase() === "DESCONTO"
            ? "<span class=\"badge text-bg-danger\">DESCONTO</span>"
            : "<span class=\"badge text-bg-success\">PROVENTO</span>";
          return [
            "<tr>",
            "<td>" + escapeHtml(it.codigo || "-") + "</td>",
            "<td>" + escapeHtml(it.descricao) + "</td>",
            "<td>" + badge + "</td>",
            "<td class=\"text-end\">" + formatCurrency(it.valor) + "</td>",
            "</tr>",
          ].join("");
        }).join("")
      : "<tr><td colspan=\"4\" class=\"text-center text-secondary\">Sem rubricas.</td></tr>";

    // Texto bruto.
    document.getElementById("detailRawText").textContent = data.raw_text || "(sem texto)";

    bootstrap.Modal.getOrCreateInstance(document.getElementById("detailModal")).show();
  } catch (err) {
    showToast("Falha ao carregar o detalhe.", "danger");
  }
}

function totalCard(label, value, cls) {
  return [
    "<div class=\"col-6 col-md-3\">",
    "  <div class=\"border rounded p-2 text-center\">",
    "    <div class=\"small text-secondary\">" + label + "</div>",
    "    <div class=\"fw-bold " + cls + "\">" + formatCurrency(value) + "</div>",
    "  </div>",
    "</div>",
  ].join("");
}

// Exclusão do holerite -----------------------------------------------
async function deleteHolerite(id) {
  if (!window.confirm("Excluir este holerite? Esta ação não pode ser desfeita.")) {
    return;
  }
  try {
    const resp = await fetch("/api/holerites/" + id, { method: "DELETE", credentials: "same-origin" });
    if (resp.status === 401) {
      window.location.href = "/login";
      return;
    }
    const data = await safeParseJson(resp, {});
    if (!resp.ok) throw new Error(data.error || "Falha ao excluir.");
    showToast("Holerite excluído.");
    await loadDashboard();
  } catch (err) {
    showToast(err.message, "danger");
  }
}

// Upload com barra de progresso --------------------------------------
function initUpload() {
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("fileInput");
  if (!dropzone || !fileInput) {
    console.error("upload: dropzone/fileInput não encontrados.");
    return;
  }

  dropzone.addEventListener("click", function () {
    fileInput.click();
  });

  dropzone.addEventListener("dragover", function (e) {
    e.preventDefault();
    dropzone.classList.add("dragover");
  });
  dropzone.addEventListener("dragleave", function () {
    dropzone.classList.remove("dragover");
  });
  dropzone.addEventListener("drop", function (e) {
    e.preventDefault();
    dropzone.classList.remove("dragover");
    if (e.dataTransfer.files.length) {
      uploadFiles(Array.from(e.dataTransfer.files));
    }
  });
  fileInput.addEventListener("change", function () {
    if (fileInput.files.length) {
      uploadFiles(Array.from(fileInput.files));
    }
    fileInput.value = "";
  });

  // Processa um lote de arquivos sequencialmente e só recarrega o
  // dashboard após toda a fila terminar.
  async function uploadFiles(files) {
    const progressWrap = document.getElementById("uploadProgress");
    const progressBar = document.getElementById("uploadProgressBar");
    const status = document.getElementById("uploadStatus");
    const total = files.length;

    progressWrap.classList.remove("d-none");
    status.classList.remove("d-none");

    let ok = 0;
    for (let i = 0; i < total; i++) {
      const file = files[i];
      status.textContent = "Enviando " + (i + 1) + " de " + total + ": " + file.name + "...";
      progressBar.style.width = ((i / total) * 100).toFixed(0) + "%";
      try {
        const success = await uploadOne(file);
        if (success) ok++;
      } catch (err) {
        status.textContent = "Falha em " + file.name + ": " + err.message;
      }
    }

    progressBar.style.width = "100%";
    if (total === 1) {
      status.textContent = ok ? "Holerite importado com sucesso!" : "Falha no upload.";
    } else {
      status.textContent = ok + " de " + total + " arquivos importados.";
    }
    if (ok > 0) {
      showToast(
        total === 1
          ? "Holerite importado com sucesso!"
          : ok + " de " + total + " holerites importados.",
        ok === total ? "success" : "warning"
      );
    }

    // Só recarrega os dados do dashboard depois que a fila inteira terminou.
    if (ok > 0) {
      await loadDashboard();
    }

    setTimeout(function () {
      progressWrap.classList.add("d-none");
      status.classList.add("d-none");
      progressBar.style.width = "0%";
    }, 1200);
  }

  // Envia um único arquivo; resolve(true) em sucesso, reject(err) em falha.
  function uploadOne(file) {
    return new Promise(function (resolve, reject) {
      const form = new FormData();
      form.append("file", file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      xhr.withCredentials = true;

      xhr.upload.addEventListener("progress", function (e) {
        if (e.lengthComputable) {
          const pct = Math.round((e.loaded / e.total) * 100);
          const status = document.getElementById("uploadStatus");
          if (status) status.textContent = "Enviando: " + file.name + " (" + pct + "%)";
        }
      });

      xhr.onload = function () {
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch (err) { /* ignore */ }
        if (xhr.status === 402 || xhr.status === 429) {
          openUpgradeModal(data.message);
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(true);
        } else {
          reject(new Error(data.message || data.error || "HTTP " + xhr.status));
        }
      };

      xhr.onerror = function () {
        reject(new Error("Erro de rede."));
      };

      xhr.send(form);
    });
  }
}

// Análises Estratégicas & Indicadores --------------------------------
// Estado dos filtros das análises avançadas (mês e empresa).
const advancedState = { month: "", company: "" };

// Estado compartilhado entre seções (Planejador, Projeção Tributária...).
let savingsData = { monthlyNet: 0, thirteenth: 0, vacation: 0 };
let savingsUpdater = null;

function formatPercent(value) {
  const num = Number(value) || 0;
  return num.toFixed(2).replace(".", ",") + "%";
}


async function loadAdvancedAnalytics(selectedMonth, selectedCompany) {
  const params = new URLSearchParams();
  if (selectedMonth) params.append("mes", selectedMonth);
  if (selectedCompany) params.append("company", selectedCompany);
  const qs = params.toString();
  const url = "/api/analytics/advanced" + (qs ? "?" + qs : "");
  try {
    const data = await apiGet(url);
    applyAdvancedAnalytics(data);
  } catch (err) {
    console.error(err);
    if (err.message !== "Não autenticado") {
      showToast("Falha ao carregar indicadores.", "danger");
    }
  }
}


async function loadAnomalies(selectedMonth, selectedCompany) {
  const params = new URLSearchParams();
  if (selectedMonth) params.append("mes", selectedMonth);
  if (selectedCompany) params.append("company", selectedCompany);
  const qs = params.toString();
  const url = "/api/analytics/audit" + (qs ? "?" + qs : "");
  try {
    const flags = await apiGet(url, []);
    renderAnomalies(flags);
  } catch (err) {
    console.error(err);
    if (err.message !== "Não autenticado") {
      showToast("Falha ao carregar inconsistências.", "danger");
    }
  }
}


function renderOvertimeBreakdown(overtime) {
  const id = "overtime-breakdown-chart";
  if (!getChartCanvas(id)) return;
  overtime = overtime || {};
  const labels = Array.isArray(overtime.labels) ? overtime.labels : [];
  const values = Array.isArray(overtime.values) ? overtime.values : [];
  const outros = overtime.outros || { labels: [], values: [] };
  const outrosTotal = (outros.values || []).reduce(function (s, v) { return s + (Number(v) || 0); }, 0);
  const sum = values.reduce(function (s, v) { return s + (Number(v) || 0); }, 0);
  const colors = ["#3b82f6", "#f59e0b", "#a855f7", "#64748b"];

  mountChart(id, {
    type: "doughnut",
    data: {
      labels: labels,
      datasets: [{
        data: values.map(function (v) { return Number(v) || 0; }),
        backgroundColor: colors,
        borderColor: "#0f172a",
        borderWidth: 2,
        hoverOffset: 6,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "58%",
      plugins: {
        legend: {
          position: isMobileViewport() ? "bottom" : "right",
          labels: { boxWidth: 10, padding: 12, color: "#e2e8f0" },
        },
        datalabels: {
          display: function (ctx) {
            if (!dataLabelsAllowed()) return false;
            const v = Number(ctx.dataset.data[ctx.dataIndex]) || 0;
            const pct = sum ? (v / sum * 100) : 0;
            return pct > 5;                 // só fatias com área relevante
          },
          color: "#ffffff",
          anchor: "center",
          align: "center",
          font: { size: 12, weight: "800" },
          formatter: function (value) {
            const pct = sum ? (Number(value) / sum * 100) : 0;
            return Math.round(pct) + "%";
          },
        },
        tooltip: chartTooltip({
          displayColors: true,
          callbacks: {
            label: function (ctx) {
              const v = Number(ctx.raw) || 0;
              const pct = sum ? (v / sum * 100) : 0;
              return " " + formatCurrency(v) + " (" + formatPercent(pct) + ")";
            },
            afterBody: function (items) {
              const i = items.length ? items[0].dataIndex : -1;
              if (i < 0 || labels[i] !== "Outros Proventos") return [];
              return (outros.labels || []).map(function (sub, j) {
                const v = Number((outros.values || [])[j] || 0);
                const pct = outrosTotal ? (v / outrosTotal * 100) : 0;
                return sub + ": " + formatCurrency(v) + " (" + formatPercent(pct) + ")";
              });
            },
          },
        }),
      },
    },
  });
}



function populateAdvancedFilters(holerites) {
  const monthSelect = document.getElementById("advFilterMonth");
  const companySelect = document.getElementById("advFilterCompany");
  if (!monthSelect || !companySelect) return;

  const months = [];
  const companies = [];
  (holerites || []).forEach(function (h) {
    if (h.mes_referencia && months.indexOf(h.mes_referencia) === -1) {
      months.push(h.mes_referencia);
    }
    if (h.company_name && companies.indexOf(h.company_name) === -1) {
      companies.push(h.company_name);
    }
  });
  months.sort();
  companies.sort();

  const baseMonth = '<option value="">Todos os meses</option>';
  const baseCompany = '<option value="">Todas as empresas</option>';
  monthSelect.innerHTML = baseMonth +
    months.map(function (m) { return '<option value="' + escapeHtml(m) + '">' + escapeHtml(m) + "</option>"; }).join("");
  companySelect.innerHTML = baseCompany +
    companies.map(function (c) { return '<option value="' + escapeHtml(c) + '">' + escapeHtml(c) + "</option>"; }).join("");

  monthSelect.value = advancedState.month;
  companySelect.value = advancedState.company;
}

function initAdvancedFilters() {
  const monthSelect = document.getElementById("advFilterMonth");
  const companySelect = document.getElementById("advFilterCompany");

  if (monthSelect) {
    monthSelect.addEventListener("change", function () {
      advancedState.month = this.value;
      loadAdvancedAnalytics(advancedState.month, advancedState.company);
      loadAnomalies(advancedState.month, advancedState.company);
    });
  }
  if (companySelect) {
    companySelect.addEventListener("change", function () {
      advancedState.company = this.value;
      loadAdvancedAnalytics(advancedState.month, advancedState.company);
      loadAnomalies(advancedState.month, advancedState.company);
    });
  }
}

// Modal de perfil ----------------------------------------------------
function initProfile() {
  const modal = document.getElementById("profileModal");
  if (!modal) return;
  const contractSelect = document.getElementById("pfContractType");
  const rateLabel = document.getElementById("pfRateLabel");

  function toggleRateLabel() {
    const isHorista = contractSelect.value === "HORISTA";
    rateLabel.textContent = isHorista ? "Valor por Hora (R$)" : "Salário Base Mensal (R$)";
  }

  // Carrega o perfil ao abrir o modal.
  modal.addEventListener("show.bs.modal", async function () {
    try {
      const p = await apiGet("/api/profile");
      document.getElementById("pfAdmission").value = p.admission_date || "";
      document.getElementById("pfJobTitle").value = p.job_title || "";
      contractSelect.value = p.contract_type || "HORISTA";
      document.getElementById("pfBaseRate").value = p.base_rate || 0;
      document.getElementById("pfMonthlyHours").value = p.monthly_hours || 220;
      document.getElementById("pfDependents").value = p.irrf_dependents || 0;
      document.getElementById("pfFixedBenefits").value = p.fixed_benefits_deduction || 0;
      document.getElementById("pfOT1Rate").value = p.overtime_tier1_rate || 1.70;
      document.getElementById("pfOT1Limit").value = p.overtime_tier1_limit || 30;
      document.getElementById("pfOT2Rate").value = p.overtime_tier2_rate || 2.00;
      toggleRateLabel();
    } catch (err) {
      showToast("Falha ao carregar perfil.", "danger");
    }
  });

  contractSelect.addEventListener("change", toggleRateLabel);

  document.getElementById("profileForm").addEventListener("submit", async function (e) {
    e.preventDefault();
    try {
      const payload = {
        admission_date: document.getElementById("pfAdmission").value || null,
        job_title: document.getElementById("pfJobTitle").value || null,
        contract_type: contractSelect.value,
        base_rate: parseFloat(document.getElementById("pfBaseRate").value) || 0,
        monthly_hours: parseFloat(document.getElementById("pfMonthlyHours").value) || 220,
        irrf_dependents: parseInt(document.getElementById("pfDependents").value, 10) || 0,
        fixed_benefits_deduction: parseFloat(document.getElementById("pfFixedBenefits").value) || 0,
        overtime_tier1_rate: parseFloat(document.getElementById("pfOT1Rate").value) || 1.70,
        overtime_tier1_limit: parseFloat(document.getElementById("pfOT1Limit").value) || 30,
        overtime_tier2_rate: parseFloat(document.getElementById("pfOT2Rate").value) || 2.00,
      };
      const resp = await fetch("/api/profile", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      if (!resp.ok) throw new Error("Falha ao salvar perfil.");
      showToast("Perfil salvo com sucesso!");
      bootstrap.Modal.getOrCreateInstance(modal).hide();
      await loadDashboard(); // recalcula o líquido recorrente com o novo perfil
    } catch (err) {
      showToast(err.message, "danger");
    }
  });
}

// Overtime Simulator -------------------------------------------------


function initOvertimeSimulator() {
  const input = document.getElementById("otTargetAmount");
  const group = document.getElementById("otRateToggle");
  if (!input || !group) return;

  const els = {
    multipliers: document.getElementById("otMultipliersLabel"),
    requiredHours: document.getElementById("otRequiredHours"),
    requiredHint: document.getElementById("otRequiredHint"),
    gross: document.getElementById("otGrossExtra"),
    grossBreakdown: document.getElementById("otGrossBreakdown"),
    net: document.getElementById("otNet"),
    taxBiteCard: document.getElementById("otTaxBiteCard"),
    taxBitePct: document.getElementById("otTaxBitePct"),
    perHour: document.getElementById("otPerHour"),
    perHourLine: document.getElementById("otPerHourLine"),
  };
  const set = function (key, value) {
    if (els[key] && value !== undefined && value !== null) els[key].textContent = value;
  };

  const RATE_LABELS = { "1.7": "70%", "2.0": "100%" };

  function otHoursText(h) {
    const rounded = Math.round((Number(h) || 0) * 2) / 2;
    return rounded.toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + " h";
  }

  function activeRate() {
    const active = group.querySelector(".btn.active");
    const r = parseFloat((active && active.getAttribute("data-rate")) || "1.7");
    return (r === 2.0) ? 2.0 : 1.7;
  }

  function zero() {
    set("requiredHours", "0 h");
    set("requiredHint", "Informe a meta LÍQUIDA desejada.");
    set("gross", formatCurrency(0));
    set("grossBreakdown", "");
    set("net", formatCurrency(0));
    set("taxBiteCard", formatCurrency(0));
    set("taxBitePct", "");
    set("perHour", "R$ 0,00");
    set("perHourLine", "");
  }

  let timer = null;

  function update() {
    const mult = activeRate();
    const lbl = RATE_LABELS[String(mult)] || ("×" + mult.toFixed(2));
    set("multipliers", "H.E. a " + lbl + " (×" + mult.toFixed(2) + ") · meta LÍQUIDA");
    if (timer) clearTimeout(timer);
    timer = setTimeout(function () { fetchReverse(mult); }, 80);
  }

  async function fetchReverse(mult) {
    const targetNet = Math.max(Number(input.value) || 0, 0);
    try {
      const d = await apiGet("/api/analytics/overtime-reverse?net=" + targetNet + "&rate=" + mult);
      if (!d || typeof d !== "object") {
        set("perHourLine", "Configure o salário base no perfil para simular.");
        return;
      }
      if ((Number(d.hourly_rate) || 0) <= 0 || targetNet <= 0) {
        zero();
        if ((Number(d.hourly_rate) || 0) <= 0) {
          set("requiredHint", "Configure o salário base no perfil para simular.");
        }
        return;
      }

      const requiredHours = Number(d.required_hours) || 0;
      const heGross = Number(d.gross_extra) || 0;
      const dsr = Number(d.dsr) || 0;
      const grossTotal = Number(d.gross_total) || 0;
      const net = Number(d.net) || targetNet;
      const taxBite = Number(d.tax_bite) || 0;
      const mInss = Number(d.marginal_inss) || 0;
      const mIrrf = Number(d.marginal_irrf) || 0;
      const perHourNet = Number(d.net_per_hour) || (requiredHours > 0 ? net / requiredHours : 0);
      const dsrPct = (Number(d.dsr_rate_pct) >= 0) ? Number(d.dsr_rate_pct) : 18;

      set("requiredHours", otHoursText(requiredHours));
      set("requiredHint", "Meta líquida " + formatCurrency(targetNet) + " no bolso · HE bruta " + formatCurrency(heGross));
      set("gross", formatCurrency(grossTotal));
      set("grossBreakdown", "HE: " + formatCurrency(heGross) + " + DSR (" + dsrPct + "%): " + formatCurrency(dsr));
      set("net", formatCurrency(net));
      set("taxBiteCard", formatCurrency(taxBite));
      set("taxBitePct", "INSS " + formatCurrency(mInss) + " + IRRF " + formatCurrency(mIrrf));
      set("perHour", formatCurrency(perHourNet));
      set("perHourLine", "Recebe " + formatCurrency(net) + " por " + otHoursText(requiredHours) + " extras (líquido na mão).");
    } catch (err) {
      console.error("overtime reverse:", err);
      set("perHourLine", "Configure o salário base no perfil para simular.");
      set("taxBitePct", "");
      set("grossBreakdown", "");
    }
  }

  input.addEventListener("input", update);
  group.addEventListener("click", function (e) {
    const btn = e.target.closest("[data-rate]");
    if (!btn) return;
    group.querySelectorAll(".btn").forEach(function (b) { b.classList.remove("active"); });
    btn.classList.add("active");
    update();
  });
  update();
}



// Planejador de Poupança e Reserva -----------------------------------
function initSavingsPlanner() {
  const slider = document.getElementById("savingsSlider");
  if (!slider) return;

  const start = document.getElementById("savingsStart");
  const end = document.getElementById("savingsEnd");
  const bonusToggle = document.getElementById("savingsBonus");

  function pad(n) { return String(n).padStart(2, "0"); }
  function ym(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1); }

  // Padrão: do mês atual até 3 meses adiante.
  if (start && !start.value) {
    const now = new Date();
    start.value = ym(now);
    end.value = ym(new Date(now.getFullYear(), now.getMonth() + 3, 1));
  }

  function parseYm(v) {
    const m = String(v || "").match(/^(\d{4})-(\d{2})$/);
    if (!m) return null;
    return { year: parseInt(m[1], 10), month: parseInt(m[2], 10) };
  }

  function countMonths(a, b) {
    if (!a || !b) return 0;
    const diff = (b.year - a.year) * 12 + (b.month - a.month) + 1;
    return Math.max(diff, 1);
  }

  function update() {
    const rate = Number(slider.value) || 0;
    const a = parseYm(start.value);
    const b = parseYm(end.value);
    const months = countMonths(a, b);
    const bonusOn = !!(bonusToggle && bonusToggle.checked);

    // Regra aprovada de centavos: baseSavings e bonus são arredondados a 2
    // casas ANTES da soma (total == soma dos valores exibidos, sem 1 centavo).
    const plan = savingsPlanCalc({
      monthlyNet: savingsData.monthlyNet,
      ratePct: rate,
      months: months,
      thirteenth: savingsData.thirteenth,
      vacation: savingsData.vacation,
      includeBonus: bonusOn,
    });

    const set = function (id, v) { const el = document.getElementById(id); if (el) el.textContent = v; };
    set("savingsRateLabel", rate + "%");
    set("savingsMonthly", formatCurrency(plan.monthly));
    set("savingsMonths", String(plan.months));
    set("savingsBonusVal", formatCurrency(plan.bonus));
    set("savingsTotal", formatCurrency(plan.total));
    set("savingsTotalSub",
      "Depósito de " + formatCurrency(plan.monthly) + "/mês · " + plan.months + " mês(es)" +
      (plan.bonus > 0 ? " + bônus " + formatCurrency(plan.bonus) : ""));
  }

  slider.addEventListener("input", update);
  if (start) start.addEventListener("change", update);
  if (end) end.addEventListener("change", update);
  if (bonusToggle) bonusToggle.addEventListener("change", update);

  savingsUpdater = update;
  update();
}


// Alertas & Inconsistências ------------------------------------------
// Central de Inconsistências & Auditoria ----------------------------
function renderAnomalies(flags) {
  flags = Array.isArray(flags) ? flags : [];

  // Total R$ das discrepâncias monetárias detectadas (ex.: surtos de
  // benefícios — refeição, plano de saúde, fretado).
  const totalDiscrepancy = flags.reduce(function (sum, f) {
    const amt = (typeof f.amount === "number" && isFinite(f.amount))
      ? f.amount
      : extractAnomalyAmount(f.description);
    return sum + (amt || 0);
  }, 0);
  safeText("kpiAnomalyTotal", formatCurrency(totalDiscrepancy));
  safeText("kpiAnomalyTotalSub", "em discrepâncias identificadas");

  // Preenche o modal de detalhamento (itemizado) com as mesmas inconsistências.
  renderInconsistencyBreakdown(flags);
}

// Detalhamento itemizado do total de inconsistências (modal + relatório).
function renderInconsistencyBreakdown(flags) {
  flags = Array.isArray(flags) ? flags : [];
  const list = document.getElementById("incModalList");
  const empty = document.getElementById("incModalEmpty");
  const totalEl = document.getElementById("incModalTotal");

  const amountOf = function (f) {
    return (typeof f.amount === "number" && isFinite(f.amount))
      ? f.amount
      : extractAnomalyAmount(f.description);
  };
  const total = flags.reduce(function (sum, f) { return sum + (amountOf(f) || 0); }, 0);
  if (totalEl) totalEl.textContent = formatCurrency(total);

  if (!list) return;
  if (!flags.length) {
    list.innerHTML = "";
    if (empty) empty.classList.remove("d-none");
    return;
  }
  if (empty) empty.classList.add("d-none");

  const order = { HIGH: 0, MEDIUM: 1, LOW: 2 };
  const sorted = flags.slice().sort(function (a, b) {
    return (order[a.severity] === undefined ? 9 : order[a.severity]) -
           (order[b.severity] === undefined ? 9 : order[b.severity]);
  });

  list.innerHTML = sorted.map(function (f) {
    const tone = f.severity === "HIGH" ? "danger" : (f.severity === "MEDIUM" ? "warning" : "info");
    const amt = amountOf(f) || 0;
    const amtHtml = (amt > 0)
      ? '<span class="badge text-bg-danger">' + formatCurrency(amt) + '</span>'
      : '<span class="badge text-bg-secondary">R$ 0,00</span>';
    return [
      '<div class="list-group-item anomaly-row">',
      '  <div class="d-flex align-items-center gap-2 flex-wrap mb-1">',
      '    <span class="badge text-bg-' + tone + ' anomaly-sev">' + escapeHtml(f.severity || "INFO") + '</span>',
      '    <span class="badge text-bg-dark anomaly-cat">' + escapeHtml(f.category || "auditoria") + '</span>',
      '    <span class="small text-secondary anomaly-date">' + escapeHtml(f.month || "—") + '</span>',
      '    ' + amtHtml,
      '  </div>',
      '  <div class="anomaly-title">' + escapeHtml(f.title) + '</div>',
      '  <div class="small text-secondary anomaly-desc">' + escapeHtml(f.description) + '</div>',
      '</div>',
    ].join("");
  }).join("");
}

function extractVariance(description) {
  if (!description) return null;
  const matches = String(description).match(/(\d+(?:[.,]\d+)?)\s*%/g);
  if (!matches || matches.length < 2) return null;
  const parse = function (s) { return parseFloat(s.replace(/[^\d.,]/g, "").replace(",", ".")); };
  const values = matches.map(parse).filter(function (v) { return !isNaN(v); });
  if (values.length < 2) return null;
  return values[values.length - 1] - values[0];
}

function _parseBrlAmount(value) {
  // Converte valores monetários: "1234.56" (formato Python :.2f) ou
  // "1.234,56" (formato pt-BR) para número.
  const s = String(value).trim();
  if (!s) return NaN;
  if (s.indexOf(",") !== -1) return parseFloat(s.replace(/\./g, "").replace(",", "."));
  return parseFloat(s);
}

function extractAnomalyAmount(description) {
  // Procura pares monetários do tipo "R$ cur vs. R$ avg" (ex.: surtos de
  // benefícios) e devolve o impacto monetário (diferença absoluta), ou null.
  if (!description) return null;
  const m = String(description).match(/R\$\s*([\d.,]+)\s*vs\.?\s*R\$\s*([\d.,]+)/i);
  if (!m) return null;
  const cur = _parseBrlAmount(m[1]);
  const avg = _parseBrlAmount(m[2]);
  if (isNaN(cur) || isNaN(avg)) return null;
  return Math.abs(cur - avg);
}


// Análise Patrimonial Anual ------------------------------------------

// Inicialização -------------------------------------------------------

function initAI() {
  // Barra "Pergunte à IA".
  const aiInput = document.getElementById("aiQuestion");
  const aiBtn = document.getElementById("aiAskBtn");
  const submit = function () { askAI(aiInput ? aiInput.value : ""); };
  if (aiBtn) aiBtn.addEventListener("click", submit);
  if (aiInput) aiInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") submit();
  });

  // Pills de sugestão rápida.
  document.querySelectorAll(".ai-pill").forEach(function (p) {
    p.addEventListener("click", function () {
      const q = p.getAttribute("data-q") || "";
      if (aiInput) aiInput.value = q;
      askAI(q);
    });
  });

  // A "Central de Inconsistências & Auditoria" foi removida do layout; as
  // inconsistências agora são detalhadas exclusivamente no modal itemizado,
  // aberto pelo card KPI "Inconsistências (Total R$)".

  // Fecha o drawer de explicação da IA.
  const drawerClose = document.getElementById("aiDrawerClose");
  if (drawerClose) {
    drawerClose.addEventListener("click", function () {
      const drawer = document.getElementById("aiDrawer");
      if (drawer) drawer.classList.add("d-none");
    });
  }
}

// Fecha o dropdown do usuário ao acionar ações que abrem modal/links (UX limpa).
function initNavbar() {
  document.querySelectorAll(".user-menu .dropdown-item[data-bs-toggle='modal']").forEach(function (item) {
    item.addEventListener("click", function () {
      const dd = item.closest(".dropdown");
      if (dd && window.bootstrap) bootstrap.Dropdown.getOrCreateInstance(dd).hide();
    });
  });
}

document.addEventListener("DOMContentLoaded", function () {
  function safeInit(fn) {
    try {
      const maybe = fn();
      if (maybe && typeof maybe.catch === "function") {
        maybe.catch(function (err) { console.error("init error:", err); });
      }
    } catch (err) {
      console.error("init error:", err);
    }
  }
  // 1) Upload PRIMEIRO — nunca deve ser bloqueado por rede/gráficos.
  safeInit(initUpload);
  safeInit(initNavbar);
  safeInit(initAdvancedFilters);
  safeInit(initProfile);
  safeInit(initOvertimeSimulator);
  safeInit(initSavingsPlanner);
  safeInit(initTaxProjection);
  safeInit(initAI);
  safeInit(initPaystubAI);
  safeInit(initEffHourlyToggle);
  safeInit(initExplainButtons);
  safeInit(initViewToggles);
  safeInit(loadPlanInfo);
  safeInit(loadDashboard);
});



