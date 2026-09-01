"""
connection.py
-------------
Gerencia a conexão com o banco de dados SQLite.

Anti-locking / multi-concorrência:
  * `PRAGMA journal_mode=WAL`  -> habilita Write-Ahead Logging, permitindo
    leituras simultâneas a uma escrita.
  * `PRAGMA busy_timeout=5000` -> aguarda até 5s quando o banco estiver
    bloqueado antes de levantar `database is locked`.
  * `PRAGMA synchronous=NORMAL`-> no modo WAL, o nível NORMAL oferece boa
    durabilidade com menos I/O (recomendado pelo SQLite).
  * `PRAGMA foreign_keys=ON`   -> garante integridade referencial.

Conexão por request:
  * Usamos `flask.g` para armazenar UMA conexão por request/contexto.
  * O `teardown_appcontext` fecha a conexão automaticamente ao final do
    request, mesmo em caso de erro.
"""
import sqlite3
from pathlib import Path

from flask import current_app, g


def get_db():
    """
    Retorna a conexão com o SQLite para o contexto atual.

    Se não existir conexão associada ao contexto (`g`) atual, cria uma,
    aplica os PRAGMAs de configuração e a guarda em `g._database`.

    Retorna:
        sqlite3.Connection: conexão configurada com row_factory de dict.
    """
    if "_database" not in g:
        db_path = current_app.config["DATABASE_PATH"]

        # Garante que o diretório do banco exista (ex.: `data/`).
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # A flag `check_same_thread=False` é necessária para o Flask, que
        # pode executar a conexão em um contexto diferente do de criação.
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row  # linhas acessíveis como dict

        # Aplica os PRAGMAs anti-locking definidos na configuração.
        pragmas = current_app.config.get("SQLITE_PRAGMAS", {})
        for key, value in pragmas.items():
            conn.execute(f"PRAGMA {key}={value};")

        # Para banco em memória (`:memory:`), cada conexão é um banco novo
        # e vazio. Criamos o schema automaticamente para que consultas nos
        # testes/contextos curtos encontrem as tabelas.
        if db_path == ":memory:":
            _exec_schema(conn)

        g._database = conn

    return g._database


def close_db(_exc=None):
    """
    Fecha a conexão armazenada no contexto atual.

    Registrado como `teardown_appcontext`, então é chamado automaticamente
    ao final de cada request/contexto, liberando os recursos e garantindo
    que o arquivo WAL seja compactado corretamente.
    """
    conn = g.pop("_database", None)

    if conn is not None:
        conn.close()


def _exec_schema(conn):
    """
    Executa o script `schema.sql` em uma conexão fornecida.

    Usa `CREATE TABLE IF NOT EXISTS`, portanto é idempotente. Separado em
    um helper para permitir a inicialização automática em bancos de memória.
    """
    schema_path = Path(__file__).resolve().parent / "schema.sql"
    schema_sql = schema_path.read_text(encoding="utf-8")
    # `executescript` roda o arquivo todo em uma transação.
    conn.executescript(schema_sql)
    _apply_migrations(conn)


def _apply_migrations(conn):
    """
    Aplica migrações de schema em bancos já existentes (idempotente).

    O `schema.sql` usa `CREATE TABLE IF NOT EXISTS`, então bancos antigos
    não recebem colunas/tabelas novas automaticamente. Este helper executa
    as migrações incrementais necessárias para compatibilizar o schema:
      * `users.role`          -> RBAC (admin/user).
      * `user_profile_history`-> histórico de revisões do perfil.
      * `app_settings`        -> configurações globais editáveis no /admin.
    """
    # Importa no corpo para evitar ciclos de import com models.
    from models.user import ensure_user_role_column
    from models.profile import create_user_profile_history_table
    from services.settings_service import ensure_settings_table
    from services.monitoring_service import ensure_parse_errors_table

    ensure_user_role_column(conn)
    create_user_profile_history_table(conn)
    ensure_settings_table(conn)
    ensure_parse_errors_table(conn)
    conn.commit()


def init_db():
    """
    Inicializa o banco de dados executando o script `schema.sql` e garante a
    existência do administrador padrão (RBAC).

    É chamado no startup da aplicação (ver `app.py`). Executar múltiplas
    vezes é seguro: as instruções usam `CREATE TABLE IF NOT EXISTS` e o seeder
    é idempotente (só cria o admin quando nenhum usuário `admin` existe).

    Nota: usuários regulares são criados via registro autenticado
    (ver `routes/auth.py`); o seeder cobre apenas o primeiro admin.
    """
    conn = get_db()
    _exec_schema(conn)

    # Importa no corpo para evitar ciclos de import com models/seed.
    from database.seed import seed_default_admin

    seed_default_admin(conn)
    conn.commit()

