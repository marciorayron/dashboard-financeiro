# =====================================================================
# Dockerfile
# Dashboard Financeiro Pessoal
# Imagem de produção baseada em Python 3.13-slim (multi-stage).
# =====================================================================

# --- Estágio de construção -------------------------------------------
FROM python:3.13-slim AS builder

# Diretório de trabalho.
WORKDIR /app

# Instala as dependências de runtime primeiro (aproveita o cache de camadas).
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- Estágio final (runtime) ----------------------------------------
FROM python:3.13-slim AS runtime

# Evita geração de arquivos .pyc e bufferização de saída.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_ENV=production \
    FLASK_APP=app.py

WORKDIR /app

# Copia as dependências instaladas no estágio builder.
COPY --from=builder /install /usr/local

# Copia o código-fonte da aplicação.
COPY . .

# Cria o usuário não-root para segurança.
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/data /app/uploads \
    && chown -R appuser:appuser /app

# Volumes persistentes: banco de dados e uploads.
VOLUME ["/app/data", "/app/uploads"]

# Porta exposta pela aplicação.
EXPOSE 5000

# Usuário não-root.
USER appuser

# Healthcheck: verifica se a aplicação responde.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health')" || exit 1

# Comando de inicialização com gunicorn (servidor WSGI de produção).
# O número de workers é configurável via GUNICORN_WORKERS (padrão 2).
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:5000 --workers ${GUNICORN_WORKERS:-2} --timeout 120 app:app"]

