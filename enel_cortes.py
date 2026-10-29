"""
Clientes afectados por comuna desde la capa de comunas del Mapa de Emergencia de Enel.

Uso:
    python enel_comunas.py                 # una lectura: imprime la tabla y la agrega al CSV
    python enel_comunas.py --cada 15       # una lectura cada 15 minutos (Ctrl+C para salir)
"""

"""
Clientes afectados por comuna desde la capa de comunas del Mapa de Emergencia de Enel.

Uso:
    python enel_comunas.py                 # una lectura: imprime la tabla y la agrega al CSV
    python enel_comunas.py --cada 15       # una lectura cada 15 minutos (Ctrl+C para salir)
"""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

# Pega aquí la URL que encontraste en la pestaña Network (sin el parámetro "_=...").
URL = "https://mapaemergencia.enel.com/galeria/documento/me-capa-comunasAfectadas.txt?&_=1793056598572"

TZ = ZoneInfo("America/Santiago")

COMUNAS_RM = [
    "Alhué", "Buin", "Calera de Tango", "Cerrillos", "Cerro Navia", "Colina", "Conchalí",
    "Curacaví", "El Bosque", "El Monte", "Estación Central", "Huechuraba", "Independencia",
    "Isla de Maipo", "La Cisterna", "La Florida", "La Granja", "La Pintana", "La Reina",
    "Lampa", "Las Condes", "Lo Barnechea", "Lo Espejo", "Lo Prado", "Macul", "Maipú",
    "María Pinto", "Melipilla", "Ñuñoa", "Padre Hurtado", "Paine", "Pedro Aguirre Cerda",
    "Peñaflor", "Peñalolén", "Pirque", "Providencia", "Pudahuel", "Puente Alto", "Quilicura",
    "Quinta Normal", "Recoleta", "Renca", "San Bernardo", "San Joaquín", "San José de Maipo",
    "San Miguel", "San Pedro", "San Ramón", "Santiago", "Talagante", "Tiltil", "Vitacura",
]


def _sin_tildes(texto: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return " ".join(t.replace("_", " ").upper().split())


_OFICIAL = {_sin_tildes(c): c for c in COMUNAS_RM} | {"TIL TIL": "Tiltil"}


def nombre_oficial(texto: str) -> str:
    """'NUNOA' → 'Ñuñoa', 'LO_ESPEJO' → 'Lo Espejo'. Si no la reconoce, la deja en formato título."""
    clave = _sin_tildes(texto)
    return _OFICIAL.get(clave, clave.title())
HEADERS = {
    "User-Agent": "Mozilla/5.0 (estudio de cortes por comuna)",
    "Referer": "https://mapaemergencia.enel.com/mapa.html?mode=comuna",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


def descargar(url: str = URL) -> dict:
    # el parámetro "_" es el anti-caché que agrega el mapa: así siempre llega la versión nueva
    r = requests.get(url, params={"_": datetime.now(TZ).strftime("%d/%m %H:%M")},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    # el servidor puede responder como text/plain; se lee el texto como JSON (con o sin BOM)
    return json.loads(r.content.decode("utf-8-sig"))


def a_tabla(geojson: dict, momento: datetime | None = None) -> pd.DataFrame:
    filas = [f["properties"] for f in geojson.get("features", [])]
    df = pd.DataFrame(filas)
    df = df[df.get("TIPO", "COMUNA") == "COMUNA"] if "TIPO" in df else df
    tabla = pd.DataFrame({
        "momento": (momento or datetime.now(TZ)).replace(second=0, microsecond=0),
        "cod_comuna": pd.to_numeric(df["COD_VNR"], errors="coerce").astype("Int64"),  # código CUT oficial (13116 = Lo Espejo)
        "comuna": df["COMUNA"].map(nombre_oficial),
        "clientes_afectados": pd.to_numeric(df["CLIENTESAFECTADOS"], errors="coerce").astype("Int64"),
        "clientes_totales": pd.to_numeric(df["CLIENTESTOTAL"], errors="coerce").astype("Int64"),
    })
    # PORCENTAJE viene redondeado a 1 decimal y con espacios ("   0.0"); se recalcula con más precisión
    tabla["porcentaje"] = (100 * tabla["clientes_afectados"] / tabla["clientes_totales"]).round(3)
    return tabla.sort_values("clientes_afectados", ascending=False).reset_index(drop=True)


def guardar(tabla: pd.DataFrame, csv: str | Path = "enel_comunas.csv") -> None:
    """Agrega las filas al CSV en formato de Excel en español (Chile): columnas separadas por
    punto y coma, coma decimal y UTF-8 con BOM para que se vean bien las tildes y la ñ.
    Para leerlo en pandas: pd.read_csv(csv, sep=";", decimal=",")."""
    csv = Path(csv)
    salida = tabla.copy()
    salida["momento"] = salida["momento"].dt.strftime("%Y-%m-%d %H:%M")  # sin zona horaria, Excel lo reconoce como fecha
    nuevo = not csv.exists()
    salida.to_csv(csv, mode="a", header=nuevo, index=False, sep=";", decimal=",",
                  encoding="utf-8-sig" if nuevo else "utf-8")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=URL)
    p.add_argument("--csv", default=str(Path(__file__).with_name("enel_comunas.csv")),
                   help="por defecto, enel_comunas.csv junto a este script")
    p.add_argument("--cada", type=int, help="minutos entre lecturas; sin esto lee una vez")
    a = p.parse_args()
    while True:
        try:
            t = a_tabla(descargar(a.url))
            guardar(t, a.csv)
            print(f"{t['momento'].iloc[0]:%Y-%m-%d %H:%M}  {len(t)} comunas, "
                  f"{t['clientes_afectados'].sum():,} clientes afectados")
            print(t.head(10).to_string(index=False))
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"{datetime.now(TZ):%H:%M} error: {e}")
        if not a.cada:
            break
        time.sleep(a.cada * 60)


if __name__ == "__main__":
    main()