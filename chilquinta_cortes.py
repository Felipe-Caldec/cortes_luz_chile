import csv
import re
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright


def texto(locator, defecto="sin info"):
    """Texto del elemento, o el valor por defecto si no existe (sin esperar 30 segundos)."""
    if locator.count() == 0:
        return defecto
    lineas = [l.strip() for l in locator.first.inner_text().splitlines() if l.strip()]
    return ", ".join(lineas)


def reposicion_a_fecha(valor, ahora):
    """'Hoy, 23:30 hrs' → '2026-10-28 23:30'. También entiende 'Mañana' y '29/10'."""
    m = re.search(r"(\d{1,2}):(\d{2})", valor)
    if not m:
        return ""
    fecha = ahora.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    if "mañana" in valor.lower():
        fecha += timedelta(days=1)
    d = re.search(r"(\d{1,2})[/-](\d{1,2})", valor)
    if d:
        fecha = fecha.replace(day=int(d.group(1)), month=int(d.group(2)))
    return fecha.strftime("%Y-%m-%d %H:%M")


playwright = sync_playwright().start()
browser = playwright.chromium.launch()
page = browser.new_page()
page.goto("https://www.chilquinta.cl/interrupciones")

tutorial = page.locator("div.map-tutorial")
try:
    tutorial.wait_for(state="visible", timeout=5000)
    tutorial.get_by_text("Saltar").click()
    tutorial.wait_for(state="hidden")
except Exception:
    pass  # si no aparece, seguimos normal

ahora = datetime.now()
clientes_sin_luz = []

page.wait_for_selector("article", timeout=60_000)
articles = page.locator("article")
cantidad = articles.count()

for i in range(cantidad):
    article = articles.nth(i)

    boton = article.locator("button.panel-summary")
    boton.click()
    page.wait_for_function(
        "el => el.getAttribute('aria-expanded') === 'true'",
        arg=boton.element_handle(),
    )

    zona_val = texto(article.locator("span.panel-location"))
    clientes_zona = texto(article.locator("span.panel-customers"))

    interrupciones = article.locator("div.grouped-interruption-card")
    for w in range(interrupciones.count()):
        interrupcion = interrupciones.nth(w)

        tipo = texto(interrupcion.locator("p.interruption-type-label"))
        sectores = texto(interrupcion.locator("div.interruption-sector-badges"))

        # el bloque trae el rótulo y el valor: "REPOSICIÓN ESTIMADA, Hoy, 23:30 hrs" → "Hoy, 23:30 hrs"
        reposicion = texto(interrupcion.locator("div.interruption-restoration"))
        reposicion = reposicion.replace("REPOSICIÓN ESTIMADA, ", "").replace("Reposición estimada, ", "")

        # clientes de esta interrupción (81, 85), no el total de la zona (166)
        m = re.search(r"([\d.]+)\s+clientes afectados", interrupcion.inner_text())
        clientes = m.group(1).replace(".", "") if m else "sin info"

        clientes_sin_luz.append((ahora.strftime("%Y-%m-%d %H:%M"), zona_val, tipo, clientes,
                                 sectores, reposicion, reposicion_a_fecha(reposicion, ahora)))
        print(zona_val, tipo, clientes, sectores, reposicion)

browser.close()
playwright.stop()

with open("chilquinta_interrupciones.csv", "a", newline="", encoding="utf-8-sig") as f:
    writer = csv.writer(f, delimiter=";")
    if f.tell() == 0:
        writer.writerow(["Momento", "Zona", "Tipo", "Clientes afectados", "Sectores",
                         "Reposición estimada", "Reposición estimada (fecha)"])
    writer.writerows(clientes_sin_luz)

print(f"{len(clientes_sin_luz)} interrupciones guardadas")