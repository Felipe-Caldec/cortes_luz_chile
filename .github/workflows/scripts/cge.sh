#!/usr/bin/env bash
# Una lectura del mapa de CGE, compartida por cge.yml (horario fijo) y
# cge_reintento.yml (cada 30 min, solo mientras CGE esté caído).
#
#   - Si la lectura funciona: se guarda y se DESACTIVA cge_reintento.yml.
#   - Si falla: se ACTIVA cge_reintento.yml, que vuelve a intentar cada
#     30 min hasta conseguir datos y luego se desactiva solo. El horario fijo
#     sigue igual, aunque la siguiente lectura quede a menos de 6 h.
#   - La ejecución se marca en rojo (y GitHub avisa por correo) solo si CGE
#     lleva más de MAX_HORAS_SIN_DATOS sin una lectura exitosa; las caídas
#     cortas quedan como advertencia, para no recibir un correo cada 30 min.
#
# Requiere GH_TOKEN en el entorno y permisos "actions: write" y
# "contents: write" en el workflow.
set -uo pipefail

REINTENTO="cge_reintento.yml"
MAX_HORAS_SIN_DATOS="${MAX_HORAS_SIN_DATOS:-12}"
API="repos/${GITHUB_REPOSITORY}/actions/workflows/${REINTENTO}"
ahora_cl() { TZ=America/Santiago date '+%Y-%m-%d %H:%M'; }

if python cge_interrupciones.py --geojson; then
  bash .github/scripts/guardar.sh "CGE: lectura $(ahora_cl)" \
    cge_interrupciones.csv cge_geocache.csv cge_ultima_lectura.txt mapas || exit 1

  estado=$(gh api "$API" --jq .state 2>/dev/null || echo "desconocido")
  if [ "$estado" = "active" ]; then
    gh api -X PUT "$API/disable" && echo "Reintentos cada 30 min desactivados."
  fi
  exit 0
fi

echo "::warning::El mapa de CGE no respondió ($(ahora_cl)). Se reintentará en 30 min."
estado=$(gh api "$API" --jq .state 2>/dev/null || echo "desconocido")
if [ "$estado" != "active" ]; then
  if gh api -X PUT "$API/enable"; then
    echo "Reintentos cada 30 min activados."
  else
    echo "::error::No se pudo activar $REINTENTO (revisa el permiso actions: write)."
    exit 1
  fi
fi

ultima=$(cat cge_ultima_lectura.txt 2>/dev/null || echo "1970-01-01 00:00")
horas=$(( ( $(date +%s) - $(TZ=America/Santiago date -d "$ultima" +%s) ) / 3600 ))
if [ "$horas" -ge "$MAX_HORAS_SIN_DATOS" ]; then
  echo "::error::CGE lleva ${horas} h sin una lectura exitosa (última: ${ultima})."
  exit 1
fi
echo "Última lectura exitosa: ${ultima} (hace ${horas} h)."
exit 0
