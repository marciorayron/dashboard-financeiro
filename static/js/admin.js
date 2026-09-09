/* =====================================================================
   admin.js
   Painel Administrativo (/admin) — SaaS Backoffice em abas.
   Gestão de usuários & assinaturas (CRM), consumo de IA (FinOps),
   auditoria de parsing & armazenamento, e configurações globais.
   Backend garante 403 para quem não for 'admin' (@admin_required).
   ===================================================================== */
"use strict";

const Admin = (function () {
  // Estado local (cache) para filtros/busca na tabela de usuários.
  let usersCache = [];
  let meId = window.ADMIN_ME_ID || null;
  // Totais de tokens (input/output) para recálculo dinâmico de custo (FinOps).
  let aiUsageState = { input_tokens: 0, output_tokens: 0, estimated_cost: 0 };

  // Helpers ------------------------------------------------------------
  function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    const div = document.createElement("div");
    div.textContent = String(str);
    return div.innerHTML;
  }

  function formatCurrency(value) {
    const num = Number(value) || 0;
    return num.toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
  }

  function formatUsd(value) {
    const num = Number(value) || 0;
    return num.toLocaleString("en-US", { style: "currency", currency: "USD" });
  }

  function formatNumber(value) {
    return Number(value || 0).toLocaleString("pt-BR");
  }

  function formatBytes(bytes) {
    bytes = Number(bytes) || 0;
    if (bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    return (bytes / Math.pow(1024, i)).toFixed(1).replace(".", ",") + " " + units[i];
  }

  function toast(message, type) {
    type = type || "success";
    const el = document.getElementById("adminToast");
    el.className = "toast align-items-center text-bg-" + type + " border-0";
    document.getElementById("adminToastMsg").textContent = message;
    bootstrap.Toast.getOrCreateInstance(el).show();
  }

  async function safeParseJson(resp, fallback) {
    const fb = (fallback === undefined) ? {} : fallback;
    try {
      return await resp.json();
    } catch (err) {
      console.warn("[safeParseJson] Falha ao interpretar JSON de " + resp.url + ": " + err.message, err);
      return fb;
    }
  }

  async function api(url, options) {
    options = options || {};
    options.credentials = "same-origin";
    options.headers = options.headers || {};
    options.headers["Content-Type"] = "application/json";
    const resp = await fetch(url, options);
    if (resp.status === 401) {
      window.location.href = "/login";
      throw new Error("Não autenticado");
    }
    if (resp.status === 403) {
      const data = await safeParseJson(resp, {});
      throw new Error(data.error || "Acesso restrito a administradores.");
    }
    if (!resp.ok) {
      let msg = "Erro na API: " + resp.status;
      const data = await safeParseJson(resp, null);
      if (data && data.error) msg = data.error;
      if (data && data.message) msg = data.message;
      throw new Error(msg);
    }
    return safeParseJson(resp, {});
  }

  function planBadge(plan) {
    return plan === "pro"
      ? '<span class="badge text-bg-warning"><i class="bi bi-stars me-1"></i>Pró ✨</span>'
      : '<span class="badge text-bg-secondary">Free</span>';
  }

  function statusBadge(active) {
    return active
      ? '<span class="badge text-bg-success">Ativo</span>'
      : '<span class="badge text-bg-danger">Suspenso</span>';
  }

  // Métricas / KPIs da barra de resumo --------------------------------
  async function loadMetrics() {
    const s = await api("/admin/api/stats");
    const u = s.users || {};
    const ai = s.ai || {};
    const st = s.storage || {};
    aiUsageState.input_tokens = Number(ai.input_tokens) || 0;
    aiUsageState.output_tokens = Number(ai.output_tokens) || 0;
    aiUsageState.estimated_cost = Number(ai.estimated_cost) || 0;

    document.getElementById("kpiUsers").textContent = formatNumber(u.total);
    document.getElementById("kpiPlanSplit").textContent =
      "Free " + formatNumber(u.free) + " · Pró " + formatNumber(u.pro);
    document.getElementById("kpiPaystubs").textContent = formatNumber((s.paystubs || {}).total);
    document.getElementById("kpiAiQueries").textContent = formatNumber(ai.queries_24h);
    document.getElementById("kpiAiCost").textContent =
      "custo estimado " + formatUsd(ai.estimated_cost) + " · " + formatNumber(ai.estimated_tokens) + " tokens";
    document.getElementById("kpiStorage").textContent = formatBytes(st.bytes);
    document.getElementById("kpiDbHealth").textContent =
      "banco " + (s.db && s.db.status === "ok" ? "online" : "erro") +
      " · " + formatNumber(st.pdf_count) + " PDFs";
  }

  // Usuários & Assinaturas (CRM) --------------------------------------
  async function loadUsers() {
    usersCache = await api("/admin/api/users");
    applyUserFilters();
  }

  function applyUserFilters() {
    const search = (document.getElementById("userSearch").value || "").trim().toLowerCase();
    const plan = document.getElementById("planFilter").value;
    const status = document.getElementById("statusFilter").value;

    const filtered = usersCache.filter(function (u) {
      const matchSearch = !search ||
        (u.name && u.name.toLowerCase().indexOf(search) !== -1) ||
        (u.email && u.email.toLowerCase().indexOf(search) !== -1);
      const matchPlan = !plan || u.plan === plan;
      const matchStatus = status === "" || String(u.is_active ? "1" : "0") === status;
      return matchSearch && matchPlan && matchStatus;
    });

    renderUsers(filtered);
  }

  function renderUsers(users) {
    const tbody = document.querySelector("#usersTable tbody");
    tbody.innerHTML = "";
    users.forEach(function (u) {
      const tr = document.createElement("tr");
      const isMe = u.id === meId;

      // O dropdown "Ações" é renderizado para TODOS os usuários. Para a própria
      // conta (admin logado) só expomos ações não destrutivas: não há como
      // suspender/excluir/alterar o próprio papel.
      const menuItems = [];
      const toggleTo = u.plan === "pro" ? "free" : "pro";
      const planLabel = u.plan === "pro" ? "Reverter para Free" : "Mudar para Pró ✨";
      menuItems.push(
        '<button class="dropdown-item" onclick="Admin.changePlan(' + u.id + ',\'' + toggleTo + '\')">' +
        '<i class="bi bi-stars me-2"></i>' + planLabel + '</button>'
      );

      // Ações não destrutivas (permitidas também para a própria conta).
      menuItems.push('<li><hr class="dropdown-divider"></li>');
      menuItems.push(
        '<button class="dropdown-item" onclick="Admin.resetPassword(' + u.id + ')">' +
        '<i class="bi bi-key me-2"></i>Redefinir Senha</button>'
      );
      menuItems.push(
        '<button class="dropdown-item" onclick="Admin.resetUsage(' + u.id + ')">' +
        '<i class="bi bi-arrow-counterclockwise me-2"></i>Zerar Uso de IA</button>'
      );
      menuItems.push(
        '<button class="dropdown-item" onclick="Admin.resetPaystubs(' + u.id + ')">' +
        '<i class="bi bi-file-earmark-x me-2"></i>Resetar Holerites</button>'
      );

      // Ações que só fazem sentido para OUTROS usuários (ocultas na própria).
      if (!isMe) {
        menuItems.push('<li><hr class="dropdown-divider"></li>');
        menuItems.push(
          '<button class="dropdown-item" onclick="Admin.toggleActive(' + u.id + ')">' +
          '<i class="bi bi-power me-2"></i>' + (u.is_active ? "Suspender Conta" : "Ativar Conta") + '</button>'
        );
        menuItems.push(
          '<button class="dropdown-item" onclick="Admin.changeRole(' + u.id + ',\'' + (u.role === "admin" ? "user" : "admin") + '\')">' +
          '<i class="bi bi-person-lines-fill me-2"></i>' + (u.role === "admin" ? "Rebaixar para user" : "Promover para admin") + '</button>'
        );
        menuItems.push('<li><hr class="dropdown-divider"></li>');
        menuItems.push(
          '<button class="dropdown-item text-danger" onclick="Admin.deleteUser(' + u.id + ')">' +
          '<i class="bi bi-trash me-2"></i>Excluir Usuário</button>'
        );
      }

      const actions =
        '<div class="dropdown admin-row-actions">' +
        '<button class="btn btn-sm btn-outline-secondary dropdown-toggle" type="button" data-bs-toggle="dropdown" aria-expanded="false" title="Ações do usuário">' +
        '<i class="bi bi-gear me-1"></i>Ações</button>' +
        '<ul class="dropdown-menu dropdown-menu-end">' +
        '  <li><h6 class="dropdown-header"><i class="bi bi-person me-1"></i>' + (isMe ? "Sua conta (você)" : escapeHtml(u.name)) + '</h6></li>' +
        menuItems.join("") +
        '</ul></div>';

      tr.innerHTML =
        "<td>" + escapeHtml(u.id) + "</td>" +
        "<td>" + escapeHtml(u.name) + (isMe ? ' <span class="badge text-bg-info">você</span>' : "") + "</td>" +
        "<td><small>" + escapeHtml(u.email) + "</small></td>" +
        "<td>" + planBadge(u.plan) + "</td>" +
        "<td>" + formatNumber(u.paystubs) + "</td>" +
        "<td>" + formatNumber(u.ai_24h) + " / " + formatNumber(u.ai_1h) + "</td>" +
        "<td>" + statusBadge(u.is_active) + "</td>" +
        '<td class="text-end text-nowrap">' + actions + "</td>";
      tbody.appendChild(tr);
    });

    const countEl = document.getElementById("usersCount");
    if (countEl) countEl.textContent = users.length + " usuário(s) exibido(s) de " + usersCache.length;
  }

  async function changePlan(userId, plan) {
    try {
      await api("/admin/api/users/" + userId + "/plan", {
        method: "POST",
        body: JSON.stringify({ plan: plan }),
      });
      toast(plan === "pro" ? "Usuário promovido ao Plano Pró ✨." : "Usuário revertido para o Plano Free.", "success");
      loadUsers();
      loadMetrics();
    } catch (e) { toast(e.message, "danger"); }
  }

  async function resetUsage(userId) {
    if (!confirm("Zerar o consumo de IA deste usuário? (suporte)")) return;
    try {
      const r = await api("/admin/api/users/" + userId + "/reset-usage", { method: "POST" });
      toast("Consumo zerado (" + r.queries_cleared + " consultas limpas).");
      loadUsers();
      loadAIUsage();
      loadMetrics();
    } catch (e) { toast(e.message, "danger"); }
  }

  async function toggleActive(userId) {
    if (userId === meId) { toast("Não é possível alterar a própria conta.", "warning"); return; }
    try {
      await api("/admin/api/users/" + userId + "/toggle-active", { method: "POST" });
      toast("Status do usuário atualizado.");
      loadUsers();
      loadMetrics();
    } catch (e) { toast(e.message, "danger"); }
  }

  async function changeRole(userId, role) {
    if (userId === meId) { toast("Não é possível alterar o próprio papel.", "warning"); return; }
    try {
      await api("/admin/api/users/" + userId + "/role", {
        method: "POST",
        body: JSON.stringify({ role: role }),
      });
      toast(role === "admin" ? "Usuário promovido a admin." : "Usuário rebaixado para user.");
      loadUsers();
    } catch (e) { toast(e.message, "danger"); }
  }

  async function resetPassword(userId) {
    try {
      const r = await api("/admin/api/users/" + userId + "/reset-password", { method: "POST" });
      prompt("Senha temporária (exiba uma vez ao usuário):", r.temporary_password || "");
      toast("Senha redefinida.");
    } catch (e) { toast(e.message, "danger"); }
  }

  // Consumo de IA & FinOps -------------------------------------------
  async function loadAIUsage() {
    const data = await api("/admin/api/ai-usage");

    // Daily breakdown
    const dailyBody = document.querySelector("#aiDailyTable tbody");
    dailyBody.innerHTML = "";
    (data.daily || []).forEach(function (d) {
      const tr = document.createElement("tr");
      tr.innerHTML = "<td>" + escapeHtml(d.date) + "</td><td class='text-end'>" + formatNumber(d.queries) + "</td>";
      dailyBody.appendChild(tr);
    });

    // Leaderboard
    const leadBody = document.querySelector("#aiLeaderboardTable tbody");
    leadBody.innerHTML = "";
    (data.leaderboard || []).forEach(function (u, i) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + (i + 1) + "</td>" +
        "<td>" + escapeHtml(u.user_name) + " <small class='text-secondary'>&lt;" + escapeHtml(u.user_email) + "&gt;</small></td>" +
        "<td>" + planBadge(u.plan) + "</td>" +
        "<td class='text-end'>" + formatNumber(u.queries) + "</td>";
      leadBody.appendChild(tr);
    });

    // Blocked injections
    const blockedBody = document.querySelector("#aiBlockedTable tbody");
    blockedBody.innerHTML = "";
    const blocked = data.blocked || [];
    blocked.forEach(function (b) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td class='text-nowrap'>" + escapeHtml(b.created_at) + "</td>" +
        "<td>" + escapeHtml(b.user_name) + " <small class='text-secondary'>&lt;" + escapeHtml(b.user_email) + "&gt;</small></td>" +
        "<td><span class='badge text-bg-danger'>" + escapeHtml(b.reason) + "</span></td>" +
        "<td><small class='text-secondary'>" + escapeHtml(b.detail) + "</small></td>";
      blockedBody.appendChild(tr);
    });
    const note = document.getElementById("aiBlockedNote");
    if (note) note.textContent = blocked.length + " tentativa(s) registrada(s).";

    // KPI cards da aba FinOps (reutiliza /admin/api/stats para tokens/custo)
    try {
      const s = await api("/admin/api/stats");
      const ai = s.ai || {};
      document.getElementById("aiTotal").textContent = formatNumber(ai.total_queries);
      document.getElementById("aiModel").textContent = escapeHtml(ai.model || "—");
      document.getElementById("aiInputTokens").textContent = formatNumber(ai.input_tokens);
      document.getElementById("aiOutputTokens").textContent = formatNumber(ai.output_tokens);
      document.getElementById("aiCost").textContent = formatUsd(ai.estimated_cost);
      document.getElementById("aiBlockedTotal").textContent = formatNumber(ai.blocked_total);
      aiUsageState.input_tokens = Number(ai.input_tokens) || 0;
      aiUsageState.output_tokens = Number(ai.output_tokens) || 0;
      aiUsageState.estimated_cost = Number(ai.estimated_cost) || 0;
      updateAIConfigPreview();
    } catch (err) { console.error(err); }
  }

  // Auditoria: erros de parsing ----------------------------------------
  async function loadErrors() {
    const errors = await api("/admin/api/errors?resolved=0");
    const empty = document.getElementById("parseErrorsEmpty");
    const table = document.getElementById("parseErrorsTable");
    const tbody = table.querySelector("tbody");
    tbody.innerHTML = "";
    const show = errors && errors.length > 0;
    empty.classList.toggle("d-none", show);
    table.classList.toggle("d-none", !show);
    if (!show) return;

    errors.forEach(function (e) {
      const tr = document.createElement("tr");
      const user = e.user_name
        ? escapeHtml(e.user_name) + " <small class='text-secondary'>&lt;" + escapeHtml(e.user_email) + "&gt;</small>"
        : "—";
      tr.innerHTML =
        "<td class='text-nowrap'>" + escapeHtml(e.created_at) + "</td>" +
        "<td>" + user + "</td>" +
        "<td>" + escapeHtml(e.filename) + "</td>" +
        "<td><span class='badge text-bg-danger'>" + escapeHtml(e.error_type || "Erro") + "</span> " +
          "<div class='small text-secondary'>" + escapeHtml(e.error_message) + "</div></td>" +
        "<td><pre class='mb-0 small' style='max-height:80px;overflow:auto;white-space:pre-wrap'>" +
          escapeHtml(e.traceback || "") + "</pre></td>" +
        "<td class='text-end'><button class='btn btn-sm btn-outline-success' onclick='Admin.resolveError(" + e.id + ")'>" +
          "<i class='bi bi-check-lg me-1'></i>Resolver</button></td>";
      tbody.appendChild(tr);
    });
  }

  async function resolveError(errorId) {
    if (!confirm("Marcar este erro de parsing como resolvido?")) return;
    try {
      await api("/admin/api/errors/" + errorId + "/resolve", { method: "POST" });
      toast("Erro marcado como resolvido.");
      loadErrors();
      loadMetrics();
    } catch (e) { toast(e.message, "danger"); }
  }

  // Gestão de armazenamento -------------------------------------------
  async function loadStorage() {
    const s = await api("/admin/api/storage");
    document.getElementById("storageTotal").textContent = formatBytes(s.total_bytes);
    document.getElementById("storageFiles").textContent = formatNumber(s.file_count);
    const tbody = document.querySelector("#storageTable tbody");
    tbody.innerHTML = "";
    (s.files || []).forEach(function (f) {
      const tr = document.createElement("tr");
      tr.innerHTML = "<td class='text-truncate' style='max-width:520px'>" + escapeHtml(f.path) + "</td>" +
        "<td class='text-end text-nowrap'>" + formatBytes(f.bytes) + "</td>";
      tbody.appendChild(tr);
    });
  }

  async function cleanupStorage() {
    if (!confirm("Remover arquivos órfãos/temporários não referenciados?")) return;
    try {
      const r = await api("/admin/api/storage/cleanup", { method: "POST" });
      toast(r.removed + " arquivo(s) órfão(s) removidos (" + formatBytes(r.freed_bytes) + ").");
      loadStorage();
      loadMetrics();
    } catch (e) { toast(e.message, "danger"); }
  }

  // Limites Freemium (dinâmicos) --------------------------------------
  async function loadFreemiumLimits() {
    const l = await api("/admin/api/freemium-limits");
    document.getElementById("ffPaystubLimit").value = l.free_paystub_limit;
    document.getElementById("ffFreeAiDaily").value = l.free_ai_queries_per_day;
    document.getElementById("ffProAiHourly").value = l.pro_ai_queries_per_hour;
  }

  async function saveFreemiumLimits() {
    const payload = {
      free_paystub_limit: parseInt(document.getElementById("ffPaystubLimit").value, 10) || 0,
      free_ai_queries_per_day: parseInt(document.getElementById("ffFreeAiDaily").value, 10) || 0,
      pro_ai_queries_per_hour: parseInt(document.getElementById("ffProAiHourly").value, 10) || 0,
    };
    try {
      await api("/admin/api/freemium-limits", { method: "PUT", body: JSON.stringify(payload) });
      toast("Limites Freemium salvos com sucesso.");
    } catch (e) { toast(e.message, "danger"); }
  }

  // Faixas de impostos -------------------------------------------------
  let taxState = { inss: [], irrf: [], dependent_deduction: 0 };

  async function loadTaxes() {
    const t = await api("/admin/api/settings/taxes");
    taxState = t;
    renderBracketRows("inss", t.inss);
    renderBracketRows("irrf", t.irrf);
    document.getElementById("dependentDeduction").value = t.dependent_deduction;
  }

  function renderBracketRows(type, rows) {
    const holder = document.getElementById(type + "Editor");
    holder.innerHTML = "";
    rows.forEach(function (row) {
      const div = document.createElement("div");
      div.className = "row g-2 mb-2 align-items-center";
      div.innerHTML =
        '<div class="col-4"><input type="number" step="0.01" min="0" class="form-control form-control-sm bracket-limit" value="' + row[0] + '" placeholder="Teto" /></div>' +
        '<div class="col-4"><input type="number" step="0.001" min="0" max="1" class="form-control form-control-sm bracket-rate" value="' + row[1] + '" placeholder="Alíquota" /></div>' +
        '<div class="col-3"><input type="number" step="0.01" min="0" class="form-control form-control-sm bracket-ded" value="' + row[2] + '" placeholder="Dedução" /></div>' +
        '<div class="col-1 text-end"><button class="btn btn-sm btn-outline-danger" onclick="Admin.removeRow(this)"><i class="bi bi-x-lg"></i></button></div>';
      holder.appendChild(div);
    });
  }

  function collectBrackets(type) {
    const holder = document.getElementById(type + "Editor");
    const rows = [];
    holder.querySelectorAll(".row").forEach(function (rowEl) {
      const limit = parseFloat(rowEl.querySelector(".bracket-limit").value);
      const rate = parseFloat(rowEl.querySelector(".bracket-rate").value);
      const ded = parseFloat(rowEl.querySelector(".bracket-ded").value);
      rows.push([isNaN(limit) ? 0 : limit, isNaN(rate) ? 0 : rate, isNaN(ded) ? 0 : ded]);
    });
    return rows;
  }

  function addRow(type) {
    renderBracketRows(type, collectBrackets(type).concat([[0, 0, 0]]));
  }

  function removeRow(btn) {
    const rowEl = btn.closest(".row");
    const type = rowEl.closest("[id]").id.replace("Editor", "");
    rowEl.remove();
    const rows = collectBrackets(type);
    taxState[type] = rows;
  }

  async function saveTaxes() {
    const payload = {
      inss: collectBrackets("inss"),
      irrf: collectBrackets("irrf"),
      dependent_deduction: parseFloat(document.getElementById("dependentDeduction").value) || 0,
    };
    try {
      const saved = await api("/admin/api/settings/taxes", { method: "PUT", body: JSON.stringify(payload) });
      taxState = saved;
      toast("Faixas de INSS/IRRF salvas com sucesso.");
    } catch (e) { toast(e.message, "danger"); }
  }

  // Catálogo de rubricas ----------------------------------------------
  async function loadCatalog() {
    const items = await api("/admin/api/catalog");
    const tbody = document.querySelector("#catalogTable tbody");
    tbody.innerHTML = "";
    items.forEach(function (it) {
      const tr = document.createElement("tr");
      const badge = it.tipo === "PROVENTO"
        ? '<span class="badge text-bg-success">PROVENTO</span>'
        : '<span class="badge text-bg-danger">DESCONTO</span>';
      tr.innerHTML =
        "<td><code>" + escapeHtml(it.codigo) + "</code></td>" +
        "<td>" + escapeHtml(it.descricao) + "</td>" +
        "<td>" + badge + "</td>" +
        '<td><button class="btn btn-sm btn-outline-danger" onclick="Admin.deleteCatalog(\'' + it.codigo + '\')"><i class="bi bi-trash"></i></button></td>';
      tbody.appendChild(tr);
    });
  }

  async function addCatalog() {
    const codigo = document.getElementById("catCodigo").value.trim();
    const descricao = document.getElementById("catDescricao").value.trim();
    const tipo = document.getElementById("catTipo").value;
    if (!codigo) { toast("Informe o código.", "warning"); return; }
    try {
      await api("/admin/api/catalog", { method: "POST", body: JSON.stringify({ codigo: codigo, descricao: descricao, tipo: tipo }) });
      document.getElementById("catCodigo").value = "";
      document.getElementById("catDescricao").value = "";
      toast("Rubrica adicionada ao catálogo.");
      loadCatalog();
    } catch (e) { toast(e.message, "danger"); }
  }

  async function deleteCatalog(codigo) {
    if (!confirm("Remover a rubrica '" + codigo + "' do catálogo?")) return;
    try {
      await api("/admin/api/catalog/" + encodeURIComponent(codigo), { method: "DELETE" });
      toast("Rubrica removida.");
      loadCatalog();
    } catch (e) { toast(e.message, "danger"); }
  }

  // Validação do histórico de perfil ----------------------------------
  async function populateHistoryUsers() {
    const users = await api("/admin/api/users");
    const sel = document.getElementById("histUser");
    sel.innerHTML = "";
    users.forEach(function (u) {
      const opt = document.createElement("option");
      opt.value = u.id;
      opt.textContent = u.name + " (" + u.email + ")";
      sel.appendChild(opt);
    });
  }

  async function checkHistory() {
    const userId = document.getElementById("histUser").value;
    const mes = document.getElementById("histMes").value.trim();
    if (!userId || !mes) { toast("Selecione usuário e competência.", "warning"); return; }
    try {
      const r = await api("/admin/api/history/net/" + userId + "/" + mes);
      document.getElementById("histResult").textContent =
        "Líquido recorrente teórico em " + mes + " (taxa ativa no período): " + formatCurrency(r.theoretical_recurrent_net);
      renderHistoryTable(r.history);
      document.getElementById("histTableWrap").classList.remove("d-none");
    } catch (e) { toast(e.message, "danger"); }
  }

  function renderHistoryTable(history) {
    const tbody = document.querySelector("#histTable tbody");
    tbody.innerHTML = "";
    history.forEach(function (h) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + escapeHtml(h.effective_date) + "</td>" +
        "<td>" + escapeHtml(h.job_title) + "</td>" +
        "<td>" + escapeHtml(h.contract_type) + "</td>" +
        "<td>" + formatCurrency(h.base_rate) + "</td>" +
        "<td>" + h.monthly_hours + "</td>";
      tbody.appendChild(tr);
    });
  }

  // Ações operacionais de usuário (CRM) -------------------------------
  function openCreateUser() {
    const modal = document.getElementById("createUserModal");
    if (modal && window.bootstrap) bootstrap.Modal.getOrCreateInstance(modal).show();
  }

  async function createUser() {
    const name = document.getElementById("cuName").value.trim();
    const email = document.getElementById("cuEmail").value.trim();
    const password = document.getElementById("cuPassword").value;
    const role = document.getElementById("cuRole").value;
    const plan = document.getElementById("cuPlan").value;
    if (!name || !email || !password) { toast("Preencha nome, e-mail e senha.", "warning"); return; }
    try {
      await api("/admin/api/users/create", {
        method: "POST",
        body: JSON.stringify({ name: name, email: email, password: password, role: role, plan: plan }),
      });
      toast("Usuário criado com sucesso.");
      const modal = document.getElementById("createUserModal");
      if (modal) bootstrap.Modal.getOrCreateInstance(modal).hide();
      document.getElementById("cuName").value = "";
      document.getElementById("cuEmail").value = "";
      document.getElementById("cuPassword").value = "";
      loadUsers();
      loadMetrics();
    } catch (e) { toast(e.message, "danger"); }
  }

  async function resetPaystubs(userId) {
    if (!confirm("Remover todos os holerites deste usuário?")) return;
    try {
      const r = await api("/admin/api/users/" + userId + "/reset-holerites", { method: "POST" });
      toast(r.holerites_cleared + " holerite(s) removido(s).");
      loadUsers();
      loadMetrics();
    } catch (e) { toast(e.message, "danger"); }
  }

  async function deleteUser(userId) {
    if (!confirm("Excluir definitivamente este usuário e todos os seus dados?")) return;
    try {
      await api("/admin/api/users/" + userId + "/delete", { method: "POST" });
      toast("Usuário excluído.");
      loadUsers();
      loadMetrics();
    } catch (e) { toast(e.message, "danger"); }
  }

  // Configurador de IA (modelo / tarifas) --------------------------------
  async function loadAIConfig() {
    const c = await api("/admin/api/ai-config");
    document.getElementById("aiModelName").value = c.model || "";
    document.getElementById("aiInputRate").value = c.input_rate_per_million;
    document.getElementById("aiOutputRate").value = c.output_rate_per_million;
    updateAIConfigPreview();
  }

  async function saveAIConfig() {
    const payload = {
      model: document.getElementById("aiModelName").value.trim(),
      input_rate_per_million: parseFloat(document.getElementById("aiInputRate").value) || 0,
      output_rate_per_million: parseFloat(document.getElementById("aiOutputRate").value) || 0,
    };
    try {
      const saved = await api("/admin/api/ai-config", { method: "PUT", body: JSON.stringify(payload) });
      toast("Configuração de IA salva (" + saved.model + ").");
      loadMetrics();
      loadAIUsage();
    } catch (e) { toast(e.message, "danger"); }
  }

  function updateAIConfigPreview() {
    const el = document.getElementById("aiConfigPreview");
    if (!el) return;
    const rateIn = parseFloat(document.getElementById("aiInputRate").value) || 0;
    const rateOut = parseFloat(document.getElementById("aiOutputRate").value) || 0;
    const cost = (aiUsageState.input_tokens / 1000000) * rateIn +
                 (aiUsageState.output_tokens / 1000000) * rateOut;
    el.textContent =
      "Recálculo dinâmico: " + formatUsd(cost) + "  (" +
      formatNumber(aiUsageState.input_tokens) + " tokens input × " + rateIn +
      " + " + formatNumber(aiUsageState.output_tokens) + " tokens output × " + rateOut + " / 1M)";
  }

  // Navegação por sidebar (dashboard Enterprise) -----------------------
  function showSection(name) {
    document.querySelectorAll(".admin-pane").forEach(function (p) {
      p.classList.add("d-none");
    });
    const target = document.getElementById("pane-" + name);
    if (target) target.classList.remove("d-none");
    document.querySelectorAll(".admin-sidebar [data-section]").forEach(function (b) {
      const on = b.getAttribute("data-section") === name;
      b.classList.toggle("active", on);
      if (on) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
  }

  function wireNav() {
    document.querySelectorAll("[data-section]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        showSection(btn.getAttribute("data-section"));
      });
    });
  }

  // Provedor de IA (API key / base URL) -------------------------------
  let providerState = { has_api_key: false };

  async function loadProviderSettings() {
    const p = await api("/admin/api/ai-provider");
    providerState.has_api_key = !!p.has_api_key;
    const baseEl = document.getElementById("aiProviderBaseUrl");
    if (baseEl) baseEl.value = p.base_url || "";
    const keyEl = document.getElementById("aiProviderApiKey");
    if (keyEl) {
      keyEl.value = "";
      keyEl.placeholder = p.has_api_key
        ? "••••" + (p.api_key_masked || "").replace(/^..../, "") + " — mantida (digite p/ trocar)"
        : "sk-… (em branco = offline)";
    }
    const modelEl = document.getElementById("aiProviderModel");
    if (modelEl) modelEl.value = p.model || "";
    const status = document.getElementById("aiProviderStatus");
    if (status) {
      status.textContent = p.has_api_key ? "IA habilitada" : "IA desabilitada (offline)";
      status.className = "badge " + (p.has_api_key ? "text-bg-success" : "text-bg-secondary");
    }
  }

  async function saveProviderSettings() {
    const payload = { base_url: document.getElementById("aiProviderBaseUrl").value.trim() };
    const typed = document.getElementById("aiProviderApiKey").value.trim();
    const model = document.getElementById("aiProviderModel").value.trim();
    if (typed) payload.api_key = typed;
    if (model) payload.model = model;
    try {
      const saved = await api("/admin/api/ai-provider", { method: "PUT", body: JSON.stringify(payload) });
      providerState.has_api_key = !!saved.has_api_key;
      toast(saved.has_api_key ? "Credenciais do provedor de IA salvas." : "IA desabilitada (offline).", "success");
      loadProviderSettings();
    } catch (e) { toast(e.message, "danger"); }
  }

  // Sistema & Banco (paths / storage) ---------------------------------
  async function loadSystemSettings() {
    const s = await api("/admin/api/system-settings");
    const db = s.database || {};
    const up = s.upload_folder || {};
    const dbPath = document.getElementById("sysDbPath");
    if (dbPath) dbPath.value = db.path || "";
    const dbStatus = document.getElementById("sysDbStatus");
    if (dbStatus) {
      dbStatus.textContent = db.status === "ok" ? "online" : "com erro";
      dbStatus.className = "badge " + (db.status === "ok" ? "text-bg-success" : "text-bg-danger");
    }
    const sysDbSize = document.getElementById("sysDbSize");
    if (sysDbSize) sysDbSize.textContent = formatBytes(db.size_bytes);
    const upEl = document.getElementById("sysUploadFolder");
    if (upEl) upEl.value = up.path || "";
    const upNote = document.getElementById("sysUploadNote");
    if (upNote) upNote.textContent = up.exists
      ? ("gravável: " + (up.writable ? "sim" : "não"))
      : "pasta ainda não criada";
    const pending = document.getElementById("sysPendingNote");
    if (pending) {
      pending.textContent = (db.pending_path || up.pending_path)
        ? "Novo caminho de banco pendente — reinicie o servidor para aplicar."
        : "";
    }
  }

  async function saveSystemSettings() {
    const payload = { upload_folder: document.getElementById("sysUploadFolder").value.trim() };
    try {
      const r = await api("/admin/api/system-settings", { method: "PUT", body: JSON.stringify(payload) });
      const msg = (r.messages || []).join(" ");
      toast(msg || "Configuração de sistema salva.", r.requires_restart ? "warning" : "success");
      loadSystemSettings();
      loadStorage();
      loadMetrics();
    } catch (e) { toast(e.message, "danger"); }
  }

  // Init ---------------------------------------------------------------
  function wireListeners() {
    ["userSearch", "planFilter", "statusFilter"].forEach(function (id) {
      const el = document.getElementById(id);
      if (el) el.addEventListener("input", applyUserFilters);
    });

    // Recálculo dinâmico do custo ao editar as tarifas do modelo.
    ["aiModelName", "aiInputRate", "aiOutputRate"].forEach(function (id) {
      const el = document.getElementById(id);
      if (el) el.addEventListener("input", updateAIConfigPreview);
    });

    wireNav();
  }

  async function init() {
    try {
      await Promise.all([
        loadMetrics(), loadUsers(), loadTaxes(), loadCatalog(),
        loadErrors(), populateHistoryUsers(), loadFreemiumLimits(), loadStorage(),
        loadAIUsage(), loadAIConfig(), loadProviderSettings(), loadSystemSettings(),
      ]);
      wireListeners();
      showSection("overview");
    } catch (e) {
      toast(e.message, "danger");
    }
  }

  return {
    init: init,
    loadUsers: loadUsers,
    openCreateUser: openCreateUser,
    createUser: createUser,
    changePlan: changePlan,
    resetUsage: resetUsage,
    resetPaystubs: resetPaystubs,
    deleteUser: deleteUser,
    toggleActive: toggleActive,
    changeRole: changeRole,
    resetPassword: resetPassword,
    loadMetrics: loadMetrics,
    loadAIUsage: loadAIUsage,
    loadAIConfig: loadAIConfig,
    saveAIConfig: saveAIConfig,
    updateAIConfigPreview: updateAIConfigPreview,
    loadProviderSettings: loadProviderSettings,
    saveProviderSettings: saveProviderSettings,
    loadSystemSettings: loadSystemSettings,
    saveSystemSettings: saveSystemSettings,
    loadErrors: loadErrors,
    resolveError: resolveError,
    loadStorage: loadStorage,
    cleanupStorage: cleanupStorage,
    saveFreemiumLimits: saveFreemiumLimits,
    addRow: addRow,
    removeRow: removeRow,
    saveTaxes: saveTaxes,
    addCatalog: addCatalog,
    deleteCatalog: deleteCatalog,
    checkHistory: checkHistory,
  };
})();

window.ADMIN_ME_ID = window.ADMIN_ME_ID || null;

document.addEventListener("DOMContentLoaded", Admin.init);


