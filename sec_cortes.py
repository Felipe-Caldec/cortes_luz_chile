"""
Clientes afectados por comuna según la SEC ("Interrupciones en línea").

La página www.sec.cl/interrupciones-en-linea/ dibuja el mapa con los datos de
una API pública de apps.sec.cl. Se consulta directo con requests (sin
navegador):

    POST https://apps.sec.cl/INTONLINEv1/ClientesAfectados/GetPorFecha
    body JSON: {"anho": 2026, "mes": 10, "dia": 29, "hora": 17}

Responde una lista con una fila por comuna afectada: NOMBRE_REGION,
NOMBRE_COMUNA, CLIENTES_AFECTADOS y otras columnas (se guardan todas tal
cual, con prefijo "sec_", para no perder nada; por ejemplo la empresa
distribuidora, si viene).

Cubre todas las distribuidoras (Enel, CGE, Chilquinta, etc.), así que sirve
de reemplazo de chilquinta.cl, que bloquea a GitHub Actions.

Uso:
    python sec_interrupciones.py              # hora actual de Chile
    python sec_interrupciones.py --hora 14    # otra hora de hoy
    python sec_interrupciones.py --fecha 2026-10-28 --hora 23
"""

import argparse
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests

URL = "https://apps.sec.cl/INTONLINEv1/ClientesAfectados/GetPorFecha"
HISTORIAL = "sec_clientes_afectados.csv"
TZ = ZoneInfo("America/Santiago")

HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://apps.sec.cl/INTONLINEv1/index.aspx",
    "Origin": "https://apps.sec.cl",
    "User-Agent": "cortes_luz/1.0 (github.com/Felipe-Caldec/cortes_luz_chile)",
}


def consultar(fecha: datetime, hora: int, intentos: int = 3) -> list[dict]:
    cuerpo = {"anho": fecha.year, "mes": fecha.month, "dia": fecha.day, "hora": hora}
    for i in range(1, intentos + 1):
        try:
            r = requests.post(URL, json=cuerpo, headers=HEADERS, timeout=30)
            if r.status_code >= 500 and i < intentos:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            datos = r.json()
            if not isinstance(datos, list):
                raise ValueError(f"Formato inesperado: {str(datos)[:200]}")
            return datos
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            if i == intentos or (isinstance(e, requests.HTTPError)
                                 and e.response is not None and e.response.status_code < 500):
                raise
            print(f"Intento {i}/{intentos} falló ({e}); reintentando en {15 * i} s...")
            time.sleep(15 * i)
    return []


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fecha", help="AAAA-MM-DD (por defecto, hoy en Chile)")
    p.add_argument("--hora", type=int, help="0-23 (por defecto, la hora actual en Chile)")
    args = p.parse_args()

    ahora = datetime.now(TZ)
    fecha = datetime.strptime(args.fecha, "%Y-%m-%d") if args.fecha else ahora
    hora = args.hora if args.hora is not None else ahora.hour
    fecha_lectura = ahora.strftime("%Y-%m-%d %H:%M")

    try:
        filas = consultar(fecha, hora)
        # Al inicio de la hora la SEC puede no haber publicado aún esa hora:
        # si viene vacía, se usa la hora anterior (queda registrado cuál).
        if not filas and args.hora is None and hora > 0:
            print(f"Hora {hora}:00 sin datos todavía; consultando {hora - 1}:00")
            hora -= 1
            filas = consultar(fecha, hora)
    except (requests.RequestException, ValueError) as e:
        print(f"{fecha_lectura}: no se pudo consultar la SEC ({type(e).__name__}: {e}). "
              "No se guardó la lectura.")
        sys.exit(1)

    base = {"fecha_lectura": fecha_lectura,
            "fecha_consultada": fecha.strftime("%Y-%m-%d"),
            "hora_consultada": hora}

    if filas:
        crudo = pd.DataFrame(filas)
        df = pd.DataFrame({
            **base,
            "region": crudo.get("NOMBRE_REGION", pd.Series(dtype=str)).astype(str).str.strip(),
            "comuna": crudo.get("NOMBRE_COMUNA", pd.Series(dtype=str)).astype(str).str.strip(),
            "clientes": pd.to_numeric(crudo.get("CLIENTES_AFECTADOS"), errors="coerce"),
        })
        # Todas las columnas originales, por si traen empresa, códigos, etc.
        for col in crudo.columns:
            df[f"sec_{col}"] = crudo[col].values
        print("Columnas que entrega la SEC:", ", ".join(map(str, crudo.columns)))
    else:
        df = pd.DataFrame([{**base, "comuna": "(sin interrupciones)", "clientes": 0}])

    nuevo = df
    # Agregar al historial; si aparecen columnas nuevas, se reescribe con la unión
    if os.path.exists(HISTORIAL):
        viejo = pd.read_csv(HISTORIAL, sep=";", decimal=",", dtype=str)
        if list(viejo.columns) != list(df.columns):
            df = pd.concat([viejo, df.astype(str)], ignore_index=True)
            df.to_csv(HISTORIAL, sep=";", decimal=",", index=False, encoding="utf-8")
        else:
            df.to_csv(HISTORIAL, mode="a", header=False, sep=";", decimal=",",
                      index=False, encoding="utf-8")
    else:
        df.to_csv(HISTORIAL, sep=";", decimal=",", index=False, encoding="utf-8")

    reales = nuevo[nuevo["comuna"] != "(sin interrupciones)"]
    print(f"{fecha_lectura}: SEC {base['fecha_consultada']} {hora}:00 -> "
          f"{len(reales)} comunas, {int(pd.to_numeric(reales['clientes']).sum())} clientes "
          f"-> {HISTORIAL}")
    if len(reales):
        top = reales.assign(clientes=pd.to_numeric(reales["clientes"]))
        print(top.nlargest(10, "clientes")[["region", "comuna", "clientes"]].to_string(index=False))


if __name__ == "__main__":
    main()