FROM python:3.12-slim

WORKDIR /app

# System-Pakete (psycopg2 hat Build-Deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
 && rm -rf /var/lib/apt/lists/*

# Python-Deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App-Code
COPY app ./app
COPY init_db.py .

# Render / Railway setzen PORT via ENV. Default 8000 für lokale Tests.
ENV PORT=8000
EXPOSE 8000

# Entrypoint: DB-Schema sicherstellen, dann uvicorn
CMD sh -c "python init_db.py && uvicorn app.main:app --host 0.0.0.0 --port $PORT"
