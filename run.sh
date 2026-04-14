#!/bin/bash

PORT=8000
PROD=0

# Vai nella directory dello script (IMPORTANTE)
cd "$(dirname "$0")"

# Attiva venv automaticamente
if [ ! -d "venv" ]; then
  echo "→ Creo venv..."
  python3 -m venv venv
fi

echo "→ Attivo virtualenv..."
source venv/bin/activate

# Installa requirements se mancano
echo "→ Controllo dependencies..."
pip install -r requirements.txt > /dev/null 2>&1

# Leggi argomenti
while [[ $# -gt 0 ]]; do
  case $1 in
    --prod)  PROD=1; shift ;;
    --port)  PORT="$2"; shift 2 ;;
    *)       shift ;;
  esac
done

# Killa porta
echo "→ Libero porta $PORT..."
lsof -ti tcp:$PORT | xargs kill -9 2>/dev/null && echo "  Processo terminato" || echo "  Porta libera"
sleep 0.5

# Avvio
if [ $PROD -eq 1 ]; then
  echo "→ Avvio uvicorn (produzione) su http://0.0.0.0:$PORT"
  python3 -m uvicorn main:app --host 0.0.0.0 --port $PORT
else
  echo "→ Avvio uvicorn (sviluppo) su http://127.0.0.1:$PORT"
  python3 -m uvicorn main:app --reload --port $PORT
fi