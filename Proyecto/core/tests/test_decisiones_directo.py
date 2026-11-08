"""Centro de decisiones y calculadoras: arrancan directo en la herramienta, sin la ruta de seis pasos.

Se eliminó del Centro de decisiones el bloque superior (barra de seis pasos, «Tu predio ya está registrado»,
«¿Cómo quieres trabajar este terreno?», «Utilizar todo el predio» / «Dividir en potreros» y los requisitos para
calcular), junto con las estadísticas decorativas y el encabezado repetido. Aquí se comprueba que ese bloque ya no se
dibuja, que el resto de las páginas conserva su ruta y que la calculadora sigue funcionando.
"""

import json
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase

from core import views
from core.models import CalculoAgricola, Cultivo, Insumo, Potrero, Predio, TipoInsumo

User = get_user_model()
PASSWORD = 'Clave-segura-123'
STATIC = Path(__file__).resolve().parent.parent / 'static'

# Texto del bloque retirado: no debe quedar en ninguna pantalla del Centro de decisiones.
BLOCK_TEXTS = (
    'Tu predio ya está registrado',
    '¿Cómo quieres trabajar este terreno?',
    'Utilizar todo el predio',
    'Dividir en potreros',
    'Para calcular necesitas',
    'Comienza registrando tu predio',
    'Siguiente paso',
)
# Marcado muerto que antes se ocultaba con CSS y ahora ni se renderiza.
DEAD_MARKUP = (
    'class="journey',
    'decision-head',
    'decision-next',
    'decision-empty',
    'has-no-plots',
    'info-strip',
    'calc-strip',
    'id="calc-stats"',
    'id="ctx-line"',
    'class="calc-head"',
    'tool-card-tag',
    'data-flow-strip',
)
URLS = ('/decisiones/', '/decisiones/?herramienta=alcanza', '/decisiones/?tipo=insumos')


def visible(response):
    """Solo el HTML que se ve: los JSON de los <script> no cuentan como contenido de la pantalla."""
    return re.sub(r'<script.*?</script>', '', response.content.decode(), flags=re.S)


def source(kind, name):
    return (STATIC / kind / name).read_text(encoding='utf-8')


class DecisionesBase(TestCase):
    def setUp(self):
        self.newcomer = User.objects.create_user('sinpredio', email='sin@example.com', password=PASSWORD)
        self.only_farm = User.objects.create_user('solopredio', email='sp@example.com', password=PASSWORD)
        self.owner = User.objects.create_user('conpotrero', email='cp@example.com', password=PASSWORD)
        self.farm_without_plots = Predio.objects.create(
            propietario=self.only_farm, nombre='Predio sin potreros', ubicacion='X', superficie_ha=5
        )
        self.farm = Predio.objects.create(propietario=self.owner, nombre='Campo', ubicacion='X', superficie_ha=10)
        self.plot = Potrero.objects.create(predio=self.farm, nombre='Lote A', superficie_ha=4)
        self.crop = Cultivo.objects.create(nombre='Maíz prueba')
        tipo = TipoInsumo.objects.create(nombre='Semilla')
        Insumo.objects.create(nombre='Semilla de maíz', tipo=tipo, cultivo=self.crop)

    def login(self, user):
        self.client.force_login(user)

    def post_calc(self, **payload):
        return self.client.post(
            '/calculos/ejecutar/', data=json.dumps(payload), content_type='application/json'
        )


class NoJourneyBlockTests(DecisionesBase):
    def test_block_is_not_rendered_in_any_account_state(self):
        for user in (self.newcomer, self.only_farm, self.owner):
            self.login(user)
            for url in URLS:
                with self.subTest(user=user.username, url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    html = visible(response)
                    for text in BLOCK_TEXTS:
                        self.assertNotIn(text, html)
                    for marker in DEAD_MARKUP:
                        self.assertNotIn(marker, response.content.decode())

    def test_view_context_carries_no_journey_for_the_decision_center(self):
        for user in (self.newcomer, self.only_farm, self.owner):
            self.login(user)
            self.assertNotIn('journey', self.client.get('/decisiones/').context)

    def test_journey_is_configured_only_for_the_other_pages(self):
        self.assertEqual(views.JOURNEY_PAGES, {'dashboard', 'predios', 'calendario', 'reportes'})
        for table in (views.JOURNEY_HERE, views.JOURNEY_NOTES):
            self.assertNotIn('decisiones', table)

    def test_other_pages_keep_their_journey(self):
        self.login(self.owner)
        for url in ('/', '/predios/', '/calendario/', '/reportes/'):
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertIn('class="journey no-print"', html)
                self.assertIn('Siguiente paso', html)

    def test_removed_pieces_are_gone_from_the_static_files_too(self):
        css = source('css', 'decisiones.css')
        for selector in ('.decision-head', '.decision-next', '.decision-empty', '.calc-strip', 'ctx-line',
                         'tool-card-tag', 'has-no-plots'):
            self.assertNotIn(selector, css)
        js = ''.join(source('js', name) for name in ('calc-flow.js', 'tool-flow.js', 'calc-panel.js', 'decisiones.js'))
        for text in BLOCK_TEXTS:
            if text != 'Siguiente paso':  # «Siguiente paso: …» sigue dentro de «Tu selección» (enlace a la siembra)
                self.assertNotIn(text, js)
        for name in ('ctx-line', 'renderContextLine', 'renderStats', 'calc-stats', 'data-flow-strip'):
            self.assertNotIn(name, js)


class DirectStructureTests(DecisionesBase):
    def test_page_starts_with_tool_tabs_then_title_form_and_result(self):
        self.login(self.owner)
        html = visible(self.client.get('/decisiones/'))
        section = html[html.index('class="decision-center"') :]
        first_tag = re.search(r'<(?!section)[a-z0-9]+[^>]*>', section[1:]).group(0)
        self.assertIn('tool-tabs', first_tag)  # nada se interpone entre la página y las dos herramientas
        order = [
            section.index(marker)
            for marker in (
                'class="tool-tabs"',
                'id="calc-modes"',
                'id="calc-title"',
                'id="calc-plot"',
                'id="calc-crop"',
                'id="calc-area"',
                'id="calc-dose"',
                'id="calc-unit"',
                'id="calc-result"',
            )
        ]
        self.assertEqual(order, sorted(order))

    def test_each_tool_has_a_single_title_and_titles_follow_the_mode(self):
        self.login(self.owner)
        html = visible(self.client.get('/decisiones/'))
        self.assertEqual(html.count('<h1'), 2)  # «Calcular …» y «¿Me alcanza lo que tengo?»
        self.assertIn('<h1 id="calc-title">Calcular semillas</h1>', html)
        self.assertIn('¿Me alcanza lo que tengo?</h1>', html)
        panel = source('js', 'calc-panel.js')
        self.assertIn('Calcular insumos', panel)

    def test_no_progress_bar_or_stat_cards_remain(self):
        self.login(self.owner)
        html = visible(self.client.get('/decisiones/'))
        for step in ('Predio', 'Potreros', 'Cultivo', 'Siembra', 'Decisiones', 'Reporte'):
            self.assertNotRegex(html, rf'journey-step[^>]*>\s*{step}')
        self.assertNotIn('aria-label="Ruta de trabajo"', html)

    def test_catalog_alert_still_shows_when_the_catalog_is_incomplete(self):
        Insumo.objects.all().delete()
        self.login(self.owner)
        html = visible(self.client.get('/decisiones/'))
        self.assertIn('Catálogo en construcción', html)
        self.assertLess(html.index('catalog-alert'), html.index('class="tool-tabs"'))

    def test_result_goes_first_in_the_side_column_on_desktop_only(self):
        css = source('css', 'decisiones.css')
        block = re.sub(r'\s+', ' ', css[css.index('@media (min-width: 1081px)') :])
        self.assertIn('.decision-center .calc-side .calc-result-card { order: -1; }', block)
        self.assertIn('@media (max-width: 1080px)', css)  # una columna: pasos → resultado → resumen


class EmptyStatesInsideTheCalculatorTests(DecisionesBase):
    def test_no_farms_shows_one_card_with_one_action(self):
        self.login(self.newcomer)
        response = self.client.get('/decisiones/')
        html = response.content.decode()
        self.assertEqual(html.count('is-no-plots'), 2)  # «Cuánto llevar» y «¿Me alcanza?»
        self.assertIn('data-farm-create-url="/predios/nuevo/"', html)
        self.assertEqual(response.context['farm_data']['plots'], [])
        for name in ('calc-flow.js', 'tool-flow.js'):
            js = source('js', name)
            self.assertIn('Aún no tienes predios', js, name)
            self.assertIn('Registrar predio', js, name)

    def test_farm_without_plots_points_to_the_single_setup_action(self):
        self.login(self.only_farm)
        response = self.client.get('/decisiones/')
        html = response.content.decode()
        self.assertEqual(html.count('is-no-plots'), 2)
        self.assertEqual(response.context['farm_data']['plots'], [])
        # La indicación va dentro de la calculadora y su botón lleva a elegir cómo organizar el predio.
        self.assertIn(f'/predios/{self.farm_without_plots.pk}/?nuevo=1', html)
        for name in ('calc-flow.js', 'tool-flow.js'):
            js = source('js', name)
            self.assertIn('Aún no tienes potreros', js, name)
            self.assertIn('Definir potreros', js, name)

    def test_with_a_plot_there_is_no_setup_guidance_at_all(self):
        self.login(self.owner)
        for url in URLS + (f'/decisiones/?tipo=semillas&potrero={self.plot.pk}',):
            with self.subTest(url=url):
                response = self.client.get(url)
                html = response.content.decode()
                self.assertNotIn('is-no-plots', html)
                self.assertEqual(len(response.context['farm_data']['plots']), 1)
                for text in ('Registrar predio', 'Definir potreros', 'Dividir en potreros', 'Utilizar todo el predio'):
                    self.assertNotIn(text, visible(response))

    def test_empty_state_styles_hide_everything_but_title_card_and_button(self):
        css = re.sub(r'\s+', ' ', source('css', 'decisiones.css'))
        for selector in ('.tool-panel.is-no-plots .calc-modes', '.tool-panel.is-no-plots .calc-hint',
                         '.tool-panel.is-no-plots .calc-step-head', '.tool-panel.is-no-plots .calc-side'):
            self.assertIn(selector, css)


class FunctionalityIsPreservedTests(DecisionesBase):
    def test_form_controls_links_and_hooks_survive(self):
        self.login(self.owner)
        html = self.client.get('/decisiones/').content.decode()
        for marker in (
            'data-calculate-url="/calculos/ejecutar/"',
            'data-mode="semillas"',
            'data-mode="insumos"',
            'id="calc-plot-picker"',
            'id="calc-crop-picker"',
            'id="calc-submit"',
            'id="calc-pdf"',
            'href="/reportes/?tipo=calculos"',
            'href="/decisiones/?herramienta=alcanza"',
            'id="cover-form"',
            'data-flow-submit',
            'data-tool="llevar"',
            'data-tool="alcanza"',
        ):
            self.assertIn(marker, html)

    def test_predio_and_potrero_links_for_the_map_and_farm_pages_are_untouched(self):
        self.login(self.owner)
        html = self.client.get('/decisiones/').content.decode()
        self.assertIn('setupUrl', html)
        self.assertIn(f'/predios/{self.farm.pk}/', html)
        self.assertEqual(self.client.get(f'/predios/{self.farm.pk}/?nuevo=1').status_code, 200)
        self.assertEqual(self.client.get('/predios/nuevo/').status_code, 200)

    def test_query_parameters_still_preselect_plot_and_tool(self):
        self.login(self.owner)
        for url in (
            f'/decisiones/?tipo=semillas&potrero={self.plot.pk}',
            f'/decisiones/?tipo=insumos&potrero={self.plot.pk}',
            f'/decisiones/?herramienta=alcanza&potrero={self.plot.pk}',
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/decisiones/?herramienta=alcanza').context['initial_tool'], 'alcanza')
        self.assertEqual(self.client.get('/decisiones/?tipo=semillas').context['initial_tool'], 'llevar')

    def test_calculation_still_runs_and_is_saved_for_the_reports(self):
        self.login(self.owner)
        response = self.post_calc(
            operation='dose',
            calculation_type='semillas',
            plot_id=self.plot.pk,
            crop=self.crop.nombre,
            area='3',
            dose='20',
            unit='kg/ha',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['amount'], 60)
        saved = CalculoAgricola.objects.get(usuario=self.owner)
        self.assertEqual(saved.potrero, self.plot)
        self.assertIn(saved.resultado['amount'], (60, '60'))
        self.assertEqual(self.client.get('/reportes/?tipo=calculos').status_code, 200)

    def test_calculation_is_still_scoped_to_the_signed_in_account(self):
        self.login(self.newcomer)
        response = self.post_calc(
            operation='dose', plot_id=self.plot.pk, crop=self.crop.nombre, area='1', dose='4', unit='kg/ha'
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CalculoAgricola.objects.exists())
