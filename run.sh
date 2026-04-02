#!/bin/bash
# run.sh — avvia uvicorn liberando prima la porta 8000
#
# Uso:
#   ./run.sh              # avvia con reload (sviluppo)
#   ./run.sh --prod       # avvia senza reload (produzione)
#   ./run.sh --port 8080  # porta diversa

PORT=8000
PROD=0

# Leggi argomenti
while [[ $# -gt 0 ]]; do
  case $1 in
    --prod)  PROD=1; shift ;;
    --port)  PORT="$2"; shift 2 ;;
    *)       shift ;;
  esac
done

# Killa tutto quello che usa la porta
echo "→ Libero porta $PORT..."
lsof -ti tcp:$PORT | xargs kill -9 2>/dev/null && echo "  Processo terminato" || echo "  Porta libera"
sleep 0.5

# Avvia
if [ $PROD -eq 1 ]; then
  echo "→ Avvio uvicorn (produzione) su http://0.0.0.0:$PORT"
  # riga 29
  python3 -m uvicorn main:app --host 0.0.0.0 --port $PORT
else
  echo "→ Avvio uvicorn (sviluppo) su http://127.0.0.1:$PORT"
  python3 -m uvicorn main:app --reload --port $PORT
fi
