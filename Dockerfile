FROM python:3.12-slim

# Evita .pyc y fuerza logs sin buffer (se ven en tiempo real en docker logs).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Instalar dependencias primero para aprovechar la cache de capas.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código de la app.
COPY bot.py .

# Ejecutar como usuario sin privilegios.
RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["python", "bot.py"]
