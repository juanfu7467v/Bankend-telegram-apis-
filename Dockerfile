FROM python:3.12.12 AS builder

ENV PYTHONUNBUFFERED=1 
PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

Crear entorno virtual

RUN python -m venv .venv

Instalar dependencias

COPY requirements.txt ./
RUN .venv/bin/pip install --no-cache-dir -r requirements.txt

=========================

Runtime

=========================

FROM python:3.12.12-slim

ENV PYTHONUNBUFFERED=1 
PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

Copiar entorno virtual

COPY --from=builder /app/.venv /app/.venv

Copiar proyecto

COPY . .

Puerto usado por Fly

EXPOSE 8080

🔥 ARRANQUE CORRECTO CON GUNICORN

CMD ["/app/.venv/bin/gunicorn", "main:app", "--bind", "0.0.0.0:8080", "--workers", "1", "--worker-class", "gthread", "--threads", "4", "--timeout", "180"]
