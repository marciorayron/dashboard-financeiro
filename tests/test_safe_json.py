"""
test_safe_json.py
-----------------
Garante que as respostas da API sejam JSON estritamente válido (sem os
literais inválidos `NaN` / `Infinity` / `-Infinity`, que quebram o parser
dos navegadores) e que o sanitizer converta valores não-finitos com segurança.
"""
import json
from decimal import Decimal

import pytest

from services import safe_json
from services.safe_json import sanitize


# ---------------------------------------------------------------------
# Sanitizer recursivo
# ---------------------------------------------------------------------
def test_sanitize_converts_non_finite_floats_to_none():
    out = sanitize(
        {
            "nan": float("nan"),
            "pos_inf": float("inf"),
            "neg_inf": float("-inf"),
            "finite": 3.14,
            "zero": 0.0,
            "nested": {"bad": float("nan")},
            "list": [float("inf"), 1.0],
            "text": "ok",
        }
    )
    assert out["nan"] is None
    assert out["pos_inf"] is None
    assert out["neg_inf"] is None
    assert out["finite"] == 3.14
    assert out["zero"] == 0.0
    assert out["nested"]["bad"] is None
    assert out["list"] == [None, 1.0]
    assert out["text"] == "ok"


def test_sanitize_converts_decimal():
    assert sanitize(Decimal("1.50")) == 1.5
    assert sanitize(Decimal("NaN")) is None
    assert sanitize(Decimal("Infinity")) is None


def test_sanitize_leaves_simple_types_intact():
    assert sanitize(1) == 1
    assert sanitize("abc") == "abc"
    assert sanitize(True) is True
    assert sanitize(None) is None


def test_sanitize_tuples_and_sets():
    assert sanitize((float("nan"), 2)) == [None, 2]
    assert sanitize({float("inf")}) == [None]


# ---------------------------------------------------------------------
# dumps() estrito (allow_nan=False, sem literais inválidos)
# ---------------------------------------------------------------------
def test_dumps_never_emits_nan_infinity():
    raw = safe_json.dumps(
        {"bad": float("inf"), "bad2": float("nan"), "ok": 1}
    )
    assert "Infinity" not in raw
    assert "NaN" not in raw
    # E o resultado é JSON estrito e parseável.
    parsed = json.loads(raw)
    assert parsed["bad"] is None
    assert parsed["bad2"] is None
    assert parsed["ok"] == 1


# ---------------------------------------------------------------------
# Endpoints: JSON estritamente válido em estado zero
# ---------------------------------------------------------------------
def test_advanced_zero_state_returns_strict_valid_json(client, register_user, login):
    register_user(email="zero@example.com")
    login(email="zero@example.com")

    resp = client.get("/api/analytics/advanced")
    assert resp.status_code == 200

    body = resp.data.decode("utf-8")
    # Nenhum literal não-finito (JSON inválido) na resposta.
    assert "Infinity" not in body
    assert "-Infinity" not in body
    assert "NaN" not in body

    # `json.loads` estrito precisa ter sucesso (sem allow_nan).
    parsed = json.loads(body)
    assert parsed["meta"]["count"] == 0
    assert parsed["meta"]["total_earnings"] == 0.0
    assert parsed["overtime"]["ratio"] == 0.0
    assert parsed["tax_rates"]["inss"]["rate"] == 0.0
    assert parsed["tax_rates"]["irrf"]["rate"] == 0.0


def test_projection_zero_state_returns_strict_valid_json(client, register_user, login):
    register_user(email="projzero@example.com")
    login(email="projzero@example.com")

    resp = client.get("/api/analytics/projection")
    assert resp.status_code == 200

    body = resp.data.decode("utf-8")
    assert "Infinity" not in body
    assert "-Infinity" not in body
    assert "NaN" not in body

    parsed = json.loads(body)
    assert "total_annual_take_home" in parsed
    # Nenhum valor de projeção pode ser não-finito.
    assert all(v is not None for v in parsed.values() if isinstance(v, float))
