"""
services
========
Pacote com a lógica de negócio da aplicação.

Contém serviços desacoplados do framework web:
  * `pdf_parser`        -> extração de texto/dados de holerites em PDF.
  * `deepseek_service`  -> integração com a API da DeepSeek (LLM) para
                           interpretação e extração inteligente de dados.
  * `analytics_service` -> cálculos e agregações para o dashboard.
  * `ai_service`        -> IA conversacional (DeepSeek), validação de prompt,
                           logging de consumo e rate limiting (Freemium).
  * `auth_service`      -> políticas de plano (limite de holerites, planos
                           Free vs Pro).
"""
