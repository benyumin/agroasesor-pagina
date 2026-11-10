"""Registrar un potrero, asignar un cultivo y completar su planificación: un flujo continuo y sin bucles.

La temporada vigente (``TemporadaCultivo``) es la fuente de la planificación. El potrero conserva una instantánea
(cultivo, variedad, fechas) solo para las calculadoras, el calendario y el dashboard, y se mantiene sincronizada con esa
temporada. Las temporadas anteriores no se modifican, no se borran y no cambian los datos vigentes del potrero.
"""

import re
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Cultivo, Potrero, Predio, PredioHistorial, TemporadaCultivo
from core.seasons import planning_state

User = get_user_model()
PASSWORD = 'Clave-segura-123'
PLANNED = TemporadaCultivo.Estado.PLANIFICADA
SOWN = TemporadaCultivo.Estado.SEMBRADA
HARVESTED = TemporadaCultivo.Estado.COSECHADA


class PlanningBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('agricultor', email='a@example.com', password=PASSWORD)
        self.other = User.objects.create_user('vecino', email='v@example.com', password=PASSWORD)
        self.haba = Cultivo.objects.create(nombre='Haba prueba')
        self.maiz = Cultivo.objects.create(nombre='Maíz prueba')
        self.predio = Predio.objects.create(
            propietario=self.user, nombre='Pomaire', ubicacion='Melipilla', superficie_ha=10
        )
        self.client.force_login(self.user)
        self.new_url = f'/predios/{self.predio.pk}/potreros/nuevo/'
        self.detail_url = f'/predios/{self.predio.pk}/'

    # ---- ayudas ---------------------------------------------------------------------------------------------
    def plot_post(self, **values):
        data = {
            'nombre': 'Potrero principal',
            'cultivo': '',
            'variedad': '',
            'superficie_ha': '2',
            'fecha_siembra': '',
            'fecha_cosecha': '',
            'latitud': '',
            'longitud': '',
            'perimetro': '[]',
            **values,
        }
        return data

    def register_plot(self, follow=False, **values):
        response = self.client.post(self.new_url, self.plot_post(**values), follow=follow)
        return response

    def make_plot(self, **values):
        """Registra un potrero por el formulario real y lo devuelve."""
        self.register_plot(**values)
        return Potrero.objects.get(predio=self.predio, nombre=values.get('nombre', 'Potrero principal'))

    def edit_plot_url(self, plot):
        return f'/predios/{self.predio.pk}/potreros/{plot.pk}/editar/'

    def season_create_url(self, plot):
        return f'/predios/{self.predio.pk}/potreros/{plot.pk}/temporadas/nueva/'

    def season_edit_url(self, season):
        return f'/predios/{self.predio.pk}/potreros/{season.potrero_id}/temporadas/{season.pk}/editar/'

    def season_post(self, crop=None, **values):
        return {
            'cultivo': (crop or self.haba).pk,
            'variedad': '',
            'temporada_agricola': '2026-2027',
            'fecha_siembra': '',
            'fecha_cosecha': '',
            'estado': 'planned',
            'observaciones': '',
            **values,
        }

    def card(self, plot):
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        for item in response.context['farm_card']['plots']:
            if item['plot'].pk == plot.pk:
                return item['ui'], response
        self.fail('El potrero no aparece en la ficha del predio.')

    def vigente(self, plot):
        return plot.temporadas.order_by('-creado', '-pk').first()

    def complete_planning(self, plot, sowing='2026-11-10', harvest='2027-03-10'):
        season = self.vigente(plot)
        response = self.client.post(
            self.season_edit_url(season),
            self.season_post(crop=season.cultivo, fecha_siembra=sowing, fecha_cosecha=harvest),
        )
        self.assertRedirects(response, self.detail_url)
        return season


class RegisterPlotTests(PlanningBase):
    def test_plot_without_crop_creates_no_season_and_asks_for_the_crop(self):
        response = self.register_plot(follow=True)
        self.assertRedirects(response, self.detail_url)
        self.assertContains(response, 'Potrero, cultivo y ubicación guardados')
        plot = Potrero.objects.get()
        self.assertEqual(plot.temporadas.count(), 0)
        ui, _ = self.card(plot)
        self.assertEqual(ui['primary']['label'], 'Asignar cultivo')
        self.assertEqual(ui['primary']['url'], self.edit_plot_url(plot))
        self.assertEqual((ui['plan_done'], ui['plan_complete']), (0, False))
        self.assertEqual(response.context['next_step']['key'], 'cultivo')

    def test_plot_with_crop_saves_and_creates_its_season_automatically(self):
        response = self.register_plot(follow=True, cultivo=self.haba.pk)
        self.assertRedirects(response, self.detail_url)
        self.assertContains(response, 'Potrero, cultivo y ubicación guardados')
        self.assertContains(response, 'Potrero principal')
        plot = Potrero.objects.get()
        season = plot.temporadas.get()  # exactamente una
        self.assertEqual((season.cultivo, season.estado), (self.haba, PLANNED))
        self.assertEqual(plot.cultivo, self.haba)
        self.assertIsNone(season.fecha_siembra)
        self.assertTrue(
            PredioHistorial.objects.filter(potrero=plot, accion=PredioHistorial.Accion.POTRERO_CREADO).exists()
        )
        self.assertContains(response, 'Haba prueba')
        self.assertEqual(response.context['next_step']['key'], 'planificacion')

    def test_plot_with_crop_and_dates_keeps_them_in_the_season_and_in_the_plot(self):
        self.register_plot(cultivo=self.haba.pk, fecha_siembra='2026-11-10', fecha_cosecha='2027-03-10')
        plot = Potrero.objects.get()
        season = plot.temporadas.get()
        self.assertEqual((season.fecha_siembra, season.fecha_cosecha), (date(2026, 11, 10), date(2027, 3, 10)))
        self.assertEqual((plot.fecha_siembra, plot.fecha_cosecha), (date(2026, 11, 10), date(2027, 3, 10)))
        ui, _ = self.card(plot)
        self.assertEqual((ui['plan_done'], ui['plan_complete']), (3, True))
        self.assertEqual(ui['primary']['label'], 'Calcular semillas')


class IncompletePlanningTests(PlanningBase):
    """El bucle: «Continuar planificación» devolvía al formulario del potrero."""

    def setUp(self):
        super().setUp()
        self.plot = self.make_plot(cultivo=self.haba.pk)
        self.season = self.plot.temporadas.get()

    def test_incomplete_planning_is_one_of_three_steps(self):
        ui, response = self.card(self.plot)
        self.assertEqual((ui['plan_done'], ui['plan_total'], ui['plan_complete']), (1, 3, False))
        self.assertEqual(ui['plan_steps'], [True, False, False])
        self.assertContains(response, 'Planificación <strong>1 de 3</strong> pasos')

    def test_complete_planning_opens_the_current_season_and_not_the_plot_form(self):
        ui, response = self.card(self.plot)
        self.assertEqual(ui['primary']['label'], 'Completar planificación')
        self.assertEqual(ui['primary']['url'], self.season_edit_url(self.season))
        self.assertNotEqual(ui['primary']['url'], self.edit_plot_url(self.plot))
        self.assertContains(response, 'Completar planificación')
        self.assertNotContains(response, 'Continuar planificación')
        # el enlace lleva de verdad al formulario de la temporada, con sus siete campos
        form_page = self.client.get(ui['primary']['url'])
        self.assertEqual(form_page.status_code, 200)
        self.assertTemplateUsed(form_page, 'temporada_form.html')
        for name in (
            'cultivo',
            'variedad',
            'temporada_agricola',
            'fecha_siembra',
            'fecha_cosecha',
            'estado',
            'observaciones',
        ):
            self.assertIn(f'name="{name}"', form_page.content.decode(), name)

    def test_next_step_of_the_farm_points_to_the_season_too(self):
        response = self.client.get(self.detail_url)
        step = response.context['next_step']
        self.assertEqual(step['key'], 'planificacion')
        self.assertEqual(step['url'], self.season_edit_url(self.season))

    def test_following_the_button_never_returns_to_the_same_screen(self):
        """Seguir el botón principal hasta completar la planificación termina en «Calcular semillas», sin vueltas."""
        seen = []
        for sowing, harvest in (('', ''), ('2026-11-10', ''), ('2026-11-10', '2027-03-10')):
            ui, _ = self.card(self.plot)
            seen.append((ui['primary']['label'], ui['primary']['url']))
            if ui['plan_complete']:
                break
            self.client.post(
                ui['primary']['url'], self.season_post(fecha_siembra=sowing, fecha_cosecha=harvest)
            )
        ui, _ = self.card(self.plot)
        seen.append((ui['primary']['label'], ui['primary']['url']))
        self.assertEqual(seen[-1][0], 'Calcular semillas')
        self.assertTrue(all('/editar/' not in url or '/temporadas/' in url for _, url in seen[:-1]))
        self.assertEqual(self.plot.temporadas.count(), 1)


class SaveDatesTests(PlanningBase):
    def setUp(self):
        super().setUp()
        self.plot = self.make_plot(cultivo=self.haba.pk)
        self.season = self.plot.temporadas.get()

    def test_sowing_and_harvest_dates_are_saved_in_the_season_and_synced_to_the_plot(self):
        response = self.client.post(
            self.season_edit_url(self.season),
            self.season_post(
                variedad='Agua dulce',
                fecha_siembra='2026-11-10',
                fecha_cosecha='2027-03-10',
                observaciones='Primer ciclo',
            ),
            follow=True,
        )
        self.assertRedirects(response, self.detail_url)
        self.assertContains(response, 'La temporada quedó guardada')
        self.season.refresh_from_db()
        self.plot.refresh_from_db()
        self.assertEqual((self.season.fecha_siembra, self.season.fecha_cosecha), (date(2026, 11, 10), date(2027, 3, 10)))
        self.assertEqual((self.season.variedad, self.season.observaciones), ('Agua dulce', 'Primer ciclo'))
        self.assertEqual((self.plot.fecha_siembra, self.plot.fecha_cosecha), (date(2026, 11, 10), date(2027, 3, 10)))
        self.assertEqual((self.plot.variedad, self.plot.cultivo), ('Agua dulce', self.haba))
        self.assertEqual(self.plot.temporadas.count(), 1)

    def test_harvest_before_sowing_is_rejected_without_touching_anything(self):
        response = self.client.post(
            self.season_edit_url(self.season),
            self.season_post(fecha_siembra='2027-03-10', fecha_cosecha='2026-11-10'),
        )
        self.assertEqual(response.status_code, 200)
        self.plot.refresh_from_db()
        self.season.refresh_from_db()
        self.assertIsNone(self.plot.fecha_siembra)
        self.assertIsNone(self.season.fecha_siembra)

    def test_saving_dates_leaves_a_trace_in_the_farm_history(self):
        before = PredioHistorial.objects.filter(predio=self.predio).count()
        self.complete_planning(self.plot)
        entries = PredioHistorial.objects.filter(predio=self.predio, potrero=self.plot).order_by('-pk')
        self.assertEqual(entries.count(), before + 1)
        latest = entries.first()
        self.assertEqual(latest.accion, PredioHistorial.Accion.POTRERO_ACTUALIZADO)
        self.assertEqual(latest.usuario, self.user)
        self.assertIn('10/11/2026', latest.resumen)
        self.assertIn('10/03/2027', latest.resumen)


class ProgressAndPrimaryButtonTests(PlanningBase):
    def setUp(self):
        super().setUp()
        self.plot = self.make_plot(cultivo=self.haba.pk)

    def test_progress_goes_from_one_to_three_of_three(self):
        season = self.vigente(self.plot)
        states = []
        for sowing, harvest in (('', ''), ('2026-11-10', ''), ('2026-11-10', '2027-03-10')):
            self.client.post(
                self.season_edit_url(season),
                self.season_post(fecha_siembra=sowing, fecha_cosecha=harvest),
            )
            ui, response = self.card(self.plot)
            states.append((ui['plan_done'], ui['plan_complete']))
        self.assertEqual(states, [(1, False), (2, False), (3, True)])
        self.assertContains(response, 'Planificación <strong>3 de 3</strong> pasos')
        self.assertContains(response, 'Planificación completa')

    def test_primary_button_changes_with_the_state(self):
        bare = self.make_plot(nombre='Sin cultivo')
        labels = []
        ui, _ = self.card(bare)
        labels.append(ui['primary']['label'])
        ui, _ = self.card(self.plot)
        labels.append(ui['primary']['label'])
        self.complete_planning(self.plot)
        ui, response = self.card(self.plot)
        labels.append(ui['primary']['label'])
        self.assertEqual(labels, ['Asignar cultivo', 'Completar planificación', 'Calcular semillas'])
        self.assertEqual(ui['primary']['url'], f'/decisiones/?tipo=semillas&potrero={self.plot.pk}')

    def test_complete_planning_also_offers_the_other_two_calculators(self):
        self.complete_planning(self.plot)
        ui, response = self.card(self.plot)
        html = response.content.decode()
        actions = re.search(r'<div class="plot-actions">(.*?)</div>', html, re.S).group(1)
        self.assertIn('Calcular semillas', actions)
        self.assertIn(f'/decisiones/?tipo=insumos&amp;potrero={self.plot.pk}', actions)
        self.assertIn('Calcular insumos', actions)
        self.assertIn(f'/decisiones/?herramienta=alcanza&amp;potrero={self.plot.pk}', actions)
        self.assertIn('Ver si alcanza', actions)
        self.assertEqual(response.context['next_step']['key'], 'decisiones')

    def test_incomplete_planning_does_not_show_the_extra_calculator_buttons(self):
        _, response = self.card(self.plot)
        actions = re.search(r'<div class="plot-actions">(.*?)</div>', response.content.decode(), re.S).group(1)
        self.assertNotIn('Ver si alcanza', actions)

    def test_the_map_panel_uses_the_same_button_as_the_card(self):
        from core.views import farm_payload

        data = farm_payload(self.user)
        payload = next(item for item in data['plots'] if item['id'] == str(self.plot.pk))
        self.assertEqual(payload['primaryLabel'], 'Completar planificación')
        self.assertEqual(payload['primaryUrl'], self.season_edit_url(self.vigente(self.plot)))
        self.complete_planning(self.plot)
        payload = next(item for item in farm_payload(self.user)['plots'] if item['id'] == str(self.plot.pk))
        self.assertEqual(payload['primaryLabel'], 'Calcular semillas')
        self.assertEqual(payload['primaryUrl'], payload['seedsUrl'])
        self.assertEqual((payload['planDone'], payload['planTotal']), (3, 3))

    def test_the_route_of_work_points_to_the_season_form(self):
        from core.views import journey_payload

        journey = journey_payload(self.user, 'predios')
        step = journey['next']
        self.assertEqual(step['cta'], 'Completar planificación')
        self.assertEqual(step['url'], self.season_edit_url(self.vigente(self.plot)))


class EditSeasonTests(PlanningBase):
    def test_editing_an_existing_season_updates_it_in_place(self):
        plot = self.make_plot(cultivo=self.haba.pk, fecha_siembra='2026-11-10', fecha_cosecha='2027-03-10')
        season = self.vigente(plot)
        response = self.client.post(
            self.season_edit_url(season),
            self.season_post(
                variedad='Aguadulce',
                fecha_siembra='2026-11-20',
                fecha_cosecha='2027-03-25',
                estado='sown',
                observaciones='Se atrasó por lluvia.',
            ),
        )
        self.assertRedirects(response, self.detail_url)
        self.assertEqual(plot.temporadas.count(), 1)
        season.refresh_from_db()
        self.assertEqual((season.variedad, season.estado), ('Aguadulce', SOWN))
        self.assertEqual((season.fecha_siembra, season.fecha_cosecha), (date(2026, 11, 20), date(2027, 3, 25)))
        plot.refresh_from_db()
        self.assertEqual((plot.variedad, plot.fecha_siembra, plot.fecha_cosecha), ('Aguadulce', date(2026, 11, 20), date(2027, 3, 25)))

    def test_the_edit_form_shows_the_saved_values(self):
        plot = self.make_plot(cultivo=self.haba.pk, fecha_siembra='2026-11-10', fecha_cosecha='2027-03-10')
        html = self.client.get(self.season_edit_url(self.vigente(plot))).content.decode()
        self.assertIn('value="2026-11-10"', html)
        self.assertIn('value="2027-03-10"', html)


class PreviousSeasonsTests(PlanningBase):
    def snapshot(self, season):
        season.refresh_from_db()
        return (
            season.pk,
            season.cultivo_id,
            season.variedad,
            season.temporada_agricola,
            season.fecha_siembra,
            season.fecha_cosecha,
            season.estado,
            season.observaciones,
        )

    def test_changing_the_crop_keeps_the_previous_season_intact(self):
        plot = self.make_plot(
            cultivo=self.haba.pk, variedad='Aguadulce', fecha_siembra='2026-05-10', fecha_cosecha='2026-10-10'
        )
        first = self.vigente(plot)
        before = self.snapshot(first)
        data = self.plot_post(cultivo=self.maiz.pk, fecha_siembra='2026-11-10', fecha_cosecha='2027-03-10')
        self.client.post(self.edit_plot_url(plot), data)
        self.assertEqual(plot.temporadas.count(), 2)
        self.assertEqual(self.snapshot(first), before)
        newest = self.vigente(plot)
        self.assertNotEqual(newest.pk, first.pk)
        self.assertEqual((newest.cultivo, newest.fecha_siembra), (self.maiz, date(2026, 11, 10)))
        plot.refresh_from_db()
        self.assertEqual((plot.cultivo, plot.fecha_siembra), (self.maiz, date(2026, 11, 10)))

    def test_registering_a_new_season_keeps_every_earlier_one(self):
        plot = self.make_plot(cultivo=self.haba.pk, fecha_siembra='2025-11-10', fecha_cosecha='2026-03-10')
        first = self.vigente(plot)
        before = self.snapshot(first)
        for crop, sowing in ((self.maiz, '2026-11-10'), (self.haba, '2027-11-10')):
            response = self.client.post(
                self.season_create_url(plot),
                self.season_post(crop=crop, temporada_agricola='', fecha_siembra=sowing, fecha_cosecha=''),
            )
            self.assertRedirects(response, self.detail_url)
        self.assertEqual(plot.temporadas.count(), 3)
        self.assertEqual(self.snapshot(first), before)
        self.assertEqual(
            list(plot.temporadas.order_by('creado', 'pk').values_list('cultivo__nombre', flat=True)),
            ['Haba prueba', 'Maíz prueba', 'Haba prueba'],
        )

    def test_the_plot_page_lists_every_season_and_marks_only_the_current_one(self):
        plot = self.make_plot(cultivo=self.haba.pk, fecha_siembra='2025-11-10', fecha_cosecha='2026-03-10')
        self.client.post(
            self.season_create_url(plot),
            self.season_post(crop=self.maiz, fecha_siembra='2026-11-10', fecha_cosecha='2027-03-10'),
        )
        _, response = self.card(plot)
        html = response.content.decode()
        self.assertEqual(html.count('Editar temporada'), 2)
        self.assertEqual(html.count('Vigente'), 1)

    def test_editing_an_old_season_does_not_touch_the_current_data_of_the_plot(self):
        plot = self.make_plot(
            cultivo=self.haba.pk, variedad='Antigua', fecha_siembra='2025-11-10', fecha_cosecha='2026-03-10'
        )
        old = self.vigente(plot)
        self.client.post(
            self.season_create_url(plot),
            self.season_post(
                crop=self.maiz, variedad='Choclero', fecha_siembra='2026-11-10', fecha_cosecha='2027-03-10'
            ),
        )
        current = self.vigente(plot)
        self.assertNotEqual(old.pk, current.pk)
        plot.refresh_from_db()
        plot_before = (plot.cultivo, plot.variedad, plot.fecha_siembra, plot.fecha_cosecha)
        current_before = self.snapshot(current)
        entries_before = PredioHistorial.objects.filter(potrero=plot).count()

        response = self.client.post(
            self.season_edit_url(old),
            self.season_post(
                variedad='Corregida',
                temporada_agricola='2025-2026',
                fecha_siembra='2025-11-12',
                fecha_cosecha='2026-03-12',
                estado='harvested',
            ),
        )
        self.assertRedirects(response, self.detail_url)
        old.refresh_from_db()
        self.assertEqual((old.variedad, old.fecha_siembra, old.estado), ('Corregida', date(2025, 11, 12), HARVESTED))
        plot.refresh_from_db()
        self.assertEqual((plot.cultivo, plot.variedad, plot.fecha_siembra, plot.fecha_cosecha), plot_before)
        self.assertEqual(self.snapshot(current), current_before)
        self.assertEqual(self.vigente(plot).pk, current.pk)
        # la corrección queda registrada y aclara que no se tocaron los datos vigentes
        entries = PredioHistorial.objects.filter(potrero=plot).order_by('-pk')
        self.assertEqual(entries.count(), entries_before + 1)
        self.assertIn('Corregida', entries.first().resumen)
        self.assertIn('no se modificaron', entries.first().resumen.lower())
        ui, _ = self.card(plot)
        self.assertEqual((ui['plan_done'], ui['primary']['label']), (3, 'Calcular semillas'))

    def test_no_season_is_ever_deleted(self):
        plot = self.make_plot(cultivo=self.haba.pk)
        before = set(TemporadaCultivo.objects.values_list('pk', flat=True))
        self.client.post(self.edit_plot_url(plot), self.plot_post(cultivo=self.maiz.pk))
        self.client.post(self.edit_plot_url(plot), self.plot_post(cultivo=''))
        self.client.post(self.edit_plot_url(plot), self.plot_post(cultivo=self.haba.pk))
        after = set(TemporadaCultivo.objects.values_list('pk', flat=True))
        self.assertTrue(before <= after)
        self.assertEqual(len(after), 3)


class SyncTests(PlanningBase):
    def test_current_season_and_plot_agree_after_every_form(self):
        plot = self.make_plot(cultivo=self.haba.pk)

        def agree():
            plot.refresh_from_db()
            season = self.vigente(plot)
            self.assertEqual(
                (plot.cultivo_id, plot.variedad, plot.fecha_siembra, plot.fecha_cosecha),
                (season.cultivo_id, season.variedad, season.fecha_siembra, season.fecha_cosecha),
            )

        agree()
        # 1) temporada vigente editada
        self.client.post(
            self.season_edit_url(self.vigente(plot)),
            self.season_post(variedad='A', fecha_siembra='2026-11-10', fecha_cosecha='2027-03-10'),
        )
        agree()
        # 2) mismo cultivo editado desde el formulario del potrero
        self.client.post(
            self.edit_plot_url(plot),
            self.plot_post(
                cultivo=self.haba.pk, variedad='B', fecha_siembra='2026-11-15', fecha_cosecha='2027-03-20'
            ),
        )
        agree()
        self.assertEqual(plot.temporadas.count(), 1)
        self.assertEqual(self.vigente(plot).variedad, 'B')
        # 3) nueva temporada registrada
        self.client.post(
            self.season_create_url(plot),
            self.season_post(crop=self.maiz, variedad='C', fecha_siembra='2027-11-01', fecha_cosecha='2028-04-01'),
        )
        agree()
        # 4) cambio de cultivo desde el formulario del potrero
        self.client.post(
            self.edit_plot_url(plot),
            self.plot_post(cultivo=self.haba.pk, variedad='D', fecha_siembra='2028-11-01', fecha_cosecha='2029-03-01'),
        )
        agree()
        self.assertEqual(plot.temporadas.count(), 3)

    def test_changing_only_the_dates_in_the_plot_form_updates_the_open_season(self):
        plot = self.make_plot(cultivo=self.haba.pk)
        season = self.vigente(plot)
        self.client.post(
            self.edit_plot_url(plot),
            self.plot_post(cultivo=self.haba.pk, fecha_siembra='2026-11-10', fecha_cosecha='2027-03-10'),
        )
        self.assertEqual(plot.temporadas.count(), 1)
        season.refresh_from_db()
        self.assertEqual((season.fecha_siembra, season.fecha_cosecha), (date(2026, 11, 10), date(2027, 3, 10)))
        ui, _ = self.card(plot)
        self.assertEqual((ui['plan_done'], ui['plan_complete']), (3, True))

    def test_a_finished_season_is_not_rewritten_by_the_plot_form(self):
        plot = self.make_plot(cultivo=self.haba.pk, fecha_siembra='2025-11-10', fecha_cosecha='2026-03-10')
        season = self.vigente(plot)
        self.client.post(
            self.season_edit_url(season),
            self.season_post(fecha_siembra='2025-11-10', fecha_cosecha='2026-03-10', estado='harvested'),
        )
        self.client.post(
            self.edit_plot_url(plot),
            self.plot_post(cultivo=self.haba.pk, fecha_siembra='2026-11-10', fecha_cosecha='2027-03-10'),
        )
        season.refresh_from_db()
        self.assertEqual((season.fecha_siembra, season.estado), (date(2025, 11, 10), HARVESTED))
        self.assertEqual(plot.temporadas.count(), 2)
        newest = self.vigente(plot)
        self.assertEqual((newest.fecha_siembra, newest.estado), (date(2026, 11, 10), PLANNED))

    def test_removing_the_crop_from_the_plot_keeps_the_seasons_and_asks_for_a_crop_again(self):
        plot = self.make_plot(cultivo=self.haba.pk, fecha_siembra='2026-11-10', fecha_cosecha='2027-03-10')
        self.client.post(self.edit_plot_url(plot), self.plot_post(cultivo=''))
        plot.refresh_from_db()
        self.assertIsNone(plot.cultivo)
        self.assertEqual(plot.temporadas.count(), 1)
        ui, _ = self.card(plot)
        self.assertEqual((ui['primary']['label'], ui['primary']['url']), ('Asignar cultivo', self.edit_plot_url(plot)))
        self.assertFalse(ui['plan_complete'])


class NoDuplicateSeasonsTests(PlanningBase):
    def test_every_path_of_the_flow_leaves_exactly_one_season(self):
        plot = self.make_plot(cultivo=self.haba.pk)
        self.assertEqual(plot.temporadas.count(), 1)
        self.complete_planning(plot)
        self.assertEqual(plot.temporadas.count(), 1)
        # guardar el formulario del potrero sin cambios
        self.client.post(
            self.edit_plot_url(plot),
            self.plot_post(cultivo=self.haba.pk, fecha_siembra='2026-11-10', fecha_cosecha='2027-03-10'),
        )
        self.assertEqual(plot.temporadas.count(), 1)
        # guardar el formulario de la temporada otra vez
        self.client.post(
            self.season_edit_url(self.vigente(plot)),
            self.season_post(fecha_siembra='2026-11-10', fecha_cosecha='2027-03-10'),
        )
        self.assertEqual(plot.temporadas.count(), 1)
        self.assertEqual(TemporadaCultivo.objects.count(), 1)

    def test_a_double_click_on_save_plot_does_not_create_two_plots_or_two_seasons(self):
        data = self.plot_post(cultivo=self.haba.pk)
        self.client.post(self.new_url, data)
        self.client.post(self.new_url, data)
        self.assertEqual(Potrero.objects.count(), 1)
        self.assertEqual(TemporadaCultivo.objects.count(), 1)

    def test_a_double_click_on_save_season_does_not_create_two_identical_seasons(self):
        plot = self.make_plot(cultivo=self.haba.pk, fecha_siembra='2025-11-10', fecha_cosecha='2026-03-10')
        data = self.season_post(
            crop=self.maiz, temporada_agricola='2026-2027', fecha_siembra='2026-11-10', fecha_cosecha='2027-03-10'
        )
        first = self.client.post(self.season_create_url(plot), data)
        second = self.client.post(self.season_create_url(plot), data)
        self.assertRedirects(first, self.detail_url)
        self.assertRedirects(second, self.detail_url)
        self.assertEqual(plot.temporadas.count(), 2)

    def test_a_legacy_plot_gets_one_season_when_its_planning_is_completed(self):
        plot = Potrero.objects.create(
            predio=self.predio,
            nombre='Antiguo',
            superficie_ha=2,
            cultivo=self.haba,
            variedad='Vieja',
            fecha_siembra=date(2026, 11, 10),
        )
        self.assertEqual(plot.temporadas.count(), 0)
        ui, _ = self.card(plot)
        self.assertEqual((ui['plan_done'], ui['plan_complete']), (2, False))
        self.assertEqual((ui['primary']['label'], ui['primary']['url']), ('Completar planificación', self.season_create_url(plot)))
        form = self.client.get(self.season_create_url(plot))
        html = form.content.decode()
        self.assertIn('value="2026-11-10"', html)  # lo que ya tenía el potrero llega precargado
        self.assertIn('value="Vieja"', html)
        self.client.post(
            self.season_create_url(plot),
            self.season_post(variedad='Vieja', fecha_siembra='2026-11-10', fecha_cosecha='2027-03-10'),
        )
        self.assertEqual(plot.temporadas.count(), 1)  # sin copia «cerrada» duplicada del mismo cultivo
        ui, _ = self.card(plot)
        self.assertEqual((ui['plan_done'], ui['primary']['label']), (3, 'Calcular semillas'))


class LegacyCompatibilityTests(PlanningBase):
    def test_plots_without_seasons_still_get_their_progress_from_their_own_data(self):
        full = Potrero.objects.create(
            predio=self.predio,
            nombre='Completo',
            superficie_ha=1,
            cultivo=self.haba,
            fecha_siembra=date(2026, 11, 10),
            fecha_cosecha=date(2027, 3, 10),
        )
        bare = Potrero.objects.create(predio=self.predio, nombre='Vacío', superficie_ha=1)
        ui, _ = self.card(full)
        self.assertEqual((ui['plan_done'], ui['plan_complete'], ui['primary']['label']), (3, True, 'Calcular semillas'))
        ui, _ = self.card(bare)
        self.assertEqual((ui['plan_done'], ui['primary']['label']), (0, 'Asignar cultivo'))

    def test_planning_state_is_computed_in_one_place(self):
        plot = Potrero.objects.create(predio=self.predio, nombre='Estado', superficie_ha=1, cultivo=self.haba)
        state = planning_state(plot)
        self.assertEqual((state['done'], state['total'], state['complete'], state['season']), (1, 3, False, None))
        self.assertEqual(state['source'], 'plot')  # potrero antiguo: sin temporadas, respaldo en sus propios datos
        # editar el potrero antiguo sin cambiar el cultivo no inventa temporadas
        self.client.post(self.edit_plot_url(plot), self.plot_post(nombre='Estado', cultivo=self.haba.pk, variedad='x'))
        self.assertEqual(plot.temporadas.count(), 0)
        # al completar la planificación nace su temporada y pasa a ser la fuente
        self.client.post(
            self.season_create_url(plot), self.season_post(fecha_siembra='2026-11-10', fecha_cosecha='2027-03-10')
        )
        season = self.vigente(plot)
        state = planning_state(Potrero.objects.get(pk=plot.pk))
        self.assertEqual((state['source'], state['season'].pk), ('season', season.pk))
        self.assertEqual((state['steps'], state['complete']), ([True, True, True], True))
        self.assertEqual((state['sowing_date'], state['harvest_date']), (date(2026, 11, 10), date(2027, 3, 10)))

    def test_a_season_without_dates_wins_over_stale_plot_dates_and_the_form_prefills_them(self):
        """Dato antiguo del error original: el potrero tiene fechas y la temporada no."""
        plot = Potrero.objects.create(
            predio=self.predio,
            nombre='Desfasado',
            superficie_ha=1,
            cultivo=self.haba,
            fecha_siembra=date(2026, 11, 10),
            fecha_cosecha=date(2027, 3, 10),
        )
        season = TemporadaCultivo.objects.create(potrero=plot, cultivo=self.haba, temporada_agricola='2026-2027')
        ui, response = self.card(plot)
        self.assertEqual((ui['plan_done'], ui['primary']['label']), (1, 'Completar planificación'))
        self.assertEqual(ui['primary']['url'], self.season_edit_url(season))
        html = self.client.get(ui['primary']['url']).content.decode()
        self.assertIn('value="2026-11-10"', html)
        self.assertIn('value="2027-03-10"', html)
        self.client.post(
            ui['primary']['url'], self.season_post(fecha_siembra='2026-11-10', fecha_cosecha='2027-03-10')
        )
        ui, _ = self.card(plot)
        self.assertEqual((ui['plan_done'], ui['primary']['label']), (3, 'Calcular semillas'))
        self.assertEqual(plot.temporadas.count(), 1)


class PlotFormShowsTheSeasonTests(PlanningBase):
    def test_the_plot_form_shows_the_dates_of_the_current_season_not_a_stale_copy(self):
        plot = Potrero.objects.create(
            predio=self.predio,
            nombre='Desfasado',
            superficie_ha=1,
            cultivo=self.haba,
            variedad='Copia vieja',
            fecha_siembra=date(2020, 1, 1),
            fecha_cosecha=date(2020, 6, 1),
        )
        TemporadaCultivo.objects.create(
            potrero=plot,
            cultivo=self.haba,
            variedad='Aguadulce',
            temporada_agricola='2026-2027',
            fecha_siembra=date(2026, 11, 10),
            fecha_cosecha=date(2027, 3, 10),
        )
        html = self.client.get(self.edit_plot_url(plot)).content.decode()
        self.assertIn('value="2026-11-10"', html)
        self.assertIn('value="2027-03-10"', html)
        self.assertIn('value="Aguadulce"', html)
        self.assertNotIn('value="2020-01-01"', html)
        self.assertNotIn('Copia vieja', html)

    def test_saving_the_plot_form_untouched_leaves_the_season_exactly_as_it_was(self):
        plot = self.make_plot(cultivo=self.haba.pk)
        self.complete_planning(plot)
        season = self.vigente(plot)
        before = (season.fecha_siembra, season.fecha_cosecha, season.variedad, season.estado, season.pk)
        form = self.client.get(self.edit_plot_url(plot)).context['form']
        self.client.post(
            self.edit_plot_url(plot),
            self.plot_post(
                cultivo=self.haba.pk,
                variedad=form['variedad'].value() or '',
                fecha_siembra=form['fecha_siembra'].value(),
                fecha_cosecha=form['fecha_cosecha'].value(),
            ),
        )
        season.refresh_from_db()
        self.assertEqual((season.fecha_siembra, season.fecha_cosecha, season.variedad, season.estado, season.pk), before)
        self.assertEqual(plot.temporadas.count(), 1)


class CalendarListTests(PlanningBase):
    def test_the_missing_data_list_of_the_calendar_opens_the_season_form(self):
        plot = self.make_plot(cultivo=self.haba.pk)
        season = self.vigente(plot)

        def link_of(cta):
            tasks = self.client.get('/calendario/').context['next_steps']['tasks']
            return next(task['url'] for task in tasks if task['cta'] == cta)

        self.assertEqual(link_of('Registrar fecha'), self.season_edit_url(season))
        self.complete_planning(plot, sowing='2999-11-10', harvest='')
        self.assertEqual(link_of('Agregar fecha'), self.season_edit_url(season))


class OwnerOnlyTests(PlanningBase):
    def setUp(self):
        super().setUp()
        self.plot = self.make_plot(cultivo=self.haba.pk)
        self.season = self.vigente(self.plot)
        self.stranger = self.client_class()
        self.stranger.force_login(self.other)

    def test_another_user_cannot_open_or_change_the_season_forms(self):
        for url in (self.season_edit_url(self.season), self.season_create_url(self.plot)):
            self.assertEqual(self.stranger.get(url).status_code, 404, url)
            self.assertEqual(self.stranger.post(url, self.season_post(fecha_siembra='2026-11-10')).status_code, 404, url)
        self.assertEqual(self.stranger.get(self.detail_url).status_code, 404)
        self.season.refresh_from_db()
        self.plot.refresh_from_db()
        self.assertIsNone(self.season.fecha_siembra)
        self.assertIsNone(self.plot.fecha_siembra)
        self.assertEqual(TemporadaCultivo.objects.count(), 1)

    def test_a_season_cannot_be_reached_through_another_plot(self):
        other_farm = Predio.objects.create(propietario=self.other, nombre='Ajeno', ubicacion='X', superficie_ha=3)
        foreign = Potrero.objects.create(predio=other_farm, nombre='Ajeno', superficie_ha=1, cultivo=self.maiz)
        foreign_season = TemporadaCultivo.objects.create(potrero=foreign, cultivo=self.maiz)
        # mi propio predio y potrero con la temporada de otro: no existe esa combinación
        url = f'/predios/{self.predio.pk}/potreros/{self.plot.pk}/temporadas/{foreign_season.pk}/editar/'
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, self.season_post(fecha_siembra='2026-11-10')).status_code, 404)
        foreign_season.refresh_from_db()
        self.assertIsNone(foreign_season.fecha_siembra)

    def test_a_foreign_plot_cannot_be_reached_through_my_farm_url(self):
        other_farm = Predio.objects.create(propietario=self.other, nombre='Ajeno', ubicacion='X', superficie_ha=3)
        foreign = Potrero.objects.create(predio=other_farm, nombre='Ajeno', superficie_ha=1, cultivo=self.maiz)
        foreign_season = TemporadaCultivo.objects.create(potrero=foreign, cultivo=self.maiz)
        urls = (
            f'/predios/{self.predio.pk}/potreros/{foreign.pk}/temporadas/{foreign_season.pk}/editar/',
            f'/predios/{self.predio.pk}/potreros/{foreign.pk}/temporadas/nueva/',
            f'/predios/{other_farm.pk}/potreros/{foreign.pk}/temporadas/{foreign_season.pk}/editar/',
            f'/predios/{other_farm.pk}/potreros/{foreign.pk}/temporadas/nueva/',
        )
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 404, url)
            self.assertEqual(self.client.post(url, self.season_post(fecha_siembra='2026-11-10')).status_code, 404, url)
        foreign_season.refresh_from_db()
        foreign.refresh_from_db()
        self.assertIsNone(foreign_season.fecha_siembra)
        self.assertEqual(foreign.temporadas.count(), 1)

    def test_my_page_never_shows_planning_data_of_other_users(self):
        other_farm = Predio.objects.create(propietario=self.other, nombre='Ajeno', ubicacion='X', superficie_ha=3)
        foreign = Potrero.objects.create(predio=other_farm, nombre='Lote ajeno', superficie_ha=1, cultivo=self.maiz)
        TemporadaCultivo.objects.create(potrero=foreign, cultivo=self.maiz, temporada_agricola='2026-2027')
        response = self.client.get(self.detail_url)
        self.assertNotContains(response, 'Lote ajeno')
        from core.views import farm_payload

        self.assertNotIn('Lote ajeno', str(farm_payload(self.user)))


class FrontEndContractTests(TestCase):
    def test_the_map_panel_no_longer_hardcodes_the_plot_form_for_incomplete_planning(self):
        from pathlib import Path

        js = (Path(__file__).resolve().parent.parent / 'static' / 'js' / 'predios-map.js').read_text(encoding='utf-8')
        self.assertIn("el('a', 'btn-primary-agro btn-sm', plot.primaryLabel || 'Calcular semillas')", js)
        self.assertIn('plotNext.href = plot.primaryUrl || plot.seedsUrl;', js)
        self.assertIn('href: plot.primaryUrl || plot.planFormUrl || plot.editUrl', js)
        self.assertNotIn("!plot.sowing ? 'Continuar planificación'", js)

    def test_the_season_form_explains_that_the_current_season_updates_the_plot(self):
        user = User.objects.create_user('texto', email='t@example.com', password=PASSWORD)
        crop = Cultivo.objects.create(nombre='Cultivo texto')
        farm = Predio.objects.create(propietario=user, nombre='P', ubicacion='X', superficie_ha=3)
        plot = Potrero.objects.create(predio=farm, nombre='L', superficie_ha=1, cultivo=crop)
        self.client.force_login(user)
        html = self.client.get(reverse('temporada_create', kwargs={'predio_pk': farm.pk, 'potrero_pk': plot.pk})).content.decode()
        self.assertNotIn('no reemplaza los datos del potrero', html)
        self.assertIn('temporada vigente', html)
        self.assertIn('temporadas anteriores', html)
