/* =====================================================================
   dashboard.js
   Dashboard Financeiro Pessoal — Tema Escuro
   Lógica de frontend: upload com progresso, KPIs, gráficos Plotly,
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

function showToast(message, type) {
  type = type || "success";
  const toastEl = document.getElementById("appToast");
  toastEl.className = "toast align-items-center text-bg-" + type + " border-0";
  document.getElementById("appToastMsg").textContent = message;
  const toast = bootstrap.Toast.getOrCreateInstance(toastEl);
  toast.show();
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
  if (el && value !== undefined && value !== null) el.textContent = value;
}
function safeRender(fn) {
  try { fn(); } catch (err) { console.error("render error:", err); }
}



// Render Plotly isolado: um erro num gráfico nunca propaga para a página.
function plotChart(target, data, layout, opts) {
  const el = typeof target === "string" ? document.getElementById(target) : target;
  if (!el) { console.error("plotChart: elemento não encontrado:", target); return; }
  try {
    Plotly.newPlot(el, data, layout || {}, opts || {});
  } catch (err) {
    console.error("plotChart error:", err);
  }
}
// KPIs orientados à decisão -----------------------------------------

function applyAdvancedAnalytics(data) {
  data = data || {};
  const overtime = data.overtime || {};
  const taxes = data.tax_rates || {};
  const meta = data.meta || {};
  const inssRate = Number(taxes.inss && taxes.inss.rate) || 0;
  const irrfRate = Number(taxes.irrf && taxes.irrf.rate) || 0;

  safeText("kpiRecurrentNet", formatCurrency(meta.recurrent_net_pay));
  safeText("kpiRecurrentNetSub", "média de " + (meta.recurrent_net_months || 0) + " meses completos");

  safeText("kpiOvertimeVuln", formatPercent(overtime.ratio));
  safeText("kpiOvertimeVulnSub", formatCurrency((Number(overtime.total) || 0) + (Number(overtime.dsr_overtime) || 0)) + " em H.E./DSR");

  // Salário-Hora Efetivo = (Líquido + Líquido das H.E.) / (Horas + Horas extras).
  const netSalary = Number(meta.recurrent_net_pay) || 0;
  const extraNet = Number(overtime.net) ||
    ((Number(overtime.total) || 0) + (Number(overtime.dsr_overtime) || 0)) *
      (1 - (Number(meta.effective_tax_rate) || 0) / 100);
  const contractHours = Number(meta.monthly_hours) || 220;
  const extraHours = Number(overtime.extra_hours) || 0;
  const totalHours = contractHours + extraHours;
  const effHourly = totalHours > 0 ? (netSalary + extraNet) / totalHours : 0;
  safeText("kpiEffHourly", formatCurrency(effHourly));
  safeText("kpiEffHourlySub",
    formatCurrency(netSalary + extraNet) + " / " + totalHours.toFixed(1) + "h");

  safeText("kpiRetentionRate", formatPercent(inssRate + irrfRate));
  safeText("kpiRetentionSub", "INSS " + formatPercent(inssRate) + " + IRRF " + formatPercent(irrfRate));

  safeRender(function () { renderOvertimeBreakdown(overtime); });
}



function renderAnnualProjection(d) {
  if (!d || d.error) return;
  safeText("kpiAnnualInflow", formatCurrency(d.total_annual_take_home));
  safeText("kpiAnnualInflowSub", "baseline " + formatCurrency(d.baseline_annual) + "/12m");
  safeRender(function () { renderAnnualWaterfall(d); });
}


function renderAnnualWaterfall(d) {
  const el = document.getElementById("annual-projection-chart");
  if (!el) return;
  const baseline = Number(d.baseline_annual) || 0;
  const thirteenth = Number(d.thirteenth && d.thirteenth.net) || 0;
  const vacation = Number(d.vacation_bonus && d.vacation_bonus.net) || 0;
  const ppr = Number(d.ppr_estimate) || 0;
  const total = Number(d.total_annual_take_home) || 0;

  const trace = {
    type: "waterfall",
    orientation: "v",
    measure: ["relative", "relative", "relative", "relative", "total"],
    x: ["Baseline 12m", "13º Salário", "Férias (1/3)", "PPR/PLR", "Total Anual"],
    y: [baseline, thirteenth, vacation, ppr, total],
    connector: { line: { color: "#2b3a55" } },
    increasing: { marker: { color: "#22c55e" } },
    decreasing: { marker: { color: "#ef4444" } },
    totals: { marker: { color: "#3b82f6" } },
    text: [formatCurrency(baseline), "+" + formatCurrency(thirteenth), "+" + formatCurrency(vacation), "+" + formatCurrency(ppr), formatCurrency(total)],
    textposition: "outside",
    cliponaxis: false,
    hovertemplate: "%{x}<br>R$ %{y:,.2f}<extra></extra>",
  };
  const layout = {
    title: "",
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#e2e8f0" },
    // Padding superior extra para os valores flutuantes (+R$ ...) não serem
    // cortados pelo topo do canvas.
    margin: { t: 45, b: 70, l: 70, r: 20 },
    xaxis: { gridcolor: "#2b3a55", automargin: true },
    yaxis: { title: "R$", gridcolor: "#2b3a55", automargin: true },
    showlegend: false,
  };
  plotChart(el.id, [trace], layout, { responsive: true, displayModeBar: false });
}


// Gráficos ------------------------------------------------------------
function renderMonthlyTrend(series) {
  series = Array.isArray(series) ? series : [];
  const mes = series.map(function (s) { return s.mes; });
  const gross = series.map(function (s) { return s.gross; });
  const net = series.map(function (s) { return s.net; });

  const layout = {
    title: "",
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#e2e8f0" },
    margin: { t: 40, b: 40, l: 70, r: 70 },
    legend: { orientation: "h", y: 1.15 },
    xaxis: { title: "Competência", gridcolor: "#2b3a55", type: "category" },
    yaxis: {
      gridcolor: "#2b3a55",
      tickprefix: "R$ ",
      tickformat: ",.0f",
    },
  };

  const traces = [
    {
      x: mes,
      y: gross,
      name: "Bruto",
      type: "scatter",
      mode: "lines+markers",
      line: { color: "#3b82f6", width: 3 },
      connectgaps: false,
    },
    {
      x: mes,
      y: net,
      name: "Líquido",
      type: "scatter",
      mode: "lines+markers",
      line: { color: "#22c55e", width: 3 },
      connectgaps: false,
    },
  ];

  plotChart("monthly-trend-chart", traces, layout, { responsive: true });
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
  const pcts = values.map(function (v) { return total ? (v / total * 100) : 0; });

  const trace = {
    x: values,
    y: labels,
    type: "bar",
    orientation: "h",
    marker: { color: "#ef4444" },
    customdata: pcts,
    // Rótulos exatos (moeda) desenhados à direita de cada barra horizontal.
    text: values.map(function (v) { return formatCurrency(v); }),
    textposition: "outside",
    cliponaxis: false,
    hovertemplate: "%{y}<br>%{customdata:.1f}% do total de descontos<br>R$ %{x:,.2f}<extra></extra>",
  };

  const layout = {
    title: "",
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#e2e8f0" },
    margin: { t: 20, b: 40, l: 150, r: 60 },
    xaxis: {
      gridcolor: "#2b3a55",
      tickprefix: "R$ ",
      tickformat: ",.0f",
      automargin: true,
    },
    yaxis: { gridcolor: "#2b3a55", automargin: true, autorange: "reversed" },
    bargap: 0.3,
  };

  plotChart("deductions-breakdown-chart", [trace], layout, { responsive: true });
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
      "  <button class=\"btn btn-sm btn-outline-primary me-1 btn-detail\" data-id=\"" + h.id + "\">",
      "    <i class=\"bi bi-eye\"></i>",
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
    loadAdvancedAnalytics(advancedState.month, advancedState.company);
  } catch (err) {
    console.error(err);
    if (err.message !== "Não autenticado") {
      showToast("Falha ao carregar o dashboard.", "danger");
    }
  }
}



// Detalhe do holerite ------------------------------------------------
async function openDetail(id) {
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
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(true);
        } else {
          reject(new Error(data.error || "HTTP " + xhr.status));
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


function renderOvertimeBreakdown(overtime) {
  overtime = overtime || {};
  const labels = Array.isArray(overtime.labels) ? overtime.labels : [];
  const values = Array.isArray(overtime.values) ? overtime.values : [];
  const outros = overtime.outros || { labels: [], values: [] };
  const outrosTotal = (outros.values || []).reduce(function (s, v) { return s + (Number(v) || 0); }, 0);

  // Tooltip rico (multi-linha) para a fatia 'Outros Proventos'.
  const customdata = labels.map(function (label) {
    if (label === "Outros Proventos") {
      const lines = (outros.labels || []).map(function (sub, j) {
        const v = Number((outros.values || [])[j] || 0);
        const pct = outrosTotal ? (v / outrosTotal * 100) : 0;
        return sub + ": " + formatCurrency(v) + " (" + formatPercent(pct) + ")";
      });
      return lines.join("<br>");
    }
    return "";
  });

  const layout = {
    title: "",
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#e2e8f0" },
    // Legenda à direita, com margem extra p/ "DSR sobre Extras" não clippar.
    margin: { t: 20, b: 20, l: 20, r: 150 },
    legend: { orientation: "v", x: 1, y: 0.5, xanchor: "left", yanchor: "middle" },
    showlegend: true,
  };

  const trace = {
    labels: labels,
    values: values,
    type: "pie",
    hole: 0.5,
    textinfo: "label+percent",
    customdata: customdata,
    hovertemplate: "%{label}<br>%{customdata}<br><b>R$ %{value:,.2f}</b><extra></extra>",
    marker: {
      colors: ["#3b82f6", "#f59e0b", "#a855f7", "#64748b"],
    },
  };

  plotChart("overtime-breakdown-chart", [trace], layout, {
    responsive: true,
    displayModeBar: false,
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
    });
  }
  if (companySelect) {
    companySelect.addEventListener("change", function () {
      advancedState.company = this.value;
      loadAdvancedAnalytics(advancedState.month, advancedState.company);
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
  const slider = document.getElementById("otHoursSlider");
  if (!slider) return;

  // Percentual padrão do DSR (Descanso Semanal Remunerado) sobre o bruto de
  // horas extras. Usado como estimativa quando a razão exata de dias
  // trabalhados/não-trabalhados não é informada pela API.
  const DSR_RATE = 0.18;

  const els = {
    label: document.getElementById("otHoursLabel"),
    multipliers: document.getElementById("otMultipliersLabel"),
    gross: document.getElementById("otGrossExtra"),
    grossBreakdown: document.getElementById("otGrossBreakdown"),
    net: document.getElementById("otNet"),
    rate: document.getElementById("otEffectiveRate"),
    perHour: document.getElementById("otPerHour"),
    taxBite: document.getElementById("otTaxBite"),
  };
  const set = function (key, value) {
    if (els[key] && value !== undefined && value !== null) els[key].textContent = value;
  };

  let timer = null;

  function update() {
    const hours = Number(slider.value) || 0;
    set("label", hours + " h");
    if (timer) clearTimeout(timer);
    timer = setTimeout(fetchImpact, 80);
  }

  async function fetchImpact() {
    const hours = Number(slider.value) || 0;
    try {
      const d = await apiGet("/api/analytics/overtime-impact?hours=" + hours);
      if (!d || typeof d !== "object") {
        set("perHour", "Configure o salário base no perfil para simular.");
        return;
      }
      // Sem salário base configurado -> mensagem orientativa (não congela).
      if ((Number(d.hourly_rate) || 0) <= 0) {
        set("perHour", "Configure o salário base no perfil para simular.");
        set("gross", formatCurrency(0));
        set("grossBreakdown", "");
        set("net", formatCurrency(0));
        set("rate", "—");
        set("taxBite", "");
        return;
      }
      if (d.multipliers_label) set("multipliers", "Horas Extras: " + d.multipliers_label);

      const extraHours = Number(d.extra_hours) || 0;
      const overtimeGross = Number(d.gross_extra) || 0;

      // DSR (Descanso Semanal Remunerado) sobre o bruto das horas extras.
      // Estimativa padrão de 18%; se a API informar a razão exata de dias
      // trabalhados/não-trabalhados, usa-se dsr_days/working_days no lugar.
      const dsr = overtimeGross * DSR_RATE;
      const totalExtraGross = overtimeGross + dsr;

      // Recalcula a retenção marginal (INSS + IRRF) sobre o TOTAL extra
      // (H.E. + DSR) e deriva o líquido e a taxa efetiva por hora.
      const retentionPct = (Number(d.tax_bite_pct) || 0) / 100;
      const marginalTax = totalExtraGross * retentionPct;
      const netExtra = totalExtraGross - marginalTax;
      const perHourNet = extraHours > 0 ? netExtra / extraHours : 0;

      set("gross", formatCurrency(totalExtraGross));
      set("grossBreakdown",
        "HE: " + formatCurrency(overtimeGross) +
        " + DSR (" + Math.round(DSR_RATE * 100) + "%): " + formatCurrency(dsr));
      set("net", formatCurrency(netExtra));
      set("rate", extraHours > 0 ? formatCurrency(perHourNet) : "—");
      set("perHour", extraHours > 0
        ? "Líquido por hora extra (com DSR): " + formatCurrency(perHourNet)
        : "Arraste o slider para simular.");
      set("taxBite", extraHours > 0
        ? "Retenção marginal: " + formatCurrency(marginalTax) + " (" + formatPercent(d.tax_bite_pct) + ")"
        : "");
    } catch (err) {
      console.error("overtime sim:", err);
      set("perHour", "Configure o salário base no perfil para simular.");
      set("taxBite", "");
      set("grossBreakdown", "");
    }
  }

  slider.addEventListener("input", update);
  update();
}



// Alertas & Inconsistências ------------------------------------------
// Central de Inconsistências & Auditoria ----------------------------
function renderAnomalies(flags) {
  flags = Array.isArray(flags) ? flags : [];
  const list = document.getElementById("anomalyList");
  const empty = document.getElementById("anomalyEmpty");
  const count = document.getElementById("anomalyCount");

  // Total R$ das discrepâncias monetárias detectadas no painel de auditoria
  // (ex.: surtos de benefícios — refeição, plano de saúde, fretado).
  const totalDiscrepancy = flags.reduce(function (sum, f) {
    const amt = (typeof f.amount === "number" && isFinite(f.amount))
      ? f.amount
      : extractAnomalyAmount(f.description);
    return sum + (amt || 0);
  }, 0);
  safeText("kpiAnomalyTotal", formatCurrency(totalDiscrepancy));
  safeText("kpiAnomalyTotalSub", "em discrepâncias identificadas");

  if (!list) return;
  count.textContent = (flags || []).length;

  if (!flags || !flags.length) {
    list.innerHTML = "";
    empty.classList.remove("d-none");
    return;
  }
  empty.classList.add("d-none");

  // Ordena por severidade (HIGH > MEDIUM > LOW).
  const order = { HIGH: 0, MEDIUM: 1, LOW: 2 };
  const sorted = (flags.slice()).sort(function (a, b) {
    return (order[a.severity] === undefined ? 9 : order[a.severity]) -
           (order[b.severity] === undefined ? 9 : order[b.severity]);
  });

  list.innerHTML = sorted.map(function (f) {
    const tone = f.severity === "HIGH" ? "danger" : (f.severity === "MEDIUM" ? "warning" : "info");
    const variance = extractVariance(f.description);
    const varianceHtml = variance === null
      ? ""
      : '<span class="badge text-bg-' + (Math.abs(variance) >= 5 ? "danger" : "secondary") + ' anomaly-var">Δ ' +
        Math.abs(variance).toFixed(1).replace(".", ",") + " p.p.</span>";
    return [
      '<div class="list-group-item anomaly-row">',
      '  <div class="d-flex align-items-center gap-2 flex-wrap mb-1">',
      '    <span class="badge text-bg-' + tone + ' anomaly-sev">' + escapeHtml(f.severity || "INFO") + '</span>',
      '    <span class="badge text-bg-dark anomaly-cat">' + escapeHtml(f.category || "auditoria") + '</span>',
      '    <span class="small text-secondary anomaly-date">' + escapeHtml(f.month || "—") + '</span>',
      '    ' + varianceHtml,
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
  safeInit(initAdvancedFilters);
  safeInit(initProfile);
  safeInit(initOvertimeSimulator);
  safeInit(loadDashboard);
});



