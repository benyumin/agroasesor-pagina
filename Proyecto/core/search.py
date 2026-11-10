"""Búsqueda unificada de AgroAsesor (se resuelve en el servidor).

Una sola lógica atiende al buscador de la barra superior y a la página «Buscar»: cultivos, semillas,
insumos, problemas agrícolas y, solo del usuario autenticado, sus predios y potreros.

Trabaja sobre los mismos datos que ya arma cada página (``catalog_payload`` y ``farm_payload``), de modo
que no consulta la base dos veces y nunca ve predios de otras cuentas: ``farm_payload`` ya viene filtrado
por propietario. No inventa cifras ni resultados: cada tarjeta muestra solo lo que existe en esos datos.
"""

import unicodedata
from collections import Counter
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.urls import reverse
from django.utils.html import escape
from django.utils.safestring import mark_safe

from .catalog import CATEGORY_LABELS, CROP_GROUP_META

PAGE_SIZE = 12
PREVIEW_SIZE = 4  # resultados por categoría en la pestaña «Todos»
MAX_QUERY_LENGTH = 80

# Pestañas: clave, nombre visible, ícono, nombre de los resultados en singular y plural.
TABS = [
    ('todos', 'Todos', 'layout-grid'),
    ('cultivos', 'Cultivos', 'sprout'),
    ('semillas', 'Semillas', 'wheat'),
    ('insumos', 'Insumos', 'flask-conical'),
    ('problemas', 'Problemas', 'bug'),
    ('predios', 'Mis predios', 'map-pin'),
]
TAB_KEYS = [key for key, _label, _icon in TABS]

# Qué categoría de resultado corresponde a cada «kind» y en qué sección de «Todos» se muestra.
SECTIONS = [
    ('cultivos', 'Cultivos', 'sprout'),
    ('semillas', 'Semillas', 'wheat'),
    ('insumos', 'Insumos', 'flask-conical'),
    ('problemas', 'Problemas agrícolas', 'bug'),
    ('predios', 'Mis predios y potreros', 'map-pin'),
]

KIND_META = {
    'cultivo': ('Cultivo', 'sprout', 'crop'),
    'semilla': ('Semilla', 'wheat', 'seed'),
    'insumo': ('Insumo', 'flask-conical', 'input'),
    'problema': ('Problema', 'bug', 'problem'),
    'predio': ('Predio', 'map-pin', 'farm'),
    'potrero': ('Potrero', 'layout-grid', 'plot'),
}

PROBLEM_TYPES = {
    'pest': 'Plagas',
    'disease': 'Enfermedades',
    'weed': 'Malezas',
    'nutrition': 'Problemas nutricionales',
    'other': 'Otros problemas',
}

PHYTO_CATEGORIES = {'herbicide', 'fungicide', 'insecticide'}

# Filtros que corresponden a cada pestaña (los demás no se muestran ni se aplican).
TAB_FILTERS = {
    'todos': ('cultivo',),
    'cultivos': ('grupo',),
    'semillas': ('cultivo',),
    'insumos': ('categoria', 'cultivo', 'problema', 'calculables'),
    'problemas': ('tipo', 'cultivo'),
    'predios': ('cultivo',),
}


def fold(text):
    """Minúsculas y sin tildes, conservando el largo (así se puede resaltar sobre el texto original)."""
    out = []
    for char in str(text or ''):
        base = ''.join(part for part in unicodedata.normalize('NFD', char) if not unicodedata.combining(part))
        base = base.lower()
        out.append(base if len(base) == 1 else char.lower()[:1] or char)
    return ''.join(out)


def _shorten(text, limit=150):
    text = ' '.join(str(text or '').split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(' ', 1)[0].rstrip(' ,;:.') + '…'


def highlight(text, tokens):
    """Texto escapado con las coincidencias en <mark> (sin tildes ni mayúsculas, como la búsqueda)."""
    text = str(text or '')
    folded = fold(text)
    if not tokens or len(folded) != len(text):
        return escape(text)
    marks = [False] * len(text)
    for token in tokens:
        start = folded.find(token)
        while start != -1:
            for index in range(start, start + len(token)):
                marks[index] = True
            start = folded.find(token, start + len(token))
    out, inside = [], False
    for char, marked in zip(text, marks):
        if marked and not inside:
            out.append('<mark>')
            inside = True
        elif not marked and inside:
            out.append('</mark>')
            inside = False
        out.append(escape(char))
    if inside:
        out.append('</mark>')
    return mark_safe(''.join(out))


def _number(value):
    if value is None or value == '':
        return ''
    text = f'{float(value):.3f}'.rstrip('0').rstrip('.')
    return text.replace('.', ',')


def _action(label, url, icon='arrow-right', primary=False, disabled=False, reason=''):
    return {'label': label, 'url': url, 'icon': icon, 'primary': primary, 'disabled': disabled, 'reason': reason}


def _decision_url(tool, **params):
    query = {'herramienta': 'llevar', 'tipo': tool}
    query.update({key: value for key, value in params.items() if value not in (None, '')})
    return reverse('decisiones') + '?' + urlencode(query)


def _item(kind, ident, title, subtitle='', summary='', chips=(), actions=(), extra_text='', **fields):
    label, icon, tone = KIND_META[kind]
    if fold(summary) == fold(subtitle):
        summary = ''  # no repetir la misma frase dos veces
    return {
        'kind': kind,
        'kind_label': label,
        'icon': icon,
        'tone': tone,
        'id': str(ident),
        'title': title,
        'subtitle': subtitle,
        'summary': summary,
        'chips': list(chips),
        'actions': list(actions),
        'folded_title': fold(title),
        'haystack': fold(' '.join([title, subtitle, summary, extra_text])),
        **fields,
    }


def build_items(catalog, farms):
    """Convierte el catálogo y los predios del usuario en resultados comparables, agrupados por pestaña."""
    plots = farms.get('plots', [])
    plots_by_crop = Counter(plot['cropId'] for plot in plots if plot.get('cropId'))
    crops_by_id = {item['id']: item for item in catalog.get('cultivos', [])}
    problems = catalog.get('problemas', [])
    problem_crops = {item['id']: set(item.get('cropIds', [])) for item in problems}
    insumos = [item for item in catalog.get('insumos', []) if item.get('active')]

    products_by_problem = Counter()
    for insumo in insumos:
        for problem_id in insumo.get('problemIds', []):
            products_by_problem[problem_id] += 1

    results = {key: [] for key in ('cultivos', 'semillas', 'insumos', 'problemas', 'predios')}

    for crop in catalog.get('cultivos', []):
        if crop.get('active') is False:
            continue
        crop_id = crop['id']
        group = crop.get('group') or ''
        group_key = next((meta[0] for name, meta in CROP_GROUP_META.items() if name == group), 'otros')
        has_density = crop.get('density') is not None
        chips = []
        if plots_by_crop.get(crop_id):
            count = plots_by_crop[crop_id]
            chips.append((f'En uso en {count} potrero{"s" if count != 1 else ""}', 'dark'))
        if has_density:
            chips.append(('Dosis de siembra documentada', 'green'))
        actions = [
            _action('Ver calendario', reverse('calendario') + '?' + urlencode({'crop': crop_id}), 'calendar-days', True),
            _action('Ver productos del cultivo', reverse('productos') + '?' + urlencode({'cultivo': crop_id}), 'library'),
        ]
        if has_density:
            actions.append(
                _action('Calcular semillas', _decision_url('semillas', crop=crop_id), 'calculator'),
            )
        results['cultivos'].append(
            _item(
                'cultivo',
                crop_id,
                crop['name'],
                subtitle=group,
                summary=_shorten(crop.get('description')),
                chips=chips,
                actions=actions,
                crop_ids=[crop_id],
                group=group_key,
            )
        )
        if has_density:
            source = crop.get('densitySource') or ''
            density = f'{_number(crop["density"])} {crop.get("unit") or "kg"}/ha'
            seed_chips = [('Con fuente: ' + _shorten(source, 40), 'green')] if source else [('Sin fuente indicada', 'neutral')]
            results['semillas'].append(
                _item(
                    'semilla',
                    f'cultivo-{crop_id}',
                    crop['name'],
                    subtitle=f'Dosis de siembra de referencia: {density}',
                    summary='',
                    chips=seed_chips,
                    actions=[
                        _action('Calcular semillas', _decision_url('semillas', crop=crop_id), 'calculator', True),
                        _action(
                            'Ver calendario',
                            reverse('calendario') + '?' + urlencode({'crop': crop_id}),
                            'calendar-days',
                        ),
                    ],
                    extra_text=group + ' semilla siembra',
                    crop_ids=[crop_id],
                )
            )

    for insumo in insumos:
        category = insumo.get('category') or 'other'
        is_seed = category == 'seed'
        phyto = category in PHYTO_CATEGORIES
        calculable = bool(insumo.get('usable')) and bool(insumo.get('verifiedUse') if phyto else insumo.get('doseVerified'))
        related_crops = set()
        for pid in insumo.get('problemIds', []):
            related_crops |= problem_crops.get(pid, set())
        if insumo.get('cropId'):
            related_crops.add(insumo['cropId'])
        chips = [
            ('Verificado con fuente', 'green') if insumo.get('verified') else ('Información no verificada', 'neutral'),
        ]
        if insumo.get('doseVerified') and insumo.get('dose') is not None:
            chips.append((f'Dosis: {_number(insumo["dose"])} {insumo.get("unit") or ""}'.strip(), 'dark'))
        for name in list(insumo.get('problems', []))[:2]:
            chips.append((name, 'amber'))
        subtitle = insumo.get('categoryLabel') or CATEGORY_LABELS.get(category, '')
        if insumo.get('crop'):
            subtitle += f' · {insumo["crop"]}'
        actions = [_action('Ver ficha', reverse('producto_detail', args=[insumo['id']]), 'file-text', True)]
        params = {'insumo': insumo['id'], 'crop': insumo.get('cropId')}
        if calculable:
            actions.append(
                _action(
                    'Calcular cuánto llevar',
                    _decision_url('semillas' if is_seed else 'insumos', **params),
                    'calculator',
                )
            )
            actions.append(
                _action(
                    '¿Me alcanza?',
                    reverse('decisiones') + '?' + urlencode({'herramienta': 'alcanza', **{k: v for k, v in params.items() if v}}),
                    'scale',
                )
            )
        else:
            actions.append(
                _action(
                    'Cálculo no disponible',
                    '',
                    'calculator',
                    disabled=True,
                    reason='Falta una dosis verificada con fuente para calcular con este producto.',
                )
            )
        results['semillas' if is_seed else 'insumos'].append(
            _item(
                'semilla' if is_seed else 'insumo',
                insumo['id'],
                insumo['name'],
                subtitle=subtitle,
                summary=_shorten(insumo.get('description') or insumo.get('activeIngredient')),
                chips=chips,
                actions=actions,
                extra_text=' '.join(
                    str(insumo.get(key) or '')
                    for key in ('manufacturer', 'activeIngredient', 'type', 'seedVariety', 'sagRegistration')
                )
                + ' '
                + ' '.join(insumo.get('problems', [])),
                crop_ids=sorted(related_crops),
                category=category,
                problem_ids=list(insumo.get('problemIds', [])),
                calculable=calculable,
            )
        )

    for problem in problems:
        pid = problem['id']
        related = products_by_problem.get(pid, 0)
        crop_names = [crops_by_id[cid]['name'] for cid in problem.get('cropIds', []) if cid in crops_by_id]
        chips = []
        if crop_names:
            extra = f' y {len(crop_names) - 3} más' if len(crop_names) > 3 else ''
            chips.append(('Afecta: ' + ', '.join(crop_names[:3]) + extra, 'neutral'))
        chips.append((f'{related} producto{"s" if related != 1 else ""} registrado{"s" if related != 1 else ""}', 'green' if related else 'neutral'))
        if related:
            action = _action(
                'Ver productos', reverse('productos') + '?' + urlencode({'problema': pid}), 'library', True
            )
        else:
            action = _action(
                'Sin productos registrados',
                '',
                'library',
                True,
                disabled=True,
                reason='Aún no hay productos del catálogo asociados a este problema.',
            )
        results['problemas'].append(
            _item(
                'problema',
                pid,
                problem['name'],
                subtitle=problem.get('typeLabel') or '',
                summary=_shorten(problem.get('symptoms') or problem.get('description')),
                chips=chips,
                actions=[action],
                extra_text=' '.join(crop_names),
                crop_ids=list(problem.get('cropIds', [])),
                problem_type=problem.get('type') or 'other',
            )
        )

    for farm in farms.get('predios', []):
        farm_plots = [plot for plot in plots if plot['predio'] == farm['id']]
        chips = []
        if farm.get('overdueTasks'):
            chips.append((f'{farm["overdueTasks"]} labor{"es" if farm["overdueTasks"] != 1 else ""} vencida{"s" if farm["overdueTasks"] != 1 else ""}', 'red'))
        elif farm.get('pendingTasks'):
            chips.append((f'{farm["pendingTasks"]} labor{"es" if farm["pendingTasks"] != 1 else ""} pendiente{"s" if farm["pendingTasks"] != 1 else ""}', 'amber'))
        crops = sorted({plot['crop'] for plot in farm_plots if plot.get('crop')})
        if crops:
            chips.append(('Cultivos: ' + ', '.join(crops[:3]), 'green'))
        potreros = farm.get('plotCount', len(farm_plots))
        summary = f'{_number(farm["area"])} ha · {potreros} potrero{"s" if potreros != 1 else ""}'
        results['predios'].append(
            _item(
                'predio',
                farm['id'],
                farm['name'],
                subtitle=farm.get('location') or 'Ubicación no registrada',
                summary=summary,
                chips=chips,
                actions=[
                    _action('Abrir predio', farm['detailUrl'], 'map-pin', True),
                    _action('Ver en el mapa', reverse('predios') + '?' + urlencode({'predio': farm['id']}) + '#mapa', 'map'),
                    _action('Agregar potrero', farm['potreroCreateUrl'], 'plus'),
                ],
                extra_text=' '.join(crops) + ' ' + (farm.get('zoneName') or ''),
                crop_ids=sorted({plot['cropId'] for plot in farm_plots if plot.get('cropId')}),
            )
        )
    for plot in plots:
        chips = []
        if plot.get('status'):
            chips.append((plot['status'], 'neutral'))
        if plot.get('overdueTasks'):
            chips.append((f'{plot["overdueTasks"]} vencida{"s" if plot["overdueTasks"] != 1 else ""}', 'red'))
        elif plot.get('pendingTasks'):
            chips.append((f'{plot["pendingTasks"]} pendiente{"s" if plot["pendingTasks"] != 1 else ""}', 'amber'))
        crop = plot.get('crop') or 'Sin cultivo asignado'
        if plot.get('variety') and plot.get('crop'):
            crop += f' · {plot["variety"]}'
        results['predios'].append(
            _item(
                'potrero',
                plot['id'],
                plot['name'],
                subtitle=f'{plot["farmName"]}',
                summary=f'{crop} · {_number(plot["areaHa"])} ha',
                chips=chips,
                actions=[
                    _action('Ver ficha del potrero', plot['detailUrl'], 'layout-grid', True),
                    _action('Programar labor', plot['taskUrl'], 'clipboard-plus'),
                    _action('Editar potrero', plot['editUrl'], 'pencil'),
                ],
                extra_text=crop,
                crop_ids=[plot['cropId']] if plot.get('cropId') else [],
            )
        )
    return results


def _parse(params):
    query = ' '.join(str(params.get('q', '')).split())[:MAX_QUERY_LENGTH]
    tab = params.get('tab', 'todos')
    tab = tab if tab in TAB_KEYS else 'todos'
    filters = {key: str(params.get(key, '')).strip() for key in ('cultivo', 'grupo', 'categoria', 'problema', 'tipo')}
    filters['calculables'] = '1' if params.get('calculables') == '1' else ''
    order = params.get('orden', '')
    try:
        page = max(1, int(params.get('pagina', 1)))
    except (TypeError, ValueError):
        page = 1
    return query, tab, filters, order, page


def _passes(item, tab, filters):
    """Aplica los filtros que corresponden a la pestaña y al tipo de resultado."""
    active = TAB_FILTERS[tab]
    if 'cultivo' in active and filters['cultivo'] and filters['cultivo'] not in item.get('crop_ids', []):
        return False
    if 'grupo' in active and filters['grupo'] and item['kind'] == 'cultivo' and item.get('group') != filters['grupo']:
        return False
    if item['kind'] == 'insumo':
        if 'categoria' in active and filters['categoria'] and item.get('category') != filters['categoria']:
            return False
        if 'problema' in active and filters['problema'] and filters['problema'] not in item.get('problem_ids', []):
            return False
        if 'calculables' in active and filters['calculables'] and not item.get('calculable'):
            return False
    if item['kind'] == 'problema' and 'tipo' in active and filters['tipo'] and item.get('problem_type') != filters['tipo']:
        return False
    return True


def _rank(item, tokens, query_folded):
    title = item['folded_title']
    if title == query_folded:
        return 0
    if title.startswith(query_folded):
        return 1
    words = title.replace('-', ' ').split()
    if all(any(word.startswith(token) for word in words) for token in tokens):
        return 2
    if all(token in title for token in tokens):
        return 3
    return 4


def _distance(a, b, limit):
    """Distancia de edición (inserción, borrado, sustitución y letras invertidas), con corte en ``limit``."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous2, previous = None, list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i] + [0] * len(b)
        for j, char_b in enumerate(b, 1):
            cost = 0 if char_a == char_b else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if previous2 is not None and i > 1 and j > 1 and char_a == b[j - 2] and a[i - 2] == char_b:
                current[j] = min(current[j], previous2[j - 2] + 1)  # «tirgo» → «trigo»
        if min(current) > limit:
            return limit + 1
        previous2, previous = previous, current
    return previous[-1]


def _tolerance(token):
    """Errores que se perdonan según el largo: palabras cortas no se corrigen (darían falsos positivos)."""
    if len(token) < 4:
        return 0
    return 1 if len(token) < 7 else 2


def _vocabulary(built):
    """Palabras (sin tildes) de los títulos y subtítulos de todo lo que se puede encontrar, con su frecuencia."""
    words = Counter()
    for items in built.values():
        for item in items:
            for word in (item['folded_title'] + ' ' + fold(item['subtitle'])).replace('-', ' ').split():
                word = word.strip('.,;:()«»"\'/')
                if len(word) >= 3 and not word.isdigit():
                    words[word] += 1
    return words


def suggest_word(token, vocabulary):
    """La palabra del catálogo más parecida a ``token`` (o ``None``): tolera errores habituales de escritura."""
    limit = _tolerance(token)
    if not limit:
        return None
    best = None
    for word, frequency in vocabulary.items():
        if word[0] != token[0] and limit < 2:
            continue  # en palabras cortas el error rara vez está en la primera letra
        # También se compara con el inicio de la palabra: «fertlizan» → «fertilizante».
        whole = _distance(token, word, limit)
        partial = _distance(token, word[: len(token)], limit) if len(word) > len(token) else limit + 1
        distance = min(whole, partial)
        if distance > limit:
            continue
        # A igual distancia gana la palabra completa sobre el comienzo de una más larga, y luego la más frecuente.
        key = (distance, whole > distance, -frequency, word)
        if best is None or key < best[0]:
            best = (key, word)
    return best[1] if best else None


def correct_tokens(tokens, built):
    """Corrige solo las palabras que no aparecen en ningún resultado. Devuelve ``None`` si no hay nada que corregir."""
    haystacks = [item['haystack'] for items in built.values() for item in items]
    vocabulary = None
    corrected, changed = [], False
    for token in tokens:
        if any(token in haystack for haystack in haystacks):
            corrected.append(token)
            continue
        if vocabulary is None:
            vocabulary = _vocabulary(built)
        suggestion = suggest_word(token, vocabulary)
        if not suggestion:
            return None
        corrected.append(suggestion)
        changed = True
    return corrected if changed else None


def _url(params, **changes):
    merged = {key: value for key, value in params.items() if value not in ('', None)}
    for key, value in changes.items():
        if value in ('', None):
            merged.pop(key, None)
        else:
            merged[key] = value
    return reverse('busqueda') + ('?' + urlencode(merged) if merged else '')


def run_search(catalog, farms, params):
    """Resultado completo de una búsqueda: pestañas con conteos, resultados, filtros y paginación."""
    query, tab, filters, order, page_number = _parse(params)
    tokens = fold(query).split()
    built = build_items(catalog, farms)
    # Tolerancia a errores de escritura: si una palabra no existe en ningún resultado se busca la más parecida
    # («trgo» → «trigo») y se avisa al usuario qué se buscó en realidad.
    corrected = correct_tokens(tokens, built) if tokens else None
    corrected_query = ''
    if corrected:
        tokens = corrected
        corrected_query = ' '.join(corrected)
    query_folded = ' '.join(tokens)

    def matching(items, with_filters=True):
        selected = []
        for item in items:
            if tokens and not all(token in item['haystack'] for token in tokens):
                continue
            if with_filters and not _passes(item, tab, filters):
                continue
            selected.append(item)
        return selected

    order = order if order in ('relevancia', 'nombre') else ('relevancia' if tokens else 'nombre')
    if order == 'relevancia' and not tokens:
        order = 'nombre'

    def ordered(items):
        if order == 'relevancia':
            return sorted(items, key=lambda item: (_rank(item, tokens, query_folded), item['folded_title']))
        return sorted(items, key=lambda item: (item['folded_title'], item['kind']))

    # Conteos de las pestañas: consideran la consulta y el cultivo elegido, para que cada número sea real.
    shared = {**{key: '' for key in filters}, 'cultivo': filters['cultivo']}
    tab_counts = {}
    for key in ('cultivos', 'semillas', 'insumos', 'problemas', 'predios'):
        tab_counts[key] = len([item for item in matching(built[key], False) if _passes(item, 'todos', shared)])
    # En «Todos» una semilla de cultivo ya aparece como cultivo: no se cuenta dos veces.
    tab_counts['todos'] = (
        tab_counts['cultivos']
        + tab_counts['insumos']
        + tab_counts['problemas']
        + tab_counts['predios']
        + len([item for item in matching(built['semillas'], False) if item['id'].startswith('cultivo-') is False and _passes(item, 'todos', shared)])
    )

    tabs = []
    for key, label, icon in TABS:
        tab_params = {'q': query, 'cultivo': filters['cultivo']}
        tabs.append(
            {
                'key': key,
                'label': label,
                'icon': icon,
                'count': tab_counts[key],
                'active': key == tab,
                'url': _url(tab_params, tab=key if key != 'todos' else ''),
            }
        )

    context = {
        'search_query': query,
        'search_tokens': tokens,
        'search_tab': tab,
        'search_tabs': tabs,
        'search_filters': filters,
        'search_order': order,
        'search_has_query': bool(tokens),
        'search_corrected': corrected_query,
        'search_corrected_url': _url({'q': corrected_query}) if corrected_query else '',  # en todas las categorías
        'search_active_filters': [],
        'search_clear_url': _url({'q': query}, tab=tab if tab != 'todos' else ''),
        'search_reset_url': reverse('busqueda'),
    }
    context['search_filter_fields'] = TAB_FILTERS[tab]

    active_filters = [
        key for key in TAB_FILTERS[tab] if (filters[key] and key != 'calculables') or (key == 'calculables' and filters['calculables'])
    ]
    context['search_active_filters'] = active_filters

    if tab == 'todos':
        if not tokens and not filters['cultivo']:
            context['search_mode'] = 'explore'
            context['search_explore'] = explore_data(built, catalog, farms)
            context['search_total'] = 0
            return context
        sections = []
        total = 0
        for key, label, icon in SECTIONS:
            items = ordered(
                [
                    item
                    for item in matching(built[key])
                    if not (key == 'semillas' and item['id'].startswith('cultivo-'))
                ]
            )
            total += len(items)
            if not items:
                continue
            sections.append(
                {
                    'key': key,
                    'label': label,
                    'icon': icon,
                    'total': len(items),
                    'items': decorate(items[:PREVIEW_SIZE], tokens),
                    'more_url': _url({'q': query, 'cultivo': filters['cultivo']}, tab=key) if len(items) > PREVIEW_SIZE else '',
                }
            )
        context.update(
            search_mode='sections',
            search_sections=sections,
            search_total=total,
            search_filter_options=filter_options(tab, built, catalog),
        )
        return context

    items = ordered(matching(built[tab]))
    paginator = Paginator(items, PAGE_SIZE)
    page = paginator.get_page(page_number)
    page_params = {
        'q': query,
        'tab': tab,
        **{key: value for key, value in filters.items() if key in TAB_FILTERS[tab]},
        'orden': params.get('orden') if params.get('orden') in ('relevancia', 'nombre') else '',
    }
    context.update(
        search_mode='list',
        search_total=paginator.count,
        search_page=page,
        search_items=decorate(list(page.object_list), tokens),
        search_page_query=urlencode({k: v for k, v in page_params.items() if v not in ('', None)}),
        search_filter_options=filter_options(tab, built, catalog),
        search_tab_label=dict((key, label) for key, label, _ in TABS)[tab],
    )
    return context


def decorate(items, tokens):
    """Agrega título y subtítulo resaltados (solo para los resultados que se muestran)."""
    decorated = []
    for item in items:
        copy = dict(item)
        copy['title_html'] = highlight(item['title'], tokens)
        copy['subtitle_html'] = highlight(item['subtitle'], tokens)
        copy['menu_actions'] = item['actions'][1:]
        copy['primary_action'] = item['actions'][0] if item['actions'] else None
        decorated.append(copy)
    return decorated


def filter_options(tab, built, catalog):
    """Opciones reales de cada filtro (solo se arman los que corresponden a la pestaña)."""
    options = {}
    fields = TAB_FILTERS[tab]
    if 'cultivo' in fields:
        options['cultivo'] = [
            (crop['id'], crop['name'])
            for crop in sorted(catalog.get('cultivos', []), key=lambda item: fold(item['name']))
            if crop.get('active') is not False
        ]
    if 'grupo' in fields:
        counts = Counter(item['group'] for item in built['cultivos'])
        options['grupo'] = [
            (meta[0], f'{meta[1]} ({counts[meta[0]]})') for name, meta in CROP_GROUP_META.items() if counts.get(meta[0])
        ]
    if 'categoria' in fields:
        counts = Counter(item['category'] for item in built['insumos'])
        options['categoria'] = [
            (key, f'{label} ({counts[key]})') for key, label in CATEGORY_LABELS.items() if key != 'seed' and counts.get(key)
        ]
    if 'problema' in fields:
        used = {pid for item in built['insumos'] for pid in item.get('problem_ids', [])}
        options['problema'] = [
            (problem['id'], problem['name'])
            for problem in sorted(catalog.get('problemas', []), key=lambda item: fold(item['name']))
            if problem['id'] in used
        ]
    if 'tipo' in fields:
        counts = Counter(item['problem_type'] for item in built['problemas'])
        options['tipo'] = [(key, f'{label} ({counts[key]})') for key, label in PROBLEM_TYPES.items() if counts.get(key)]
    return options


def explore_data(built, catalog, farms):
    """Exploración inicial sin consulta: accesos por categoría con conteos reales (nunca estadísticas inventadas)."""

    def tab_url(tab, **params):
        return _url({}, tab=tab, **params)

    group_counts = Counter(item['group'] for item in built['cultivos'])
    group_chips = [
        {'label': meta[1], 'count': group_counts[meta[0]], 'url': tab_url('cultivos', grupo=meta[0])}
        for meta in CROP_GROUP_META.values()
        if group_counts.get(meta[0])
    ]
    category_counts = Counter(item['category'] for item in built['insumos'])
    category_chips = [
        {'label': label, 'count': category_counts[key], 'url': tab_url('insumos', categoria=key)}
        for key, label in CATEGORY_LABELS.items()
        if key != 'seed' and category_counts.get(key)
    ]
    type_counts = Counter(item['problem_type'] for item in built['problemas'])
    problem_chips = [
        {'label': label, 'count': type_counts[key], 'url': tab_url('problemas', tipo=key)}
        for key, label in PROBLEM_TYPES.items()
        if type_counts.get(key)
    ]
    farm_count = len(farms.get('predios', []))
    plot_count = len(farms.get('plots', []))
    crops_in_use = Counter(plot['crop'] for plot in farms.get('plots', []) if plot.get('crop'))
    examples = []
    first_farm = (farms.get('predios') or [None])[0]
    if first_farm:
        examples.append(first_farm['name'])
    if built['cultivos']:
        examples.append(built['cultivos'][0]['title'])
    if built['insumos']:
        examples.append(built['insumos'][0]['title'])
    return {
        'categories': [
            {
                'key': 'cultivos',
                'icon': 'sprout',
                'title': 'Cultivos',
                'count': len(built['cultivos']),
                'text': 'Calendario de siembra, productos y semillas de cada cultivo.',
                'url': tab_url('cultivos'),
                'chips': group_chips,
            },
            {
                'key': 'semillas',
                'icon': 'wheat',
                'title': 'Semillas',
                'count': len(built['semillas']),
                'text': 'Cultivos con dosis de siembra documentada y semillas del catálogo.',
                'url': tab_url('semillas'),
                'chips': [],
            },
            {
                'key': 'insumos',
                'icon': 'flask-conical',
                'title': 'Insumos',
                'count': len(built['insumos']),
                'text': 'Fertilizantes, fitosanitarios y otros productos vigentes.',
                'url': tab_url('insumos'),
                'chips': category_chips,
            },
            {
                'key': 'problemas',
                'icon': 'bug',
                'title': 'Problemas agrícolas',
                'count': len(built['problemas']),
                'text': 'Plagas, enfermedades y malezas, con sus productos relacionados.',
                'url': tab_url('problemas'),
                'chips': problem_chips,
            },
            {
                'key': 'predios',
                'icon': 'map-pin',
                'title': 'Mis predios',
                'count': farm_count,
                'text': (
                    f'{farm_count} predio{"s" if farm_count != 1 else ""} y {plot_count} potrero{"s" if plot_count != 1 else ""} registrados.'
                    if farm_count
                    else 'Aún no registras predios. Al registrar uno podrás encontrarlo aquí.'
                ),
                'url': tab_url('predios') if farm_count else reverse('predio_create'),
                'cta': '' if farm_count else 'Registrar predio',
                'chips': [],
            },
        ],
        'crops_in_use': [
            {'name': name, 'count': count, 'url': _url({'q': name})} for name, count in sorted(crops_in_use.items())
        ],
        'examples': examples,
    }
