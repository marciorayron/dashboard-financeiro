"""
services
========
Pacote com a lógica de negócio da aplicação.

Contém serviços desacoplados do framework web:
  * `pdf_parser`        -> extração de texto/dados de holerites em PDF.
  * `deepseek_service`  -> integração com a API da DeepSeek (LLM) para
                           interpretação e extração inteligente de dados.
  * `analytics_service` -> cálculos e agregações para o dashboard.
"""
