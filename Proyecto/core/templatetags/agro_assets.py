"""Librerías del navegador (íconos, mapa, PDF, fuentes): copia local primero, CDN solo si falta.

Si existe ``core/static/vendor/<archivo>`` (lo crea ``descargar-librerias.ps1``), la página lo usa y deja de
depender de internet para dibujar íconos, menús y mapas. Si no existe, usa el CDN como hasta ahora.
"""

from decimal import Decimal, InvalidOperation

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def vendor(archivo, cdn):
    ruta = f'vendor/{archivo}'
    return static(ruta) if finders.find(ruta) else cdn


@register.filter
def cifra(valor, decimales=2):
    """Número en formato chileno sin ceros de relleno: 12.5 → «12,5», 32000 → «32.000», 0.0125 → «0,0125».

    ``decimales`` es el máximo que se muestra; los valores menores que 1 se muestran con hasta cuatro
    decimales para no esconder superficies chicas.
    """
    try:
        numero = Decimal(str(valor))
        decimales = int(decimales)
    except (InvalidOperation, TypeError, ValueError):
        return valor if valor is not None else ''
    if 0 < abs(numero) < 1:
        decimales = max(decimales, 4)
    redondeado = round(numero, decimales)
    entero, _, fraccion = f'{redondeado:f}'.partition('.')
    fraccion = fraccion.rstrip('0')
    signo = '-' if entero.startswith('-') else ''
    entero = f'{int(entero.lstrip("-") or 0):,}'.replace(',', '.')
    return f'{signo}{entero},{fraccion}' if fraccion else f'{signo}{entero}'
