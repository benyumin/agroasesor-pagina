import json
import math
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.db import IntegrityError, transaction
from django.core.paginator import Paginator
from django.db.models import Count, Prefetch, Q, Sum
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.utils.formats import number_format
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from .auth import MENSAJE_VENCIDA
from .search import run_search
from .catalog import (
    CATEGORY_LABELS,
    catalog_payload,
    category_key,
    default_zone,
    ensure_base_crops,
    CROP_GROUPS,
    crop_picker_data,
)
from .seasons import (
    assign_crop,
    current_season,
    season_payload,
    update_current_dates,
    agricultural_season_for,
    record_form_crop_change,
)
from .agronomy import evaluate_harvest_date, evaluate_sowing_date, find_reference
from .geometry import coordinates_from_geojson
from .forms import (
    UNIDADES_DOSIS,
    ActividadAgricolaForm,
    PerfilForm,
    RegistroForm,
    PredioForm,
    PotreroForm,
    TemporadaCultivoForm,
)
from .models import (
    AceptacionPoliticas,
    ActividadAgricola,
    Agricultor,
    CalculoAgricola,
    ConfiguracionAgronomica,
    Predio,
    PredioHistorial,
    Potrero,
    Cultivo,
    Insumo,
    Zona,
    ReferenciaAgronomica,
    TemporadaCultivo,
    ProblemaAgricola,
)


def build_menu(request, current):
    """Menú lateral (según el diseño): enlaces simples y el grupo «Calculadoras» con sus herramientas."""
    tool = request.GET.get('herramienta', 'llevar') if current == 'decisiones' else ''
    kind = request.GET.get('tipo', '')
    calculators = [
        {
            'label': 'Semillas',
            'icon': 'wheat',
            'href': reverse('decisiones') + '?herramienta=llevar&tipo=semillas',
            'active': current == 'decisiones' and tool == 'llevar' and kind == 'semillas',
        },
        {
            'label': 'Fertilizantes',
            'icon': 'flask-conical',
            'href': reverse('decisiones') + '?herramienta=llevar&tipo=insumos',
            'active': current == 'decisiones' and tool == 'llevar' and kind == 'insumos',
        },
        {
            'label': '¿Me alcanza?',
            'icon': 'scale',
            'href': reverse('decisiones') + '?herramienta=alcanza',
            'active': current == 'decisiones' and tool == 'alcanza',
        },
    ]
    in_calculators = any(item['active'] for item in calculators)
    items = [
        {'label': 'Inicio', 'icon': 'house', 'href': reverse('dashboard'), 'active': current == 'dashboard'},
        {'label': 'Mis predios', 'icon': 'map-pinned', 'href': reverse('predios'), 'active': current == 'predios'},
        {'label': 'Mapa', 'icon': 'map', 'href': reverse('predios') + '#mapa', 'active': False},
        {
            'label': 'Catálogo agrícola',
            'icon': 'library',
            'href': reverse('productos'),
            'active': current == 'productos',
        },
        {
            'label': 'Calculadoras',
            'icon': 'calculator',
            'children': calculators,
            'open': in_calculators,
            'active': in_calculators,
        },
        {
            'label': 'Actividades',
            'icon': 'clipboard-list',
            'href': reverse('actividad_list'),
            'active': current == 'actividades',
        },
        {
            'label': 'Toma de decisiones',
            'icon': 'lightbulb',
            'href': reverse('decisiones'),
            'active': current == 'decisiones' and not in_calculators,
        },
        {'label': 'Reportes', 'icon': 'file-text', 'href': reverse('reportes'), 'active': current == 'reportes'},
        {
            'label': 'Calendario',
            'icon': 'calendar-days',
            'href': reverse('calendario'),
            'active': current == 'calendario',
        },
        {'label': 'Buscar', 'icon': 'search', 'href': reverse('busqueda'), 'active': current == 'busqueda'},
    ]
    if request.user.is_staff:
        items.append(
            {
                'label': 'Administración',
                'icon': 'shield-check',
                'href': reverse('admin_panel'),
                'active': current == 'admin_panel',
            }
        )
    return items


def map_config():
    return {'tile_url': settings.MAP_TILE_URL, 'attribution': settings.MAP_TILE_ATTRIBUTION}


PLAN_TOTAL = 3  # pasos de planificación que ya existen en los datos: cultivo, fecha de siembra y fecha de cosecha


def _task_summary(user, today):
    """Labores pendientes/vencidas por potrero y predio, y la próxima de cada potrero (solo lectura)."""
    from django.utils.formats import date_format

    plots, farms = {}, {}
    pending = (
        ActividadAgricola.objects.filter(
            usuario=user,
            estado=ActividadAgricola.Estado.PENDIENTE,
            potrero__predio__propietario=user,
        )
        .select_related('potrero')
        .order_by('fecha_programada', 'pk')
    )
    for activity in pending:
        overdue = activity.fecha_programada < today
        plot = plots.setdefault(activity.potrero_id, {'pending': 0, 'overdue': 0, 'next': None})
        farm = farms.setdefault(activity.potrero.predio_id, {'pending': 0, 'overdue': 0})
        plot['pending'] += 1
        farm['pending'] += 1
        if overdue:
            plot['overdue'] += 1
            farm['overdue'] += 1
        if plot['next'] is None:
            plot['next'] = {
                'label': activity.get_tipo_display(),
                'type': activity.tipo,
                'icon': ACTIVITY_ICONS.get(activity.tipo, 'clipboard-list'),
                'date': date_format(activity.fecha_programada, 'j M'),
                'overdue': overdue,
            }
    return plots, farms


def _plot_ui(plot, farm, tasks):
    done = int(bool(plot.cultivo_id)) + int(bool(plot.fecha_siembra)) + int(bool(plot.fecha_cosecha))
    info = tasks.get(plot.pk, {})
    plan_query = {'potrero': plot.pk}
    if plot.cultivo_id:
        plan_query['crop'] = plot.cultivo_id
    if farm.zona_id:
        plan_query['zone'] = farm.zona_id
    edit_url = reverse('potrero_edit', kwargs={'predio_pk': farm.pk, 'pk': plot.pk})
    # Acción principal del potrero según lo que le falta; con cultivo y siembra ya se puede calcular.
    if not plot.cultivo_id:
        primary = {'label': 'Asignar cultivo', 'url': edit_url}
    elif not plot.fecha_siembra:
        primary = {'label': 'Continuar planificación', 'url': edit_url}
    else:
        primary = {'label': 'Calcular semillas', 'url': decision_url('semillas', plot)}
    tool_actions = []
    if plot.cultivo_id:
        tool_actions = [
            {'label': 'Calcular semillas', 'url': decision_url('semillas', plot), 'icon': 'sprout'},
            {'label': 'Calcular insumos', 'url': decision_url('insumos', plot), 'icon': 'flask-conical'},
            {'label': 'Ver si alcanza', 'url': decision_url('alcanza', plot), 'icon': 'warehouse'},
        ]
    return {
        'primary': primary,
        'tool_actions': tool_actions,
        'edit_url': edit_url,
        'can_calculate': bool(plot.cultivo_id and plot.fecha_siembra),
        'status': plot_season_summary(plot)['status'],
        'has_crop': bool(plot.cultivo_id),
        'plan_done': done,
        'plan_total': PLAN_TOTAL,
        'plan_complete': done == PLAN_TOTAL,
        'plan_steps': [bool(plot.cultivo_id), bool(plot.fecha_siembra), bool(plot.fecha_cosecha)],
        'pending': info.get('pending', 0),
        'overdue': info.get('overdue', 0),
        'next': info.get('next'),
        'task_url': reverse('actividad_create') + '?' + urlencode({'potrero': plot.pk}),
        'plan_url': reverse('calendario') + '?' + urlencode(plan_query),
    }


def _farm_next_step(farm, plots, pending=0):
    """Una única recomendación para el predio, según lo que ya tiene registrado (solo presentación)."""
    plots = sorted(plots, key=lambda item: item.pk)
    if not plots:
        return {
            'key': 'potrero',
            'title': 'Crea tu primer potrero',
            'text': 'Divide tu predio en sectores o usa todo el predio como un solo potrero.',
            'cta': 'Agregar potrero',
            'url': reverse('potrero_create', kwargs={'predio_pk': farm.pk}),
        }
    without_crop = next((plot for plot in plots if not plot.cultivo_id), None)
    if without_crop:
        return {
            'key': 'cultivo',
            'title': f'Asigna un cultivo a «{without_crop.nombre}»',
            'text': 'Con el cultivo podrás planificar la siembra y usar las herramientas de cálculo.',
            'cta': 'Asignar cultivo',
            'url': reverse('potrero_edit', kwargs={'predio_pk': farm.pk, 'pk': without_crop.pk}),
        }
    unplanned = next((plot for plot in plots if not (plot.fecha_siembra and plot.fecha_cosecha)), None)
    if unplanned:
        return {
            'key': 'planificacion',
            'title': f'Define la planificación de «{unplanned.nombre}»',
            'text': 'Registra la fecha de siembra y la cosecha prevista.',
            'cta': 'Definir planificación',
            'url': reverse('potrero_edit', kwargs={'predio_pk': farm.pk, 'pk': unplanned.pk}),
        }
    first = plots[0]
    return {
        'key': 'decisiones',
        'title': f'Calcula lo que necesitas para «{first.nombre}»',
        'text': 'Tu predio ya está planificado: calcula semillas e insumos, o revisa si te alcanza.'
        + ('' if pending else ' Después puedes programar tus labores.'),
        'cta': 'Calcular semillas',
        'url': decision_url('semillas', first),
    }


def _farm_ui(farm, plots, farm_tasks, plot_uis):
    info = farm_tasks.get(farm.pk, {})
    pending = info.get('pending', 0)
    return {
        'plot_count': len(plots),
        'crop_count': len({plot.cultivo_id for plot in plots if plot.cultivo_id}),
        'pending': pending,
        'overdue': info.get('overdue', 0),
        'all_planned': bool(plots) and all(plot_uis[plot.pk]['plan_complete'] for plot in plots),
        'next_step': _farm_next_step(farm, plots, pending),
    }


ORIGIN_LABELS = {
    'documented': 'Dato documentado',
    'catalog': 'Dato de catálogo (sin respaldo completo)',
    'adjusted': 'Ajustado manualmente',
    'simulation': 'Simulación',
}
# El Centro de decisiones y las calculadoras no llevan la ruta de seis pasos: arrancan directo en la herramienta.
JOURNEY_PAGES = {'dashboard', 'predios', 'calendario', 'reportes'}
JOURNEY_HERE = {
    'predios': {'predio', 'potreros', 'cultivo'},
    'calendario': {'siembra'},
    'reportes': {'reporte'},
}
JOURNEY_NOTES = {
    'calendario': 'Después de elegir cultivo, completa la fecha de siembra del potrero.',
    'reportes': 'Los cálculos del Centro de decisiones quedan guardados y aparecen aquí.',
}


def decision_url(tool='semillas', plot=None):
    """Enlace al Centro de decisiones con el potrero ya elegido (la herramienta y el cultivo se precargan)."""
    query = {}
    if tool in ('semillas', 'insumos'):
        query['tipo'] = tool
    elif tool == 'alcanza':
        query['herramienta'] = tool
    if plot is not None:
        query['potrero'] = plot.pk if hasattr(plot, 'pk') else plot
    return reverse('decisiones') + ('?' + urlencode(query) if query else '')


def journey_payload(user, current):
    """Ruta de trabajo del usuario: qué ya hizo y cuál es el siguiente paso (solo lectura, sin datos nuevos)."""
    farms = list(Predio.objects.filter(propietario=user).order_by('pk'))
    plots = list(Potrero.objects.filter(predio__propietario=user).select_related('cultivo', 'predio').order_by('pk'))
    has_calculation = CalculoAgricola.objects.filter(usuario=user).exists()
    farm_ids_with_plots = {plot.predio_id for plot in plots}
    farm_without_plots = next((farm for farm in farms if farm.pk not in farm_ids_with_plots), None)
    plot_without_crop = next((plot for plot in plots if not plot.cultivo_id), None)
    plot_without_sowing = next((plot for plot in plots if plot.cultivo_id and not plot.fecha_siembra), None)
    plot_with_crop = next((plot for plot in plots if plot.cultivo_id), None)

    def action(label, url, primary=True, post=False):
        return {'label': label, 'url': url, 'primary': primary, 'post': post}

    # Estado 5: con cultivo y siembra planificada ya se pueden usar las herramientas de cálculo.
    tools_actions = [
        action('Calcular semillas', decision_url('semillas', plot_with_crop)),
        action('Calcular insumos', decision_url('insumos', plot_with_crop), primary=False),
        action('Ver si me alcanza', decision_url('alcanza', plot_with_crop), primary=False),
        action('Consultar ficha de producto', reverse('productos'), primary=False),
    ]
    split_url = farm_without_plots and reverse('potrero_create', kwargs={'predio_pk': farm_without_plots.pk})
    potrero_actions = []
    if farm_without_plots:
        if farm_without_plots.geojson:
            potrero_actions.append(
                action(
                    'Utilizar todo el predio',
                    reverse('predio_usar_completo', kwargs={'pk': farm_without_plots.pk}),
                    post=True,
                )
            )
        potrero_actions.append(action('Dividir en potreros', split_url, primary=not farm_without_plots.geojson))
    steps = [
        {
            'key': 'predio',
            'label': 'Predio',
            'done': bool(farms),
            'url': reverse('predios'),
            'next': {
                'status': '',
                'title': 'Comienza registrando tu predio',
                'text': 'Dibuja su perímetro en el mapa; después defines cómo trabajarlo.',
                'actions': [action('Registrar predio', reverse('predio_create'))],
            },
        },
        {
            'key': 'potreros',
            'label': 'Potreros',
            'done': bool(plots) and not farm_without_plots,
            'url': reverse('predios'),
            'next': farm_without_plots
            and {
                'status': 'Tu predio ya está registrado',
                'title': '¿Cómo quieres trabajar este terreno?',
                'text': f'«{farm_without_plots.nombre}»: los cálculos y la siembra se hacen por potrero: usa todo el predio como uno solo o divídelo en sectores.',
                'actions': potrero_actions,
            },
        },
        {
            'key': 'cultivo',
            'label': 'Cultivo',
            'done': bool(plots) and not plot_without_crop,
            'url': reverse('predios'),
            'next': plot_without_crop
            and {
                'status': 'Potrero listo',
                'title': 'Selecciona qué quieres plantar',
                'text': f'«{plot_without_crop.nombre}»: con el cultivo podrás planificar la siembra y usar las herramientas de cálculo.',
                'actions': [
                    action(
                        'Asignar cultivo',
                        reverse(
                            'potrero_edit',
                            kwargs={'predio_pk': plot_without_crop.predio_id, 'pk': plot_without_crop.pk},
                        ),
                    )
                ],
            },
        },
        {
            'key': 'siembra',
            'label': 'Siembra',
            'done': bool(plot_with_crop) and not plot_without_sowing,
            'url': reverse('calendario'),
            'next': plot_without_sowing
            and {
                'status': 'Cultivo seleccionado',
                'title': 'Planifica la siembra',
                'text': f'{plot_without_sowing.cultivo.nombre} en «{plot_without_sowing.nombre}»: falta la fecha de siembra para dejar el potrero planificado.',
                'actions': [
                    action(
                        'Continuar planificación',
                        reverse(
                            'potrero_edit',
                            kwargs={'predio_pk': plot_without_sowing.predio_id, 'pk': plot_without_sowing.pk},
                        ),
                    )
                ],
            },
        },
        {
            'key': 'decisiones',
            'label': 'Decisiones',
            'done': has_calculation,
            'url': reverse('decisiones'),
            'next': {
                'status': 'Ya puedes utilizar las herramientas agrícolas' if plot_with_crop else '',
                'title': 'Calcula cuánto llevar o si te alcanza',
                'text': (
                    'Elige una herramienta; el potrero y el cultivo ya van cargados.'
                    if plot_with_crop
                    else 'Semillas, fertilizantes u otros insumos para tu potrero.'
                ),
                'actions': (
                    tools_actions
                    if plot_with_crop
                    else [action('Ir al Centro de decisiones', decision_url('semillas'))]
                ),
            },
        },
        {
            'key': 'reporte',
            'label': 'Reporte',
            'done': False,
            'url': reverse('reportes'),
            'next': {
                'status': '',
                'title': 'Revisa tus reportes y el historial',
                'text': 'Resumen del predio, cuaderno de campo y cálculos guardados.',
                'actions': [action('Ver reportes', reverse('reportes'))],
            },
        },
    ]
    # Un paso está hecho cuando no queda nada pendiente en él (todos los predios tienen potreros, todos los potreros
    # tienen cultivo y fecha de siembra). El paso actual es el primero sin completar con una acción concreta por hacer.
    current_key = None
    for step in steps:
        if step['key'] == 'reporte' or (not step['done'] and step['next']):
            current_key = step['key']
            break
    here = JOURNEY_HERE.get(current, set())
    items, current_step = [], None
    for step in steps:
        state = 'current' if step['key'] == current_key else ('done' if step['done'] else 'todo')
        items.append(
            {
                'key': step['key'],
                'label': step['label'],
                'url': step['url'],
                'state': state,
                'here': step['key'] in here,
            }
        )
        if step['key'] == current_key:
            current_step = dict(
                step['next'], cta=step['next']['actions'][0]['label'], url=step['next']['actions'][0]['url']
            )
    return {
        'steps': items,
        'next': current_step,
        'note': JOURNEY_NOTES.get(current, ''),
        'done_count': sum(1 for step in steps if step['done']),
        'total': len(steps) - 1,
    }


def farm_cards(user, predios=None):
    """Predios con sus potreros y los indicadores de presentación que ya salen de los datos existentes."""
    today = timezone.localdate()
    task_plots, task_farms = _task_summary(user, today)
    if predios is None:
        predios = (
            Predio.objects.filter(propietario=user)
            .annotate(potreros_count=Count('potreros'))
            .prefetch_related('potreros__cultivo', 'potreros__temporadas__cultivo')
        )
    cards = []
    for farm in predios:
        plots = list(farm.potreros.all())
        plot_uis = {plot.pk: _plot_ui(plot, farm, task_plots) for plot in plots}
        cards.append(
            {
                'predio': farm,
                'ui': _farm_ui(farm, plots, task_farms, plot_uis),
                'plots': [{'plot': plot, 'ui': plot_uis[plot.pk]} for plot in plots],
            }
        )
    return cards


def farm_payload(user):
    predios = list(
        Predio.objects.filter(propietario=user).select_related('zona').annotate(potreros_count=Count('potreros'))
    )
    plots = list(
        Potrero.objects.filter(predio__propietario=user)
        .select_related('predio__zona', 'cultivo')
        .prefetch_related('temporadas__cultivo')
    )
    assigned_by_farm = {}
    for plot in plots:
        assigned_by_farm[plot.predio_id] = assigned_by_farm.get(plot.predio_id, Decimal('0')) + plot.superficie_ha
    total_farm_area = sum(
        (predio.superficie_ha for predio in predios),
        Decimal('0'),
    )
    assigned_area = sum(
        (plot.superficie_ha for plot in plots),
        Decimal('0'),
    )
    unassigned_area = sum(
        (max(Decimal('0'), predio.superficie_ha - assigned_by_farm.get(predio.pk, Decimal('0'))) for predio in predios),
        Decimal('0'),
    )
    crop_count = (
        Potrero.objects.filter(
            predio__propietario=user,
            cultivo__activo=True,
        )
        .values('cultivo_id')
        .distinct()
        .count()
    )
    distribution_pct = assigned_area / total_farm_area * 100 if total_farm_area else Decimal('0')
    task_plots, task_farms = _task_summary(user, timezone.localdate())
    plots_by_farm = {}
    for plot in sorted(plots, key=lambda item: item.pk):
        plots_by_farm.setdefault(plot.predio_id, []).append(plot)
    plot_ui = {plot.pk: _plot_ui(plot, plot.predio, task_plots) for plot in plots}
    farm_ui = {farm.pk: _farm_ui(farm, plots_by_farm.get(farm.pk, []), task_farms, plot_ui) for farm in predios}
    return {
        'predios': [
            {
                'id': str(p.pk),
                'name': p.nombre,
                'location': p.ubicacion,
                'area': float(p.superficie_ha),
                'cropCount': farm_ui[p.pk]['crop_count'],
                'pendingTasks': farm_ui[p.pk]['pending'],
                'overdueTasks': farm_ui[p.pk]['overdue'],
                'allPlanned': farm_ui[p.pk]['all_planned'],
                'nextStep': farm_ui[p.pk]['next_step'],
                'zoneId': p.zona_id,
                'zoneName': p.zona.nombre if p.zona_id else '',
                'plotCount': p.potreros_count,
                'updated': timezone.localtime(p.actualizado).strftime('%d/%m/%Y'),
                'lat': p.latitud,
                'lng': p.longitud,
                'geojson': p.geojson,
                'perimeterM': float(p.perimetro_m) if p.perimetro_m is not None else None,
                'detailUrl': reverse('predio_detail', kwargs={'pk': p.pk}),
                'setupUrl': reverse('predio_detail', kwargs={'pk': p.pk}) + '?nuevo=1',
                'editUrl': reverse('predio_edit', kwargs={'pk': p.pk}),
                'potreroCreateUrl': reverse('potrero_create', kwargs={'predio_pk': p.pk}),
            }
            for p in predios
        ],
        'plots': [
            {
                **plot_season_summary(p),
                'id': str(p.pk),
                'predio': str(p.predio_id),
                'farmName': p.predio.nombre,
                'zoneId': p.predio.zona_id,
                'zoneName': p.predio.zona.nombre if p.predio.zona_id else '',
                'planDone': plot_ui[p.pk]['plan_done'],
                'planTotal': PLAN_TOTAL,
                'pendingTasks': plot_ui[p.pk]['pending'],
                'overdueTasks': plot_ui[p.pk]['overdue'],
                'nextTask': plot_ui[p.pk]['next'],
                'taskUrl': plot_ui[p.pk]['task_url'],
                'planUrl': plot_ui[p.pk]['plan_url'],
                'seedsUrl': decision_url('semillas', p),
                'name': p.nombre,
                'crop': p.cultivo.nombre if p.cultivo else '',
                'variety': p.variedad,
                'sowing': p.fecha_siembra.isoformat() if p.fecha_siembra else '',
                'harvest': p.fecha_cosecha.isoformat() if p.fecha_cosecha else '',
                'areaHa': float(p.superficie_ha),
                'areaM2': float(p.superficie_ha * Decimal('10000')),
                'perimeterM': float(p.perimetro_m) if p.perimetro_m is not None else None,
                'coordinates': p.perimetro,
                'geojson': p.geojson,
                'lat': p.latitud,
                'lng': p.longitud,
                'created': timezone.localtime(p.creado).strftime('%d/%m/%Y'),
                'updated': timezone.localtime(p.actualizado).strftime('%d/%m/%Y'),
                'cropId': str(p.cultivo_id) if p.cultivo_id else '',
                'assignUrl': reverse('potrero_assign_crop', kwargs={'pk': p.pk}),
                'seasonCreateUrl': reverse('temporada_create', kwargs={'predio_pk': p.predio_id, 'potrero_pk': p.pk}),
                'editUrl': reverse('potrero_edit', kwargs={'predio_pk': p.predio_id, 'pk': p.pk}),
                'detailUrl': (reverse('predio_detail', kwargs={'pk': p.predio_id}) + f'?potrero={p.pk}#predio-history'),
            }
            for p in plots
        ],
        'stats': {
            'predios': len(predios),
            'potreros': len(plots),
            'superficie_predios': float(total_farm_area),
            'superficie': float(assigned_area),
            'superficie_sin_asignar': float(unassigned_area),
            'distribucion_pct': float(distribution_pct),
            'distribucion_bar_pct': float(min(Decimal('100'), max(Decimal('0'), distribution_pct))),
            'sobreasignada': assigned_area > total_farm_area,
            'cultivos': crop_count,
        },
    }


def plot_season_summary(plot):
    seasons = sorted(plot.temporadas.all(), key=lambda item: (item.creado, item.pk), reverse=True)
    current = seasons[0] if seasons else None
    if current:
        status = current.get_estado_display()
        status_code = current.estado
    elif plot.cultivo_id:
        status, status_code = 'Cultivo registrado sin temporada', 'legacy'
    else:
        status, status_code = 'Sin cultivo', 'none'
    return {
        'status': status,
        'statusCode': status_code,
        'season': current.temporada_agricola if current else '',
        'currentSeasonId': current.pk if current else None,
        'seasons': [season_payload(season) for season in seasons],
    }


def _page(request, template, current, extra=None):
    if not request.user.is_authenticated:
        return redirect('login')
    if current == 'admin_panel' and not request.user.is_staff:
        raise PermissionDenied
    extra = extra or {}
    name = request.user.get_full_name() or request.user.username
    context = {
        'user': {
            'name': name,
            'email': request.user.email,
            'role': 'Administrador' if request.user.is_staff else 'Agricultor',
        },
        'first_name': name.split()[0],
        'initials': ''.join(p[0] for p in name.split()[:2]).upper(),
        'menu': build_menu(request, current),
        'current': current,
        'query': request.GET.get('q', ''),
        'catalog': extra.get('catalog') or catalog_payload(),
        'farm_data': extra.get('farm_data') or farm_payload(request.user),
        'pending_activities': ActividadAgricola.objects.filter(
            usuario=request.user,
            estado=ActividadAgricola.Estado.PENDIENTE,
        )
        .select_related('potrero', 'potrero__predio')
        .order_by('fecha_programada', 'pk')[:5],
        'phase_a': current
        in {
            'dashboard',
            'predios',
            'calendario',
            'admin_panel',
            'actividades',
            'decisiones',
            'productos',
            'reportes',
            'busqueda',
            'perfil',
        },
        # Cuentas creadas antes de pedir teléfono y comuna: se invita a completar el perfil (sin bloquear nada).
        'perfil_incompleto': (
            current != 'perfil'
            and not request.user.is_staff
            and not Agricultor.objects.filter(user=request.user).exists()
        ),
    }
    if current in JOURNEY_PAGES:
        context['journey'] = journey_payload(request.user, current)
    if current == 'decisiones':
        # Cultivos frecuentes y categorías para elegir cultivo en el Centro de decisiones.
        context['crop_picker'] = crop_picker_data(request.user)
    context.update(extra)
    response = render(request, template, context)
    add_never_cache_headers(response)
    return response


def politica_privacidad(request):
    return render(
        request,
        'politica_privacidad.html',
        {
            'policy_version': AceptacionPoliticas.VERSION_ACTUAL,
        },
    )


@never_cache
def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    mode = request.POST.get('mode') or request.GET.get('mode') or 'login'
    if mode in {'recover', 'reset'}:
        return redirect('password_reset')
    mode = 'register' if mode == 'register' else 'login'
    error = ''
    if request.method == 'GET' and request.GET.get('vencida'):
        messages.warning(
            request, MENSAJE_VENCIDA
        )  # la página detectó que se perdió la sesión (p. ej. cookies borradas)
    identifier = (request.POST.get('username') or request.POST.get('email') or '').strip()
    registration = RegistroForm(request.POST if request.method == 'POST' and mode == 'register' else None)
    if request.method == 'POST':
        if mode == 'register':
            if registration.is_valid():
                try:
                    with transaction.atomic():
                        user = registration.save()
                        AceptacionPoliticas.objects.create(
                            usuario=user,
                            version=AceptacionPoliticas.VERSION_ACTUAL,
                        )
                except IntegrityError:
                    error = 'El nombre de usuario o el correo electrónico ya está registrado.'
                else:
                    login(request, user, backend='core.auth.EmailBackend')
                    messages.success(request, 'Tu cuenta se creó correctamente. Ya iniciaste sesión.')
                    return redirect('dashboard')
        else:
            user = authenticate(
                request,
                username=identifier,
                password=request.POST.get('password', ''),
            )
            if user is None:
                error = 'Usuario, correo electrónico o contraseña incorrectos.'
            else:
                login(request, user)
                return redirect('dashboard')
    return render(
        request,
        'login.html',
        {
            'mode': mode,
            'error': error,
            'registration_form': registration,
            'identifier': identifier,
            'headline': (
                'Administra los datos reales de tu campo.' if mode == 'login' else 'Registra tu cuenta de agricultor.'
            ),
        },
    )


@require_POST
def logout_view(request):
    logout(request)
    return redirect('login')


@login_required
def perfil(request):
    """Datos personales del agricultor: nombre, teléfono, comuna y RUT opcional."""
    form = PerfilForm(request.POST or None, user=request.user)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Tus datos se guardaron correctamente.')
        return redirect('perfil')
    return _page(request, 'perfil.html', 'perfil', {'form': form, 'tiene_ficha': form.ficha is not None})


@login_required
@require_POST
def enter_as_admin(request):
    if not request.user.is_staff:
        raise PermissionDenied
    return redirect('admin_panel')


@login_required
def dashboard(request):
    farm_data = farm_payload(request.user)
    pending = (
        ActividadAgricola.objects.filter(
            usuario=request.user,
            potrero__predio__propietario=request.user,
            estado=ActividadAgricola.Estado.PENDIENTE,
        )
        .select_related('potrero', 'potrero__predio')
        .order_by('fecha_programada', 'pk')
    )
    return _page(
        request,
        'dashboard.html',
        'dashboard',
        {
            'farm_data': farm_data,
            'dashboard_predios': farm_data['predios'],
            'dashboard_plots': farm_data['plots'],
            'dashboard_activities': pending[:5],
            'pending_activity_count': pending.count(),
            'dashboard_calculations': [
                describe_calculation(item)
                for item in CalculoAgricola.objects.filter(
                    usuario=request.user,
                    potrero__predio__propietario=request.user,
                )
                .select_related('potrero__predio')
                .order_by('-creado', '-pk')[:4]
            ],
        },
    )


def _activities_for_user(user):
    return ActividadAgricola.objects.filter(
        usuario=user,
    ).select_related('potrero', 'potrero__predio', 'potrero__cultivo', 'temporada__cultivo', 'insumo__tipo')


# Recomendaciones generales de Buenas Prácticas Agrícolas por tipo de labor.
# Son pautas de manejo y registro, no dosis ni prescripciones.
ACTIVITY_GUIDANCE = {
    'preparacion': {
        'title': 'Preparación de suelo',
        'tips': [
            'Labra con humedad de suelo adecuada (ni seco ni barroso) para evitar terrones y compactación.',
            'Registra el implemento usado y la profundidad de trabajo.',
            'Si corresponde, toma muestras de suelo antes de preparar para definir la fertilización.',
        ],
    },
    'siembra': {
        'title': 'Siembra',
        'tips': [
            'Usa semilla certificada y guarda la etiqueta: variedad, lote, germinación y pureza.',
            'Compara la fecha con la ventana referencial del Calendario antes de sembrar.',
            'Calibra la sembradora para la densidad calculada en «Cuánto llevar».',
        ],
    },
    'fertilizacion': {
        'title': 'Fertilización',
        'tips': [
            'Define la dosis a partir de un análisis de suelo y la recomendación técnica.',
            'Fracciona el nitrógeno según la etapa del cultivo cuando la recomendación lo indique.',
            'Evita aplicar antes de lluvias intensas o sobre suelo saturado para reducir pérdidas.',
        ],
    },
    'riego': {
        'title': 'Riego',
        'tips': [
            'Decide el riego según la humedad del suelo y la etapa del cultivo.',
            'Registra tiempo de riego o volumen aplicado y revisa la uniformidad.',
            'Evita encharcamientos prolongados que favorecen enfermedades de raíz.',
        ],
    },
    'monitoreo': {
        'title': 'Monitoreo',
        'tips': [
            'Recorre el potrero en zigzag o por estaciones fijas para que la muestra sea representativa.',
            'Anota plagas, enfermedades y malezas con su intensidad y la etapa del cultivo.',
            'Define con tu asesor el umbral que justifica una intervención.',
        ],
    },
    'control_fitosanitario': {
        'title': 'Aplicación fitosanitaria',
        'tips': [
            'Usa solo productos autorizados por el SAG para ese cultivo y objetivo, con la dosis de la etiqueta.',
            'Usa el equipo de protección personal indicado en la etiqueta.',
            'No apliques con viento fuerte, lluvia inminente o temperaturas altas.',
            'Respeta el período de carencia y el tiempo de reingreso; señaliza el potrero.',
            'Haz triple lavado de los envases y entrégalos a un centro de acopio autorizado.',
            'Registra producto, dosis, superficie, fecha y aplicador en el cuaderno de campo.',
        ],
    },
    'cosecha': {
        'title': 'Cosecha',
        'tips': [
            'Verifica que haya terminado el período de carencia de la última aplicación.',
            'Confirma la madurez con indicadores observados en terreno, no solo con el calendario.',
            'Registra la producción obtenida para mejorar tus estimaciones de rendimiento.',
        ],
    },
    'otra': {
        'title': 'Otra labor',
        'tips': ['Describe la labor y su resultado en observaciones para mantener el historial completo.'],
    },
}


def _activity_form_context(request, form, heading, activity=None):
    plots = (
        Potrero.objects.filter(predio__propietario=request.user)
        .select_related(
            'predio',
            'cultivo',
        )
        .prefetch_related('temporadas__cultivo')
        .order_by('predio__nombre', 'nombre')
    )
    plot_data = []
    for plot in plots:
        summary = plot_season_summary(plot)
        plot_data.append(
            {
                'id': plot.pk,
                'name': plot.nombre,
                'farm': plot.predio.nombre,
                'areaHa': float(plot.superficie_ha),
                'crop': plot.cultivo.nombre if plot.cultivo_id else '',
                'cropId': plot.cultivo_id,
                'variety': plot.variedad,
                'sowing': plot.fecha_siembra.isoformat() if plot.fecha_siembra else '',
                'harvest': plot.fecha_cosecha.isoformat() if plot.fecha_cosecha else '',
                'currentSeasonId': summary['currentSeasonId'],
                'seasons': summary['seasons'],
            }
        )
    upcoming = (
        _activities_for_user(request.user)
        .filter(
            estado=ActividadAgricola.Estado.PENDIENTE,
        )
        .order_by('fecha_programada', 'pk')
    )
    if activity:
        upcoming = upcoming.exclude(pk=activity.pk)
    return {
        'form': form,
        'heading': heading,
        'activity': activity,
        'activity_plots': plot_data,
        'activity_guidance': ACTIVITY_GUIDANCE,
        'upcoming_activities': list(upcoming[:40]),
    }


def _activity_initial(request):
    initial = {}
    params = request.GET
    plot_id = params.get('potrero', '')
    if plot_id.isdigit() and Potrero.objects.filter(pk=plot_id, predio__propietario=request.user).exists():
        initial['potrero'] = int(plot_id)
        plot = Potrero.objects.get(pk=plot_id)
        season = current_season(plot)
        if season:
            initial['temporada'] = season.pk
    if params.get('tipo') in ActividadAgricola.Tipo.values:
        initial['tipo'] = params['tipo']
        initial['nombre'] = ACTIVITY_GUIDANCE[params['tipo']]['title']
    try:
        if params.get('fecha'):
            initial['fecha_programada'] = date.fromisoformat(params['fecha'])
    except ValueError:
        pass
    if params.get('insumo', '').isdigit():
        initial['insumo'] = int(params['insumo'])
    for key, field in (('dosis', 'dosis'), ('superficie', 'superficie_ha')):
        try:
            value = Decimal(params.get(key, ''))
            if value.is_finite() and value > 0:
                initial[field] = value
        except (InvalidOperation, ValueError):
            pass
    if params.get('unidad') in dict(UNIDADES_DOSIS):
        initial['unidad_dosis'] = params['unidad']
    if params.get('objetivo'):
        initial['objetivo'] = params['objetivo'][:150]
    return initial


@login_required
def activity_list(request):
    today = timezone.localdate()
    base = _activities_for_user(request.user)
    activities = base
    filters = {
        'estado': request.GET.get('estado', ''),
        'tipo': request.GET.get('tipo', ''),
        'potrero': request.GET.get('potrero', ''),
        'desde': request.GET.get('desde', ''),
        'hasta': request.GET.get('hasta', ''),
        'q': request.GET.get('buscar', '').strip(),
        'vista': 'calendario' if request.GET.get('vista') == 'calendario' else 'lista',
    }
    if filters['estado'] == 'vencida':
        activities = activities.filter(estado=ActividadAgricola.Estado.PENDIENTE, fecha_programada__lt=today)
    elif filters['estado'] in ActividadAgricola.Estado.values:
        activities = activities.filter(estado=filters['estado'])
    if filters['tipo'] in ActividadAgricola.Tipo.values:
        activities = activities.filter(tipo=filters['tipo'])
    if filters['potrero'].isdigit():
        activities = activities.filter(potrero_id=filters['potrero'])
    for key, lookup in (('desde', 'fecha_programada__gte'), ('hasta', 'fecha_programada__lte')):
        try:
            if filters[key]:
                activities = activities.filter(**{lookup: date.fromisoformat(filters[key])})
        except ValueError:
            filters[key] = ''
    if filters['q']:
        activities = activities.filter(
            Q(nombre__icontains=filters['q'])
            | Q(observaciones__icontains=filters['q'])
            | Q(insumo__nombre__icontains=filters['q'])
            | Q(insumo_otro__icontains=filters['q'])
        )
    activities = activities.order_by('fecha_programada', 'pk')

    try:
        month = date.fromisoformat((request.GET.get('mes') or today.strftime('%Y-%m')) + '-01')
    except ValueError:
        month = today.replace(day=1)
    import calendar as _calendar

    month_days = _calendar.Calendar(firstweekday=0).monthdatescalendar(month.year, month.month)
    month_activities = {}
    for item in activities.filter(fecha_programada__gte=month_days[0][0], fecha_programada__lte=month_days[-1][-1]):
        month_activities.setdefault(item.fecha_programada, []).append(item)
    weeks = [
        [
            {
                'date': day,
                'in_month': day.month == month.month,
                'is_today': day == today,
                'items': month_activities.get(day, []),
            }
            for day in week
        ]
        for week in month_days
    ]
    previous_month = (month - timedelta(days=1)).replace(day=1)
    next_month = (month + timedelta(days=32)).replace(day=1)
    pending = base.filter(estado=ActividadAgricola.Estado.PENDIENTE)
    stats = {
        'pendientes': pending.count(),
        'vencidas': pending.filter(fecha_programada__lt=today).count(),
        'proximas': pending.filter(
            fecha_programada__gte=today, fecha_programada__lte=today + timedelta(days=7)
        ).count(),
        'completadas_mes': base.filter(
            estado=ActividadAgricola.Estado.COMPLETADA,
            fecha_realizada__year=today.year,
            fecha_realizada__month=today.month,
        ).count(),
    }
    plots = (
        Potrero.objects.filter(predio__propietario=request.user)
        .select_related('predio', 'cultivo')
        .prefetch_related(
            'temporadas__cultivo',
        )
        .order_by('predio__nombre', 'nombre')
    )
    plan_plots = []
    for plot in plots:
        season = current_season(plot)
        plan_plots.append(
            {
                'plot': plot,
                'season': season,
                'sowing': season.fecha_siembra if season else plot.fecha_siembra,
                'harvest': season.fecha_cosecha if season else plot.fecha_cosecha,
            }
        )
    return _page(
        request,
        'activity_list.html',
        'actividades',
        {
            'activities': activities,
            'activity_filters': filters,
            'activity_types': ActividadAgricola.Tipo.choices,
            'activity_states': ActividadAgricola.Estado.choices,
            'activity_plots': plots,
            'activity_stats': stats,
            'calendar_weeks': weeks,
            'calendar_month': month,
            'previous_month': previous_month.strftime('%Y-%m'),
            'next_month': next_month.strftime('%Y-%m'),
            'today': today,
            'plan_plots': plan_plots,
        },
    )


@login_required
def activity_create(request):
    form = ActividadAgricolaForm(
        request.POST or None,
        user=request.user,
        instance=ActividadAgricola(usuario=request.user),
        initial=_activity_initial(request) if request.method == 'GET' else None,
    )
    if request.method == 'POST' and form.is_valid():
        activity = form.save()
        messages.success(request, 'La actividad quedó guardada y aparecerá en Hoy, Siembra y el cuaderno de campo.')
        if request.POST.get('siguiente') == 'otra':
            return redirect(f"{reverse('actividad_create')}?potrero={activity.potrero_id}")
        return redirect('actividad_detail', pk=activity.pk)
    return _page(
        request,
        'activity_form.html',
        'actividades',
        _activity_form_context(
            request,
            form,
            'Programar labor agrícola',
        ),
    )


@login_required
def activity_detail(request, pk):
    activity = get_object_or_404(_activities_for_user(request.user), pk=pk)
    guidance = ACTIVITY_GUIDANCE.get(activity.tipo)
    related = (
        _activities_for_user(request.user)
        .filter(potrero=activity.potrero)
        .exclude(pk=activity.pk)
        .order_by('-fecha_programada')[:6]
    )
    return _page(
        request,
        'activity_detail.html',
        'actividades',
        {
            'activity': activity,
            'guidance': guidance,
            'related_activities': related,
        },
    )


@login_required
def activity_edit(request, pk):
    activity = get_object_or_404(_activities_for_user(request.user), pk=pk)
    form = ActividadAgricolaForm(request.POST or None, user=request.user, instance=activity)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'La actividad fue actualizada.')
        return redirect('actividad_detail', pk=activity.pk)
    return _page(
        request,
        'activity_form.html',
        'actividades',
        _activity_form_context(
            request,
            form,
            'Editar actividad agrícola',
            activity,
        ),
    )


@login_required
@require_POST
def activity_change_status(request, pk):
    activity = get_object_or_404(_activities_for_user(request.user), pk=pk)
    new_status = request.POST.get('estado', '')
    if new_status not in ActividadAgricola.Estado.values:
        messages.error(request, 'Selecciona un estado válido para la actividad.')
        return redirect('actividad_detail', pk=activity.pk)
    activity.estado = new_status
    fields = ['estado', 'actualizado']
    if new_status == ActividadAgricola.Estado.COMPLETADA and not activity.fecha_realizada:
        activity.fecha_realizada = timezone.localdate()
        fields.append('fecha_realizada')
    if new_status == ActividadAgricola.Estado.PENDIENTE and activity.fecha_realizada:
        activity.fecha_realizada = None
        fields.append('fecha_realizada')
    activity.save(update_fields=fields)
    messages.success(request, 'El estado de la actividad fue actualizado.')
    destination = request.POST.get('next')
    if destination == 'dashboard':
        return redirect('dashboard')
    if destination == 'calendario':
        return redirect('calendario')
    if destination == 'actividades':
        return redirect('actividad_list')
    return redirect('actividad_detail', pk=activity.pk)


@login_required
def activity_delete(request, pk):
    activity = get_object_or_404(_activities_for_user(request.user), pk=pk)
    if request.method == 'POST':
        activity.delete()
        messages.success(request, 'La actividad fue eliminada.')
        return redirect('actividad_list')
    return _page(
        request,
        'activity_confirm_delete.html',
        'actividades',
        {
            'activity': activity,
        },
    )


@login_required
@require_POST
@transaction.atomic
def activity_plan(request):
    """Crea actividades a partir de las fechas que el agricultor registró en su temporada.

    No agrega labores ni frecuencias por defecto: solo las que el usuario marca,
    con los intervalos que él mismo indica.
    """
    plot_id = request.POST.get('potrero', '')
    plot = get_object_or_404(Potrero, pk=plot_id if plot_id.isdigit() else 0, predio__propietario=request.user)
    season = current_season(plot)
    sowing = season.fecha_siembra if season else plot.fecha_siembra
    harvest = season.fecha_cosecha if season else plot.fecha_cosecha
    crop = season.cultivo.nombre if season else (plot.cultivo.nombre if plot.cultivo_id else '')
    planned = []

    def add(tipo, nombre, day, observaciones=''):
        exists = ActividadAgricola.objects.filter(
            usuario=request.user, potrero=plot, tipo=tipo, fecha_programada=day
        ).exists()
        if not exists:
            planned.append(
                ActividadAgricola(
                    usuario=request.user,
                    potrero=plot,
                    temporada=season,
                    tipo=tipo,
                    nombre=nombre,
                    fecha_programada=day,
                    observaciones=observaciones,
                    prioridad=(
                        ActividadAgricola.Prioridad.ALTA
                        if tipo in {'siembra', 'cosecha'}
                        else ActividadAgricola.Prioridad.MEDIA
                    ),
                )
            )

    def positive_int(name, maximum):
        try:
            value = int(request.POST.get(name, ''))
        except ValueError:
            return None
        return value if 1 <= value <= maximum else None

    label = f' de {crop}' if crop else ''
    if request.POST.get('incluir_preparacion') and sowing:
        days = positive_int('dias_preparacion', 120)
        if days is None:
            messages.error(request, 'Indica cuántos días antes de la siembra se prepara el suelo (1 a 120).')
            return redirect('actividad_list')
        add('preparacion', f'Preparación de suelo{label}', sowing - timedelta(days=days))
    if request.POST.get('incluir_siembra') and sowing:
        add('siembra', f'Siembra{label}', sowing)
    if request.POST.get('incluir_monitoreo') and sowing and harvest:
        every = positive_int('frecuencia_monitoreo', 60)
        if every is None:
            messages.error(request, 'Indica cada cuántos días quieres monitorear (1 a 60).')
            return redirect('actividad_list')
        day = sowing + timedelta(days=every)
        count = 0
        while day < harvest and count < 40:
            add('monitoreo', f'Monitoreo{label}', day, 'Registrar plagas, enfermedades, malezas y estado del cultivo.')
            day += timedelta(days=every)
            count += 1
    if request.POST.get('incluir_cosecha') and harvest:
        add('cosecha', f'Cosecha{label}', harvest, 'Confirmar madurez y fin de carencia antes de cosechar.')
    if not sowing and not harvest:
        messages.error(
            request,
            'El potrero no tiene fechas de siembra ni de cosecha registradas. Regístralas en Predios o en el Calendario.',
        )
        return redirect('actividad_list')
    for activity in planned:
        activity.full_clean()
        activity.save()
    messages.success(
        request,
        (
            f'Se programaron {len(planned)} actividad(es) para «{plot.nombre}».'
            if planned
            else 'No se agregaron actividades: ya existían en esas fechas o no marcaste ninguna.'
        ),
    )
    return redirect(f"{reverse('actividad_list')}?potrero={plot.pk}")


@login_required
def predios(request):
    predios_list = (
        Predio.objects.filter(propietario=request.user)
        .annotate(
            potreros_count=Count('potreros'),
        )
        .prefetch_related('potreros__cultivo', 'potreros__temporadas__cultivo')
    )
    selected_predio_id = request.GET.get('predio', '')
    if selected_predio_id:
        selected_predio = get_object_or_404(
            Predio,
            pk=selected_predio_id,
            propietario=request.user,
        )
    else:
        selected_predio = None
    selected_potrero_id = request.GET.get('potrero', '').strip()
    if selected_potrero_id:
        selected_potrero = get_object_or_404(
            Potrero.objects.select_related('predio'),
            pk=selected_potrero_id if selected_potrero_id.isdigit() else 0,
            predio__propietario=request.user,
        )
        selected_predio = selected_potrero.predio
        selected_potrero_id = str(selected_potrero.pk)

    potrero_queryset = Potrero.objects.filter(
        predio__propietario=request.user,
    ).select_related('predio', 'cultivo')
    search_term = request.GET.get('q', '').strip()
    filter_predio = request.GET.get('filtro_predio', '').strip()
    filter_cultivo = request.GET.get('cultivo', '').strip()
    sort_by = request.GET.get('orden', '-creado')
    if search_term:
        potrero_queryset = potrero_queryset.filter(nombre__icontains=search_term)
    if filter_predio:
        potrero_queryset = potrero_queryset.filter(predio_id=filter_predio)
    if filter_cultivo:
        potrero_queryset = potrero_queryset.filter(cultivo_id=filter_cultivo)
    ordering = {
        'creado': 'creado',
        '-creado': '-creado',
        'actualizado': 'actualizado',
        '-actualizado': '-actualizado',
        'nombre': 'nombre',
        '-nombre': '-nombre',
    }
    potrero_queryset = potrero_queryset.order_by(ordering.get(sort_by, '-creado'), 'pk')
    history_page = Paginator(potrero_queryset, 25).get_page(request.GET.get('pagina'))
    crop_options = (
        Cultivo.objects.filter(
            potreros__predio__propietario=request.user,
        )
        .distinct()
        .order_by('nombre')
    )
    return _page(
        request,
        'predios.html',
        'predios',
        {
            'predios_list': predios_list,
            'farm_cards': farm_cards(request.user, predios_list),
            'selected_predio': selected_predio,
            'selected_predio_id': str(selected_predio.pk) if selected_predio else '',
            'selected_potrero_id': selected_potrero_id,
            'potrero_history_page': history_page,
            'crop_options': crop_options,
            'terrain_filters': {
                'q': search_term,
                'predio': filter_predio,
                'cultivo': filter_cultivo,
                'orden': sort_by,
            },
            'maps_config': map_config(),
        },
    )


@login_required
def predio_detail(request, pk):
    predio = get_object_or_404(
        Predio.objects.prefetch_related(
            'potreros__cultivo',
            'potreros__temporadas__cultivo',
            'historial__usuario',
        ),
        pk=pk,
        propietario=request.user,
    )
    potreros = predio.potreros.all()
    selected_potrero_id = request.GET.get('potrero', '').strip()
    selected_potrero = None
    historial = predio.historial.select_related('usuario', 'potrero')
    if selected_potrero_id:
        selected_potrero = get_object_or_404(
            potreros,
            pk=selected_potrero_id,
        )
        historial = historial.filter(potrero=selected_potrero)
    superficie_utilizada = potreros.aggregate(total=Sum('superficie_ha'))['total'] or Decimal('0')
    # El asistente «¿Cómo deseas organizar tu predio?» se muestra mientras el predio no tenga potreros (recién
    # registrado o con la decisión pendiente) y desaparece en cuanto existe el primero.
    needs_setup = not potreros
    can_use_whole = bool(predio.geojson) and needs_setup
    farm_card = farm_cards(request.user, [predio])[0]
    return _page(
        request,
        'predio_detail.html',
        'predios',
        {
            'journey': None,  # la ficha ya trae su propio «Siguiente paso» por predio
            'farm_card': farm_card,
            'next_step': None if needs_setup else farm_card['ui']['next_step'],
            'needs_setup': needs_setup,
            'can_use_whole': can_use_whole,
            'predio': predio,
            'potreros': potreros,
            'historial': historial,
            'selected_potrero': selected_potrero,
            'superficie_utilizada': superficie_utilizada,
            'superficie_restante': max(Decimal('0'), predio.superficie_ha - superficie_utilizada),
            'cultivos': Cultivo.objects.filter(potreros__predio=predio).distinct(),
            'maps_config': map_config(),
        },
    )


def _without_geometry(farm_data):
    """Copia del resumen de predios sin trazados ni coordenadas.

    El formulario de predio no necesita (ni debe recibir) las geometrías de otros predios o potreros:
    la del predio en edición llega por el propio formulario.
    """
    return {
        **farm_data,
        'predios': [{**farm, 'geojson': None, 'lat': None, 'lng': None} for farm in farm_data['predios']],
        'plots': [
            {**plot, 'geojson': None, 'coordinates': None, 'lat': None, 'lng': None} for plot in farm_data['plots']
        ],
    }


# Ventana en la que un segundo envío idéntico se considera el mismo registro enviado dos veces (doble clic, toque
# repetido, botón «Atrás» + reenviar). Pasado ese tiempo se trata como un registro nuevo.
RESUBMIT_WINDOW = timedelta(minutes=2)


def _same_json(left, right):
    """Compara dos geometrías ya guardadas o recibidas, sin importar el orden de las claves."""
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _recent_predio_duplicate(user, cleaned):
    """Predio que esta misma persona acaba de registrar con los mismos datos y el mismo trazado."""
    candidates = Predio.objects.filter(
        propietario=user,
        nombre=cleaned.get('nombre'),
        superficie_ha=cleaned.get('superficie_ha'),
        creado__gte=timezone.now() - RESUBMIT_WINDOW,
    ).order_by('-creado', '-pk')
    for candidate in candidates:
        if _same_json(candidate.geojson, cleaned.get('geojson')):
            return candidate
    return None


@login_required
@transaction.atomic
def predio_form(request, pk=None):
    instance = get_object_or_404(Predio, pk=pk, propietario=request.user) if pk else Predio(propietario=request.user)
    original = None
    if instance.pk:
        original = {
            'nombre': instance.nombre,
            'descripcion': instance.descripcion,
            'ubicacion': instance.ubicacion,
            'observaciones': instance.observaciones,
            'zona_id': instance.zona_id,
            'zona': instance.zona.nombre if instance.zona_id else 'Sin zona',
            'superficie_ha': instance.superficie_ha,
            'geojson': instance.geojson,
        }
    form = PredioForm(request.POST or None, instance=instance)
    if request.method == 'POST' and form.is_valid():
        geometry = form.cleaned_data.get('geojson')
        if not original and not geometry:
            form.add_error('geojson', 'Dibuja y acepta el perímetro para registrar el predio.')
        elif not original and not form.cleaned_data.get('perimetro_aceptado'):
            form.add_error('perimetro_aceptado', 'Acepta el perímetro antes de registrar el predio.')
        elif original and geometry != original['geojson'] and not form.cleaned_data.get('perimetro_aceptado'):
            form.add_error('perimetro_aceptado', 'Acepta el nuevo perímetro antes de guardar.')
        if form.errors:
            pass
        else:
            if original is None:
                # Se serializan los registros de la misma persona: dos envíos simultáneos esperan su turno y el
                # segundo reconoce que el primero ya creó el predio, en vez de crear otro y repetir el aviso.
                get_user_model().objects.select_for_update().get(pk=request.user.pk)
                repeated = _recent_predio_duplicate(request.user, form.cleaned_data)
                if repeated is not None:
                    return redirect(f"{reverse('predio_detail', args=[repeated.pk])}?nuevo=1")
            predio = form.save(commit=False)
            predio.propietario = request.user
            predio.save()
            if original is None:
                PredioHistorial.objects.create(
                    predio=predio,
                    usuario=request.user,
                    accion=PredioHistorial.Accion.CREADO,
                    resumen=f'Predio creado con una superficie de {predio.superficie_ha} ha.',
                    geometria_nueva=predio.geojson,
                    superficie_nueva_ha=predio.superficie_ha,
                )
            else:
                geometry_changed = predio.geojson != original['geojson']
                area_changed = predio.superficie_ha != original['superficie_ha']
                metadata_changed = any(
                    getattr(predio, field) != original[field]
                    for field in ('nombre', 'descripcion', 'ubicacion', 'observaciones', 'zona_id')
                )
                metadata_labels = {
                    'nombre': 'Nombre',
                    'descripcion': 'Descripción',
                    'ubicacion': 'Ubicación',
                    'observaciones': 'Observaciones',
                    'zona_id': 'Zona',
                }
                metadata_changes = [
                    f'{metadata_labels[field]}: '
                    f'{original["zona"] if field == "zona_id" else (original[field] or "Sin dato")} → '
                    f'{(predio.zona.nombre if predio.zona_id else "Sin zona") if field == "zona_id" else (getattr(predio, field) or "Sin dato")}'
                    for field in metadata_labels
                    if getattr(predio, field) != original[field]
                ]
                if geometry_changed:
                    PredioHistorial.objects.create(
                        predio=predio,
                        usuario=request.user,
                        accion=PredioHistorial.Accion.PERIMETRO,
                        resumen=(
                            f'Perímetro actualizado; superficie: {original["superficie_ha"]} ha '
                            f'→ {predio.superficie_ha} ha.'
                        ),
                        geometria_anterior=original['geojson'],
                        geometria_nueva=predio.geojson,
                        superficie_anterior_ha=original['superficie_ha'],
                        superficie_nueva_ha=predio.superficie_ha,
                    )
                elif area_changed:
                    PredioHistorial.objects.create(
                        predio=predio,
                        usuario=request.user,
                        accion=PredioHistorial.Accion.SUPERFICIE,
                        resumen=(
                            f'Superficie actualizada de {original["superficie_ha"]} ha ' f'a {predio.superficie_ha} ha.'
                        ),
                        superficie_anterior_ha=original['superficie_ha'],
                        superficie_nueva_ha=predio.superficie_ha,
                    )
                if metadata_changed:
                    PredioHistorial.objects.create(
                        predio=predio,
                        usuario=request.user,
                        accion=PredioHistorial.Accion.ACTUALIZADO,
                        resumen='Se actualizaron los datos del predio: ' + '; '.join(metadata_changes) + '.',
                        geometria_anterior=original['geojson'] if geometry_changed else None,
                        geometria_nueva=predio.geojson if geometry_changed else None,
                        superficie_anterior_ha=original['superficie_ha'] if area_changed else None,
                        superficie_nueva_ha=predio.superficie_ha if area_changed else None,
                    )
            if original is None:
                # Al registrar no se manda ningún aviso: la ficha del predio nuevo ya es la confirmación y trae
                # la pregunta siguiente («¿Cómo deseas organizar tu predio?»).
                return redirect(f"{reverse('predio_detail', args=[predio.pk])}?nuevo=1")
            messages.success(request, 'El predio quedó guardado correctamente.')
            return redirect('predio_detail', pk=predio.pk)
    return _page(
        request,
        'terreno_form.html',
        'predios',
        {
            'journey': None,  # en un formulario se trabaja un solo paso: sin la ruta general
            'form': form,
            'heading': 'Editar predio' if pk else 'Registrar predio',
            'is_predio': True,
            'maps_config': map_config(),
            'farm_data': _without_geometry(farm_payload(request.user)),
            'registration_open': bool(request.method == 'POST' and form.errors),
        },
    )


@login_required
@require_POST
@transaction.atomic
def predio_usar_completo(request, pk):
    """Crea un único potrero con la misma geometría del predio, sin pedir dibujar otra vez."""
    predio = get_object_or_404(Predio.objects.select_for_update(), pk=pk, propietario=request.user)
    if predio.potreros.exists():
        # Segundo envío del mismo botón (doble clic): el primero ya creó el potrero, se continúa hacia él sin
        # repetir ningún aviso. Solo si el predio ya tenía potreros de antes se explica por qué no se creó otro.
        reciente = (
            predio.potreros.filter(creado__gte=timezone.now() - RESUBMIT_WINDOW).order_by('-creado', '-pk').first()
        )
        if reciente is not None and predio.potreros.count() == 1:
            return redirect('potrero_edit', predio_pk=predio.pk, pk=reciente.pk)
        messages.info(request, 'Este predio ya tiene potreros. Puedes agregar más desde esta ficha.')
        return redirect('predio_detail', pk=predio.pk)
    if not predio.geojson:
        messages.error(
            request, 'Este predio no tiene un perímetro dibujado. Usa «Dividir mi predio» para registrar sus potreros.'
        )
        return redirect('predio_detail', pk=predio.pk)
    name = ' '.join((request.POST.get('nombre') or '').split())[:150] or 'Potrero principal'
    potrero = Potrero(
        predio=predio,
        nombre=name,
        superficie_ha=predio.superficie_ha,
        latitud=predio.latitud,
        longitud=predio.longitud,
        perimetro=[list(point) for point in coordinates_from_geojson(predio.geojson)],
    )
    try:
        potrero.full_clean()
    except ValidationError as exc:
        messages.error(request, 'No se pudo crear el potrero: ' + ' '.join(exc.messages))
        return redirect('predio_detail', pk=predio.pk)
    potrero.save()
    PredioHistorial.objects.create(
        predio=predio,
        potrero=potrero,
        usuario=request.user,
        accion=PredioHistorial.Accion.POTRERO_CREADO,
        resumen=f'Se creó el potrero «{potrero.nombre}» usando todo el predio ({potrero.superficie_ha} ha).',
        geometria_nueva=potrero.geojson,
        superficie_nueva_ha=potrero.superficie_ha,
    )
    messages.success(request, f'Listo: «{potrero.nombre}» usa todo tu predio. Ahora indica qué cultivas aquí.')
    return redirect('potrero_edit', predio_pk=predio.pk, pk=potrero.pk)


def _recent_potrero_duplicate(predio, data):
    """Potrero de este predio recién creado con el mismo nombre y el mismo trazado (o la misma superficie)."""
    nombre = ' '.join((data.get('nombre') or '').split())
    if not nombre:
        return None
    try:
        perimetro = json.loads(data.get('perimetro') or '[]')
    except ValueError:
        perimetro = None
    try:
        superficie = Decimal(str(data.get('superficie_ha') or '').replace(',', '.'))
    except InvalidOperation:
        superficie = None
    candidates = predio.potreros.filter(
        nombre__iexact=nombre, creado__gte=timezone.now() - RESUBMIT_WINDOW
    ).order_by('-creado', '-pk')
    for candidate in candidates:
        if perimetro:
            if _same_json(candidate.perimetro, perimetro):
                return candidate
        elif not candidate.perimetro and superficie is not None and candidate.superficie_ha == superficie:
            return candidate
    return None


@login_required
@transaction.atomic
def potrero_form(request, predio_pk, pk=None):
    predio = get_object_or_404(Predio.objects.select_for_update(), pk=predio_pk, propietario=request.user)
    instance = get_object_or_404(Potrero, pk=pk, predio=predio) if pk else Potrero(predio=predio)
    used_area_query = predio.potreros.all()
    if instance.pk:
        used_area_query = used_area_query.exclude(pk=instance.pk)
    used_area = used_area_query.aggregate(total=Sum('superficie_ha'))['total'] or Decimal('0')
    available_area = max(Decimal('0'), predio.superficie_ha - used_area)
    original = None
    if instance.pk:
        original = {
            'nombre': instance.nombre,
            'superficie_ha': instance.superficie_ha,
            'geojson': instance.geojson,
            'cultivo': instance.cultivo.nombre if instance.cultivo_id else 'Sin cultivo',
            'variedad': instance.variedad,
            'fecha_siembra': instance.fecha_siembra,
            'fecha_cosecha': instance.fecha_cosecha,
            'latitud': instance.latitud,
            'longitud': instance.longitud,
        }
    if request.method == 'POST' and not instance.pk:
        repeated = _recent_potrero_duplicate(predio, request.POST)
        if repeated is not None:
            # El mismo formulario llegó dos veces (doble clic): el primer envío ya creó el potrero. No se crea otro
            # ni se repite el aviso; se continúa a la ficha del predio.
            return redirect('predio_detail', pk=predio.pk)
    form = PotreroForm(request.POST or None, instance=instance)
    min_area = Decimal('0.0001')
    # Sin hectáreas libres no se pone «max»: un máximo menor que el mínimo hace que el navegador rechace todo en silencio.
    sin_espacio = available_area < min_area
    if not sin_espacio:
        form.fields['superficie_ha'].widget.attrs['max'] = format(available_area, 'f')
    form.fields['superficie_ha'].help_text = (
        f'Superficie disponible en este predio: {available_area} ha. '
        'Si dibujas el perímetro, la superficie se calculará automáticamente.'
    )
    previous_crop = (
        {
            'cultivo_id': instance.cultivo_id,
            'cultivo': instance.cultivo if instance.cultivo_id else None,
            'variedad': instance.variedad,
            'fecha_siembra': instance.fecha_siembra,
            'fecha_cosecha': instance.fecha_cosecha,
        }
        if instance.pk
        else None
    )
    if request.method == 'POST' and form.is_valid():
        potrero = form.save()
        record_form_crop_change(potrero, previous_crop)
        if original is None:
            PredioHistorial.objects.create(
                predio=predio,
                potrero=potrero,
                usuario=request.user,
                accion=PredioHistorial.Accion.POTRERO_CREADO,
                resumen=f'Se creó el potrero «{potrero.nombre}» con {potrero.superficie_ha} ha.',
                geometria_nueva=potrero.geojson,
                superficie_nueva_ha=potrero.superficie_ha,
            )
        else:
            current = {
                'nombre': potrero.nombre,
                'superficie_ha': potrero.superficie_ha,
                'geojson': potrero.geojson,
                'cultivo': potrero.cultivo.nombre if potrero.cultivo_id else 'Sin cultivo',
                'variedad': potrero.variedad,
                'fecha_siembra': potrero.fecha_siembra,
                'fecha_cosecha': potrero.fecha_cosecha,
                'latitud': potrero.latitud,
                'longitud': potrero.longitud,
            }
            labels = {
                'nombre': 'Nombre',
                'superficie_ha': 'Superficie (ha)',
                'cultivo': 'Cultivo',
                'variedad': 'Variedad',
                'fecha_siembra': 'Fecha de siembra',
                'fecha_cosecha': 'Fecha de cosecha',
                'latitud': 'Latitud',
                'longitud': 'Longitud',
            }
            changes = [
                f'{labels[field]}: {original[field] or "Sin dato"} → {current[field] or "Sin dato"}'
                for field in labels
                if original[field] != current[field]
            ]
            geometry_changed = original['geojson'] != current['geojson']
            if geometry_changed:
                changes.append('Perímetro actualizado')
            if changes:
                PredioHistorial.objects.create(
                    predio=predio,
                    potrero=potrero,
                    usuario=request.user,
                    accion=PredioHistorial.Accion.POTRERO_ACTUALIZADO,
                    resumen=f'Se modificó el potrero «{original["nombre"]}»: ' + '; '.join(changes) + '.',
                    geometria_anterior=original['geojson'] if geometry_changed else None,
                    geometria_nueva=current['geojson'] if geometry_changed else None,
                    superficie_anterior_ha=original['superficie_ha'],
                    superficie_nueva_ha=current['superficie_ha'],
                )
        messages.success(
            request,
            (
                'Potrero, cultivo y ubicación guardados. Ya aparece en Siembra.'
                if original is None
                else 'Potrero, cultivo y ubicación guardados en la base de datos.'
            ),
        )
        return redirect('predio_detail', pk=predio.pk)
    return _page(
        request,
        'terreno_form.html',
        'predios',
        {
            'journey': None,  # en un formulario se trabaja un solo paso: sin la ruta general
            'form': form,
            'heading': 'Editar potrero' if pk else 'Registrar potrero',
            'predio': predio,
            'is_potrero': True,
            'maps_config': map_config(),
            'potrero_superficie_disponible': available_area,
            'potrero_sin_espacio': sin_espacio and not instance.pk,
        },
    )


@login_required
@transaction.atomic
def temporada_form(request, predio_pk, potrero_pk, pk=None):
    predio = get_object_or_404(
        Predio.objects.select_for_update(),
        pk=predio_pk,
        propietario=request.user,
    )
    potrero = get_object_or_404(
        Potrero.objects.select_for_update().select_related('cultivo'),
        pk=potrero_pk,
        predio=predio,
    )
    instance = (
        get_object_or_404(
            TemporadaCultivo.objects.select_for_update(),
            pk=pk,
            potrero=potrero,
        )
        if pk
        else TemporadaCultivo(potrero=potrero)
    )
    form = TemporadaCultivoForm(request.POST or None, instance=instance)
    if request.method == 'POST' and form.is_valid():
        season = form.save(commit=False)
        season.potrero = potrero
        season.save()
        messages.success(
            request,
            'La temporada quedó guardada. Las temporadas anteriores se conservan.',
        )
        return redirect('predio_detail', pk=predio.pk)
    return _page(
        request,
        'temporada_form.html',
        'predios',
        {
            'journey': None,
            'form': form,
            'predio': predio,
            'potrero': potrero,
            'heading': 'Editar temporada' if pk else 'Registrar nueva temporada',
        },
    )


def _decimal_input(value, label, allow_zero=False):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'{label} debe ser un número válido.')
    if not number.is_finite() or number > Decimal('1e18') or number < 0 or (not allow_zero and number == 0):
        condition = '0 o mayor' if allow_zero else 'mayor a 0'
        raise ValueError(f'{label} debe ser {condition}.')
    return number


@login_required
@require_POST
def calculate(request):
    if request.content_type != 'application/json':
        return JsonResponse({'error': 'Envía los datos como JSON.'}, status=415)
    if len(request.body) > 65536:
        return JsonResponse({'error': 'El cálculo supera el tamaño permitido.'}, status=413)
    try:
        payload = json.loads(request.body)
        if not isinstance(payload, dict):
            raise ValueError('Los datos del cálculo deben ser un objeto JSON.')
        operation = payload.get('operation')
        if not isinstance(operation, str):
            raise ValueError('El tipo de cálculo no está disponible.')
        calculation_type = payload.get('calculation_type')
        if calculation_type is not None and (
            not isinstance(calculation_type, str)
            or calculation_type
            not in {
                'semillas',
                'insumos',
                'proteccion',
                'coverage',
            }
        ):
            raise ValueError('La categoría del cálculo no está disponible.')
        area = _decimal_input(payload.get('area'), 'La superficie')
        plot_id = payload.get('plot_id')
        if not str(plot_id or '').isdigit():
            raise ValueError('Selecciona un potrero registrado.')
        plot = (
            Potrero.objects.select_related('cultivo', 'predio__zona')
            .filter(predio__propietario=request.user, pk=plot_id)
            .first()
        )
        if plot is None:
            raise ValueError('El potrero no existe o no pertenece a tu cuenta.')
        if area > plot.superficie_ha:
            raise ValueError(f'La superficie no puede superar {plot.superficie_ha} ha registradas.')
        result = {}
        if operation in {'dose', 'coverage'}:
            crop_name = str(payload.get('crop') or '').strip()
            if not crop_name and plot.cultivo_id:
                crop_name = plot.cultivo.nombre
            crop = Cultivo.objects.filter(nombre=crop_name, activo=True).first() if crop_name else None
            if crop_name and crop is None:
                raise ValueError('Selecciona un cultivo activo del catálogo.')
            product = None
            if payload.get('product_id'):
                if not str(payload['product_id']).isdigit():
                    raise ValueError('Selecciona un producto del catálogo.')
                product = (
                    Insumo.objects.select_related('tipo', 'cultivo')
                    .filter(
                        vigente=True,
                        pk=payload['product_id'],
                    )
                    .filter(
                        Q(vigencia_desde__isnull=True) | Q(vigencia_desde__lte=timezone.localdate()),
                        Q(vigencia_hasta__isnull=True) | Q(vigencia_hasta__gte=timezone.localdate()),
                    )
                    .first()
                )
                if product is None:
                    raise ValueError('El producto no existe o no está vigente.')
                if product.estado_verificacion in {'restricted', 'cancelled'}:
                    raise ValueError(
                        'El producto está restringido o tiene su registro cancelado; no se puede calcular.'
                    )
                if product.cultivo_id and (crop is None or product.cultivo.nombre != crop.nombre):
                    raise ValueError('El producto del catálogo no corresponde al cultivo seleccionado.')
                product_category = category_key(product.tipo.nombre)
                if calculation_type == 'semillas' and product_category != 'seed':
                    raise ValueError('Selecciona una semilla: el cálculo de semillas no admite otros insumos.')
                if calculation_type == 'insumos' and (product_category == 'seed' or product.es_fitosanitario):
                    raise ValueError(
                        'Las semillas y los productos fitosanitarios tienen su propio cálculo; '
                        'selecciona un fertilizante u otro insumo.'
                    )
            if product and product.es_fitosanitario:
                target = payload.get('target')
                if (
                    not product.uso_verificado
                    or crop is None
                    or crop_name != product.cultivo.nombre
                    or crop_name not in product.cultivos_autorizados
                    or target not in product.objetivos_control
                ):
                    raise ValueError(
                        'El producto fitosanitario no tiene un uso verificado para ese cultivo y objetivo.'
                    )
                dose = product.dosis_hectarea
                unit = product.unidad_dosis
            else:
                dose = _decimal_input(payload.get('dose'), 'La dosis')
                unit = str(payload.get('unit') or '').strip()
            if product and product.es_fitosanitario and not unit:
                raise ValueError('El producto fitosanitario no tiene una unidad de dosis verificada.')
            if product and product.unidad_dosis and unit:
                registered_unit = product.unidad_dosis.removesuffix('/ha').casefold()
                submitted_unit = unit.removesuffix('/ha').casefold()
                if registered_unit != submitted_unit:
                    raise ValueError('La unidad ingresada no coincide con la unidad registrada del producto.')
            # Dosis máxima documentada: superarla no impide calcular, pero se advierte con claridad.
            above_max = bool(
                product
                and product.dosis_maxima_hectarea is not None
                and product.dosis_con_fuente
                and dose > product.dosis_maxima_hectarea
            )
            # Origen del dato, en cuatro niveles:
            #   documented → coincide con un valor del catálogo que tiene fuente verificada;
            #   catalog    → coincide con un valor del catálogo sin respaldo completo;
            #   adjusted   → la persona cambió un valor que el catálogo sí tenía;
            #   simulation → no había valor en el catálogo y se ingresó a mano.
            same_unit = lambda a, b: a.removesuffix('/ha').casefold() == b.removesuffix('/ha').casefold()
            catalog_reference = None  # (valor, unidad, ¿con respaldo?, fuente, url, fecha)
            if product and product.dosis_hectarea is not None and product.unidad_dosis:
                catalog_reference = (
                    product.dosis_hectarea,
                    product.unidad_dosis,
                    product.dosis_con_fuente,
                    product.dosis_fuente or product.fuente_nombre,
                    product.etiqueta_url or product.fuente_url,
                    product.fecha_verificacion.isoformat() if product.fecha_verificacion else '',
                )
            elif not product and crop and calculation_type == 'semillas' and crop.densidad_siembra is not None:
                backed = bool(crop.densidad_fuente and crop.densidad_url and crop.densidad_verificada)
                catalog_reference = (
                    crop.densidad_siembra,
                    (crop.unidad_siembra or 'kg') + '/ha',
                    backed,
                    crop.densidad_fuente,
                    crop.densidad_url,
                    crop.densidad_verificada.isoformat() if crop.densidad_verificada else '',
                )
            origin = 'simulation'
            provenance = {'verified': False, 'source': '', 'source_url': '', 'source_date': ''}
            if catalog_reference:
                if dose == catalog_reference[0] and same_unit(unit, catalog_reference[1]):
                    origin = 'documented' if catalog_reference[2] else 'catalog'
                    if catalog_reference[2]:
                        provenance = {
                            'verified': True,
                            'source': catalog_reference[3],
                            'source_url': catalog_reference[4],
                            'source_date': catalog_reference[5],
                        }
                else:
                    origin = 'adjusted'
            max_reference = (
                (float(product.dosis_maxima_hectarea), product.unidad_dosis_maxima or product.unidad_dosis)
                if product and product.dosis_maxima_hectarea is not None and product.dosis_con_fuente
                else None
            )
            provenance.update(
                {
                    'origin': origin,
                    'origin_label': ORIGIN_LABELS[origin],
                    'adjusted': origin == 'adjusted',
                    'catalog_dose': float(catalog_reference[0]) if origin == 'adjusted' else None,
                    'catalog_unit': catalog_reference[1] if origin == 'adjusted' else '',
                    'above_max': above_max,
                    'max_dose': max_reference[0] if above_max else None,
                    'max_unit': max_reference[1] if above_max else '',
                    'simulation': origin != 'documented',
                    'product': product.nombre if product else '',
                    'crop': crop.nombre if crop else '',
                    'plot': plot.nombre,
                    'area': float(area),
                }
            )
            if operation == 'dose':
                amount = area * dose
                result = {
                    'amount': float(amount),
                    'unit': unit.replace('/ha', ''),
                    'dose': float(dose),
                    'dose_unit': unit,
                    **provenance,
                }
                if (
                    product
                    and product.presentacion_cantidad
                    and product.presentacion_unidad
                    and same_unit(product.presentacion_unidad, unit)
                ):
                    result['package'] = {
                        'size': float(product.presentacion_cantidad),
                        'unit': product.presentacion_unidad,
                        'count': math.ceil(amount / product.presentacion_cantidad),
                    }
                # Saco que la persona elige o escribe (no es un producto del catálogo): sacos, sobrante y costo.
                if payload.get('bag_kg') not in (None, ''):
                    if result['unit'].casefold() != 'kg':
                        raise ValueError('Los sacos solo se calculan cuando la dosis está en kg/ha.')
                    bag = _decimal_input(payload.get('bag_kg'), 'El tamaño del saco')
                    if bag > 1000:
                        raise ValueError('El tamaño del saco no puede superar 1.000 kg.')
                    bags = math.ceil(amount / bag)
                    user_bag = {
                        'size': float(bag),
                        'unit': 'kg',
                        'count': bags,
                        'leftover': float(bag * bags - amount),
                        'source': 'ingresado por la persona',
                    }
                    if payload.get('bag_price_clp') not in (None, ''):
                        price = _decimal_input(payload.get('bag_price_clp'), 'El precio por saco', allow_zero=True)
                        user_bag['price_clp'] = float(price)
                        user_bag['total_cost_clp'] = float(price * bags)
                    result['user_bag'] = user_bag
            else:
                stock = _decimal_input(payload.get('stock'), 'La cantidad disponible', allow_zero=True)
                needed = area * dose
                covered_area = min(area, stock / dose)
                result = {
                    'dose': float(dose),
                    'dose_unit': unit,
                    'needed': float(needed),
                    'coveredHa': float(covered_area),
                    'percent': float(covered_area / area * 100),
                    'missingHa': float(max(Decimal('0'), area - covered_area)),
                    'missingQty': float(max(Decimal('0'), needed - stock)),
                    'surplus': float(max(Decimal('0'), stock - needed)),
                    'stock': float(stock),
                    'unit': unit.replace('/ha', ''),
                    'inventory': 'simulation',
                    **provenance,
                }
        elif operation == 'seed_population':
            plants = _decimal_input(payload.get('plants_m2'), 'Las plantas objetivo por m²')
            weight = _decimal_input(payload.get('thousand_seed_weight_g'), 'El peso de mil semillas')
            germination = _decimal_input(payload.get('germination_pct'), 'La germinación')
            purity = _decimal_input(payload.get('purity_pct'), 'La pureza')
            if germination > 100 or purity > 100:
                raise ValueError('Germinación y pureza deben ser porcentajes de hasta 100.')
            source = str(payload.get('source') or '').strip()
            if not source:
                raise ValueError('Indica la fuente de los parámetros de semilla.')
            kg_per_ha = plants * weight / (Decimal('100') * (germination / 100) * (purity / 100))
            result = {
                'rate': float(kg_per_ha),
                'amount': float(kg_per_ha * area),
                'unit': 'kg',
                'dose_unit': 'kg/ha',
                'source': source,
            }
        elif operation == 'fertilizer_nutrition':
            product_id = payload.get('product_id')
            if not str(product_id or '').isdigit():
                raise ValueError('Selecciona un fertilizante del catálogo.')
            product = (
                Insumo.objects.select_related('tipo', 'cultivo')
                .filter(
                    pk=product_id,
                    vigente=True,
                )
                .filter(
                    Q(vigencia_desde__isnull=True) | Q(vigencia_desde__lte=timezone.localdate()),
                    Q(vigencia_hasta__isnull=True) | Q(vigencia_hasta__gte=timezone.localdate()),
                )
                .first()
            )
            if (
                product is None
                or product.tipo.nombre.casefold() not in {'fertilizante', 'fertilizer'}
                or product.estado_verificacion != 'verified'
                or not product.fuente_nombre
                or not product.fuente_url
                or not product.fecha_verificacion
                or not product.composicion_nutricional
                or product.unidad_dosis != 'kg/ha'
            ):
                raise ValueError('La composición o unidad del fertilizante no está verificada con una fuente.')
            if product.cultivo_id and payload.get('crop') != product.cultivo.nombre:
                raise ValueError('El fertilizante no corresponde al cultivo seleccionado.')
            nutrient_fields = {
                'N': product.nitrogeno_pct,
                'P2O5': product.fosforo_p2o5_pct,
                'K2O': product.potasio_k2o_pct,
            }
            nutrient = payload.get('nutrient')
            if not isinstance(nutrient, str):
                raise ValueError('Selecciona un nutriente válido.')
            percentage = nutrient_fields.get(nutrient)
            if percentage is None or percentage <= 0:
                raise ValueError('El fertilizante no tiene composición verificada para este nutriente.')
            need_per_ha = _decimal_input(payload.get('need_per_ha'), 'La necesidad nutricional')
            need_source = str(payload.get('need_source') or '').strip()
            if not need_source:
                raise ValueError('Indica la fuente documentada de la necesidad nutricional.')
            product_per_ha = need_per_ha / (percentage / 100)
            amount = area * product_per_ha
            nutrient_applied = amount * (percentage / 100)
            result = {
                'amount': float(amount),
                'unit': product.unidad_dosis.replace('/ha', '') or 'kg',
                'dose': float(product_per_ha),
                'dose_unit': 'kg de producto/ha',
                'nutrient': nutrient,
                'nutrientNeedPerHa': float(need_per_ha),
                'nutrientPct': float(percentage),
                'nutrientApplied': float(nutrient_applied),
                'nutrientSource': need_source,
                'product': product.nombre,
                'productSource': product.fuente_nombre,
            }
        else:
            raise ValueError('El tipo de cálculo no está disponible.')
        expected_types = {
            'dose': {'semillas', 'insumos', 'proteccion'},
            'coverage': {'coverage', 'proteccion'},
            'seed_population': {'semillas'},
            'fertilizer_nutrition': {'insumos'},
        }
        default_types = {
            'dose': 'insumos',
            'coverage': 'coverage',
            'seed_population': 'semillas',
            'fertilizer_nutrition': 'insumos',
        }
        calculation_type = calculation_type or default_types[operation]
        if calculation_type not in expected_types[operation]:
            raise ValueError('La categoría no corresponde al tipo de cálculo.')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'El cuerpo JSON no se pudo interpretar.'}, status=400)
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    input_fields = (
        'crop',
        'product_id',
        'target',
        'dose',
        'unit',
        'stock',
        'plants_m2',
        'thousand_seed_weight_g',
        'germination_pct',
        'purity_pct',
        'source',
        'nutrient',
        'need_per_ha',
        'need_source',
        'calculation_type',
        'bag_kg',
        'bag_price_clp',
    )
    stored_input = {
        'operation': operation,
        'area_ha': str(area),
        'plot_id': plot.pk,
    }
    stored_input.update(
        {
            key: str(value)
            for key in input_fields
            for value in [payload.get(key)]
            if isinstance(value, (str, int, float, bool))
        }
    )
    if payload.get('preview') is True:
        # Vista previa en pantalla: se calcula con la misma lógica pero NO se guarda en el historial.
        return JsonResponse({'ok': True, 'calculation_id': None, 'preview': True, **result})
    calculation = CalculoAgricola.objects.create(
        usuario=request.user,
        potrero=plot,
        operacion=operation,
        entrada=stored_input,
        resultado=result,
    )
    return JsonResponse({'ok': True, 'calculation_id': calculation.pk, **result})


def decisiones(request):
    tool = request.GET.get('herramienta', 'llevar')
    if tool not in {'llevar', 'alcanza'}:
        tool = 'llevar'
    return _page(request, 'decisiones.html', 'decisiones', {'initial_tool': tool})


def calendar_reference_payload(reference):
    return {
        'id': reference.pk,
        'cropId': reference.cultivo_id,
        'crop': reference.cultivo.nombre,
        'zoneId': reference.zona_id,
        'zone': reference.zona.nombre,
        'referenceYear': reference.anio_referencia,
        'variety': reference.variedad,
        'scientificName': reference.nombre_cientifico,
        'botanicalFamily': reference.familia_botanica,
        'cropType': reference.tipo_cultivo,
        'sowingConditions': reference.condicion_siembra,
        'requirements': reference.requerimientos,
        'sowingDepth': reference.profundidad_siembra,
        'phenologicalStages': reference.etapas_fenologicas,
        'growthConditions': reference.condiciones_crecimiento,
        'management': reference.manejo_general,
        'maturityIndicators': reference.indicadores_madurez,
        'harvestConsiderations': reference.consideraciones_cosecha,
        'pests': reference.plagas,
        'diseases': reference.enfermedades,
        'weeds': reference.malezas,
        'integratedManagement': reference.manejo_integrado,
        'sowingStartMonth': reference.siembra_inicio_mes,
        'sowingStartDay': reference.siembra_inicio_dia,
        'sowingEndMonth': reference.siembra_fin_mes,
        'sowingEndDay': reference.siembra_fin_dia,
        'harvestStartMonth': reference.cosecha_inicio_mes,
        'harvestStartDay': reference.cosecha_inicio_dia,
        'harvestEndMonth': reference.cosecha_fin_mes,
        'harvestEndDay': reference.cosecha_fin_dia,
        'cycleDays': reference.duracion_ciclo_dias,
        'yieldTonHa': (float(reference.rendimiento_ton_ha) if reference.rendimiento_ton_ha is not None else None),
        'status': reference.estado,
        'sourceInstitution': reference.institucion_fuente,
        'sourceDocument': reference.documento_fuente,
        'sourceUrl': reference.url_fuente,
        'sourceDate': (reference.fecha_consulta_fuente.isoformat() if reference.fecha_consulta_fuente else ''),
        'sourceVerified': reference.tiene_fuente_verificada,
    }


ACTIVITY_ICONS = {
    'preparacion': 'shovel',
    'siembra': 'sprout',
    'fertilizacion': 'flask-conical',
    'riego': 'droplets',
    'monitoreo': 'scan-search',
    'control_fitosanitario': 'shield-alert',
    'cosecha': 'wheat',
}


def _days_text(days):
    return f'{days} día' if days == 1 else f'{days} días'


def _sowing_next_steps(user, farms, today):
    """Próximas labores del agricultor y qué le falta completar en sus potreros."""
    pending = list(
        ActividadAgricola.objects.filter(
            usuario=user,
            estado=ActividadAgricola.Estado.PENDIENTE,
            potrero__predio__propietario=user,
        )
        .select_related('potrero', 'potrero__predio')
        .order_by('fecha_programada', 'pk')
    )
    upcoming = []
    for activity in pending[:6]:
        days = (activity.fecha_programada - today).days
        if days < 0:
            label, tone = f'Atrasada {_days_text(-days)}', 'late'
        elif days == 0:
            label, tone = 'Hoy', 'today'
        elif days == 1:
            label, tone = 'Mañana', 'soon'
        else:
            label, tone = f'En {_days_text(days)}', 'soon' if days <= 7 else 'later'
        upcoming.append(
            {
                'activity': activity,
                'label': label,
                'tone': tone,
                'icon': ACTIVITY_ICONS.get(activity.tipo, 'clipboard-list'),
            }
        )
    overdue = sum(1 for activity in pending if activity.fecha_programada < today)
    sowing_planned = {activity.potrero_id for activity in pending if activity.tipo == ActividadAgricola.Tipo.SIEMBRA}

    tasks = []
    for farm in farms:
        plots = list(farm.potreros.all())
        if not plots:
            tasks.append(
                {
                    'icon': 'layout-grid',
                    'tone': 'info',
                    'text': f'Agrega el primer potrero de «{farm.nombre}»',
                    'hint': 'Sin potreros no se puede planificar la siembra.',
                    'url': reverse('potrero_create', kwargs={'predio_pk': farm.pk}),
                    'cta': 'Agregar potrero',
                }
            )
            continue
        for plot in plots:
            edit_url = reverse('potrero_edit', kwargs={'predio_pk': farm.pk, 'pk': plot.pk})
            if not plot.cultivo_id:
                tasks.append(
                    {
                        'icon': 'sprout',
                        'tone': 'warn',
                        'text': f'Asigna un cultivo a «{plot.nombre}»',
                        'hint': f'{farm.nombre} · {number_format(plot.superficie_ha, decimal_pos=2)} ha',
                        'url': edit_url,
                        'cta': 'Asignar cultivo',
                    }
                )
            elif not plot.fecha_siembra:
                tasks.append(
                    {
                        'icon': 'calendar-plus',
                        'tone': 'warn',
                        'text': f'Registra la fecha de siembra de «{plot.nombre}»',
                        'hint': f'{plot.cultivo.nombre} · {farm.nombre}',
                        'url': edit_url,
                        'cta': 'Registrar fecha',
                    }
                )
            elif not plot.fecha_cosecha:
                tasks.append(
                    {
                        'icon': 'wheat',
                        'tone': 'info',
                        'text': f'Agrega la cosecha prevista de «{plot.nombre}»',
                        'hint': f'{plot.cultivo.nombre} · siembra {plot.fecha_siembra:%d/%m/%Y}',
                        'url': edit_url,
                        'cta': 'Agregar fecha',
                    }
                )
            if plot.fecha_siembra and plot.fecha_siembra >= today and plot.pk not in sowing_planned:
                query = urlencode({'potrero': plot.pk, 'tipo': 'siembra', 'fecha': plot.fecha_siembra.isoformat()})
                tasks.append(
                    {
                        'icon': 'clipboard-list',
                        'tone': 'action',
                        'text': f'Programa la labor de siembra de «{plot.nombre}»',
                        'hint': f'Siembra prevista el {plot.fecha_siembra:%d/%m/%Y}',
                        'url': reverse('actividad_create') + '?' + query,
                        'cta': 'Programar labor',
                    }
                )
    return {
        'upcoming': upcoming,
        'pending_total': len(pending),
        'overdue': overdue,
        'tasks': tasks[:8],
        'tasks_total': len(tasks),
    }


def _sowing_timeline(farms, year, today):
    """Barras siembra -> cosecha de los potreros del usuario para un año (posiciones en %)."""
    import calendar as month_calendar

    def position(day):
        day = min(max(day, date(year, 1, 1)), date(year, 12, 31))
        days_in_month = month_calendar.monthrange(year, day.month)[1]
        return (day.month - 1 + (day.day - 1) / days_in_month) / 12 * 100

    rows, undated, outside = [], 0, 0
    for farm in farms:
        for plot in farm.potreros.all():
            sow, harvest = plot.fecha_siembra, plot.fecha_cosecha
            if not (sow or harvest):
                undated += 1
                continue
            start, end = sow or harvest, harvest or sow
            if end < date(year, 1, 1) or start > date(year, 12, 31):
                outside += 1
                continue
            left = position(start)
            right = position(end) + (100 / 12 / 30 if end.year == year else 0)
            width = max(right - left, 1.6)
            if sow and harvest:
                kind = 'range'
            elif sow:
                kind = 'sow'
            else:
                kind = 'harvest'
            rows.append(
                {
                    'farm': farm,
                    'plot': plot,
                    'kind': kind,
                    'start': start,
                    'end': end,
                    'left': f'{left:.2f}',
                    'width': f'{min(width, 100 - left):.2f}',
                    'cut_left': start.year < year,
                    'cut_right': end.year > year,
                    'crop': plot.cultivo.nombre if plot.cultivo_id else '',
                }
            )
    rows.sort(key=lambda row: (row['start'], row['farm'].nombre, row['plot'].nombre))
    return {
        'rows': rows,
        'undated': undated,
        'outside': outside,
        'year': year,
        'today': f'{position(today):.2f}' if today.year == year else '',
        'months': ['ENE', 'FEB', 'MAR', 'ABR', 'MAY', 'JUN', 'JUL', 'AGO', 'SEP', 'OCT', 'NOV', 'DIC'],
    }


@login_required
def calendario(request):
    zones = list(Zona.objects.filter(activo=True).order_by('nombre'))
    current_year = timezone.localdate().year
    try:
        selected_year = int(request.GET.get('year', current_year))
    except (TypeError, ValueError):
        selected_year = current_year
    if not 1900 <= selected_year <= 2100:
        selected_year = current_year

    selected_zone = request.GET.get('zone', '')
    if selected_zone and not any(str(zone.pk) == selected_zone for zone in zones):
        selected_zone = ''
    selected_crop = request.GET.get('crop', '')
    if selected_crop and not (
        selected_crop.isdigit() and Cultivo.objects.filter(pk=selected_crop, activo=True).exists()
    ):
        selected_crop = ''
    selected_variety = request.GET.get('variety', '*').strip()[:150]

    # Se envían todas las referencias vigentes (todas las zonas, cultivos y años): el año,
    # la zona y el cultivo se filtran en el navegador sin recargar la página.
    references = list(
        ReferenciaAgronomica.objects.filter(
            cultivo__activo=True,
            zona__activo=True,
        )
        .exclude(
            estado=ReferenciaAgronomica.Estado.RETIRADA,
        )
        .select_related('cultivo', 'zona')
        .order_by(
            'cultivo__nombre',
            'zona__nombre',
            'variedad',
            'pk',
        )
    )
    if not selected_zone and zones:
        # Sin zona en la URL se abre la del primer predio del usuario (si la tiene) y, si no,
        # la zona con más ventanas registradas (a igualdad, la primera por nombre).
        own_zone = (
            Predio.objects.filter(
                propietario=request.user,
                zona__in=zones,
            )
            .values_list('zona_id', flat=True)
            .first()
        )
        preferred_zone = default_zone()
        if own_zone:
            selected_zone = str(own_zone)
        elif preferred_zone and preferred_zone in zones:
            selected_zone = str(preferred_zone.pk)  # Melipilla, la zona de trabajo por defecto
        else:
            windows = Counter(reference.zona_id for reference in references)
            selected_zone = str(max(zones, key=lambda zone: windows[zone.pk]).pk)
    # Registro de siembra: todos los predios del usuario con sus potreros, siempre leídos de la
    # base de datos, para que cada predio/potrero recién creado aparezca aquí sin pasos extra.
    sowing_farms = list(
        Predio.objects.filter(propietario=request.user)
        .select_related('zona')
        .prefetch_related(
            Prefetch(
                'potreros',
                queryset=Potrero.objects.select_related('cultivo')
                .prefetch_related('temporadas__cultivo')
                .order_by('nombre', 'pk'),
            )
        )
        .order_by('nombre', 'pk')
    )
    try:
        timeline_year = int(request.GET.get('anio', current_year))
    except (TypeError, ValueError):
        timeline_year = current_year
    if not 2000 <= timeline_year <= 2100:
        timeline_year = current_year
    timeline = _sowing_timeline(sowing_farms, timeline_year, timezone.localdate())
    next_steps = _sowing_next_steps(request.user, sowing_farms, timezone.localdate())
    years = {current_year, current_year + 1, selected_year}
    years.update(reference.anio_referencia for reference in references if reference.anio_referencia)
    return _page(
        request,
        'calendario.html',
        'calendario',
        {
            'calendar_references': [calendar_reference_payload(reference) for reference in references],
            'calendar_crops': Cultivo.objects.filter(activo=True).order_by('nombre'),
            'calendar_zones': zones,
            'calendar_years': sorted(years),
            'calendar_current_year': selected_year,
            'calendar_default_zone': int(selected_zone) if selected_zone else '',
            'calendar_selected_crop': selected_crop,
            'calendar_selected_variety': selected_variety or '*',
            'sowing_farms': sowing_farms,
            'farm_cards': farm_cards(request.user, sowing_farms),
            'timeline': timeline,
            'next_steps': next_steps,
            'timeline_prev': timeline_year - 1,
            'timeline_next': timeline_year + 1,
            'sowing_plot_total': sum(len(farm.potreros.all()) for farm in sowing_farms),
            'sowing_area_total': sum((farm.superficie_ha for farm in sowing_farms), Decimal('0')),
            'sowing_planted_total': sum(
                1 for farm in sowing_farms for plot in farm.potreros.all() if plot.cultivo_id and plot.fecha_siembra
            ),
            'calendar_has_unverified': any(not reference.tiene_fuente_verificada for reference in references),
        },
    )


@login_required
@require_POST
def evaluate_calendar_date(request):
    if request.content_type != 'application/json':
        return JsonResponse({'error': 'Envía los datos como JSON.'}, status=415)
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'El cuerpo JSON no se pudo interpretar.'}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({'error': 'Los datos deben ser un objeto JSON.'}, status=400)

    evaluation = payload.get('evaluation')
    if evaluation not in {'sowing', 'harvest'}:
        return JsonResponse({'error': 'Selecciona una evaluación válida.'}, status=400)
    crop_id = payload.get('crop_id')
    zone_id = payload.get('zone_id')
    if not str(crop_id or '').isdigit() or not str(zone_id or '').isdigit():
        return JsonResponse({'error': 'Selecciona un cultivo y una zona.'}, status=400)
    if not Cultivo.objects.filter(pk=crop_id, activo=True).exists():
        return JsonResponse({'error': 'El cultivo seleccionado no está disponible.'}, status=400)
    if not Zona.objects.filter(pk=zone_id, activo=True).exists():
        return JsonResponse({'error': 'La zona seleccionada no está disponible.'}, status=400)
    try:
        proposal = date.fromisoformat(str(payload.get('proposed_date', '')))
        sowing_date = date.fromisoformat(str(payload.get('sowing_date', ''))) if evaluation == 'harvest' else None
    except ValueError:
        return JsonResponse({'error': 'Ingresa fechas válidas.'}, status=400)
    if evaluation == 'harvest' and proposal < sowing_date:
        return JsonResponse(
            {'error': 'La fecha de cosecha no puede ser anterior a la fecha de siembra.'},
            status=400,
        )
    variety = str(payload.get('variety') or '').strip()
    if len(variety) > 150:
        return JsonResponse({'error': 'La variedad supera la longitud permitida.'}, status=400)

    reference = find_reference(
        crop_id,
        zone_id,
        variety,
        year=(sowing_date or proposal).year,
    )
    configuration = ConfiguracionAgronomica.objects.get_or_create(pk=1)[0]
    if evaluation == 'sowing':
        result = evaluate_sowing_date(
            proposal,
            reference,
            configuration.margen_ventana_dias,
        )
    else:
        result = evaluate_harvest_date(
            sowing_date,
            proposal,
            reference,
            configuration.margen_ventana_dias,
        )
    result.update(
        {
            'proposed_date': proposal.isoformat(),
            'sowing_date': sowing_date.isoformat() if sowing_date else '',
            'reference_context': (
                {
                    'crop': reference.cultivo.nombre,
                    'zone': reference.zona.nombre,
                    'variety': reference.variedad or 'Referencia general del cultivo',
                    'year': reference.anio_referencia,
                }
                if reference and reference.tiene_fuente_verificada
                else None
            ),
            'margin_days': configuration.margen_ventana_dias,
            'source': (
                {
                    'institution': reference.institucion_fuente,
                    'document': reference.documento_fuente,
                    'url': reference.url_fuente,
                    'consulted_at': reference.fecha_consulta_fuente.isoformat(),
                }
                if reference and reference.tiene_fuente_verificada
                else None
            ),
            'factors_not_evaluated': [
                'Tipo y condición del suelo',
                'Pronóstico y variabilidad climática local',
                'Manejo y estado fenológico observados en terreno',
            ],
            'disclaimer': (
                'Evaluación orientativa basada en una ventana referencial; no garantiza '
                'la viabilidad de siembra ni la madurez real del cultivo.'
            ),
        }
    )
    return JsonResponse(result)


@login_required
def busqueda(request):
    """Buscar: una sola búsqueda en el servidor (también atiende al buscador de la barra superior).

    Reutiliza el catálogo y los predios del usuario que la página ya necesita (``catalog_payload`` y
    ``farm_payload``), así que no suma consultas y nunca muestra predios de otras cuentas.
    """
    catalog = catalog_payload()
    farm_data = farm_payload(request.user)
    context = run_search(catalog, farm_data, request.GET)
    context.update(catalog=catalog, farm_data=farm_data)
    return _page(request, 'busqueda.html', 'busqueda', context)


REPORT_TYPES = [
    ('resumen', 'Resumen del predio', 'Superficie, potreros, cultivos actuales y estado de las labores.'),
    ('cuaderno', 'Cuaderno de campo', 'Labores realizadas con producto, dosis, superficie y responsable.'),
    ('cultivos', 'Historial de cultivos', 'Temporadas por potrero: cultivo, variedad, fechas y estado.'),
    ('calculos', 'Cálculos guardados', 'Semillas, insumos y disponibilidad.'),
]


def _fmt_number(value, digits=2):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ''
    text = f'{number:,.{digits}f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    return text.rstrip('0').rstrip(',') if ',' in text else text


def describe_calculation(calculation):
    """Resumen legible de un cálculo guardado (entrada y resultado JSON)."""
    data, entry = calculation.resultado or {}, calculation.entrada or {}
    operation = calculation.operacion
    detail, value = '', ''
    if operation == 'dose':
        value = f"{_fmt_number(data.get('amount'))} {data.get('unit', '')}"
        detail = (
            f"{_fmt_number(data.get('dose'), 3)} {data.get('dose_unit', '')} × {_fmt_number(entry.get('area_ha'))} ha"
        )
    elif operation == 'coverage':
        value = f"{_fmt_number(data.get('percent'), 0)} % cubierto"
        detail = f"Necesario {_fmt_number(data.get('needed'))} {data.get('unit', '')} · disponible {_fmt_number(data.get('stock', entry.get('stock')))} {data.get('unit', '')}"
    elif operation == 'seed_population':
        value = f"{_fmt_number(data.get('amount'))} kg"
        detail = f"{_fmt_number(data.get('rate'))} kg/ha por población objetivo"
    elif operation == 'fertilizer_nutrition':
        value = f"{_fmt_number(data.get('amount'))} {data.get('unit', 'kg')}"
        detail = f"{data.get('nutrient', '')}: {_fmt_number(data.get('nutrientNeedPerHa'))} kg/ha"
    elif operation == 'yield':
        value = f"{_fmt_number(data.get('production'))} ton"
        detail = f"{_fmt_number(data.get('rate'))} ton/ha × {_fmt_number(entry.get('area_ha'))} ha"
    source = data.get('source') or data.get('nutrientSource') or ''
    kind = data.get('origin')
    if kind == 'adjusted' or (kind is None and data.get('adjusted')):
        origin = f"Ajustado manualmente (catálogo: {_fmt_number(data.get('catalog_dose'), 3)} {data.get('catalog_unit', '')}) · simulación"
    elif kind == 'catalog':
        origin = 'Dato de catálogo (sin respaldo completo)'
    elif kind == 'documented':
        origin = f'Documentado: {source}' if source else 'Documentado'
    elif data.get('simulation'):
        origin = 'Simulación (dato sin fuente verificada)'
    elif data.get('verified') or data.get('source_verified'):
        origin = f'Documentado: {source}' if source else 'Documentado'
    else:
        origin = source or 'Dato ingresado por el usuario'
    return {
        'date': timezone.localtime(calculation.creado),
        'farm': calculation.potrero.predio.nombre,
        'plot': calculation.potrero.nombre,
        'operation': calculation.get_operacion_display(),
        'crop': entry.get('crop', ''),
        'product': data.get('product', '') or '',
        'value': value.strip(),
        'detail': detail.strip(),
        'origin': origin,
    }


@login_required
def reportes(request):
    today = timezone.localdate()
    report_type = request.GET.get('tipo', 'resumen')
    if report_type not in {key for key, _label, _help in REPORT_TYPES}:
        report_type = 'resumen'
    farms = Predio.objects.filter(propietario=request.user).order_by('nombre')
    plots_all = (
        Potrero.objects.filter(predio__propietario=request.user)
        .select_related('predio')
        .order_by('predio__nombre', 'nombre')
    )
    filters = {
        'predio': request.GET.get('predio', ''),
        'potrero': request.GET.get('potrero', ''),
        'desde': request.GET.get('desde', ''),
        'hasta': request.GET.get('hasta', ''),
        'incluir_pendientes': request.GET.get('incluir_pendientes') == '1',
    }
    start = end = None
    try:
        start = date.fromisoformat(filters['desde']) if filters['desde'] else None
    except ValueError:
        filters['desde'] = ''
    try:
        end = date.fromisoformat(filters['hasta']) if filters['hasta'] else None
    except ValueError:
        filters['hasta'] = ''

    plots = plots_all.select_related('cultivo').prefetch_related('temporadas__cultivo')
    if filters['predio'].isdigit():
        plots = plots.filter(predio_id=filters['predio'])
    if filters['potrero'].isdigit():
        plots = plots.filter(pk=filters['potrero'])
    plot_ids = list(plots.values_list('pk', flat=True))
    selected_farms = (
        farms.filter(pk__in=plots.values('predio_id')) if (filters['predio'] or filters['potrero']) else farms
    )

    context = {
        'report_types': REPORT_TYPES,
        'report_type': report_type,
        'report_title': dict((key, label) for key, label, _help in REPORT_TYPES)[report_type],
        'report_filters': filters,
        'report_farms': farms,
        'report_plots': plots_all,
        'generated_at': timezone.localtime(),
        'owner_name': request.user.get_full_name() or request.user.username,
    }

    columns, rows = [], []
    if report_type == 'resumen':
        farm_rows = []
        for farm in selected_farms:
            farm_plots = [plot for plot in plots if plot.predio_id == farm.pk]
            used = sum((plot.superficie_ha for plot in farm_plots), Decimal('0'))
            plot_rows = []
            for plot in farm_plots:
                summary = plot_season_summary(plot)
                plot_rows.append(
                    {
                        'plot': plot,
                        'status': summary['status'],
                        'season': summary['season'],
                    }
                )
                rows.append(
                    [
                        farm.nombre,
                        plot.nombre,
                        _fmt_number(plot.superficie_ha, 4),
                        plot.cultivo.nombre if plot.cultivo_id else 'Sin cultivo',
                        plot.variedad,
                        plot.fecha_siembra.strftime('%d/%m/%Y') if plot.fecha_siembra else '',
                        plot.fecha_cosecha.strftime('%d/%m/%Y') if plot.fecha_cosecha else '',
                        summary['status'],
                    ]
                )
            farm_rows.append(
                {
                    'farm': farm,
                    'plots': plot_rows,
                    'used': used,
                    'free': max(Decimal('0'), farm.superficie_ha - used),
                    'use_pct': (used / farm.superficie_ha * 100) if farm.superficie_ha else 0,
                }
            )
        activities = ActividadAgricola.objects.filter(usuario=request.user, potrero_id__in=plot_ids)
        context.update(
            {
                'farm_rows': farm_rows,
                'summary_kpis': {
                    'predios': len(farm_rows),
                    'superficie': sum((row['farm'].superficie_ha for row in farm_rows), Decimal('0')),
                    'potreros': len(plot_ids),
                    'con_cultivo': plots.exclude(cultivo=None).count(),
                    'pendientes': activities.filter(estado=ActividadAgricola.Estado.PENDIENTE).count(),
                    'vencidas': activities.filter(
                        estado=ActividadAgricola.Estado.PENDIENTE, fecha_programada__lt=today
                    ).count(),
                    'completadas': activities.filter(estado=ActividadAgricola.Estado.COMPLETADA).count(),
                },
                'recent_calculations': [
                    describe_calculation(item)
                    for item in CalculoAgricola.objects.filter(
                        usuario=request.user,
                        potrero_id__in=plot_ids,
                    )
                    .select_related('potrero__predio')
                    .order_by('-creado', '-pk')[:5]
                ],
            }
        )
        columns = [
            'Predio',
            'Potrero',
            'Superficie (ha)',
            'Cultivo',
            'Variedad',
            'Siembra',
            'Cosecha prevista',
            'Estado',
        ]
    elif report_type == 'cuaderno':
        activities = ActividadAgricola.objects.filter(
            usuario=request.user,
            potrero_id__in=plot_ids,
        ).select_related('potrero__predio', 'insumo__tipo', 'temporada__cultivo', 'potrero__cultivo')
        if not filters['incluir_pendientes']:
            activities = activities.filter(estado=ActividadAgricola.Estado.COMPLETADA)
        else:
            activities = activities.exclude(estado=ActividadAgricola.Estado.CANCELADA)
        date_field = 'fecha_realizada' if not filters['incluir_pendientes'] else 'fecha_programada'
        if start:
            activities = activities.filter(**{f'{date_field}__gte': start})
        if end:
            activities = activities.filter(**{f'{date_field}__lte': end})
        activities = list(activities.order_by(date_field, 'pk'))
        context['field_log'] = activities
        context['field_log_phyto'] = sum(
            1 for item in activities if item.tipo == ActividadAgricola.Tipo.CONTROL_FITOSANITARIO
        )
        context['field_log_conflicts'] = sum(1 for item in activities if item.conflicto_carencia)
        columns = [
            'Fecha',
            'Predio',
            'Potrero',
            'Cultivo',
            'Labor',
            'Producto',
            'Objetivo',
            'Dosis',
            'Unidad',
            'Superficie (ha)',
            'Cantidad total',
            'Responsable',
            'Condiciones',
            'Fin de carencia',
            'Estado',
        ]
        for item in activities:
            when = item.fecha_realizada or item.fecha_programada
            crop = (
                item.temporada.cultivo.nombre
                if item.temporada_id
                else (item.potrero.cultivo.nombre if item.potrero.cultivo_id else '')
            )
            rows.append(
                [
                    when.strftime('%d/%m/%Y'),
                    item.potrero.predio.nombre,
                    item.potrero.nombre,
                    crop,
                    item.get_tipo_display(),
                    item.producto_nombre,
                    item.objetivo,
                    _fmt_number(item.dosis, 3) if item.dosis else '',
                    item.unidad_dosis,
                    _fmt_number(item.superficie_ha, 4) if item.superficie_ha else '',
                    f'{_fmt_number(item.cantidad_total)} {item.unidad_total}' if item.cantidad_total else '',
                    item.responsable,
                    item.condiciones_clima,
                    item.fecha_fin_carencia.strftime('%d/%m/%Y') if item.fecha_fin_carencia else '',
                    item.get_estado_display(),
                ]
            )
    elif report_type == 'cultivos':
        seasons = TemporadaCultivo.objects.filter(potrero_id__in=plot_ids).select_related('potrero__predio', 'cultivo')
        if start:
            seasons = seasons.filter(
                Q(fecha_siembra__gte=start) | Q(fecha_siembra__isnull=True, creado__date__gte=start)
            )
        if end:
            seasons = seasons.filter(Q(fecha_siembra__lte=end) | Q(fecha_siembra__isnull=True, creado__date__lte=end))
        seasons = list(seasons.order_by('potrero__predio__nombre', 'potrero__nombre', '-creado'))
        for season in seasons:
            season.duration = (
                (season.fecha_cosecha - season.fecha_siembra).days
                if season.fecha_siembra and season.fecha_cosecha
                else None
            )
        context['seasons'] = seasons
        columns = [
            'Predio',
            'Potrero',
            'Temporada',
            'Cultivo',
            'Variedad',
            'Siembra',
            'Cosecha',
            'Días',
            'Estado',
            'Observaciones',
        ]
        for season in seasons:
            rows.append(
                [
                    season.potrero.predio.nombre,
                    season.potrero.nombre,
                    season.temporada_agricola,
                    season.cultivo.nombre,
                    season.variedad,
                    season.fecha_siembra.strftime('%d/%m/%Y') if season.fecha_siembra else '',
                    season.fecha_cosecha.strftime('%d/%m/%Y') if season.fecha_cosecha else '',
                    season.duration if season.duration is not None else '',
                    season.get_estado_display(),
                    season.observaciones,
                ]
            )
    else:
        calculations = (
            CalculoAgricola.objects.filter(
                usuario=request.user,
                potrero_id__in=plot_ids,
            )
            .select_related('potrero__predio')
            .order_by('-creado', '-pk')
        )
        if start:
            calculations = calculations.filter(creado__date__gte=start)
        if end:
            calculations = calculations.filter(creado__date__lte=end)
        described = [describe_calculation(item) for item in calculations[:300]]
        context['calculations'] = described
        columns = [
            'Fecha',
            'Predio',
            'Potrero',
            'Operación',
            'Cultivo',
            'Producto',
            'Resultado',
            'Detalle',
            'Origen del dato',
        ]
        for item in described:
            rows.append(
                [
                    item['date'].strftime('%d/%m/%Y %H:%M'),
                    item['farm'],
                    item['plot'],
                    item['operation'],
                    item['crop'],
                    item['product'],
                    item['value'],
                    item['detail'],
                    item['origin'],
                ]
            )

    if request.GET.get('formato') == 'csv':
        import csv
        from django.http import HttpResponse

        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="agroasesor-{report_type}-{today:%Y%m%d}.csv"'
        response.write('﻿')
        writer = csv.writer(response, delimiter=';')
        writer.writerow(columns)
        writer.writerows(rows)
        return response

    # Compatibilidad: historial de cálculos del usuario para la exportación anterior.
    calculations = (
        CalculoAgricola.objects.filter(
            usuario=request.user,
            potrero__predio__propietario=request.user,
        )
        .select_related('potrero', 'potrero__predio')
        .order_by('-creado', '-pk')[:100]
    )
    context['report_records'] = [
        {
            'id': calculation.pk,
            'operation': calculation.operacion,
            'operationLabel': calculation.get_operacion_display(),
            'category': calculation.entrada.get('calculation_type', calculation.operacion),
            'created': timezone.localtime(calculation.creado).strftime('%d/%m/%Y %H:%M'),
            'plot': calculation.potrero.nombre,
            'farm': calculation.potrero.predio.nombre,
            'input': calculation.entrada,
            'result': calculation.resultado,
        }
        for calculation in calculations
    ]
    query = request.GET.copy()
    query['formato'] = 'csv'
    context['csv_query'] = query.urlencode()
    context['row_count'] = len(rows)
    return _page(request, 'reportes.html', 'reportes', context)


@login_required
def admin_panel(request):
    if not request.user.is_staff:
        raise PermissionDenied
    from .estadisticas import resumen_administrador

    return _page(
        request,
        'admin.html',
        'admin_panel',
        {
            'stats': resumen_administrador(),
            'base_crops_total': sum(len(names) for _group, names in CROP_GROUPS),
        },
    )


@login_required
@require_POST
def cultivos_base_cargar(request):
    """Acción explícita del administrador: carga los cultivos base que falten."""
    if not request.user.is_staff:
        raise PermissionDenied
    created = ensure_base_crops()
    if created:
        messages.success(request, f'Se cargaron {created} cultivos base. Ya aparecen al registrar potreros y cultivos.')
    else:
        messages.info(request, 'Los cultivos base ya estaban cargados; no se cambió nada.')
    return redirect('admin_panel')


def _json_body(request):
    if request.content_type != 'application/json':
        raise ValueError('Envía los datos como JSON.')
    if len(request.body) > 16384:
        raise ValueError('La solicitud supera el tamaño permitido.')
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        raise ValueError('El cuerpo JSON no se pudo interpretar.')
    if not isinstance(payload, dict):
        raise ValueError('Los datos deben ser un objeto JSON.')
    return payload


def _optional_date(value, label):
    if value in (None, ''):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f'{label} no es una fecha válida.')


def _validation_messages(exc):
    if hasattr(exc, 'message_dict'):
        return ' '.join(message for messages_list in exc.message_dict.values() for message in messages_list)
    return ' '.join(exc.messages)


def _plot_response(plot):
    plot = (
        Potrero.objects.select_related('predio__zona', 'cultivo')
        .prefetch_related(
            'temporadas__cultivo',
        )
        .get(pk=plot.pk)
    )
    data = next(item for item in farm_payload(plot.predio.propietario)['plots'] if item['id'] == str(plot.pk))
    data['history'] = [
        {
            'created': timezone.localtime(item.creado).strftime('%d/%m/%Y %H:%M'),
            'action': item.get_accion_display(),
            'summary': item.resumen,
        }
        for item in plot.historial.order_by('-creado', '-pk')[:20]
    ]
    return data


@login_required
def potrero_api(request, pk):
    plot = get_object_or_404(Potrero, pk=pk, predio__propietario=request.user)
    response = JsonResponse({'ok': True, 'plot': _plot_response(plot)})
    add_never_cache_headers(response)
    return response


@login_required
@require_POST
def potrero_assign_crop(request, pk):
    plot = get_object_or_404(
        Potrero.objects.select_related('predio', 'cultivo'),
        pk=pk,
        predio__propietario=request.user,
    )
    try:
        payload = _json_body(request)
        crop_id = payload.get('crop_id')
        if not str(crop_id or '').isdigit():
            raise ValueError('Selecciona un cultivo del catálogo.')
        crop = Cultivo.objects.filter(pk=crop_id, activo=True).first()
        if crop is None:
            raise ValueError('El cultivo seleccionado no existe o no está activo en el catálogo.')
        variety = str(payload.get('variety') or '').strip()
        if len(variety) > 150:
            raise ValueError('La variedad supera los 150 caracteres.')
        notes = str(payload.get('notes') or '').strip()
        if len(notes) > 2000:
            raise ValueError('Las observaciones superan los 2000 caracteres.')
        status = payload.get('status') or TemporadaCultivo.Estado.PLANIFICADA
        if status not in TemporadaCultivo.Estado.values:
            raise ValueError('Selecciona un estado válido.')
        sowing = _optional_date(payload.get('sowing_date'), 'La fecha de siembra')
        harvest = _optional_date(payload.get('harvest_date'), 'La fecha prevista de cosecha')
        season_label = str(payload.get('season') or '').strip()
        mode = payload.get('mode') or 'new'
        if mode not in {'new', 'update_dates'}:
            raise ValueError('La acción solicitada no está disponible.')
        if mode == 'update_dates':
            season_id = payload.get('season_id')
            season = (
                TemporadaCultivo.objects.filter(pk=season_id, potrero=plot).select_related('cultivo').first()
                if str(season_id or '').isdigit()
                else None
            )
            if season is None:
                raise ValueError('La temporada seleccionada no pertenece a este potrero.')
            if season.cultivo_id != crop.pk:
                raise ValueError(
                    'Para cambiar de cultivo registra una temporada nueva; '
                    'la temporada existente conserva su cultivo original.'
                )
            update_current_dates(plot, request.user, season, sowing_date=sowing, harvest_date=harvest, status=status)
            message = 'Fechas registradas en la temporada existente. El historial conserva el cambio.'
        else:
            assign_crop(
                plot,
                request.user,
                crop=crop,
                variety=variety,
                season_label=season_label,
                sowing_date=sowing,
                harvest_date=harvest,
                status=status,
                notes=notes,
                close_previous=bool(payload.get('close_previous', True)),
            )
            message = 'Cultivo asignado. Las temporadas anteriores se conservan en el historial.'
    except ValueError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'error': _validation_messages(exc)}, status=400)
    return JsonResponse({'ok': True, 'message': message, 'plot': _plot_response(plot)})


# ---------------------------------------------------------------------------
# Consultar productos: catálogo navegable y ficha técnica imprimible
# ---------------------------------------------------------------------------
PRODUCT_STATUS_LABELS = dict(Insumo.ESTADO_VERIFICACION)
ACTIVITY_TYPE_BY_CATEGORY = {
    'seed': 'siembra',
    'fertilizer': 'fertilizacion',
    'herbicide': 'control_fitosanitario',
    'fungicide': 'control_fitosanitario',
    'insecticide': 'control_fitosanitario',
    'other': 'fertilizacion',
}


def _product_queryset():
    return (
        Insumo.objects.select_related('tipo', 'cultivo', 'zona').prefetch_related('problemas').order_by('nombre', 'pk')
    )


def _decorate_product(product):
    product.category = category_key(product.tipo.nombre)
    product.category_label = CATEGORY_LABELS[product.category]
    product.status_label = PRODUCT_STATUS_LABELS.get(product.estado_verificacion, 'Información no verificada')
    product.is_phyto = product.es_fitosanitario
    product.is_current = product.vigente_en
    product.calculable = (
        product.is_current
        and product.estado_verificacion not in {'restricted', 'cancelled'}
        and (product.uso_verificado if product.is_phyto else product.dosis_con_fuente)
    )
    return product


@login_required
def productos(request):
    products = [_decorate_product(item) for item in _product_queryset()]
    counts = {key: 0 for key in CATEGORY_LABELS}
    for item in products:
        counts[item.category] += 1
    filters = {
        'q': request.GET.get('q', '').strip(),
        'categoria': request.GET.get('categoria', ''),
        'cultivo': request.GET.get('cultivo', ''),
        'estado': request.GET.get('estado', ''),
        'problema': request.GET.get('problema', ''),
        'solo_calculables': request.GET.get('solo_calculables') == '1',
        'orden': request.GET.get('orden', 'nombre'),
    }
    result = products
    if filters['q']:
        needle = filters['q'].casefold()
        result = [
            item
            for item in result
            if any(
                needle in (value or '').casefold()
                for value in (
                    item.nombre,
                    item.fabricante,
                    item.ingrediente_activo,
                    item.composicion_nutricional,
                    item.variedad_semilla,
                    item.numero_autorizacion_sag,
                    item.descripcion,
                )
            )
        ]
    if filters['categoria'] in CATEGORY_LABELS:
        result = [item for item in result if item.category == filters['categoria']]
    if filters['cultivo'].isdigit():
        result = [item for item in result if item.cultivo_id is None or str(item.cultivo_id) == filters['cultivo']]
    if filters['estado'] in PRODUCT_STATUS_LABELS:
        result = [item for item in result if item.estado_verificacion == filters['estado']]
    if filters['problema'].isdigit():
        result = [
            item for item in result if any(str(problem.pk) == filters['problema'] for problem in item.problemas.all())
        ]
    if filters['solo_calculables']:
        result = [item for item in result if item.calculable]
    if filters['orden'] == 'categoria':
        result.sort(key=lambda item: (item.category_label, item.nombre.casefold()))
    elif filters['orden'] == 'verificacion':
        result.sort(key=lambda item: (item.estado_verificacion != 'verified', item.nombre.casefold()))
    page = Paginator(result, 18).get_page(request.GET.get('pagina'))
    query = request.GET.copy()
    query.pop('pagina', None)
    return _page(
        request,
        'productos.html',
        'productos',
        {
            'products_page': page,
            'product_total': len(products),
            'product_count': len(result),
            'product_counts': [(key, CATEGORY_LABELS[key], counts[key]) for key in CATEGORY_LABELS],
            'product_filters': filters,
            'product_query': query.urlencode(),
            'product_crops': Cultivo.objects.filter(activo=True).order_by('nombre'),
            'product_problems': ProblemaAgricola.objects.filter(activo=True).order_by('nombre'),
            'product_statuses': Insumo.ESTADO_VERIFICACION,
            'category_labels': CATEGORY_LABELS,
        },
    )


@login_required
def producto_detail(request, pk):
    product = _decorate_product(get_object_or_404(_product_queryset(), pk=pk))
    related = [
        _decorate_product(item)
        for item in _product_queryset()
        .exclude(pk=product.pk)
        .filter(Q(cultivo=product.cultivo) if product.cultivo_id else Q(tipo=product.tipo))[:6]
    ]
    missing = []
    if product.estado_verificacion != 'verified':
        missing.append('verificación del producto con fuente, URL y fecha')
    if not product.dosis_con_fuente:
        missing.append('dosis por hectárea verificada con fuente')
    if product.is_phyto:
        if not product.numero_autorizacion_sag:
            missing.append('número de autorización SAG')
        if not product.etiqueta_url:
            missing.append('enlace a la etiqueta autorizada')
        if not product.cultivos_autorizados:
            missing.append('cultivos autorizados en etiqueta')
        if not product.objetivos_control:
            missing.append('objetivos de control')
    params = {'insumo': product.pk}
    if product.cultivo_id:
        params['crop'] = product.cultivo_id
    from urllib.parse import urlencode

    plan_params = {'tipo': ACTIVITY_TYPE_BY_CATEGORY[product.category], 'insumo': product.pk}
    if product.calculable and product.dosis_hectarea:
        plan_params.update({'dosis': product.dosis_hectarea, 'unidad': product.unidad_dosis})
    return _page(
        request,
        'producto_detail.html',
        'productos',
        {
            'product': product,
            'related_products': related,
            'missing_data': missing,
            'calc_url': reverse('decisiones')
            + '?'
            + urlencode(
                {
                    'herramienta': 'llevar',
                    'tipo': 'semillas' if product.category == 'seed' else 'insumos',
                    **params,
                }
            ),
            'availability_url': reverse('decisiones') + '?' + urlencode({'herramienta': 'alcanza', **params}),
            'plan_url': reverse('actividad_create') + '?' + urlencode(plan_params),
        },
    )
