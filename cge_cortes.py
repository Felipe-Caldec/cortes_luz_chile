"""
Interrupciones de CGE desde su "Mapa de zonas afectadas".

La página sucursalvirtual.cge.cl/mapa-de-afectacion es solo un iframe a
https://mapa-afectaciones.grupocge.cl/afectaciones/, que dibuja con
google.maps.KmlLayer el archivo:

    https://mapa-afectaciones.grupocge.cl/afectaciones/mapa_cge.kmz?key=<key>

La key viene escrita en el HTML de esa página (var key = '...') y la página
la renueva con key.php?empresa=cge. El KMZ es un zip con un KML; cada
Placemark es un corte con:
  - styleUrl: confirmed-outage | planned-outage | unconfirmed-outage
  - description: "Clientes afectados: N<br/>Restauración estimada: dd/mm/aaaa HH:MM hrs"
  - ExtendedData label: "N / HH:MM h"  (HH:MM = tiempo transcurrido del corte)
  - Point (centro) + Polygon (zona afectada)
El KML no trae comuna: se obtiene del punto con Nominatim (OpenStreetMap):
  - zoom=10 devuelve el límite administrativo de la comuna; su etiqueta
    dpachile:id es el código INE (13120 = Ñuñoa), comparable con COD_VNR de Enel.
  - zoom=17 da el sector (villa, población, barrio o localidad).
Los resultados se guardan en cge_geocache.csv, así cada punto se consulta una
sola vez (política de Nominatim: máx. 1 consulta por segundo y usar caché).

Uso:
    python cge_interrupciones.py            # agrega una lectura a cge_interrupciones.csv
    python cge_interrupciones.py --geojson  # además guarda los polígonos en mapas/
    python cge_interrupciones.py --sin-geo  # no consulta comuna/sector
"""

import html
import io
import json
import os
import re
import socket
import sys
import time
import zipfile
from datetime import datetime
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import pandas as pd
import requests

BASE = "https://mapa-afectaciones.grupocge.cl/afectaciones/"
EMPRESA = "cge"
HISTORIAL = "cge_interrupciones.csv"
TZ = ZoneInfo("America/Santiago")
NS = {"k": "http://www.opengis.net/kml/2.2"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Referer": BASE,
}

NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
GEOCACHE = "cge_geocache.csv"
# Nominatim exige identificar la aplicación (idealmente con un contacto)
UA_NOMINATIM = "cortes_luz/1.0 (github.com/Felipe-Caldec/cortes_luz)"
GEO_COLS = ["comuna", "cod_comuna", "region", "sector"]
# Orden de preferencia para el sector dentro de la respuesta zoom=17
CAMPOS_SECTOR = ["neighbourhood", "residential", "quarter", "hamlet",
                 "isolated_dwelling", "suburb", "village", "town"]

TIPOS = {
    "confirmed-outage": "confirmada",
    "planned-outage": "programada",
    "unconfirmed-outage": "no_confirmada",
    "hypothetic-outage": "hipotetica",
}


# --- Respaldo de DNS -------------------------------------------------------
# El DNS de algunas redes no resuelve a veces mapa-afectaciones.grupocge.cl
# ("getaddrinfo failed") aunque el dominio existe. Si pasa, se pregunta la IP
# por DNS sobre HTTPS a Cloudflare o Google (por IP, sin depender del DNS
# local). El certificado TLS se sigue verificando contra el nombre del dominio.
DOH = [
    ("https://1.1.1.1/dns-query", {"accept": "application/dns-json"}),
    ("https://8.8.8.8/resolve", {}),
]
_getaddrinfo_sistema = socket.getaddrinfo
_ips_respaldo: dict[str, list[str]] = {}


def _resolver_doh(host: str) -> list[str]:
    for url, headers in DOH:
        try:
            r = requests.get(url, params={"name": host, "type": "A"},
                             headers=headers, timeout=10)
            ips = [a["data"] for a in r.json().get("Answer", []) if a.get("type") == 1]
            if ips:
                return ips
        except Exception:
            continue
    return []


def _getaddrinfo_con_respaldo(host, port, *args, **kwargs):
    try:
        return _getaddrinfo_sistema(host, port, *args, **kwargs)
    except socket.gaierror:
        if not isinstance(host, str) or re.fullmatch(r"[\d.]+", host):
            raise
        if host not in _ips_respaldo:
            _ips_respaldo[host] = _resolver_doh(host)
            if _ips_respaldo[host]:
                print(f"DNS local no resolvió {host}; usando "
                      f"{', '.join(_ips_respaldo[host])} (DNS sobre HTTPS)")
        if not _ips_respaldo[host]:
            raise
        return [res for ip in _ips_respaldo[host]
                for res in _getaddrinfo_sistema(ip, port, *args, **kwargs)]


socket.getaddrinfo = _getaddrinfo_con_respaldo


def con_reintentos(fn, *args, intentos=3, espera=20):
    """Reintenta ante errores de red (DNS, conexión, timeout, 5xx)."""
    for i in range(1, intentos + 1):
        try:
            return fn(*args)
        except (requests.ConnectionError, requests.Timeout) as e:
            error = e
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code < 500:
                raise
            error = e
        if i < intentos:
            print(f"Intento {i}/{intentos} falló ({type(error).__name__}); "
                  f"reintentando en {espera * i} s...")
            time.sleep(espera * i)
    raise error


def obtener_key(s: requests.Session) -> str:
    """Igual que la página: la key inicial viene en el HTML; si no, key.php."""
    r = s.get(BASE, timeout=30)
    r.raise_for_status()
    m = re.search(r"var\s+key\s*=\s*'([0-9a-f]+)'", r.text)
    if m:
        return m.group(1)
    r = s.get(f"{BASE}key.php", params={"empresa": EMPRESA}, timeout=30)
    r.raise_for_status()
    return r.text.strip()


def descargar_kml(s: requests.Session) -> tuple[bytes, str | None]:
    key = obtener_key(s)
    r = s.get(f"{BASE}mapa_{EMPRESA}.kmz", params={"key": key}, timeout=60)
    r.raise_for_status()
    datos = r.content
    if datos[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(datos)) as z:
            datos = z.read(next(n for n in z.namelist() if n.lower().endswith(".kml")))
    return datos, r.headers.get("Last-Modified")


def _hhmm_a_horas(txt: str | None) -> float | None:
    m = re.search(r"(\d+):(\d{2})", txt or "")
    return round(int(m.group(1)) + int(m.group(2)) / 60, 2) if m else None


def _anillo(el) -> list[list[float]]:
    txt = el.findtext("k:coordinates", "", NS)
    return [[float(v) for v in par.split(",")[:2]] for par in txt.split()]


def parsear(kml: bytes, fecha_lectura: str) -> tuple[pd.DataFrame, list[dict]]:
    raiz = ET.fromstring(kml)
    filas, features = [], []
    for pm in raiz.iter(f"{{{NS['k']}}}Placemark"):
        estilo = pm.findtext("k:styleUrl", "", NS).lstrip("#")
        # "#dext.confirmed-outage", "#cge.public.unconfirmed-outage": vale el último tramo
        sufijo = estilo.rsplit(".", 1)[-1]
        tipo = TIPOS.get(sufijo, sufijo)

        desc = html.unescape(pm.findtext("k:description", "", NS))
        clientes = re.search(r"Clientes afectados:\s*(\d+)", desc)
        rest = re.search(r"Restauraci.n estimada:\s*(\d{2}/\d{2}/\d{4} \d{2}:\d{2})", desc)

        datos = {d.get("name"): d.findtext("k:value", "", NS)
                 for d in pm.iterfind(".//k:Data", NS)}
        punto = pm.find(".//k:Point", NS)
        lon, lat = _anillo(punto)[0] if punto is not None else (None, None)

        fila = {
            "fecha_lectura": fecha_lectura,
            "tipo": tipo,
            "clientes": int(clientes.group(1)) if clientes else None,
            "restauracion_estimada": (
                datetime.strptime(rest.group(1), "%d/%m/%Y %H:%M").strftime("%Y-%m-%d %H:%M")
                if rest else None),
            "horas_transcurridas": _hhmm_a_horas(datos.get("label")),
            "label": datos.get("label"),
            "lat": lat,
            "lon": lon,
            # Sin ID de incidencia en el KML: el centro del polígono sirve para
            # seguir el mismo corte entre lecturas.
            "clave": f"{tipo}_{lat:.5f}_{lon:.5f}" if lat is not None else None,
        }
        filas.append(fila)

        poligonos = []
        for pol in pm.iterfind(".//k:Polygon", NS):
            ext = pol.find("k:outerBoundaryIs/k:LinearRing", NS)
            ints = pol.findall("k:innerBoundaryIs/k:LinearRing", NS)
            poligonos.append([_anillo(ext)] + [_anillo(i) for i in ints])
        if poligonos:
            features.append({
                "type": "Feature",
                "properties": {k: v for k, v in fila.items() if k not in ("lat", "lon")},
                "geometry": {"type": "MultiPolygon", "coordinates": poligonos},
            })

    return pd.DataFrame(filas), features


def _clave_geo(lat: float, lon: float) -> str:
    return f"{lat:.4f}_{lon:.4f}"  # ~11 m: el mismo corte cae en la misma clave


def _nominatim(s: requests.Session, lat: float, lon: float, zoom: int) -> dict:
    r = s.get(NOMINATIM, timeout=30, params={
        "format": "jsonv2", "lat": lat, "lon": lon, "zoom": zoom,
        "addressdetails": 1, "extratags": 1 if zoom == 10 else 0,
        "accept-language": "es",
    })
    time.sleep(1.1)  # política de uso: 1 consulta por segundo
    r.raise_for_status()
    return r.json()


def _codigo_por_nombre(s: requests.Session, comuna: str, region: str) -> str | None:
    """Código INE (dpachile:id) del límite comunal con ese nombre."""
    try:
        r = s.get(NOMINATIM.replace("/reverse", "/search"), timeout=30, params={
            "format": "jsonv2", "q": f"{comuna}, {region}, Chile",
            "extratags": 1, "limit": 10, "accept-language": "es",
        })
        time.sleep(1.1)
        r.raise_for_status()
        for x in r.json():
            t = x.get("extratags") or {}
            if t.get("admin_level") == "8" and x.get("name", "").casefold() == comuna.casefold():
                return t.get("dpachile:id")
    except Exception as e:
        print(f"  no se encontró el código INE de {comuna}: {e}")
    return None


def _consultar(s: requests.Session, lat: float, lon: float) -> dict:
    com = _nominatim(s, lat, lon, 10)
    det = _nominatim(s, lat, lon, 17)

    dir_ = det.get("address", {})

    # zoom=10 devuelve el límite comunal (admin_level 8) en ciudades y en regiones
    if (com.get("extratags") or {}).get("admin_level") == "8":
        comuna = com.get("name")
        cod = com["extratags"].get("dpachile:id")
    else:
        # Respaldo: la comuna desde la dirección detallada (zoom=17), que sí
        # baja hasta ese nivel. En Gran Santiago OSM pone la comuna en
        # "suburb" y "Santiago" en "city".
        comuna, cod = None, None
        for a in (dir_, com.get("address", {})):
            comuna = a.get("suburb") if a.get("city") == "Santiago" and a.get("suburb") else (
                a.get("city") or a.get("town") or a.get("municipality")
                or a.get("village") or a.get("suburb"))
            if comuna:
                break
        if comuna:  # el código INE se busca por nombre en el límite comunal
            region = dir_.get("state") or com.get("address", {}).get("state") or ""
            cod = _codigo_por_nombre(s, comuna, region)

    sector = next((dir_[c] for c in CAMPOS_SECTOR
                   if dir_.get(c) and dir_[c] not in (comuna, "Santiago")), None)
    return {
        "comuna": comuna,
        "cod_comuna": str(cod).zfill(5) if cod else None,
        "region": dir_.get("state") or com.get("address", {}).get("state"),
        "sector": sector,
    }


def geolocalizar(df: pd.DataFrame) -> pd.DataFrame:
    """Completa comuna, cod_comuna, region y sector en las filas que no los
    tengan, incluidas lecturas antiguas. Usa cge_geocache.csv para no
    repetir consultas; un punto sin comuna se vuelve a consultar."""
    for col in GEO_COLS:  # object: columnas vacías leídas del CSV vienen como float
        df[col] = df[col].astype(object) if col in df else None
    cache = {}
    if os.path.exists(GEOCACHE):
        c = pd.read_csv(GEOCACHE, sep=";", dtype=str, keep_default_na=False)
        cache = {r["clave_geo"]: {k: (r[k] or None) for k in GEO_COLS}
                 for _, r in c.iterrows()}

    falta = df["lat"].notna() & (df["comuna"].isna() | df["cod_comuna"].isna())
    puntos = df.loc[falta, ["lat", "lon"]].drop_duplicates()
    nuevos, fallidos = 0, []
    with requests.Session() as s:
        s.headers["User-Agent"] = UA_NOMINATIM
        for lat, lon in puntos.itertuples(index=False):
            k = _clave_geo(lat, lon)
            if cache.get(k, {}).get("comuna") and cache[k].get("cod_comuna"):
                continue
            try:
                cache[k] = _consultar(s, lat, lon)
                nuevos += 1
                if not cache[k]["comuna"]:
                    fallidos.append(f"{k} (Nominatim no devolvió comuna)")
                elif not cache[k]["cod_comuna"]:
                    fallidos.append(f"{k} ({cache[k]['comuna']}, sin código INE)")
            except Exception as e:  # sin red o Nominatim caído: no frena la lectura
                fallidos.append(f"{k} ({type(e).__name__}: {e})")

    if nuevos:
        filas = [{"clave_geo": k, **v} for k, v in cache.items()]
        pd.DataFrame(filas).to_csv(GEOCACHE, sep=";", index=False, encoding="utf-8")

    for i in df.index[falta]:
        g = cache.get(_clave_geo(df.at[i, "lat"], df.at[i, "lon"]), {})
        for col in GEO_COLS:
            df.at[i, col] = g.get(col)

    con = df["lat"].notna()
    print(f"Geolocalización: {falta.sum()} filas por completar, {nuevos} puntos consultados; "
          f"ahora {df.loc[con, 'comuna'].notna().sum()}/{con.sum()} con comuna y "
          f"{df.loc[con, 'cod_comuna'].notna().sum()}/{con.sum()} con código INE")
    for f in fallidos:
        print(f"  incompleto: {f}")
    return df


def leer_historial() -> pd.DataFrame:
    if not os.path.exists(HISTORIAL):
        return pd.DataFrame()
    return pd.read_csv(HISTORIAL, sep=";", decimal=",", dtype={"cod_comuna": str})


def main():
    ahora = datetime.now(TZ)
    fecha_lectura = ahora.strftime("%Y-%m-%d %H:%M")

    try:
        with requests.Session() as s:
            s.headers.update(HEADERS)
            kml, last_mod = con_reintentos(descargar_kml, s)
    except requests.RequestException as e:
        # Mensaje corto en vez del traceback; código 1 para que GitHub Actions
        # marque la ejecución como fallida (y no se confunda con "sin cortes").
        print(f"{fecha_lectura}: no se pudo descargar el mapa de CGE "
              f"({type(e).__name__}: {e}). No se guardó la lectura.")
        sys.exit(1)

    df, features = parsear(kml, fecha_lectura)
    if df.empty:
        # Igual que Chilquinta: distinguir "sin cortes" de una ejecución fallida
        df = pd.DataFrame([{"fecha_lectura": fecha_lectura, "tipo": "(sin interrupciones)"}])
    df["kmz_last_modified"] = last_mod
    for col in GEO_COLS:
        df[col] = None

    # Historial completo + lectura nueva; se completan las comunas que falten
    # en cualquier fila (lecturas antiguas o puntos que fallaron antes).
    hist = leer_historial()
    n_viejo = len(hist)
    todo = pd.concat([hist, df], ignore_index=True) if n_viejo else df.copy()
    if "--sin-geo" not in sys.argv and "lat" in todo:
        todo = geolocalizar(todo)

    primeras = ["fecha_lectura", "tipo", *GEO_COLS]
    todo = todo[primeras + [c for c in todo.columns if c not in primeras]]
    todo.to_csv(HISTORIAL, sep=";", decimal=",", index=False, encoding="utf-8")

    df = todo.iloc[n_viejo:]
    if "clave" in df:
        g = df.dropna(subset=["clave"]).drop_duplicates("clave").set_index("clave")
        g = g[GEO_COLS].astype(object)
        geo = g.where(g.notna(), None).to_dict("index")  # NaN no es JSON válido
        for f in features:
            f["properties"].update(geo.get(f["properties"]["clave"], {}))

    reales = df[df["tipo"] != "(sin interrupciones)"]
    print(f"{fecha_lectura}: {len(reales)} cortes, "
          f"{int(reales['clientes'].sum()) if len(reales) else 0} clientes -> {HISTORIAL}")
    if len(reales):
        print(reales[["tipo", "comuna", "sector", "clientes", "horas_transcurridas"]]
              .to_string(index=False))

    if "--geojson" in sys.argv and features:
        os.makedirs("mapas", exist_ok=True)
        ruta = f"mapas/cge_{ahora:%Y%m%d_%H%M}.geojson"
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": features}, f, ensure_ascii=False)
        print(f"Polígonos -> {ruta}")


if __name__ == "__main__":
    main()
