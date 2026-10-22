FROM python:3.12-slim

WORKDIR /app
COPY requirements-app.txt .
RUN pip install --no-cache-dir -r requirements-app.txt
COPY app.py .
COPY templates ./templates

ENV GEFIN_PAINEL=/data/painel_sgf.html
ENV GEFIN_DB=/usuarios/usuarios.db

EXPOSE 8000
CMD ["gunicorn", "-b", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "app:app"]
