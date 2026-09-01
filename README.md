# Dashboard Financeiro Pessoal (CLT)

Dashboard multi-tenant para análise financeira de contracheques (holerites) CLT.
Importe PDFs, acompanhe proventos/descontos/líquido, simule horas extras,
projete o ano e exporte seus dados em **CSV/XLSX** — tudo isolado por usuário.

---

## Índice

- [Visão Geral & Arquitetura](#visão-geral--arquitetura)
- [Stack Tecnológico](#stack-tecnológico)
- [Quickstart — Execução Local (venv)](#quickstart--execução-local-venv)
- [Configuração de Ambiente (variáveis)](#configuração-de-ambiente-variáveis)
- [Quickstart — Docker Compose (ZimaOS)](#quickstart--docker-compose-zimaos)
- [Painel Administrativo (/admin)](#painel-administrativo-admin)
- [API Reference](#api-reference)
- [Testes Automatizados](#testes-automatizados)
- [Estrutura do Projeto](#estrutura-do-projeto)

---

## Visão Geral & Arquitetura

A aplicação é uma **app factory Flask** com arquitetura em camadas e
**multi-tenancy real**: todas as tabelas `core` são escopadas por `user_id`,
lido sempre da sessão autenticada (nunca de parâmetros do cliente).

```
                        ┌─────────────────────────────┐
   Navegador (Bootstrap)│  Blueprints (routes/)       │
   Plotly / admin.js    │  main · auth · api · profile│
                        │  reports · admin            │
                        └──────────┬──────────────────┘
                                   │
              ┌────────────────────┼─────────────────────┐
              ▼                    ▼                     ▼
       services/            models/ (domínio)       database/
  analytics · settings     profile · user          connection · schema
  deepseek · db · pdf_parser                        (SQLite / WAL)
              │
              └── pandas (export) / reportlab (PDF)
```

**Fluxo de dados:** o PDF é enviado → texto extraído (pypdf/pdfplumber) →
parsing universal via DeepSeek com fallback local → persistência em `holerites`
+ `rubricas_holerite` → agregações em `analytics_service` → renderização no
dashboard (Plotly) ou exportação (`/api/export`).

O projeto também possui **histórico de revisão de perfil**
(`user_profile_history`) que permite ao analytics resolver a **taxa de
pagamento ativa na data** de cada holerite, e um **painel administrativo**
para gestão de usuários, faixas de INSS/IRRF e catálogo de rubricas.

## Stack Tecnológico

| Camada        | Tecnologia                                        |
|---------------|---------------------------------------------------|
| Backend       | Python 3.12+, Flask 3, Blueprints                  |
| Banco de dados| SQLite (WAL, anti-locking) + `schema.sql`          |
| Frontend      | Bootstrap 5, JavaScript puro, Plotly.js            |
| PDFs          | pypdf, pdfplumber (extração), ReportLab (dossiê)   |
| IA (opcional) | DeepSeek API para parsing universal                |
| Exportação    | pandas + openpyxl (XLSX) / CSV                     |
| Infra         | Docker, Docker Compose, gunicorn                   |
| Testes        | pytest, unittest, Flask test client                |

## Quickstart — Execução Local (venv)

Pré-requisitos: **Python 3.10+** (o projeto foi testado com 3.12/3.13).

```bash
# 1) Clone e entre no diretório
git clone <seu-repositorio> dashboard-financeiro
cd dashboard-financeiro

# 2) Crie e ative o ambiente virtual
python -m venv .venv
# Windows (PowerShell):
.\.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

# 3) Instale as dependências (runtime)
pip install -r requirements.txt
# (opcional) inclui ferramentas de teste:
#   pip install -r requirements-dev.txt

# 4) Configure o ambiente (opcional; usa defaults se omitido)
cp .env.example .env
# edite o SECRET_KEY e demais valores, se desejar

# 5) Inicie a aplicação
python app.py
# ou via Flask:
# flask --app app.py run --host 0.0.0.0 --port 5000
```

Acesse **http://localhost:5000**, crie uma conta e comece a importar PDFs.
O banco SQLite é criado automaticamente em `data/financeiro.db` (com as
tabelas e migrações aplicadas no startup).

> **Primeiro admin:** o papel de administrador é definido via banco
> (`users.role = 'admin'`). Após criar a primeira conta, promova-a com:
> ```bash
> .venv/Scripts/python -c "
> from app import create_app
> from database.connection import get_db
> from models.user import set_user_role, get_user_by_email
> app = create_app('development')
> with app.app_context():
>     db = get_db()
>     u = get_user_by_email(db, 'seu@email.com')
>     set_user_role(db, u.id, 'admin')
> "
> ```

## Configuração de Ambiente (variáveis)

A aplicação lê configuração de variáveis de ambiente (via `.env`, carregado por
`python-dotenv`). Copie `.env.example` para `.env` e ajuste:

| Variável | Descrição | Padrão |
|---|---|---|
| `FLASK_ENV` | `development` \| `production` \| `testing` | `development` |
| `FLASK_APP` | Entrada da aplicação (`app.py`) | `app.py` |
| `FLASK_DEBUG` | Ativa o debugger do Flask (só em dev) | `1` |
| `SECRET_KEY` | **Obrigatória** — assina sessões/cookies. Gere com `python -c "import secrets; print(secrets.token_hex(32))"` | *(vazio)* |
| `DATABASE_PATH` | Caminho do banco SQLite (relativo ou absoluto) | `data/financeiro.db` |
| `DATABASE_URL` | Alias opcional (`sqlite:///caminho` ou caminho puro). Tem precedência sobre `DATABASE_PATH` | *(vazio)* |
| `UPLOAD_FOLDER` | Pasta dos PDFs enviados | `uploads` |
| `MAX_USERS` | Limite de contas (multi-tenant) | `10` |
| `DEEPSEEK_API_KEY` | Chave da API DeepSeek (vazio = IA desabilitada) | *(vazio)* |
| `DEEPSEEK_BASE_URL` | URL base da API DeepSeek | `https://api.deepseek.com` |
| `SESSION_COOKIE_SECURE` | Marca o cookie como `Secure` (use `1` só atrás de HTTPS) | `0` |
| `GUNICORN_WORKERS` | Nº de workers do gunicorn (produção/Docker) | `2` |

> **Segurança:** nunca commite o `.env` real — ele está no `.gitignore`. Apenas
> o template `.env.example` (sem segredos) é versionado. Rode
> `git check-ignore .env` para confirmar.

## Quickstart — Docker Compose (ZimaOS)

O **ZimaOS** já traz o Docker embutido. Para subir a aplicação:

1. Copie o repositório para um local persistente no ZimaOS (ex.: volume).
2. Crie o arquivo `.env` a partir do template e defina um `SECRET_KEY` forte:
   ```bash
   cp .env.example .env
   ```
3. **(Opcional)** valide a construção da imagem localmente:
   ```bash
   docker compose build
   ```
4. Suba o serviço:
   ```bash
   docker compose up -d --build
   ```
5. Acompanhe a saúde do container (healthcheck em `/health`):
   ```bash
   docker compose ps
   ```
6. Acesse **http://<ip-do-zimaos>:5000**.

Volumes persistentes (`data/` e `uploads/`) garantem que banco e PDFs
sobrevivam a reinícios. Para atualizar depois de alterações:

```bash
docker compose up -d --build
```

Para ver os logs:

```bash
docker compose logs -f dashboard-financeiro
```

## Painel Administrativo (/admin)

O blueprint `routes/admin.py` expõe o painel sob `/admin`, protegido por
`@admin_required` (papel `admin`; não-admins recebem **HTTP 403**).

- **Gestão de usuários** — ativar/desativar contas, trocar papel (admin/user)
  e resetar senha (gera senha temporária).
- **Métricas globais** — total de usuários, holerites processados e erros de
  parsing não resolvidos.
- **Impostos & Configurações** — editar faixas progressivas de **INSS/IRRF** e
  o **catálogo de rubricas** (sem novo deploy).
- **Validação do histórico** — avalia a taxa de pagamento ativa por competência.

## API Reference

Todas as rotas de API são escopadas ao usuário autenticado via sessão.

| Método | Endpoint                          | Descrição                                          |
|--------|-----------------------------------|----------------------------------------------------|
| POST   | `/register`                       | Cria uma conta (nome, e-mail, senha).              |
| POST   | `/login`                          | Autentica e abre a sessão.                         |
| GET    | `/logout`                         | Encerra a sessão.                                  |
| GET    | `/`                               | Dashboard do usuário (página).                     |
| POST   | `/api/upload`                     | Envia um PDF de holerite e importa os dados.       |
| GET    | `/api/summary`                    | KPIs agregados (proventos, descontos, líquido).    |
| GET    | `/api/monthly`                    | Série temporal mensal (bruto × líquido).           |
| GET    | `/api/descontos`                  | Descontos agregados por rubrica.                   |
| GET    | `/api/empresas`                   | Lista empregadores do usuário.                     |
| GET    | `/api/analytics/advanced`         | Overtime, taxas efetivas, base × variável.         |
| GET    | `/api/analytics/overtime-impact`  | Simula horas extras (impacto marginal).            |
| GET    | `/api/analytics/audit`            | Auditoria/anomalias nos paystubs.                  |
| GET    | `/api/analytics/projection`       | Projeção financeira anual (13º, férias, PPR).      |
| GET    | `/api/holerites`                  | Lista holerites do usuário.                        |
| GET    | `/api/holerites/<id>`             | Detalhe do holerite (rubricas + texto).            |
| DELETE | `/api/holerites/<id>`             | Exclui um holerite.                                |
| GET    | `/api/export?format=xlsx\|csv`    | **Exporta** holerites/rubricas em XLSX ou CSV.     |
| GET    | `/api/reports/dossier-pdf`        | Dossiê de capacidade financeira em PDF.            |
| GET    | `/api/profile` / `POST`           | Lê/atualiza o perfil de contratação do usuário.    |
| GET    | `/admin`                          | Painel administrativo (papel `admin`).             |
| GET    | `/health`                         | Healthcheck público.                               |

### Exemplo de exportação

```bash
# CSV
curl -b cookies.txt "http://localhost:5000/api/export?format=csv" -o holerites.csv

# XLSX
curl -b cookies.txt "http://localhost:5000/api/export?format=xlsx" -o holerites.xlsx
```

O `Content-Type` retornado é `text/csv` ou
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, conforme
o formato solicitado.


## Testes Automatizados

A suíte usa **pytest** (com `tests/conftest.py` fornecendo fixtures de app e
test client com banco isolado):

```bash
pip install -r requirements.txt        # inclui pytest
pytest tests -v
# ou
.venv/Scripts/python -m pytest tests -q
```

Cobertura principal:

- `tests/test_analytics.py` — INSS/IRRF progressivos, horas extras em
  patamares, agregação do líquido recorrente.
- `tests/test_routes.py` — autenticação, endpoints principais e exportação
  (CSV/XLSX, isolamento por usuário).
- `tests/test_multi_tenant.py` — RBAC (403 no `/admin`), histórico de perfil
  e cálculo histórico por competência.

## Estrutura do Projeto

```
dashboard-financeiro/
├── app.py                  # App factory Flask
├── config.py               # Configurações por ambiente (dev/prod/test)
├── requirements.txt
├── .env.example            # Template de variáveis de ambiente
├── docker-compose.yml      # Deploy ZimaOS/Docker
├── Dockerfile              # Imagem de produção (python:3.12-slim)
├── database/
│   ├── connection.py       # Conexão SQLite (WAL) + migrações
│   └── schema.sql          # Modelo relacional
├── models/
│   ├── profile.py          # Perfil + histórico de revisões
│   └── user.py             # Usuário + RBAC (admin/user)
├── routes/
│   ├── api.py              # API (upload, analytics, export)
│   ├── auth.py             # Login/registro + decorators
│   ├── admin.py            # Painel administrativo
│   ├── main.py             # Páginas
│   ├── profile.py          # Perfil
│   └── reports.py          # Dossiê PDF (ReportLab)
├── services/
│   ├── analytics_service.py# Cálculos e agregações
│   ├── settings_service.py # Faixas de impostos + catálogo
│   ├── db_service.py       # Migrações/classificação de rubricas
│   ├── deepseek_service.py # Parsing universal (IA)
│   └── pdf_parser.py       # Extração de PDFs
├── static/                 # CSS + JS (dashboard, admin)
├── templates/              # HTML (login, dashboard, admin, erros)
└── tests/                  # Suíte de testes (pytest/unittest)
```

## Licença

Projeto interno/exemplo. Consulte a organização mantenedora para detalhes de
uso e distribuição.

