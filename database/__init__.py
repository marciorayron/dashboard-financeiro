"""
database
========
Pacote responsável pela camada de acesso a dados da aplicação.

Expose funções e utilitários para gerenciar a conexão com o SQLite,
inicializar o schema e executar consultas de forma isolada por usuário.
"""
from .connection import close_db, get_db, init_db  # noqa: F401

__all__ = ["get_db", "close_db", "init_db"]
