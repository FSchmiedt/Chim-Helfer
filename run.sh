#!/usr/bin/env bash
# Lokaler Dev-Start
set -e

if [ ! -f .env ]; then
    cp .env.example .env
    echo "✓ .env aus .env.example erstellt – bitte prüfen!"
fi

if [ ! -d .venv ]; then
    python3 -m venv .venv
    echo "✓ Virtualenv angelegt"
fi

# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

python init_db.py --with-days

echo ""
echo "🚀 Starte Dev-Server auf http://localhost:8000"
echo "   Anmeldeformular: http://localhost:8000/"
echo "   Admin:           http://localhost:8000/admin  (Login laut .env)"
echo ""

uvicorn app.main:app --reload --port 8000
