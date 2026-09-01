"""
config.py
---------
Centraliza toda a configuração da aplicação Flask.

A configuração é carregada de variáveis de ambiente (via `.env`) e pode
ser sobrescrita por classes específicas de ambiente (Dev/Prod/Test).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Carrega as variáveis do arquivo `.env` se ele existir.
# (Nunca falha se o arquivo não estiver presente.)
load_dotenv()

# Diretório raiz do projeto (subindo um nível a partir de `config.py`).
BASE_DIR = Path(__file__).resolve().parent


class Config:
    """Configuração base compartilhada por todos os ambientes."""

    # Segredo para assinar sessões e cookies.
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-me")

    # Caminho absoluto do banco SQLite.
    # Aceita `DATABASE_PATH` (caminho puro) ou `DATABASE_URL` (alias no formato
    # SQLAlchemy `sqlite:///caminho`). Quando `DATABASE_URL` está definida, ela
    # tem precedência — útil em ambientes que padronizam a configuração via URL.
    _DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
    if _DATABASE_URL:
        _db_path = _DATABASE_URL
        if _db_path.startswith("sqlite:///"):
            _db_path = _db_path[len("sqlite:///"):]
        _db_path = os.path.expanduser(_db_path)
    else:
        _db_path = os.getenv(
            "DATABASE_PATH",
            str(BASE_DIR / "data" / "financeiro.db"),
        )
    DATABASE_PATH = _db_path

    # Pasta para armazenar os PDFs enviados.
    UPLOAD_FOLDER = os.getenv(
        "UPLOAD_FOLDER",
        str(BASE_DIR / "uploads"),
    )

    # Tamanho máximo de upload: 16 MB.
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024

    # Extensões permitidas para upload de holerites.
    ALLOWED_EXTENSIONS = {"pdf"}

    # Integração com a API DeepSeek.
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    # Parâmetros do SQLite (anti-locking).
    SQLITE_PRAGMAS = {
        "journal_mode": "WAL",        # Write-Ahead Logging
        "busy_timeout": 5000,         # 5 segundos antes de levantar erro
        "foreign_keys": "ON",         # integridade referencial
        "synchronous": "NORMAL",      # equilíbrio segurança/performance no WAL
    }

    # Usuários iniciais suportados (multi-tenant).
    MAX_USERS = 10

    # Configuração da sessão (cookie assinado com SECRET_KEY).
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False  # True atrás de HTTPS/TLS em produção
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 24 * 7  # 7 dias

    # Extensão dos arquivos exportados.
    EXPORT_ALLOWED_EXTENSIONS = {"xlsx", "csv"}


class DevelopmentConfig(Config):
    """Configuração de desenvolvimento (padrão)."""

    DEBUG = True


class ProductionConfig(Config):
    """Configuração de produção (mais rígida)."""

    DEBUG = False

    # Em produção exigimos uma SECRET_KEY forte definida no ambiente.
    SECRET_KEY = os.getenv("SECRET_KEY")

    # Arquivos estáticos com hash para cache-busting.
    SEND_FILE_MAX_AGE_DEFAULT = 31536000  # 1 ano


class TestingConfig(Config):
    """Configuração de testes (banco em memória, sem persistir)."""

    TESTING = True
    DATABASE_PATH = ":memory:"
    SECRET_KEY = "test-secret-key"


# Mapeamento usado pelo `app.py` para selecionar a configuração por ambiente.
config_by_name = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}


def get_config(env: str = None):
    """Retorna a classe de configuração adequada para o ambiente informado."""
    env = (env or os.getenv("FLASK_ENV", "development")).lower()
    return config_by_name.get(env, config_by_name["default"])
