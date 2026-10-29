"""
Mapa interactivo de cortes de luz en la RM con las capas del Mapa de Emergencia de Enel.

Genera mapa_cortes.html (se abre en cualquier navegador) con cuatro capas que se encienden
y apagan desde el control de capas:

  1. Comunas coloreadas por % de clientes afectados (mismos rangos que el mapa de Enel).
  2. Un punto por comuna afectada, de tamaño proporcional a los clientes sin luz.
  3. Zonas afectadas: el área de cada falla o corte programado, con un punto en su centro
     (capa me-capa-trafosAfectados).
  4. Avisos de clientes agrupados (capa me-capa-avisos). Apagada al inicio. No muestra
     nombre, dirección ni número de cliente, y redondea la ubicación a ~100 m.

Uso:
    python mapa_cortes.py                     # descarga las capas y genera mapa_cortes.html
    python mapa_cortes.py --historial         # además guarda una copia con fecha y hora
Requiere: pip install folium requests pandas   (y enel_comunas.py en la misma carpeta)
"""

from __future__ import annotations

import argparse
import math
from datetime import datetime
from pathlib import Path

import folium
from folium.plugins import MarkerCluster

from enel_cortes_comunas import TZ, URL as URL_COMUNAS, a_tabla, descargar, nombre_oficial

URL_ZONAS = "https://mapaemergencia.enel.com/galeria/documento/me-capa-trafosAfectados.txt"
URL_AVISOS = "https://mapaemergencia.enel.com/galeria/documento/me-capa-avisos.txt"

# Rangos de la leyenda del mapa de Enel
RANGOS = [(0, 5, "#f2d14b", "0% a 5%"), (5, 15, "#f39c32", "5% a 15%"),
          (15, 50, "#e0542b", "15% a 50%"), (50, 101, "#a3171d", "+50%")]
SIN_AFECTADOS = "#e9ecef"


def color_porcentaje(pct: float | None, afectados: int | None) -> str:
    if not afectados:
        return SIN_AFECTADOS
    for desde, hasta, color, _ in RANGOS:
        if desde <= (pct or 0) < hasta:
            return color
    return RANGOS[-1][2]


# ---------------------------------------------------------------- geometría
def _anillos_exteriores(geom: dict) -> list[list]:
    if not geom:
        return []
    if geom["type"] == "Polygon":
        return [geom["coordinates"][0]]
    if geom["type"] == "MultiPolygon":
        return [p[0] for p in geom["coordinates"]]
    return []


def _area_centroide(anillo: list) -> tuple[float, float, float]:
    """Área con signo y centroide de un anillo (fórmula del polígono, en grados)."""
    a = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(anillo, anillo[1:] + anillo[:1]):
        f = x1 * y2 - x2 * y1
        a += f
        cx += (x1 + x2) * f
        cy += (y1 + y2) * f
    a /= 2
    if abs(a) < 1e-12:
        xs, ys = zip(*anillo)
        return 0.0, sum(xs) / len(xs), sum(ys) / len(ys)
    return a, cx / (6 * a), cy / (6 * a)


def centroide(geom: dict) -> tuple[float, float] | None:
    """(lat, lon) del centro del polígono más grande. None si no hay polígono."""
    partes = [_area_centroide(r) for r in _anillos_exteriores(geom) if len(r) >= 3]
    if not partes:
        if geom and geom.get("type") == "Point":
            lon, lat = geom["coordinates"][:2]
            return lat, lon
        return None
    _, lon, lat = max(partes, key=lambda p: abs(p[0]))
    return lat, lon


# ---------------------------------------------------------------- capas
def _fmt(n) -> str:
    return "—" if n is None else f"{int(n):,}".replace(",", ".")


def capa_comunas(mapa, comunas: dict, tabla) -> tuple[folium.FeatureGroup, folium.FeatureGroup]:
    datos = {int(r.cod_comuna): r for r in tabla.itertuples() if r.cod_comuna is not None}
    poligonos = folium.FeatureGroup(name="Comunas por % afectado", show=True)
    puntos = folium.FeatureGroup(name="Comunas afectadas (puntos)", show=True)
    max_af = max([r.clientes_afectados or 0 for r in datos.values()] + [1])

    for f in comunas.get("features", []):
        p = f.get("properties", {})
        if p.get("TIPO", "COMUNA") != "COMUNA":
            continue
        try:
            r = datos[int(p["COD_VNR"])]
        except (KeyError, ValueError, TypeError):
            continue
        af = None if r.clientes_afectados is None else int(r.clientes_afectados)
        pct = None if r.porcentaje is None or math.isnan(r.porcentaje) else float(r.porcentaje)
        color = color_porcentaje(pct, af)
        texto = (f"<b>{r.comuna}</b><br>Clientes afectados: {_fmt(af)}<br>"
                 f"Clientes totales: {_fmt(r.clientes_totales)}<br>"
                 f"Porcentaje: {'—' if pct is None else f'{pct:.2f}'.replace('.', ',')} %")
        folium.GeoJson(
            {"type": "Feature", "geometry": f["geometry"], "properties": {}},
            style_function=lambda _, c=color: {"fillColor": c, "color": "#555", "weight": 0.8,
                                               "fillOpacity": 0.55},
            highlight_function=lambda _: {"weight": 2.5, "color": "#111"},
            tooltip=folium.Tooltip(texto),
        ).add_to(poligonos)

        centro = centroide(f["geometry"])
        if af and centro:
            radio = 6 + 24 * math.sqrt(af / max_af)          # área del círculo ∝ clientes afectados
            folium.CircleMarker(
                centro, radius=radio, color="#7a1015", weight=1.5, fill=True,
                fill_color=color, fill_opacity=0.9, tooltip=folium.Tooltip(texto),
            ).add_to(puntos)
    return poligonos, puntos


def capa_zonas(zonas: dict) -> folium.FeatureGroup:
    grupo = folium.FeatureGroup(name="Zonas afectadas (fallas y programados)", show=True)
    for f in zonas.get("features", []):
        p = f.get("properties", {})
        programado = str(p.get("TIPO", "")).upper() == "DESCARGO"
        color = "#2b6cb0" if programado else "#c0392b"
        tipo = "Corte programado" if programado else "Falla"
        inicio = p.get("FECHA_INIDESC") if programado else p.get("FECHA_INICIO")
        texto = (f"<b>{tipo}</b> {p.get('INCIDENCIA', '')}<br>"
                 f"Clientes: {_fmt(p.get('CLITOTAL') or None)}<br>"
                 f"Inicio: {inicio or '—'}<br>"
                 f"Reposición estimada: {p.get('FECHA_REPOSICION') or '—'}<br>"
                 f"Alimentador: {p.get('id_alim', '—')}")
        folium.GeoJson(
            {"type": "Feature", "geometry": f["geometry"], "properties": {}},
            style_function=lambda _, c=color: {"color": c, "weight": 1, "fillColor": c, "fillOpacity": 0.25},
            tooltip=folium.Tooltip(texto),
        ).add_to(grupo)
        centro = centroide(f["geometry"])
        if centro:
            folium.CircleMarker(centro, radius=5, color=color, fill=True, fill_opacity=1,
                                tooltip=folium.Tooltip(texto)).add_to(grupo)
    return grupo


def capa_avisos(avisos: dict) -> folium.FeatureGroup:
    """Solo código de evento y fechas. Coordenadas redondeadas a 3 decimales (~100 m)."""
    grupo = folium.FeatureGroup(name="Avisos de clientes", show=False)
    cluster = MarkerCluster().add_to(grupo)
    for f in avisos.get("features", []):
        g, p = f.get("geometry") or {}, f.get("properties", {})
        if g.get("type") != "Point":
            continue
        lon, lat = (round(v, 3) for v in g["coordinates"][:2])
        texto = (f"Evento {p.get('CODIGO') or p.get('COD_EVENTO') or '—'}<br>"
                 f"Inicio: {p.get('FECHA_INI') or '—'}<br>"
                 f"Reposición estimada: {p.get('FECHA_REPOSICION') or '—'}")
        folium.CircleMarker((lat, lon), radius=4, color="#6c3483", fill=True, fill_opacity=0.8,
                            tooltip=folium.Tooltip(texto)).add_to(cluster)
    return grupo


def _leyenda(momento: datetime, tabla) -> str:
    filas = "".join(
        f"<div><span style='background:{c};width:14px;height:14px;display:inline-block;"
        f"border:1px solid #888;margin-right:6px;vertical-align:middle'></span>{etq}</div>"
        for _, _, c, etq in RANGOS)
    total = int(tabla["clientes_afectados"].fillna(0).sum())
    n = int((tabla["clientes_afectados"].fillna(0) > 0).sum())
    return f"""
    <div style="position:fixed;bottom:28px;right:12px;z-index:9999;background:white;padding:10px 12px;
                border-radius:6px;box-shadow:0 1px 5px rgba(0,0,0,.3);font:13px/1.45 sans-serif;max-width:280px">
      <div style="font-size:15px;font-weight:600">Cortes de luz – Enel RM</div>
      <div style="color:#555">{momento:%d-%m-%Y %H:%M} · {_fmt(total)} clientes afectados en {n} comunas</div>
      <div style="margin-top:6px">{filas}
        <div><span style='background:{SIN_AFECTADOS};width:14px;height:14px;display:inline-block;
              border:1px solid #888;margin-right:6px;vertical-align:middle'></span>Sin afectados</div>
        <div style="margin-top:4px"><span style="color:#c0392b">●</span> Falla
             &nbsp;<span style="color:#2b6cb0">●</span> Corte programado</div>
      </div>
      <div style="color:#777;margin-top:6px;font-size:11px">Fuente: Mapa de Emergencia de Enel. Solo concesión Enel.</div>
    </div>"""


# Fondos de mapa. OpenStreetMap bloquea los mapas abiertos como archivo local (file://),
# porque esas solicitudes no llevan Referer; los de Esri funcionan sin clave.
_ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/{}/MapServer/tile/{{z}}/{{y}}/{{x}}"
FONDOS = [
    ("Gris claro (Esri)", _ESRI.format("Canvas/World_Light_Gray_Base"),
     "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, OpenStreetMap contributors", 16),
    ("Calles (Esri)", _ESRI.format("World_Street_Map"),
     "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, OpenStreetMap contributors", 19),
    ("Satélite (Esri)", _ESRI.format("World_Imagery"),
     "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics", 19),
]


def agregar_fondos(mapa: folium.Map) -> None:
    """El primero queda visible; los otros se eligen en el panel de capas."""
    for i, (nombre, url, atribucion, zoom_max) in enumerate(FONDOS):
        folium.TileLayer(tiles=url, attr=atribucion, name=nombre, max_zoom=19,
                         max_native_zoom=zoom_max, show=(i == 0)).add_to(mapa)


def construir_mapa(comunas: dict, zonas: dict | None = None, avisos: dict | None = None,
                   momento: datetime | None = None) -> folium.Map:
    momento = momento or datetime.now(TZ)
    tabla = a_tabla(comunas, momento)
    mapa = folium.Map(location=(-33.47, -70.65), zoom_start=10, tiles=None, control_scale=True)
    agregar_fondos(mapa)
    poligonos, puntos = capa_comunas(mapa, comunas, tabla)
    poligonos.add_to(mapa)
    puntos.add_to(mapa)                 # las burbujas por comuna van debajo de las zonas
    if zonas:
        capa_zonas(zonas).add_to(mapa)
    if avisos:
        capa_avisos(avisos).add_to(mapa)
    folium.LayerControl(collapsed=False).add_to(mapa)
    mapa.get_root().html.add_child(folium.Element(_leyenda(momento, tabla)))
    try:
        mapa.fit_bounds(poligonos.get_bounds(), padding_top_left=(40, 40), padding_bottom_right=(320, 40))
    except Exception:
        pass
    return mapa


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--salida", default=str(Path(__file__).with_name("mapa_cortes.html")))
    p.add_argument("--historial", action="store_true", help="guarda también mapas/mapa_AAAAMMDD_HHMM.html")
    p.add_argument("--sin-avisos", action="store_true")
    a = p.parse_args()

    momento = datetime.now(TZ)
    comunas = descargar(URL_COMUNAS)
    zonas = avisos = None
    try:
        zonas = descargar(URL_ZONAS)
    except Exception as e:
        print(f"No se pudo leer la capa de zonas: {e}")
    if not a.sin_avisos:
        try:
            avisos = descargar(URL_AVISOS)
        except Exception as e:
            print(f"No se pudo leer la capa de avisos: {e}")

    mapa = construir_mapa(comunas, zonas, avisos, momento)
    mapa.save(a.salida)
    print(f"Mapa guardado en {a.salida}")
    if a.historial:
        carpeta = Path(a.salida).with_name("mapas")
        carpeta.mkdir(exist_ok=True)
        copia = carpeta / f"mapa_{momento:%Y%m%d_%H%M}.html"
        mapa.save(str(copia))
        print(f"Copia en {copia}")


if __name__ == "__main__":
    main()