"""Registrar y editar potrero: datos a la izquierda; herramientas, mapa y superficie a la derecha.

Cambio solo de presentación: mismos campos, botones, ids y reglas. El <form> envuelve las dos columnas para que la
superficie (que vive bajo el mapa, en un único bloque) y «Guardar potrero» se sigan enviando juntos.
"""

import re
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Cultivo, Potrero, Predio

User = get_user_model()
PASSWORD = 'Clave-segura-123'
CORE = Path(__file__).resolve().parent.parent
CSS = (CORE / 'static' / 'css' / 'predios.css').read_text(encoding='utf-8').replace('\r\n', '\n')
JS = (CORE / 'static' / 'js' / 'predios-map.js').read_text(encoding='utf-8').replace('\r\n', '\n')
BLOCK_START = 'Potrero: datos a la izquierda, mapa y superficie a la derecha'

# Todo lo que existía en la pantalla y debe seguir existiendo, con el mismo id.
FIELD_IDS = (
    'id_nombre',
    'id_cultivo',
    'id_variedad',
    'id_fecha_siembra',
    'id_fecha_cosecha',
    'id_superficie_ha',
    'id_latitud',
    'id_longitud',
    'id_perimetro',
)
BUTTON_IDS = (
    'draw-perimeter',
    'undo-point',
    'finish-perimeter',
    'finish-line',
    'cancel-drawing',
    'toggle-edit',
    'clear-perimeter',
    'measure-line',
    'clear-measurement',
    'locate-me',
    'center-map',
)


def section(html, marker, until):
    start = html.index(marker)
    return html[start : html.index(until, start)]


class PotreroTwoColumnsBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('agricultor', email='a@example.com', password=PASSWORD)
        self.crop = Cultivo.objects.create(nombre='Zanahoria prueba')
        self.predio = Predio.objects.create(
            propietario=self.user, nombre='Mi campo', ubicacion='Melipilla', superficie_ha=10
        )
        self.potrero = Potrero.objects.create(predio=self.predio, nombre='Lote A', superficie_ha=2, cultivo=self.crop)
        self.client.force_login(self.user)
        self.new_url = f'/predios/{self.predio.pk}/potreros/nuevo/'
        self.edit_url = f'/predios/{self.predio.pk}/potreros/{self.potrero.pk}/editar/'

    def page(self, url=None):
        response = self.client.get(url or self.new_url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def data(self, **values):
        return {
            'nombre': 'Lote B',
            'cultivo': self.crop.pk,
            'variedad': 'Nantes',
            'superficie_ha': '1.5',
            'fecha_siembra': '2026-11-10',
            'fecha_cosecha': '2027-03-10',
            'latitud': '-33.6',
            'longitud': '-71.2',
            'perimetro': '[]',
            **values,
        }


class LayoutOrderTests(PotreroTwoColumnsBase):
    def test_left_column_is_what_to_plant_and_right_column_is_tools_map_then_surface(self):
        for url in (self.new_url, self.edit_url):
            with self.subTest(url=url):
                html = self.page(url)
                order = [
                    html.index(marker)
                    for marker in (
                        '1 · ¿Qué vas a plantar?',
                        'id="id_nombre"',
                        'id="id_cultivo"',
                        'Agregar más detalles',
                        'id="id_variedad"',
                        'id="id_fecha_siembra"',
                        'id="id_fecha_cosecha"',
                        '2 · ¿Dónde queda?',
                        'id="draw-perimeter"',
                        'id="farm-map"',
                        'id="id_superficie_ha"',
                        'Ubicación exacta (avanzado)',
                        'id="id_latitud"',
                        'id="id_longitud"',
                        'Guardar potrero',
                    )
                ]
                self.assertEqual(order, sorted(order))

    def test_each_field_lives_in_its_own_column(self):
        html = self.page(self.edit_url)
        plant = section(html, 'potrero-plant-card', 'potrero-where-card')
        where = section(html, 'potrero-where-card', 'potrero-footer')
        for field in ('id_nombre', 'id_cultivo', 'id_variedad', 'id_fecha_siembra', 'id_fecha_cosecha'):
            self.assertIn(f'id="{field}"', plant)
            self.assertNotIn(f'id="{field}"', where)
        for field in ('id_superficie_ha', 'farm-map', 'draw-perimeter', 'toggle-edit', 'clear-perimeter',
                      'measure-line', 'locate-me', 'center-map', 'live-area', 'map-status'):
            self.assertIn(f'id="{field}"', where)
            self.assertNotIn(f'id="{field}"', plant)
        self.assertIn('data-fill-available', where)  # «Usar lo que queda» acompaña a la superficie
        # El potrero y su predio se identifican arriba de la tarjeta de datos, no en el encabezado de la página.
        self.assertIn('potrero-identity', plant)
        self.assertIn('Predio: <strong>Mi campo</strong>', plant)
        self.assertNotIn('Predio: <strong>', where)

    def test_location_details_and_save_close_the_form(self):
        html = self.page()
        footer = html[html.index('potrero-footer') :]
        self.assertIn('Ubicación exacta (avanzado)', footer)
        self.assertIn('id="id_latitud"', footer)
        self.assertIn('id="id_longitud"', footer)
        self.assertEqual(html.count('Guardar potrero'), 1)
        self.assertTrue(footer.index('Ubicación exacta') < footer.index('Guardar potrero') < footer.index('</form>'))

    def test_collapsible_sections_keep_their_default_state(self):
        html = self.page()
        details = re.findall(r'<details class="more-details[^"]*"([^>]*)>', html)
        self.assertEqual(len(details), 2)  # «Agregar más detalles» y «Ubicación exacta (avanzado)»
        self.assertFalse(any('open' in attrs for attrs in details))
        # Con errores en sus campos se abren solos, como antes.
        bad = self.client.post(self.new_url, self.data(fecha_siembra='no-es-fecha', latitud='999'))
        opened = re.findall(r'<details class="more-details[^"]*"[^>]*open[^>]*>', bad.content.decode())
        self.assertEqual(len(opened), 2)


class NothingNewTests(PotreroTwoColumnsBase):
    def test_same_elements_and_nothing_added(self):
        html = self.page()
        for element_id in FIELD_IDS + BUTTON_IDS + ('farm-map', 'map-status', 'live-perimeter', 'live-area', 'terrain-form'):
            self.assertEqual(html.count(f'id="{element_id}"'), 1, element_id)
        for added in ('<progress', 'role="progressbar"', 'step-indicator', 'summary-card', 'kpi', 'stat-card',
                      'Siguiente paso', 'Recomendación', 'Resumen de planificación'):
            self.assertNotIn(added, html)
        # Fuera del <form> solo quedan los formularios del marco general (buscador y salir): el de la página es uno solo.
        self.assertEqual(html.count('<form'), 3)
        form = section(html, 'id="terrain-form"', '</form>')
        self.assertEqual(form.count('<form'), 0)
        self.assertEqual(form.count('type="submit"'), 1)
        self.assertEqual(html.count('2 · ¿Dónde queda?'), 1)
        self.assertEqual(html.count('1 · ¿Qué vas a plantar?'), 1)

    def test_every_field_and_the_save_button_belong_to_the_same_form(self):
        html = self.page(self.edit_url)
        form = section(html, 'id="terrain-form"', '</form>')
        for element_id in FIELD_IDS + ('farm-map', 'draw-perimeter'):
            self.assertIn(f'id="{element_id}"', form)
        self.assertIn('type="submit"', form)
        self.assertIn('csrfmiddlewaretoken', form)

    def test_toolbar_buttons_never_submit_the_form(self):
        html = self.page()
        toolbar = section(html, 'role="toolbar"', 'id="farm-map"')
        for button in re.findall(r'<button[^>]*>', toolbar):
            self.assertIn('type="button"', button)

    def test_predio_form_is_untouched(self):
        html = self.client.get('/predios/nuevo/').content.decode()
        self.assertIn('is-predio-layout', html)
        self.assertNotIn('potrero-form', html)
        self.assertNotIn('is-potrero-heading', html)
        self.assertIn('Guardar predio', html)
        edit = self.client.get(f'/predios/{self.predio.pk}/editar/').content.decode()
        self.assertNotIn('potrero-form', edit)
        self.assertIn('Guardar predio', edit)

    def test_full_predio_still_shows_the_no_space_notice_instead_of_the_form(self):
        Potrero.objects.create(predio=self.predio, nombre='Resto', superficie_ha=Decimal('8'))
        html = self.page()
        self.assertIn('Este predio ya no tiene superficie libre', html)
        self.assertNotIn('id="terrain-form"', html)


class SavingStillWorksTests(PotreroTwoColumnsBase):
    def test_create_saves_every_field_from_both_columns(self):
        response = self.client.post(self.new_url, self.data())
        self.assertRedirects(response, f'/predios/{self.predio.pk}/')
        saved = Potrero.objects.get(nombre='Lote B')
        self.assertEqual(saved.superficie_ha, Decimal('1.5'))
        self.assertEqual(saved.cultivo, self.crop)
        self.assertEqual(saved.variedad, 'Nantes')
        self.assertEqual(str(saved.fecha_siembra), '2026-11-10')
        self.assertEqual(str(saved.fecha_cosecha), '2027-03-10')
        self.assertEqual((saved.latitud, saved.longitud), (-33.6, -71.2))

    def test_edit_updates_the_potrero(self):
        response = self.client.post(self.edit_url, self.data(nombre='Lote A editado', superficie_ha='3'))
        self.assertRedirects(response, f'/predios/{self.predio.pk}/')
        self.potrero.refresh_from_db()
        self.assertEqual((self.potrero.nombre, self.potrero.superficie_ha), ('Lote A editado', Decimal('3')))

    def test_optional_fields_stay_optional(self):
        response = self.client.post(
            self.new_url,
            self.data(nombre='Mínimo', cultivo='', variedad='', fecha_siembra='', fecha_cosecha='', latitud='', longitud=''),
        )
        self.assertRedirects(response, f'/predios/{self.predio.pk}/')
        saved = Potrero.objects.get(nombre='Mínimo')
        self.assertIsNone(saved.cultivo)
        self.assertIsNone(saved.fecha_siembra)

    def test_surface_rules_are_unchanged(self):
        for values, message in (
            ({'superficie_ha': ''}, 'Ingresa la superficie o dibuja el perímetro.'),
            ({'nombre': ''}, 'This field is required.'),
        ):
            with self.subTest(values=values):
                response = self.client.post(self.new_url, self.data(**values))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'id="terrain-form"')
        html = self.client.post(self.new_url, self.data(superficie_ha='')).content.decode()
        where = section(html, 'potrero-where-card', 'potrero-footer')
        self.assertIn('Ingresa la superficie o dibuja el perímetro.', where)  # el error queda junto a la superficie
        self.assertFalse(Potrero.objects.filter(nombre='Lote B').exists())
        too_big = self.client.post(self.new_url, self.data(superficie_ha='50'))
        self.assertEqual(too_big.status_code, 200)
        self.assertFalse(Potrero.objects.filter(nombre='Lote B').exists())

    def test_surface_keeps_the_remaining_area_limit_for_the_browser(self):
        response = self.client.get(self.new_url)
        self.assertEqual(response.context['form'].fields['superficie_ha'].widget.attrs['max'], '8.0000')
        self.assertContains(response, 'Usar lo que queda (8,00 ha)')

    def test_other_accounts_cannot_open_the_form(self):
        other = User.objects.create_user('otro', email='o@example.com', password=PASSWORD)
        self.client.force_login(other)
        self.assertEqual(self.client.get(self.new_url).status_code, 404)
        self.assertEqual(self.client.get(self.edit_url).status_code, 404)


class SurfaceBlockTests(PotreroTwoColumnsBase):
    def test_surface_is_one_highlighted_block_under_the_map(self):
        for url in (self.new_url, self.edit_url):
            with self.subTest(url=url):
                html = self.page(url)
                where = section(html, 'potrero-where-card', 'potrero-footer')
                self.assertEqual(where.count('class="potrero-area"'), 1)
                area = section(where, 'class="potrero-area"', '</section>')
                self.assertIn('for="id_superficie_ha">Superficie (ha)</label>', area)
                self.assertIn('class="area-input"', area)
                self.assertIn('id="id_superficie_ha"', area)
                self.assertIn('data-fill-available', area)
                self.assertLess(where.index('id="farm-map"'), where.index('class="potrero-area"'))
                self.assertEqual(html.count('id="id_superficie_ha"'), 1)
                self.assertEqual(html.count('data-fill-available'), 1)

    def test_long_explanations_and_repeated_hints_are_gone(self):
        html = self.page()
        for text in (
            'Escríbela, o dibuja tu potrero en el mapa y se calcula sola.',
            'Puedes dejarlo vacío y elegirlo después.',
            'Opcional: dibuja tu potrero dentro del límite verde',
        ):
            self.assertNotIn(text, html)
        self.assertIn('Dibuja tu potrero dentro del límite verde, o escribe las hectáreas abajo.', html)
        self.assertEqual(html.count('Usar lo que queda'), 1)

    def test_measurement_readout_stays_in_the_page_for_the_map_script(self):
        html = self.page()
        readout = section(html, 'class="measurement-readout"', '</div>')
        self.assertIn('aria-live="polite"', readout)
        self.assertIn('id="live-perimeter"', readout)
        self.assertIn('id="live-area"', readout)

    def test_server_error_for_the_surface_appears_inside_the_surface_block(self):
        html = self.client.post(self.new_url, self.data(superficie_ha='')).content.decode()
        area = section(html, 'class="potrero-area"', '</section>')
        self.assertIn('Ingresa la superficie o dibuja el perímetro.', area)


class IdentityHeaderTests(PotreroTwoColumnsBase):
    def test_editing_names_the_potrero_and_its_predio(self):
        identity = section(self.page(self.edit_url), 'class="potrero-identity"', 'class="potrero-body"')
        self.assertIn('>Lote A</strong>', identity)
        self.assertIn('Predio: <strong>Mi campo</strong>', identity)
        self.assertRegex(identity, r'quedan \d+,\d{2} ha disponibles')

    def test_creating_says_it_is_a_new_potrero(self):
        identity = section(self.page(), 'class="potrero-identity"', 'class="potrero-body"')
        self.assertIn('>Potrero nuevo</strong>', identity)
        self.assertIn('Predio: <strong>Mi campo</strong> · quedan 8,00 ha disponibles', identity)

    def test_after_a_server_error_the_header_keeps_the_saved_name(self):
        response = self.client.post(self.edit_url, self.data(nombre='Nombre sin guardar', superficie_ha=''))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        identity = section(html, 'class="potrero-identity"', 'class="potrero-body"')
        self.assertIn('>Lote A</strong>', identity)
        self.assertNotIn('Nombre sin guardar', identity)
        self.assertIn('value="Nombre sin guardar"', html)  # lo escrito en el campo no se pierde

    def test_page_heading_is_just_the_title_for_potreros_and_unchanged_for_predios(self):
        html = self.page(self.edit_url)
        heading = html[html.index('terrain-page-heading') : html.index('id="terrain-form"')]
        self.assertIn('Editar potrero', heading)
        for gone in ('page-sub', 'terrain-parent', 'quedan'):
            self.assertNotIn(gone, heading)
        self.assertIn('Dibuja el contorno de tu campo en el mapa.', self.client.get('/predios/nuevo/').content.decode())
        self.assertIn(
            'Corrige el contorno o los datos de tu predio.',
            self.client.get(f'/predios/{self.predio.pk}/editar/').content.decode(),
        )


class MoreToolsMenuTests(PotreroTwoColumnsBase):
    def test_every_auxiliary_tool_is_still_inside_more_tools(self):
        tools = section(self.page(), 'class="more-tools"', '</details>')
        for element_id in ('measure-line', 'clear-measurement', 'locate-me', 'center-map'):
            self.assertIn(f'id="{element_id}"', tools)

    def test_script_closes_the_floating_menu_without_touching_the_map_logic(self):
        snippet = JS[JS.index("document.querySelector('.potrero-form .more-tools')") :]
        self.assertIn("event.key !== 'Escape'", snippet)
        self.assertIn('menu.open = false', snippet)
        for map_logic in ('currentLayer', 'areaInput', 'geojson', 'setStatus', 'map.', 'L.'):
            self.assertNotIn(map_logic, snippet)


class StyleScopeTests(PotreroTwoColumnsBase):
    def block(self):
        start = CSS.rindex('/*', 0, CSS.index(BLOCK_START))
        # El bloque del potrero termina donde empieza el siguiente (el de «Registrar predio»).
        end = CSS.rindex('/*', 0, CSS.index('Registrar / editar predio: mapa a la izquierda'))
        return CSS[start:end]

    def test_new_rules_are_scoped_to_the_potrero_form(self):
        selectors = []
        for chunk in re.split(r'}\s*', re.sub(r'/\*.*?\*/', '', self.block(), flags=re.S)):
            if '{' not in chunk:
                continue
            head = chunk.split('{')[0].strip()
            if head.startswith('@media'):
                head = chunk.split('{')[1].strip()
            selectors += [part.strip() for part in head.split(',') if part.strip()]
        self.assertGreater(len(selectors), 40)
        for selector in selectors:
            self.assertRegex(selector, r'potrero-form|is-potrero-heading|potrero-identity', selector)

    def test_two_columns_only_on_wide_screens_and_one_column_below(self):
        css = re.sub(r'\s+', ' ', self.block())
        self.assertIn('#terrain-form.potrero-form { display: grid; grid-template-columns: minmax(0, 1fr);', css)
        wide = css[css.index('@media (min-width: 1100px)') :]
        self.assertIn('grid-template-columns: minmax(340px, min(32%, 420px)) minmax(0, 1fr);', wide)
        self.assertIn('grid-template-rows: 1fr auto;', wide)
        self.assertIn('.potrero-form .potrero-where-card { grid-column: 2; grid-row: 1 / span 2; }', wide)
        self.assertIn('.potrero-form .potrero-footer { grid-column: 1; grid-row: 2;', wide)

    def test_surface_block_is_highlighted_and_hides_the_shortcut_that_no_longer_applies(self):
        css = re.sub(r'\s+', ' ', self.block())
        self.assertIn('.potrero-form .potrero-area { display: grid;', css)
        self.assertIn('.potrero-form .potrero-area:has(input[readonly]) .link-btn { display: none; }', css)
        self.assertIn('font-size: 1.5rem; font-weight: 800;', css)

    def test_repeated_measurement_text_is_hidden_only_when_it_is_neither_an_error_nor_a_line_measurement(self):
        css = re.sub(r'\s+', ' ', self.block())
        selector = (
            '.potrero-form:not(:has(#measure-line.is-active)):not(:has(#clear-measurement:not([hidden]))) '
            '#live-area:not(.is-error) {'
        )
        self.assertIn(selector, css)
        rule = css[css.index(selector) : css.index('}', css.index(selector))]
        self.assertIn('clip: rect(0 0 0 0)', rule)
        self.assertNotIn('display: none', rule)  # sigue anunciándose a lectores de pantalla

    def test_more_tools_is_a_floating_menu_above_the_leaflet_controls(self):
        css = re.sub(r'\s+', ' ', self.block())
        rule = css[css.index('.potrero-form .more-tools .tool-group-aux {') :]
        rule = rule[: rule.index('}')]
        self.assertIn('position: absolute;', rule)
        self.assertRegex(rule, r'z-index: 1[1-9]\d\d;')  # Leaflet usa hasta 1000

    def test_map_height_follows_the_screen_within_limits(self):
        css = re.sub(r'\s+', ' ', self.block())
        self.assertIn('height: clamp(340px, calc(100vh - 540px), 420px);', css)

    def test_stylesheet_and_script_versions_were_bumped_so_browsers_reload_them(self):
        html = self.page()
        self.assertRegex(html, r'predios\.css\?v=(3[4-9]|[4-9]\d)')
        self.assertRegex(html, r'predios-map\.js\?v=(2[7-9]|[3-9]\d)')
