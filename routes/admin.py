"""
routes/admin.py
---------------
Blueprint do painel de administração (`/admin`).

Rotas protegidas por `@admin_required` (papel 'admin') — não-admins recebem
HTTP 403. Reúne:
  * Página do painel e endpoints de API de gestão de usuários
    (ativar/desativar, trocar papel, resetar senha).
  * Métricas globais do sistema.
  * Edição de faixas de INSS/IRRF e do catálogo de rubricas (sem deploy).
"""
from flask import Blueprint, current_app, jsonify, render_template, request, session

from database.connection import get_db
from models.user import (
    create_user,
    delete_user,
    get_user,
    list_users,
    reset_user_password,
    set_user_active,
    set_user_password,
    set_user_plan,
    set_user_role,
    to_dict as user_to_dict,
)
from routes.auth import admin_required
from services import admin_service, monitoring_service, settings_service
from services.analytics_service import theoretical_recurrent_net_for_month

admin_bp = Blueprint("admin", __name__)


# ---------------------------------------------------------------------
# Página do painel
# ---------------------------------------------------------------------
@admin_bp.route("/admin")
@admin_required
def index():
    """Renderiza o painel administrativo."""
    return render_template(
        "admin/index.html",
        title="Administração",
        user_name=session.get("user_name", "Usuário"),
        is_admin=True,
        current_user_id=session.get("user_id"),
    )


# ---------------------------------------------------------------------
# Gestão de usuários
# ---------------------------------------------------------------------
def _user_row_with_stats(db, user):
    """Serializa um usuário com contagens associadas (via admin_service)."""
    return admin_service.user_row_with_stats(db, user)


@admin_bp.route("/admin/api/users", methods=["GET"])
@admin_required
def admin_users_list():
    """Lista todos os usuários com contagens associadas (CRM)."""
    db = get_db()
    return jsonify(admin_service.list_users_with_stats(db))


@admin_bp.route("/admin/api/users/create", methods=["POST"])
@admin_required
def admin_user_create():
    """
    Cria um novo usuário operacionalmente (a partir do painel admin).

    Corpo JSON: {"name", "email", "password", "role": user|admin,
                 "plan": free|pro}.
    """
    body = request.get_json(silent=True) or {}
    # Limite de usuários suportados pela instância (multi-tenant).
    max_users = int(current_app.config.get("MAX_USERS", 10) or 10)
    db = get_db()
    current_count = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if current_count >= max_users:
        return jsonify(
            {"error": f"Limite de {max_users} usuários atingido."}
        ), 400
    try:
        user = create_user(
            db,
            name=body.get("name"),
            email=body.get("email"),
            password=body.get("password"),
            role=body.get("role"),
            plan=body.get("plan"),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(_user_row_with_stats(db, user)), 201


@admin_bp.route("/admin/api/users/<int:user_id>/toggle-status", methods=["POST"])
@admin_required
def admin_user_toggle_status(user_id: int):
    """
    Suspende ou reinstate uma conta.

    Corpo JSON (opcional): {"active": true|false}. Sem `active`, alterna o
    estado atual. Não permite alterar a própria conta.
    """
    body = request.get_json(silent=True) or {}
    db = get_db()
    target = get_user(db, user_id)
    if target is None:
        return jsonify({"error": "Usuário não encontrado."}), 404
    if user_id == session.get("user_id"):
        return jsonify({"error": "Não é possível alterar a própria conta."}), 400

    requested = body.get("active")
    if isinstance(requested, bool):
        active = requested
    else:
        active = not target.is_active
    updated = set_user_active(db, user_id, active)
    return jsonify(_user_row_with_stats(db, updated))


@admin_bp.route("/admin/api/users/<int:user_id>/reset-holerites", methods=["POST"])
@admin_required
def admin_user_reset_holerites(user_id: int):
    """Apaga todos os holerites/rubricas do usuário (suporte)."""
    db = get_db()
    if get_user(db, user_id) is None:
        return jsonify({"error": "Usuário não encontrado."}), 404
    result = admin_service.reset_holerites(db, user_id)
    return jsonify({**result, "message": "Holerites do usuário removidos."})


@admin_bp.route("/admin/api/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_user_delete(user_id: int):
    """Exclui definitivamente um usuário e seus dados (cascade)."""
    if user_id == session.get("user_id"):
        return jsonify({"error": "Não é possível excluir a própria conta."}), 400
    db = get_db()
    target = get_user(db, user_id)
    if target is None:
        return jsonify({"error": "Usuário não encontrado."}), 404
    # Garante a exclusão em cascata das tabelas associadas (suporte/gestão).
    admin_service.reset_holerites(db, user_id)
    admin_service.reset_user_usage(db, user_id)
    deleted = delete_user(db, user_id)
    if not deleted:
        return jsonify({"error": "Usuário não encontrado."}), 404
    return jsonify({"message": "Usuário excluído.", "user_id": user_id})


@admin_bp.route("/admin/api/users/<int:user_id>/plan", methods=["POST"])
@admin_required
def admin_user_plan(user_id: int):
    """
    Altera o plano de assinatura de um usuário ('free' | 'pro').

    Corpo JSON: {"plan": "pro"}.
    """
    body = request.get_json(silent=True) or {}
    plan = str(body.get("plan") or "").lower()
    if plan not in ("free", "pro"):
        return jsonify({"error": "Plano inválido. Use 'free' ou 'pro'."}), 400
    db = get_db()
    updated = set_user_plan(db, user_id, plan)
    if updated is None:
        return jsonify({"error": "Usuário não encontrado."}), 404
    return jsonify(_user_row_with_stats(db, updated))


@admin_bp.route("/admin/api/users/<int:user_id>/reset-usage", methods=["POST"])
@admin_required
def admin_user_reset_usage(user_id: int):
    """
    Zera o consumo de IA de um usuário (limpa `ai_usage_logs` e
    `ai_blocked_logs`) — usado em chamados de suporte.
    """
    db = get_db()
    target = get_user(db, user_id)
    if target is None:
        return jsonify({"error": "Usuário não encontrado."}), 404
    result = admin_service.reset_user_usage(db, user_id)
    return jsonify({**result, "message": "Consumo de IA zerado."})


@admin_bp.route("/admin/api/users/<int:user_id>/toggle-active", methods=["POST"])
@admin_required
def admin_user_toggle_active(user_id: int):
    """Ativa/desativa uma conta. Não permite desativar a si mesmo."""
    db = get_db()
    target = get_user(db, user_id)
    if target is None:
        return jsonify({"error": "Usuário não encontrado."}), 404
    if user_id == session.get("user_id"):
        return jsonify({"error": "Não é possível alterar a própria conta."}), 400
    updated = set_user_active(db, user_id, not target.is_active)
    return jsonify(_user_row_with_stats(db, updated))


@admin_bp.route("/admin/api/users/<int:user_id>/role", methods=["POST"])
@admin_required
def admin_user_role(user_id: int):
    """Define o papel de um usuário ('admin' | 'user')."""
    body = request.get_json(silent=True) or {}
    role = str(body.get("role") or "").lower()
    if role not in ("admin", "user"):
        return jsonify({"error": "Papel inválido. Use 'admin' ou 'user'."}), 400
    db = get_db()
    if user_id == session.get("user_id"):
        # Impede auto-rebaixamento para não perder o último admin.
        return jsonify({"error": "Não é possível alterar o próprio papel."}), 400
    updated = set_user_role(db, user_id, role)
    if updated is None:
        return jsonify({"error": "Usuário não encontrado."}), 404
    return jsonify(_user_row_with_stats(db, updated))


@admin_bp.route("/admin/api/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def admin_user_reset_password(user_id: int):
    """
    Redefine a senha do usuário.

    Se o corpo trouxer `{"password": "..."}`, define essa senha explícita;
    caso contrário, gera uma senha temporária e a retorna UMA vez (exibida ao
    admin para repasse ao usuário).
    """
    body = request.get_json(silent=True) or {}
    db = get_db()
    target = get_user(db, user_id)
    if target is None:
        return jsonify({"error": "Usuário não encontrado."}), 404

    explicit = body.get("password")
    if explicit:
        try:
            set_user_password(db, user_id, explicit)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"message": "Senha redefinida.", "user_id": user_id})

    temporary = reset_user_password(db, user_id)
    return jsonify(
        {
            "message": "Senha redefinida.",
            "user_id": user_id,
            "temporary_password": temporary,
        }
    )

# ---------------------------------------------------------------------
# Métricas globais do sistema
# ---------------------------------------------------------------------
@admin_bp.route("/admin/api/metrics", methods=["GET"])
@admin_required
def admin_metrics():
    """
    Saúde do sistema: estado do banco, usuários, holerites, armazenamento
    (PDFs) e erros de parsing não resolvidos.
    """
    db = get_db()
    return jsonify(monitoring_service.get_system_health(db))


@admin_bp.route("/admin/api/stats", methods=["GET"])
@admin_required
def admin_stats():
    """
    KPIs agregados para o backoffice: usuários e divisão de planos (MRR/Pro),
    holerites, consumo de IA (tokens estimados e custo), armazenamento e
    erros de parsing.
    """
    db = get_db()
    return jsonify(admin_service.get_stats(db))


@admin_bp.route("/admin/api/ai-usage", methods=["GET"])
@admin_required
def admin_ai_usage():
    """Dados de consumo de IA para a aba FinOps (breakdown + leaderboard)."""
    db = get_db()
    return jsonify(admin_service.get_ai_usage(db))


@admin_bp.route("/admin/api/freemium-limits", methods=["GET"])
@admin_required
def admin_get_freemium_limits():
    """Retorna os limites dinâmicos do modelo Freemium."""
    return jsonify(settings_service.get_freemium_limits(get_db()))


@admin_bp.route("/admin/api/freemium-limits", methods=["PUT"])
@admin_required
def admin_put_freemium_limits():
    """Persiste os limites do modelo Freemium editados no painel."""
    body = request.get_json(silent=True) or {}
    db = get_db()
    saved = settings_service.save_freemium_limits(db, body)
    return jsonify(saved)


@admin_bp.route("/admin/api/ai-config", methods=["GET"])
@admin_required
def admin_get_ai_config():
    """Retorna a configuração ativa do modelo/tarifas de IA."""
    return jsonify(settings_service.get_ai_config(get_db()))


@admin_bp.route("/admin/api/ai-config", methods=["PUT"])
@admin_required
def admin_put_ai_config():
    """
    Persiste a configuração ativa do modelo de IA e as tarifas de tokens.

    Corpo JSON: {"model": str, "input_rate_per_million": float,
                 "output_rate_per_million": float}
    """
    body = request.get_json(silent=True) or {}
    db = get_db()
    saved = settings_service.save_ai_config(db, body)
    return jsonify(saved)


@admin_bp.route("/admin/api/storage", methods=["GET"])
@admin_required
def admin_storage():
    """Detalhamento do consumo de armazenamento em disco."""
    db = get_db()
    return jsonify(admin_service.get_storage_breakdown(db))


@admin_bp.route("/admin/api/storage/cleanup", methods=["POST"])
@admin_required
def admin_storage_cleanup():
    """Remove arquivos órfãos/temporários não referenciados por holerites."""
    db = get_db()
    result = admin_service.clean_orphaned_files(db)
    return jsonify({**result, "message": "Limpeza concluída."})


# ---------------------------------------------------------------------
# Auditoria: erros de parsing de PDFs (log de uploads quebrados)
# ---------------------------------------------------------------------
@admin_bp.route("/admin/api/errors", methods=["GET"])
@admin_required
def admin_errors_list():
    """Lista os erros de parsing com contexto do usuário (tenant)."""
    only_unresolved = request.args.get("resolved", "0") != "1"
    db = get_db()
    return jsonify(
        monitoring_service.list_parse_errors(db, only_unresolved=only_unresolved)
    )


@admin_bp.route("/admin/api/errors/<int:error_id>/resolve", methods=["POST"])
@admin_required
def admin_errors_resolve(error_id: int):
    """Marca um erro de parsing como resolvido."""
    db = get_db()
    if not monitoring_service.mark_parse_error_resolved(db, error_id):
        return jsonify({"error": "Erro de parsing não encontrado."}), 404
    return jsonify({"message": "Erro marcado como resolvido.", "id": error_id})


# ---------------------------------------------------------------------
# Configurações: faixas de INSS/IRRF
# ---------------------------------------------------------------------
@admin_bp.route("/admin/api/settings/taxes", methods=["GET"])
@admin_required
def admin_get_taxes():
    """Retorna as faixas de INSS/IRRF e dedução por dependente vigentes."""
    db = get_db()
    return jsonify(settings_service.get_tax_settings(db))


@admin_bp.route("/admin/api/settings/taxes", methods=["PUT"])
@admin_required
def admin_put_taxes():
    """Salva as faixas de INSS/IRRF e a dedução por dependente."""
    body = request.get_json(silent=True) or {}
    db = get_db()
    saved = settings_service.save_tax_settings(db, body)
    return jsonify(saved)

# ---------------------------------------------------------------------
# Configurações: catálogo de rubricas (códigos padrão)
# ---------------------------------------------------------------------
@admin_bp.route("/admin/api/catalog", methods=["GET"])
@admin_required
def admin_list_catalog():
    """Lista os códigos padrão de rubricas (proventos/descontos)."""
    db = get_db()
    return jsonify(settings_service.list_catalog(db))


@admin_bp.route("/admin/api/catalog", methods=["POST"])
@admin_required
def admin_upsert_catalog():
    """Insere/atualiza um código padrão de rubrica no catálogo."""
    body = request.get_json(silent=True) or {}
    try:
        item = settings_service.upsert_catalog(
            db=get_db(),
            codigo=body.get("codigo"),
            descricao=body.get("descricao"),
            tipo=body.get("tipo"),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(item)


@admin_bp.route("/admin/api/catalog/<codigo>", methods=["DELETE"])
@admin_required
def admin_delete_catalog(codigo: str):
    """Remove um código padrão de rubrica do catálogo."""
    removed = settings_service.delete_catalog(get_db(), codigo)
    if not removed:
        return jsonify({"error": "Código não encontrado no catálogo."}), 404
    return jsonify({"message": "Código removido do catálogo.", "codigo": codigo})


# ---------------------------------------------------------------------
# Previsualização: líquido teórico por competência (validação do histórico)
# ---------------------------------------------------------------------
@admin_bp.route("/admin/api/history/net/<int:user_id>/<mes_referencia>", methods=["GET"])
@admin_required
def admin_history_net(user_id: int, mes_referencia: str):
    """
    Retorna o líquido recorrente teórico do usuário para uma competência,
    avaliando a taxa de pagamento ATIVA naquela data (histórico de revisões).
    """
    from models.profile import load_profile_history

    db = get_db()
    net = theoretical_recurrent_net_for_month(db, user_id, mes_referencia)
    history = load_profile_history(db, user_id)
    return jsonify(
        {
            "user_id": user_id,
            "mes_referencia": mes_referencia,
            "theoretical_recurrent_net": round(net, 2),
            "history": [
                {
                    "id": h.id,
                    "effective_date": h.effective_date,
                    "job_title": h.job_title,
                    "contract_type": h.contract_type,
                    "base_rate": h.base_rate,
                    "monthly_hours": h.monthly_hours,
                    "irrf_dependents": h.irrf_dependents,
                    "fixed_benefits_deduction": h.fixed_benefits_deduction,
                    "created_at": h.created_at,
                }
                for h in history
            ],
        }
    )

