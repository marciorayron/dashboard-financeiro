"""
routes/profile.py
-----------------
Endpoints de perfil do usuário (dados de contratação/benefícios).

  * GET  /api/profile -> retorna o perfil do usuário logado.
  * POST /api/profile -> salva/atualiza o perfil.
"""
from flask import Blueprint, jsonify, request

from database.connection import get_db
from models.profile import load_profile, save_profile, to_dict
from routes.auth import current_user_id, login_required

profile_bp = Blueprint("profile", __name__)


@profile_bp.route("/profile", methods=["GET"])
@login_required
def get_profile():
    """Retorna o perfil do usuário (padrão se ainda não configurado)."""
    user_id = current_user_id()
    db = get_db()
    return jsonify(to_dict(load_profile(db, user_id)))


@profile_bp.route("/profile", methods=["POST"])
@login_required
def post_profile():
    """Salva/atualiza o perfil do usuário com os dados enviados em JSON."""
    user_id = current_user_id()
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Corpo inválido."}), 400
    db = get_db()
    profile = save_profile(db, user_id, data)
    return jsonify(to_dict(profile)), 200
