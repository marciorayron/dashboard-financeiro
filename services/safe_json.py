"""
safe_json.py
------------
Serialização JSON robusta para as respostas da API.

Garante que nenhum valor não-finito (NaN / Infinity / -Infinity) chegue ao
cliente — esses literais quebram o parser JSON dos navegadores (`JSON.parse`)
e derrubariam o `fetch().then(r => r.json())` do dashboard.

Estratégia em duas camadas (defesa em profundidade):

  1. `sanitize()` — percorre recursivamente dict/list/tuple e converte floats
     não-finitos e `Decimal` para valores JSON válidos:
       * NaN / +Infinity / -Infinity -> ``None`` (vira ``null`` no JSON).
       * Decimal                     -> ``float`` finito (ou ``None`` se não-finito).

  2. `SafeJSONProvider` — provedor de JSON do Flask que aplica o `sanitize()`
     ANTES de serializar e usa ``allow_nan=False`` como rede de segurança: se
     algum valor não-finito sobreviver, lança exceção em vez de emitir JSON
     inválido (fail-fast, nunca corrompe a resposta).

Registro (ver `app.py`)::

    from services.safe_json import SafeJSONProvider
    app.json = SafeJSONProvider(app)

O `jsonify(...)` de todos os blueprints passa por esse provedor.
"""
from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import Any

from flask.json.provider import DefaultJSONProvider


def _is_non_finite(value: Any) -> bool:
    """True se o valor for float/Decimal não-finito (NaN ou ±Infinity)."""
    try:
        return math.isinf(value) or math.isnan(value)
    except (TypeError, ValueError):
        return False


def _decimal_to_float(value: Decimal) -> Any:
    """Converte Decimal para float, retornando None se não-finito."""
    if _is_non_finite(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def sanitize(value: Any) -> Any:
    """
    Converte recursivamente valores não serializáveis de forma estrita.

    * NaN / ±Infinity -> ``None`` (``null`` no JSON).
    * ``Decimal``     -> ``float`` finito (ou ``None`` se não-finito).
    * ``dict``/``list``/``tuple``/``set`` -> percorridos recursivamente.

    Valores já finitos e tipos simples (str, int, bool, None) são retornados
    intactos. É seguro chamar com qualquer tipo (inclusive ``sqlite3.Row``).
    """
    if isinstance(value, float):
        return None if _is_non_finite(value) else value
    if isinstance(value, Decimal):
        return _decimal_to_float(value)
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [sanitize(v) for v in value]
    # Tipo simples (str, int, bool, None) ou desconhecido — retorna intacto.
    return value


class SafeJSONEncoder(json.JSONEncoder):
    """
    Encoder que estende o padrão para tipos comuns (Decimal e numpy scalars)
    sem nunca emitir NaN/Infinity. Os floats já foram sanitizados por
    `sanitize()`; este encoder é a camada extra para tipos aninhados.
    """

    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return _decimal_to_float(o)
        # NumPy scalars (comuns em agregações) — import defensivo.
        try:
            import numpy as np  # type: ignore

            if isinstance(o, np.floating):  # type: ignore
                return None if _is_non_finite(float(o)) else float(o)
            if isinstance(o, np.integer):  # type: ignore
                return int(o)
            if isinstance(o, np.bool_):  # type: ignore
                return bool(o)
        except Exception:  # numpy ausente ou versão incompatível
            pass
        return super().default(o)


class SafeJSONProvider(DefaultJSONProvider):
    """
    Provedor de JSON do Flask que garante saída estritamente válida.

    Aplica `sanitize()` a qualquer objeto serializado (dict retornado por
    `jsonify`/`make_response`) e força `allow_nan=False` para que, se algo
    não-finito sobreviver por engano, a serialização falhe com erro claro em
    vez de emitir `NaN`/`Infinity` (JSON inválido).
    """

    default = SafeJSONEncoder
    allow_nan = False

    def dumps(self, obj: Any, **kwargs: Any) -> str:
        # Sanitiza ANTES de serializar: nunca emite NaN/Infinity.
        obj = sanitize(obj)
        kwargs.setdefault("allow_nan", self.allow_nan)
        return super().dumps(obj, **kwargs)


def dumps(obj: Any, **kwargs: Any) -> str:
    """
    Conveniência: serializa um objeto para JSON estrito (sem NaN/Infinity),
    com `allow_nan=False` por padrão. Útil para testes e colunas TEXT.
    """
    kwargs.setdefault("allow_nan", False)
    return json.dumps(sanitize(obj), cls=SafeJSONEncoder, **kwargs)


__all__ = [
    "SafeJSONEncoder",
    "SafeJSONProvider",
    "dumps",
    "sanitize",
]
