"""Asignación de cultivos a potreros por temporada, con trazabilidad.

El potrero conserva en sus campos ``cultivo``, ``variedad``, ``fecha_siembra`` y
``fecha_cosecha`` una *instantánea* del cultivo vigente, porque varios módulos
(dashboard, calculadoras y calendario) la leen directamente. El historial real
queda en ``TemporadaCultivo``: cada cambio de cultivo crea una temporada nueva y
las anteriores nunca se borran ni se sobrescriben.

La planificación del potrero (cuántos de sus tres pasos están hechos y cuál es la siguiente acción) se calcula
siempre sobre la *temporada vigente* —la más reciente— con ``planning_state``. Solo los potreros antiguos, que aún no
tienen ninguna temporada, usan como respaldo los datos del propio potrero. La instantánea del potrero se mantiene
sincronizada con la temporada vigente y nunca con una temporada anterior.
"""

from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Cultivo, PredioHistorial, Potrero, TemporadaCultivo

PLAN_TOTAL = 3  # pasos de la planificación: cultivo, fecha de siembra y fecha de cosecha
FINISHED_STATES = (TemporadaCultivo.Estado.COSECHADA, TemporadaCultivo.Estado.CERRADA)
DOUBLE_SUBMIT_WINDOW = timedelta(seconds=30)  # dos envíos idénticos tan seguidos son un doble clic, no dos temporadas


def agricultural_season_for(day):
    """Temporada agrícola chilena referencial: julio-junio (p. ej. 2026-2027)."""
    if day is None:
        return ''
    start = day.year if day.month >= 7 else day.year - 1
    return f'{start}-{start + 1}'


def current_season(plot):
    """Temporada más reciente del potrero, aunque esté cerrada."""
    return TemporadaCultivo.objects.filter(potrero=plot).select_related('cultivo').order_by('-creado', '-pk').first()


def vigente_season(plot):
    """Temporada vigente del potrero: la más reciente (la misma regla que ``current_season``).

    Lee ``plot.temporadas.all()``: con ``prefetch_related('temporadas')`` no hace una consulta por potrero.
    """
    if plot.pk is None:
        return None
    seasons = list(plot.temporadas.all())
    return max(seasons, key=lambda item: (item.creado, item.pk)) if seasons else None


def planning_state(plot):
    """Progreso de la planificación del potrero, calculado en un único lugar.

    Los tres pasos son: cultivo, fecha de siembra y fecha de cosecha. Se leen de la temporada vigente cuando
    corresponde al cultivo actual del potrero; si el potrero es antiguo y todavía no tiene temporadas (o su cultivo
    fue retirado), se leen de los campos del propio potrero. Sin cultivo no hay planificación que medir.
    """
    latest = vigente_season(plot)
    has_crop = bool(plot.cultivo_id)
    season = latest if latest is not None and has_crop and latest.cultivo_id == plot.cultivo_id else None
    source = season or plot
    steps = [has_crop, has_crop and bool(source.fecha_siembra), has_crop and bool(source.fecha_cosecha)]
    done = sum(steps)
    return {
        'season': season,
        'source': 'season' if season else 'plot',
        'has_crop': has_crop,
        'steps': steps,
        'done': done,
        'total': PLAN_TOTAL,
        'complete': done == PLAN_TOTAL,
        'variety': source.variedad if has_crop else '',
        'sowing_date': source.fecha_siembra if has_crop else None,
        'harvest_date': source.fecha_cosecha if has_crop else None,
    }


def season_payload(season):
    return {
        'id': season.pk,
        'cropId': season.cultivo_id,
        'crop': season.cultivo.nombre,
        'variety': season.variedad,
        'season': season.temporada_agricola,
        'sowingDate': season.fecha_siembra.isoformat() if season.fecha_siembra else '',
        'harvestDate': season.fecha_cosecha.isoformat() if season.fecha_cosecha else '',
        'status': season.estado,
        'statusLabel': season.get_estado_display(),
        'notes': season.observaciones,
        'created': season.creado.isoformat() if season.creado else '',
    }


def _preserve_legacy_crop(plot, user):
    """Si el potrero tenía un cultivo sin temporada, lo guarda antes del cambio."""
    if not plot.cultivo_id or plot.temporadas.exists():
        return None
    legacy = TemporadaCultivo(
        potrero=plot,
        cultivo=plot.cultivo,
        variedad=plot.variedad,
        temporada_agricola=agricultural_season_for(plot.fecha_siembra),
        fecha_siembra=plot.fecha_siembra,
        fecha_cosecha=plot.fecha_cosecha,
        estado=(
            TemporadaCultivo.Estado.SEMBRADA
            if plot.fecha_siembra and plot.fecha_siembra <= date.today()
            else TemporadaCultivo.Estado.PLANIFICADA
        ),
        observaciones=(
            'Registro conservado automáticamente desde los datos del potrero ' 'antes de asignar otro cultivo.'
        ),
    )
    # Se omite la validación de cultivo activo: es un dato histórico existente.
    legacy.save()
    return legacy


@transaction.atomic
def assign_crop(
    plot,
    user,
    *,
    crop,
    variety='',
    season_label='',
    sowing_date=None,
    harvest_date=None,
    status=TemporadaCultivo.Estado.PLANIFICADA,
    notes='',
    close_previous=True,
):
    """Crea una temporada nueva para ``plot`` y actualiza la instantánea del potrero.

    Lanza ``ValidationError`` con mensajes en español cuando los datos no son
    válidos. Nunca modifica temporadas anteriores salvo para cerrarlas cuando
    ``close_previous`` es verdadero y el usuario cambia de temporada.
    """
    plot = Potrero.objects.select_for_update().select_related('predio', 'cultivo').get(pk=plot.pk)
    if plot.predio.propietario_id != user.pk:
        raise ValidationError('El potrero no pertenece a tu cuenta.')
    if not isinstance(crop, Cultivo) or not crop.activo:
        raise ValidationError({'cultivo': 'Selecciona un cultivo activo del catálogo.'})
    variety = (variety or '').strip()
    notes = (notes or '').strip()
    season_label = (season_label or '').strip() or agricultural_season_for(sowing_date)

    previous_crop = plot.cultivo.nombre if plot.cultivo_id else 'Sin cultivo'
    _preserve_legacy_crop(plot, user)
    previous_open = list(
        plot.temporadas.exclude(estado__in=[TemporadaCultivo.Estado.CERRADA, TemporadaCultivo.Estado.COSECHADA])
    )

    season = TemporadaCultivo(
        potrero=plot,
        cultivo=crop,
        variedad=variety,
        temporada_agricola=season_label,
        fecha_siembra=sowing_date,
        fecha_cosecha=harvest_date,
        estado=status,
        observaciones=notes,
    )
    season.full_clean()
    season.save()

    closed = []
    if close_previous:
        for old in previous_open:
            # Solo se cambia el estado; fechas, cultivo y observaciones se conservan.
            TemporadaCultivo.objects.filter(pk=old.pk).update(estado=TemporadaCultivo.Estado.CERRADA)
            closed.append(old)

    plot.cultivo = crop
    plot.variedad = variety
    plot.fecha_siembra = sowing_date
    plot.fecha_cosecha = harvest_date
    plot.full_clean()
    plot.save()

    detail = [f'Cultivo: {previous_crop} → {crop.nombre}']
    if variety:
        detail.append(f'Variedad: {variety}')
    if season_label:
        detail.append(f'Temporada: {season_label}')
    if sowing_date:
        detail.append(f'Siembra: {sowing_date:%d/%m/%Y}')
    if harvest_date:
        detail.append(f'Cosecha prevista: {harvest_date:%d/%m/%Y}')
    detail.append(f'Estado: {season.get_estado_display()}')
    if closed:
        detail.append(f'{len(closed)} temporada(s) anterior(es) cerrada(s) sin borrar sus datos')
    PredioHistorial.objects.create(
        predio=plot.predio,
        potrero=plot,
        usuario=user,
        accion=PredioHistorial.Accion.CULTIVO_ASIGNADO,
        resumen=f'Se asignó un cultivo al potrero «{plot.nombre}»: ' + '; '.join(detail) + '.',
        superficie_anterior_ha=plot.superficie_ha,
        superficie_nueva_ha=plot.superficie_ha,
    )
    return season


@transaction.atomic
def update_current_dates(plot, user, season, *, sowing_date, harvest_date, status=None):
    """Actualiza fechas reales de la temporada vigente sin crear otra.

    Se usa cuando el agricultor registra desde el calendario las fechas de la
    temporada que ya tiene asignada. El cambio queda en el historial del predio.
    """
    plot = Potrero.objects.select_for_update().select_related('predio').get(pk=plot.pk)
    if plot.predio.propietario_id != user.pk or season.potrero_id != plot.pk:
        raise ValidationError('La temporada no pertenece a tu cuenta.')
    before = (season.fecha_siembra, season.fecha_cosecha, season.get_estado_display())
    season.fecha_siembra = sowing_date
    season.fecha_cosecha = harvest_date
    if status:
        season.estado = status
    if not season.temporada_agricola:
        season.temporada_agricola = agricultural_season_for(sowing_date)
    season.full_clean()
    season.save()
    latest = current_season(plot)
    if latest and latest.pk == season.pk:
        plot.fecha_siembra = sowing_date
        plot.fecha_cosecha = harvest_date
        plot.full_clean()
        plot.save()

    def fmt(value):
        return value.strftime('%d/%m/%Y') if value else 'Sin dato'

    PredioHistorial.objects.create(
        predio=plot.predio,
        potrero=plot,
        usuario=user,
        accion=PredioHistorial.Accion.POTRERO_ACTUALIZADO,
        resumen=(
            f'Fechas de la temporada {season.temporada_agricola or "sin rótulo"} de '
            f'«{season.cultivo.nombre}» actualizadas: siembra {fmt(before[0])} → '
            f'{fmt(season.fecha_siembra)}; cosecha {fmt(before[1])} → '
            f'{fmt(season.fecha_cosecha)}; estado {before[2]} → {season.get_estado_display()}.'
        ),
    )
    return season


def record_form_crop_change(plot, previous):
    """Conserva trazabilidad cuando el cultivo se cambia desde el formulario del potrero.

    ``previous`` es un diccionario con ``cultivo_id``, ``cultivo``, ``variedad``,
    ``fecha_siembra`` y ``fecha_cosecha`` anteriores al guardado (o ``None`` para
    un potrero nuevo). Solo crea temporadas; nunca borra ni modifica las existentes.
    """
    previous = previous or {}
    if plot.cultivo_id == previous.get('cultivo_id') or not plot.cultivo_id:
        return None
    if previous.get('cultivo_id') and not plot.temporadas.exists():
        TemporadaCultivo(
            potrero=plot,
            cultivo=previous['cultivo'],
            variedad=previous.get('variedad', ''),
            temporada_agricola=agricultural_season_for(previous.get('fecha_siembra')),
            fecha_siembra=previous.get('fecha_siembra'),
            fecha_cosecha=previous.get('fecha_cosecha'),
            estado=TemporadaCultivo.Estado.CERRADA,
            observaciones='Registro conservado automáticamente al cambiar el cultivo desde el formulario del potrero.',
        ).save()
    season = TemporadaCultivo(
        potrero=plot,
        cultivo=plot.cultivo,
        variedad=plot.variedad,
        temporada_agricola=agricultural_season_for(plot.fecha_siembra),
        fecha_siembra=plot.fecha_siembra,
        fecha_cosecha=plot.fecha_cosecha,
        estado=TemporadaCultivo.Estado.PLANIFICADA,
        observaciones='Temporada creada desde el formulario del potrero.',
    )
    season.save()
    return season


def _format_date(value):
    return value.strftime('%d/%m/%Y') if value else 'Sin dato'


def _season_snapshot(season):
    """Valores legibles de una temporada, para describir qué cambió en el historial."""
    return {
        'Cultivo': season.cultivo.nombre,
        'Variedad': season.variedad or 'Sin dato',
        'Temporada': season.temporada_agricola or 'Sin rótulo',
        'Siembra': _format_date(season.fecha_siembra),
        'Cosecha': _format_date(season.fecha_cosecha),
        'Estado': season.get_estado_display(),
    }


def season_form_initial(plot, season=None):
    """Valores que el formulario de temporada muestra donde todavía no hay dato.

    Un potrero antiguo (con cultivo pero sin temporadas) o una temporada vigente sin fechas parten de lo que el potrero
    ya tiene registrado, de modo que completar la planificación sea revisar y guardar, no volver a escribir. Es solo
    lo que se muestra: nada se guarda hasta que el usuario envía el formulario, y una temporada anterior nunca toma
    datos del potrero porque ese potrero ya describe otra temporada.
    """
    initial = {}
    latest = vigente_season(plot)
    if season is None:
        if latest is None and plot.cultivo_id:
            initial = {
                'cultivo': plot.cultivo_id,
                'variedad': plot.variedad,
                'fecha_siembra': plot.fecha_siembra,
                'fecha_cosecha': plot.fecha_cosecha,
            }
            initial['temporada_agricola'] = agricultural_season_for(plot.fecha_siembra or date.today())
        return initial
    if latest is None or latest.pk != season.pk or season.cultivo_id != plot.cultivo_id:
        return initial
    for field in ('variedad', 'fecha_siembra', 'fecha_cosecha'):
        if not getattr(season, field) and getattr(plot, field):
            initial[field] = getattr(plot, field)
    if not season.temporada_agricola:
        sowing = initial.get('fecha_siembra') or season.fecha_siembra
        initial['temporada_agricola'] = agricultural_season_for(sowing or date.today())
    return initial


def plot_form_initial(plot):
    """Variedad y fechas que el formulario del potrero muestra: las de la temporada vigente.

    Así el formulario del potrero y el de la temporada no pueden enseñar dos versiones distintas de la misma
    planificación. Un potrero sin temporada vigente (o con otro cultivo) muestra sus propios datos.
    """
    season = planning_state(plot)['season']
    if season is None:
        return {}
    return {
        'variedad': season.variedad,
        'fecha_siembra': season.fecha_siembra,
        'fecha_cosecha': season.fecha_cosecha,
    }


def _sync_plot_snapshot(plot, season):
    """Copia la temporada vigente a la instantánea del potrero. Devuelve los campos que cambiaron."""
    changed = []
    if plot.cultivo_id != season.cultivo_id:
        plot.cultivo = season.cultivo
        changed.append('cultivo')
    for field in ('variedad', 'fecha_siembra', 'fecha_cosecha'):
        if getattr(plot, field) != getattr(season, field):
            setattr(plot, field, getattr(season, field))
            changed.append(field)
    if changed:
        # La temporada ya se validó con sus propias reglas; aquí solo se copian cuatro campos, sin revalidar el
        # perímetro ni la superficie del potrero (que no cambian).
        plot.save(update_fields=[*changed, 'actualizado'])
    return changed


@transaction.atomic
def save_season_form(plot, user, season):
    """Guarda una temporada ya validada por ``TemporadaCultivoForm`` y mantiene coherente al potrero.

    * Nueva temporada: pasa a ser la vigente. Un potrero antiguo con otro cultivo conserva ese cultivo como
      temporada cerrada antes de continuar; si es el mismo cultivo, la nueva temporada lo representa y no se duplica.
    * Dos envíos idénticos seguidos (doble clic) no crean dos temporadas.
    * Editar la temporada vigente actualiza también la instantánea del potrero. Editar una temporada anterior solo
      cambia esa temporada: los datos vigentes del potrero no se tocan.
    * Las demás temporadas no se modifican. Cada cambio queda en ``PredioHistorial``.

    Devuelve ``{'season', 'created', 'current', 'duplicate', 'synced'}``.
    """
    plot = Potrero.objects.select_for_update().select_related('predio', 'cultivo').get(pk=plot.pk)
    if plot.predio.propietario_id != user.pk or (season.potrero_id and season.potrero_id != plot.pk):
        raise ValidationError('La temporada no pertenece a tu cuenta.')
    season.potrero = plot
    creating = season.pk is None
    notes = []
    before = None
    if creating:
        repeated = plot.temporadas.filter(
            cultivo=season.cultivo,
            variedad=season.variedad.strip(),
            temporada_agricola=season.temporada_agricola,
            fecha_siembra=season.fecha_siembra,
            fecha_cosecha=season.fecha_cosecha,
            estado=season.estado,
            observaciones=season.observaciones,
            creado__gte=timezone.now() - DOUBLE_SUBMIT_WINDOW,
        ).first()
        if repeated is not None:
            return {'season': repeated, 'created': False, 'current': True, 'duplicate': True, 'synced': []}
        if plot.cultivo_id and plot.cultivo_id != season.cultivo_id and not plot.temporadas.exists():
            legacy = _preserve_legacy_crop(plot, user)
            if legacy is not None:
                TemporadaCultivo.objects.filter(pk=legacy.pk).update(estado=TemporadaCultivo.Estado.CERRADA)
                notes.append('el cultivo anterior del potrero se conservó como temporada cerrada')
        season.save()
    else:
        stored = TemporadaCultivo.objects.select_related('cultivo').get(pk=season.pk, potrero=plot)
        before = _season_snapshot(stored)
        old_notes = stored.observaciones
        season.save()
    latest = current_season(plot)
    is_current = latest is not None and latest.pk == season.pk
    synced = _sync_plot_snapshot(plot, season) if is_current else []

    after = _season_snapshot(season)
    label = season.temporada_agricola or 'sin rótulo'
    if creating:
        detail = '; '.join(f'{key}: {value}' for key, value in after.items())
        summary = (
            f'Se registró una temporada de «{season.cultivo.nombre}» en el potrero «{plot.nombre}» ({detail}). '
            'Es la temporada vigente: los datos del potrero se actualizaron con ella.'
        )
        if notes:
            summary += ' Además, ' + '; '.join(notes) + '.'
        PredioHistorial.objects.create(
            predio=plot.predio,
            potrero=plot,
            usuario=user,
            accion=PredioHistorial.Accion.CULTIVO_ASIGNADO,
            resumen=summary,
            superficie_anterior_ha=plot.superficie_ha,
            superficie_nueva_ha=plot.superficie_ha,
        )
    else:
        changes = [f'{key}: {before[key]} → {after[key]}' for key in after if before[key] != after[key]]
        if old_notes != season.observaciones:
            changes.append('Observaciones actualizadas')
        if changes:
            tail = (
                'Es la temporada vigente: los datos del potrero quedaron sincronizados.'
                if is_current
                else 'Es una temporada anterior: no se modificaron los datos vigentes del potrero.'
            )
            PredioHistorial.objects.create(
                predio=plot.predio,
                potrero=plot,
                usuario=user,
                accion=PredioHistorial.Accion.POTRERO_ACTUALIZADO,
                resumen=f'Temporada {label} de «{season.cultivo.nombre}» modificada ({"; ".join(changes)}). {tail}',
            )
    return {'season': season, 'created': creating, 'current': is_current, 'duplicate': False, 'synced': synced}


def sync_season_with_plot_form(plot, previous):
    """El formulario del potrero cambió variedad o fechas del *mismo* cultivo: llévalos a la temporada vigente.

    Sin esto el formulario del potrero y la temporada quedaban con fechas distintas. Si la temporada vigente ya
    terminó (cosechada o cerrada) no se reescribe su historia: los datos nuevos abren una temporada nueva. Un cambio
    de cultivo lo resuelve ``record_form_crop_change``. Devuelve la temporada que quedó vigente, o ``None`` si no
    hubo nada que sincronizar (por ejemplo, un potrero antiguo todavía sin temporadas).
    """
    previous = previous or {}
    if not plot.cultivo_id or previous.get('cultivo_id') != plot.cultivo_id:
        return None
    latest = vigente_season(plot)
    if latest is None or latest.cultivo_id != plot.cultivo_id:
        return None
    changed = [
        field for field in ('variedad', 'fecha_siembra', 'fecha_cosecha') if getattr(latest, field) != getattr(plot, field)
    ]
    if not changed:
        return None
    if latest.estado in FINISHED_STATES:
        return TemporadaCultivo.objects.create(
            potrero=plot,
            cultivo=plot.cultivo,
            variedad=plot.variedad,
            temporada_agricola=agricultural_season_for(plot.fecha_siembra),
            fecha_siembra=plot.fecha_siembra,
            fecha_cosecha=plot.fecha_cosecha,
            estado=TemporadaCultivo.Estado.PLANIFICADA,
            observaciones='Temporada creada desde el formulario del potrero: la anterior ya estaba terminada.',
        )
    for field in changed:
        setattr(latest, field, getattr(plot, field))
    if not latest.temporada_agricola and latest.fecha_siembra:
        latest.temporada_agricola = agricultural_season_for(latest.fecha_siembra)
    if latest.estado == TemporadaCultivo.Estado.SEMBRADA and not latest.fecha_siembra:
        latest.estado = TemporadaCultivo.Estado.PLANIFICADA  # «sembrada» exige fecha de siembra
    latest.save()
    return latest
