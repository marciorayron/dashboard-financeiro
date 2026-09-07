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
- [LGPD, Privacidade & IA Segura](#lgpd-privacidade--ia-segura)
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
   Chart.js / admin.js    │  main · auth · api · profile│
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
dashboard (Chart.js) ou exportação (`/api/export`).

O projeto também possui **histórico de revisão de perfil**
(`user_profile_history`) que permite ao analytics resolver a **taxa de
pagamento ativa na data** de cada holerite, e um **painel administrativo**
para gestão de usuários, faixas de INSS/IRRF e catálogo de rubricas.

A camada de IA (`services/ai_service.py`) adiciona um assistente
conversacional contextualizado com os dados reais do dashboard (perguntas
livres, explicação de cards e de inconsistências), com **governança de
tokens** (rate limiting por plano), **bloqueio de prompt injection** e
**anonimização estrita de dados pessoais antes de qualquer chamada ao LLM**.
Por fim, o projeto implementa os **direitos do titular previstos na LGPD
(Art. 18)**: portabilidade em JSON e exclusão (hard delete em cascata).

## Stack Tecnológico

| Camada        | Tecnologia                                        |
|---------------|---------------------------------------------------|
| Backend       | Python 3.12+, Flask 3, Blueprints                  |
| Banco de dados| SQLite (WAL, anti-locking) + `schema.sql`          |
| Frontend      | Bootstrap 5, JavaScript puro, Chart.js            |
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

> **Primeiro admin:** a conta de administrador é provisionada **automaticamente**
> na primeira inicialização do banco (quando nenhum usuário com papel `admin`
> existe). Credenciais padrão — configuraveis por variáveis de ambiente:
>
> | Variável | Padrão |
> |---|---|
> | `ADMIN_EMAIL` | `admin@admin.com` |
> | `ADMIN_PASSWORD` | `admin123` |
>
> Em produção, defina `ADMIN_EMAIL`/`ADMIN_PASSWORD` no `.env` **antes** do
> primeiro startup. Para recriar/resetar a conta do admin manualmente:
> ```bash
> flask seed-admin            # cria se ainda não existir
> flask seed-admin --reset    # força a recriação/reset da conta padrão
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

## LGPD, Privacidade & IA Segura

Este ciclo de refatoração consolidou as exigências da **LGPD (Lei nº
13.709/2018)** e endureceu a segurança da integração com a IA. Resumo das
funcionalidades implementadas:

### Privacidade dos dados do titular (Art. 18)

| Recurso | Onde | Descrição |
|---|---|---|
| **Anonimização estrita de PII** | `services/ai_service.py::anonymize_payload` | Antes de **qualquer chamada ao LLM**, o contexto é percorrido recursivamente e campos de identificação pessoal (nome, CPF, CNPJ, matrícula, e-mail, cargo, empresa, `raw_text`, etc.) são **removidos**. O modelo recebe **apenas** métricas numéricas, rubricas, taxas e competências. |
| **Contexto sem PII para a IA** | `services/analytics_service.py::build_ai_paystub_context` | Constrói o contexto mês a mês e os agregados exclusivamente com números — nunca com dados pessoais — além de aplicar anonimização defensiva no formatter do prompt. |
| **Automated PDF Cleanup** | `services/user_service.py`, `routes/admin.py::/admin/api/storage/cleanup` | Exclusão de um holerite, falha de upload ou exclusão de conta **removem o PDF em disco**. Há também endpoint administrativo que limpa arquivos órfãos/temporários não referenciados. |
| **Hard Delete em cascata** | `DELETE /api/user/account` + `services/user_service.py::delete_user_account` | Direito ao esquecimento: apaga **em transação única** holerites, rubricas, perfil/histórico, logs de IA e erros de parsing, depois remove os PDFs em disco e limpa a sessão. |
| **Portabilidade (JSON)** | `GET /api/user/export-data` + `services/user_service.py::export_user_data` | Retorna todos os dados do usuário logado (perfil, paystubs, rubricas e métricas) em um payload JSON estruturado, pronto para importação em outro controlador. |

Todas as operações são **escopadas por `user_id` lido da sessão** (nunca de
parâmetros do cliente) e o `user_id` NUNCA chega ao prompt da IA.

### Arquitetura unificada de prompt (single source of truth)

Todas as interações com o LLM (`ask_ai`, `explain_anomaly`, `explain_card`)
usam **um único prompt de sistema** em `services/ai_service.py`
(`_MASTER_SYSTEM_PROMPT`), que consolida:

- **Grounding temporal** — âncoras de competência corrente e última folha
  fechada (ex.: sistema em `2026-09`, última folha `2026-08`);
- **Regras de categoria/vocabulário** — responde em português, nunca usa
  nomes técnicos em inglês/`snake_case`;
- **Metodologia de projeção** — passos transparentes para projetar o resto
  do ano (acumulado real + média × meses restantes + 13º);
- **Qualidade da resposta** — proibição de alegar "sem dados" quando há
  números reais no contexto fornecido.

O `role: system` nunca é alterado e a pergunta do usuário passa por
`sanitize_query` (teto de **200 caracteres**, rejeição de padrões de
jailbreak/sobrescrita de sistema) **antes** de consumir tokens.

### Filtragem de categorias — estrita `FOLHA_MENSAL`

Para análises de salário mensal (maior/menor salário, médias e tendências),
o contexto alimenta a IA **somente com documentos `FOLHA_MENSAL`**:

- `build_ai_paystub_context` filtra em SQL `tipo_documento = 'FOLHA_MENSAL'`
  e exclui PPR, 13º, adiantamento e férias dos agregados temporais;
- se a mesma competência tiver mais de um `FOLHA_MENSAL`, usa-se o documento
  de **maior bruto** (evita dupla contagem);
- o prompt de sistema reforça essa regra: *"considere exclusivamente
  registros da categoria FOLHA_MENSAL... ignore PPR, Adiantamento e 13º"*.

### Governança de consumo & auditoria

- **Rate limiting por plano** (`ai_usage_logs`): Gratuito → 5 consultas/24h;
  Pró → 15 consultas/1h (HTTP 429 com cooldown);
- **Registro de tentativas bloqueadas** (`ai_blocked_logs`): prompt injection
  e entradas inválidas são auditadas no painel administrativo.

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
| POST   | `/api/analytics/ask-ai`           | Pergunta livre à IA (contexto anonimizado, Art. 18).|
| POST   | `/api/analytics/explain-card`     | Explica um card do dashboard via IA.              |
| POST   | `/api/analytics/explain-anomaly`  | Explica uma inconsistência detectada (auditoria). |
| GET    | `/api/user/export-data`          | **Portabilidade JSON** dos dados do usuário (LGPD).|
| DELETE | `/api/user/account`               | **Hard delete em cascata** (LGPD Art. 18).        |
| POST   | `/admin/api/storage/cleanup`      | Remove PDFs órfãos/temporários (automated cleanup).|
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
test client com banco isolado). São **137 testes passando** atualmente:

```bash
# Instala o runtime + pytest (as dependências de teste ficam em
# requirements-dev.txt, que referencia o requirements.txt)
pip install -r requirements-dev.txt

# Roda a suíte completa a partir da raiz do projeto
pytest tests -v
# ou, de forma compacta:
.venv/Scripts/python -m pytest tests -q
```

Cobertura principal:

- `tests/test_security.py` — LGPD (hard delete em cascata, portabilidade,
  contexto de IA sem PII), prompt injection, RBAC e isolamento por usuário.
- `tests/test_analytics.py` — INSS/IRRF progressivos, horas extras em
  patamares, agregação do líquido recorrente e contexto da IA restrito a
  `FOLHA_MENSAL`.
- `tests/test_routes.py` — autenticação, endpoints principais e exportação
  (CSV/XLSX, isolamento por usuário).
- `tests/test_admin.py` — gestão de usuários no painel (`/admin`), limites
  Freemium e endpoints administrativos protegidos.
- `tests/test_savings.py` — simulação de meta de investimento/ano.
- `tests/test_multi_tenant.py` — RBAC (403 no `/admin`), histórico de perfil
  e cálculo histórico por competência.
- `tests/test_admin_monitoring.py`, `tests/test_safe_json.py` e
  `tests/test_seed.py` — monitoramento de parsing, JSON estritamente válido
  e provisionamento do admin padrão.

## Estrutura do Projeto

```
dashboard-financeiro/
├── app.py                  # App factory Flask
├── config.py               # Configurações por ambiente (dev/prod/test)
├── requirements.txt        # Dependências de runtime (pins exatos)
├── requirements-dev.txt    # Dependências de teste (pytest)
├── .env.example            # Template de variáveis de ambiente
├── docker-compose.yml      # Deploy ZimaOS/Docker
├── Dockerfile              # Imagem de produção (python:3.13-slim)
├── database/
│   ├── connection.py       # Conexão SQLite (WAL) + migrações
│   ├── queries.py          # Consultas auxiliares (admin/orfanato)
│   ├── seed.py             # Provisionamento do admin padrão
│   └── schema.sql          # Modelo relacional
├── models/
│   ├── profile.py          # Perfil + histórico de revisões
│   └── user.py             # Usuário + RBAC (admin/user) + planos
├── routes/
│   ├── api.py              # API (upload, analytics, export, LGPD)
│   ├── auth.py             # Login/registro + decorators
│   ├── admin.py            # Painel administrativo + storage cleanup
│   ├── main.py             # Páginas
│   ├── profile.py          # Perfil
│   └── reports.py          # Dossiê PDF (ReportLab)
├── services/
│   ├── analytics_service.py# Cálculos, agregações e contexto da IA
│   ├── ai_service.py       # IA conversacional + anonimização de PII
│   ├── settings_service.py # Faixas de impostos + catálogo + limites IA
│   ├── user_service.py     # LGPD: delete em cascata + portabilidade
│   ├── admin_service.py    # Métricas/gestão do painel + FinOps de IA
│   ├── auth_service.py     # Políticas de plano (limite de holerites)
│   ├── savings_service.py  # Simulação de meta de investimento/ano
│   ├── monitoring_service.py# Erros de parsing (auditoria)
│   ├── db_service.py       # Migrações/classificação de rubricas
│   ├── deepseek_service.py # Parsing universal (LLM via requests)
│   ├── pdf_parser.py       # Extração de PDFs
│   └── safe_json.py        # Serialização JSON estrita (sem NaN/Inf)
├── static/                 # CSS + JS (dashboard, admin)
├── templates/              # HTML (login, dashboard, admin, erros)
└── tests/                  # Suíte de testes (137) — pytest/unittest
    ├── conftest.py, test_routes.py, test_analytics.py, test_security.py,
    ├── test_admin.py, test_admin_monitoring.py, test_multi_tenant.py,
    ├── test_savings.py, test_safe_json.py e test_seed.py
```

## Licença

Projeto interno/exemplo. Consulte a organização mantenedora para detalhes de
uso e distribuição.

