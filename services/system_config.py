"""
services/system_config.py
--------------------------
Configuração administrativa persistida em disco (runtime overrides).

Enquanto `config.py` lê as variáveis de ambiente do `.env`, alguns valores são
editáveis pelo painel administrativo (um *admin* autenticado) em tempo real:

  * Credenciais do provedor de IA (``DEEPSEEK_API_KEY`` / ``DEEPSEEK_BASE_URL``)
    — aplicadas imediatamente em ``current_app.config`` e restauradas no boot.
  * ``UPLOAD_FOLDER`` — aplicada imediatamente e restaurada no boot.
  * ``DATABASE_PATH`` — NÃO é trocada a quente (o SQLite já está aberto); o
    valor fica **pendente** e passa a valer somente após um reinício seguro.

O estado é gravado em um JSON fora do repositório (``instance/admin_config.json``)
e **nunca** contém a chave em claro nas respostas de API (apenas mascarada).

Segurança: o JSON vive dentro de `instance/`, que já é ignorado no `.gitignore`
e fora da raiz de estáticos — mesma sensibilidade de um `.env`.
"""
import json
from pathlib import Path
from typing import Any, Dict, Optional

# Diretório raiz do projeto (um nível acima deste módulo).
_BASE_DIR = Path(__file__).resolve().parent.parent

# Caminho do arquivo de overrides (fora do versionamento).
OVERRIDE_PATH = _BASE_DIR / "instance" / "admin_config.json"

# URL base padrão do provedor de IA (DeepSeek).
DEFAULT_AI_BASE_URL = "https://api.deepseek.com"

# Valores de sistema que podem ser sobrescritos em runtime.
PATH_KEYS = ("UPLOAD_FOLDER", "DATABASE_PATH")
AI_KEYS = ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL")


def _read() -> Dict[str, Any]:
    """Lê o arquivo de overrides. Retorna dict vazio se ausente/corrompido."""
    try:
        return json.loads(OVERRIDE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def _write(data: Dict[str, Any]) -> None:
    """Grava o estado no disco, garantindo que o diretório `instance` exista."""
    OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_overrides() -> Dict[str, Any]:
    """Retorna o dict completo de overrides: ``{"paths": {...}, "ai": {...}}``."""
    return _read()


def save_paths(
    upload_folder: Optional[str] = None, database_path: Optional[str] = None
) -> Dict[str, Any]:
    """Persiste caminhos de sistema escolhidos pelo admin (sobre .env)."""
    overrides = _read()
    paths = overrides.setdefault("paths", {})
    if upload_folder is not None:
        paths["UPLOAD_FOLDER"] = upload_folder
    if database_path is not None:
        paths["DATABASE_PATH"] = database_path
    # Remove chaves vazias para não sobrescrever o `.env` com valores nulos.
    for key in PATH_KEYS:
        if key in paths and not paths[key]:
            del paths[key]
    if not overrides.get("paths"):
        overrides.pop("paths", None)
    _write(overrides)
    return paths


def save_ai_provider(
    api_key: Optional[str] = None, base_url: Optional[str] = None
) -> Dict[str, Any]:
    """
    Persiste credenciais do provedor de IA.

    ``api_key=None`` mantém o valor atual. ``api_key=""`` desabilita a IA.
    ``base_url=""``/``None`` restaura a URL padrão do provedor.
    """
    overrides = _read()
    ai = overrides.setdefault("ai", {})
    if api_key is not None:
        ai["DEEPSEEK_API_KEY"] = api_key
    if base_url is not None:
        ai["DEEPSEEK_BASE_URL"] = base_url or DEFAULT_AI_BASE_URL
    _write(overrides)
    return ai


def apply_to_config(app, env: Optional[str] = None) -> None:
    """
    Aplica os overrides persistidos sobre ``app.config`` no boot.

    Deve ser chamado em `create_app`, ANTES de inicializar o banco, para que
    ``DATABASE_PATH``/``UPLOAD_FOLDER`` alterados façam efeito.

    Em ambiente de teste a aplicação ignora os overrides (cada suíte usa seu
    próprio banco/upload isolado — ver ``tests/conftest.py``).
    """
    if env and str(env).lower() == "testing":
        return
    overrides = _read()

    paths = overrides.get("paths") or {}
    for key in ("UPLOAD_FOLDER", "DATABASE_PATH"):
        if paths.get(key):
            app.config[key] = paths[key]

    ai = overrides.get("ai") or {}
    if ai.get("DEEPSEEK_API_KEY") is not None:
        app.config["DEEPSEEK_API_KEY"] = ai["DEEPSEEK_API_KEY"]
    if ai.get("DEEPSEEK_BASE_URL"):
        app.config["DEEPSEEK_BASE_URL"] = ai["DEEPSEEK_BASE_URL"]
