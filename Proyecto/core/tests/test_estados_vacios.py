"""Estados vacíos compactos: una cuenta sin predios ve una sola guía por pantalla, no avisos repetidos."""

import re

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import ActividadAgricola, Cultivo, Potrero, Predio

User = get_user_model()
PASSWORD = 'Clave-segura-123'


def visible(response):
    """Solo el HTML que se ve: los JSON de los <script> no cuentan como contenido de la pantalla."""
    return re.sub(r'<script.*?</script>', '', response.content.decode(), flags=re.S)


class NewcomerBase(TestCase):
    def setUp(self):
        self.newcomer = User.objects.create_user('sinpredios', email='sin@example.com', password=PASSWORD)
        self.owner = User.objects.create_user('conpredio', email='con@example.com', password=PASSWORD)
        self.farm = Predio.objects.create(propietario=self.owner, nombre='Campo', ubicacion='X', superficie_ha=10)
        self.plot = Potrero.objects.create(predio=self.farm, nombre='Lote A', superficie_ha=4)
        Cultivo.objects.create(nombre='Maíz prueba')
        self.client.force_login(self.newcomer)


class EmptyActivitiesTests(NewcomerBase):
    def test_empty_activities_page_is_one_card_with_one_action(self):
        html = visible(self.client.get('/actividades/'))
        self.assertEqual(html.count('Registrar primer predio'), 1)
        self.assertIn('No tienes labores', html)
        for removed in ('kpi-row', 'filter-bar', 'Plan de temporada', 'Tipos de labor', 'Cuaderno de campo'):
            self.assertNotIn(removed, html)

    def test_activities_page_with_data_keeps_filters_and_season_plan(self):
        ActividadAgricola.objects.create(
            usuario=self.owner,
            potrero=self.plot,
            nombre='Preparar suelo',
            tipo=ActividadAgricola.Tipo.PREPARACION,
            fecha_programada='2026-11-20',
        )
        self.client.force_login(self.owner)
        html = visible(self.client.get('/actividades/'))
        self.assertNotIn('No tienes labores', html)
        self.assertIn('filter-bar', html)
        self.assertIn('Plan de temporada', html)


class EmptyReportsTests(NewcomerBase):
    def test_reports_without_data_show_one_card_and_disabled_downloads(self):
        html = visible(self.client.get('/reportes/'))
        self.assertIn('Todavía no hay nada que reportar', html)
        self.assertEqual(html.count('Registrar primer predio'), 1)
        self.assertNotIn('report-types', html)
        self.assertNotIn('report-filters', html)
        self.assertNotIn('class="report-doc', html)
        self.assertEqual(len(re.findall(r'<button class="button[^"]*" type="button" disabled', html)), 2)
        self.assertNotIn('Descargar CSV</a>', html)
        self.assertIn('aria-describedby="report-empty-help"', html)
        self.assertIn('id="report-empty-help"', html)

    def test_reports_with_data_keep_real_downloads_and_filters(self):
        self.client.force_login(self.owner)
        response = self.client.get('/reportes/')
        html = visible(response)
        self.assertIn('Descargar CSV</a>', html)
        self.assertIn('report-types', html)
        self.assertIn('class="report-doc', html)
        self.assertNotIn('Todavía no hay nada que reportar', html)
        csv_response = self.client.get('/reportes/?tipo=resumen&formato=csv')
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn('text/csv', csv_response['Content-Type'])


class EmptyCalendarAndCalculatorsTests(NewcomerBase):
    def test_calendar_without_farms_has_no_zero_stats(self):
        html = visible(self.client.get('/calendario/'))
        self.assertIn('No tienes predios registrados.', html)
        self.assertNotIn('sowing-stats', html)
        self.assertEqual(html.count('Registrar primer predio'), 1)

    def test_calendar_with_farms_keeps_its_stats(self):
        self.client.force_login(self.owner)
        self.assertIn('sowing-stats', visible(self.client.get('/calendario/')))

    def test_calculators_without_plots_are_marked_so_only_the_in_calculator_card_remains(self):
        html = self.client.get('/decisiones/').content.decode()
        self.assertEqual(html.count('is-no-plots'), 2)  # «Cuánto llevar» y «¿Me alcanza?»
        self.assertIn('data-farm-create-url="/predios/nuevo/"', html)  # el botón único de la tarjeta lleva aquí
        self.assertNotIn('Comienza registrando tu predio', html)  # la guía de seis pasos ya no se dibuja en la página
        self.assertNotIn('has-no-plots', html)
        self.assertNotIn('decision-empty', html)

    def test_calculators_with_plots_are_not_collapsed(self):
        self.client.force_login(self.owner)
        html = self.client.get('/decisiones/').content.decode()
        self.assertNotIn('is-no-plots', html)
        self.assertNotIn('has-no-plots', html)
        self.assertIn('id="calc-form"', html)
        self.assertNotIn('Tu predio ya está registrado', html)

    def test_me_alcanza_is_labelled_as_a_manual_simulation(self):
        html = self.client.get('/decisiones/?herramienta=alcanza').content.decode()
        self.assertIn('no descuenta existencias ni registra inventario', html)
        self.assertNotIn('Simulación manual', html)  # el aviso va una sola vez, en el texto del formulario


class JourneyBannerTests(NewcomerBase):
    def test_banner_is_hidden_without_farms_and_shown_with_them(self):
        for url in ('/', '/predios/', '/calendario/', '/reportes/'):
            with self.subTest(url=url):
                self.assertNotIn('class="journey no-print"', self.client.get(url).content.decode())
        self.client.force_login(self.owner)
        for url in ('/', '/predios/', '/calendario/', '/reportes/'):
            with self.subTest(url=url, farm=True):
                self.assertIn('class="journey no-print"', self.client.get(url).content.decode())

    def test_decision_center_never_shows_the_banner(self):
        # El Centro de decisiones y las calculadoras arrancan directo en la herramienta, con o sin predios.
        for url in ('/decisiones/', '/decisiones/?herramienta=alcanza', '/decisiones/?tipo=insumos'):
            with self.subTest(url=url):
                self.assertNotIn('class="journey no-print"', self.client.get(url).content.decode())
        self.client.force_login(self.owner)
        for url in ('/decisiones/', '/decisiones/?herramienta=alcanza', '/decisiones/?tipo=insumos'):
            with self.subTest(url=url, farm=True):
                self.assertNotIn('class="journey no-print"', self.client.get(url).content.decode())

    def test_context_still_exposes_the_first_step_for_newcomers(self):
        journey = self.client.get('/').context['journey']
        self.assertEqual(journey['steps'][0]['state'], 'current')
        self.assertEqual(journey['next']['cta'], 'Registrar predio')
