/* =====================================================================
   admin.js
   Painel Administrativo (/admin) — RBAC.
   Gestão de usuários, métricas globais, faixas de INSS/IRRF e catálogo
   de rubricas. As chamadas usam a sessão autenticada (same-origin); o
   backend garante 403 para quem não for 'admin'.
   ===================================================================== */
"use strict";

const Admin = (function () {
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
      console.warn("[safeParseJson] Falha ao interpretar resposta JSON de " + resp.url + ": " + err.message, err);
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
      throw new Error(msg);
    }
    return safeParseJson(resp, {});
  }

  // Métricas -----------------------------------------------------------
  async function loadMetrics() {
    const m = await api("/admin/api/metrics");
    document.getElementById("metricUsers").textContent = m.total_users;
    document.getElementById("metricPaystubs").textContent = m.total_paystubs;
    document.getElementById("metricErrors").textContent = m.unresolved_parsing_errors;
    document.getElementById("metricAdmins").textContent = "Administradores: " + m.admins;

    // Saúde do sistema (Database Status / Storage).
    refreshHealth(m);
  }

  async function refreshHealth(m) {
    m = m || await api("/admin/api/metrics");
    const dbStatus = document.getElementById("healthDb");
    const dbOk = m.db && m.db.status === "ok";
    dbStatus.textContent = dbOk ? "Online" : "Erro";
    dbStatus.className = "fw-bold text-" + (dbOk ? "success" : "danger");
    document.getElementById("healthActiveUsers").textContent = m.active_users;
    document.getElementById("healthPdfs").textContent = m.pdf_uploads;
    document.getElementById("healthStorage").textContent = formatBytes(m.storage_bytes);
    document.getElementById("healthDbPath").textContent =
      "Conexão: SQLite" + (m.db && m.db.path ? " · " + m.db.path : "");
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
        "<td>" +
          "<span class='badge text-bg-danger'>" + escapeHtml(e.error_type || "Erro") + "</span> " +
          "<div class='small text-secondary'>" + escapeHtml(e.error_message) + "</div>" +
        "</td>" +
        "<td><pre class='mb-0 small' style='max-height:80px;overflow:auto;white-space:pre-wrap'>" +
          escapeHtml(e.traceback || "") + "</pre></td>" +
        "<td class='text-end'><button class='btn btn-sm btn-outline-success' " +
          "onclick='Admin.resolveError(" + e.id + ")'>" +
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

  // Usuários -----------------------------------------------------------
  async function loadUsers() {
    const users = await api("/admin/api/users");
    const tbody = document.querySelector("#usersTable tbody");
    tbody.innerHTML = "";
    const me = window.ADMIN_ME_ID;

    users.forEach(function (u) {
      const tr = document.createElement("tr");
      const statusBadge =
        u.is_active
          ? '<span class="badge text-bg-success">Ativo</span>'
          : '<span class="badge text-bg-secondary">Inativo</span>';
      const roleBadge =
        u.role === "admin"
          ? '<span class="badge text-bg-warning">admin</span>'
          : '<span class="badge text-bg-info">user</span>';

      let actions = "";
      // Não permite alterar a própria conta (backend também bloqueia).
      if (u.id !== me) {
        actions +=
          '<button class="btn btn-sm btn-outline-secondary me-1" title="Ativar/Desativar" ' +
          'onclick="Admin.toggleActive(' + u.id + ')"><i class="bi bi-power"></i></button>';
        actions +=
          '<button class="btn btn-sm btn-outline-warning me-1" title="Promover/Rebaixar" ' +
          'onclick="Admin.changeRole(' + u.id + ', \'' + (u.role === "admin" ? "user" : "admin") + '\')">' +
          '<i class="bi bi-person-lines-fill"></i></button>';
        actions +=
          '<button class="btn btn-sm btn-outline-danger" title="Resetar senha" ' +
          'onclick="Admin.resetPassword(' + u.id + ')"><i class="bi bi-key"></i></button>';
      } else {
        actions += '<span class="text-secondary small">você</span>';
      }

      tr.innerHTML =
        "<td>" + u.id + "</td>" +
        "<td>" + escapeHtml(u.name) + "</td>" +
        "<td>" + escapeHtml(u.email) + "</td>" +
        "<td>" + roleBadge + "</td>" +
        "<td>" + statusBadge + "</td>" +
        "<td>" + u.paystubs + "</td>" +
        "<td>" + actions + "</td>";
      tbody.appendChild(tr);
    });
  }

  async function toggleActive(id) {
    try {
      await api("/admin/api/users/" + id + "/toggle-active", { method: "POST" });
      toast("Status do usuário atualizado.");
      loadUsers();
    } catch (e) { toast(e.message, "danger"); }
  }

  async function changeRole(id, role) {
    if (!confirm("Alterar o papel deste usuário para '" + role + "'?")) return;
    try {
      await api("/admin/api/users/" + id + "/role", {
        method: "POST",
        body: JSON.stringify({ role: role }),
      });
      toast("Papel atualizado para " + role + ".");
      loadUsers();
    } catch (e) { toast(e.message, "danger"); }
  }

  async function resetPassword(id) {
    if (!confirm("Gerar uma nova senha temporária para este usuário?")) return;
    try {
      const r = await api("/admin/api/users/" + id + "/reset-password", { method: "POST" });
      alert("Senha temporária (copie agora):\n\n" + r.temporary_password);
      toast("Senha redefinida.");
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
      const saved = await api("/admin/api/settings/taxes", {
        method: "PUT",
        body: JSON.stringify(payload),
      });
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
      const badge =
        it.tipo === "PROVENTO"
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
      await api("/admin/api/catalog", {
        method: "POST",
        body: JSON.stringify({ codigo: codigo, descricao: descricao, tipo: tipo }),
      });
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
        "Líquido recorrente teórico em " + mes + " (taxa ativa no período): " +
        formatCurrency(r.theoretical_recurrent_net);
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

  // Init ---------------------------------------------------------------
  async function init() {
    try {
      await Promise.all([
        loadMetrics(), loadUsers(), loadTaxes(), loadCatalog(),
        loadErrors(), populateHistoryUsers(),
      ]);
    } catch (e) {
      toast(e.message, "danger");
    }
  }

  return {
    init: init,
    loadUsers: loadUsers,
    toggleActive: toggleActive,
    changeRole: changeRole,
    resetPassword: resetPassword,
    refreshHealth: refreshHealth,
    loadErrors: loadErrors,
    resolveError: resolveError,
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

