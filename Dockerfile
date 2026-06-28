FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["gunicorn", "--chdir", "api-server", "app:app", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--worker-class", "gthread", "--timeout", "60"]
