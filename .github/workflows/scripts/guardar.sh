#!/usr/bin/env bash
# Commitea los archivos indicados y hace push, reintentando si otro workflow
# empujó antes (cada scraper escribe archivos distintos, así que el rebase no
# genera conflictos).
#   uso: bash .github/scripts/guardar.sh "mensaje" archivo_o_carpeta [...]
set -euo pipefail

mensaje="$1"; shift

git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

for ruta in "$@"; do
  if [ -e "$ruta" ]; then git add -- "$ruta"; fi
done

if git diff --cached --quiet; then
  echo "Sin cambios que guardar."
  exit 0
fi

git commit -m "$mensaje"

for intento in 1 2 3 4 5; do
  # --autostash: otros archivos modificados que no se guardan no bloquean el rebase
  if git pull --rebase --autostash --quiet && git push --quiet; then
    echo "Guardado: $mensaje"
    exit 0
  fi
  echo "Push rechazado (intento $intento/5); reintentando..."
  sleep $((intento * 10))
done

echo "::error::No se pudo hacer push después de 5 intentos."
exit 1
