FROM mcr.microsoft.com/playwright/python:v1.63.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sync_sgf.py placeholder.html entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh

ENV SGF_OUT=/data/painel_sgf.html
ENV SGF_NO_SANDBOX=1
ENV SGF_A_CADA=30

USER root
ENTRYPOINT ["/app/entrypoint.sh"]
