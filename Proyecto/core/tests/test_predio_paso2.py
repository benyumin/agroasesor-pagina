"""Registrar predio, paso 2 de 3 («Confirma la medida»): mapa a la izquierda, datos a la derecha, un paso a la vez.

Cambio de presentación: mismos campos, botones, ids y reglas. Se corrige además lo que impedía el flujo previsto:
- el formulario (con «Guardar predio») se veía junto a «Confirmar y continuar» porque un display:flex anulaba su
  atributo hidden;
- «Volver a editar» / «Corregir contorno» no hacían nada sobre un contorno recién dibujado (Geoman en modo opt-in
  solo actúa sobre capas con pmIgnore:false);
- al abrir «Editar predio» el CSS escondía «Corregir contorno».
Lo visual y lo de navegador se comprueba con Playwright; aquí se protege el contrato del HTML, del CSS y del JS.
"""

import json
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Predio

User = get_user_model()
PASSWORD = 'Clave-segura-123'
CORE = Path(__file__).resolve().parent.parent
CSS = (CORE / 'static' / 'css' / 'predios.css').read_text(encoding='utf-8').replace('\r\n', '\n')
JS = (CORE / 'static' / 'js' / 'predios-map.js').read_text(encoding='utf-8').replace('\r\n', '\n')
MARKER = 'Registrar / editar predio: mapa a la izquierda'

# Todo lo que existía en la pantalla y debe seguir existiendo, una sola vez y con el mismo id.
PREDIO_IDS = (
    'terrain-form',
    'farm-map',
    'map-status',
    'step-indicator',
    'draw-perimeter',
    'undo-point',
    'finish-perimeter',
    'finish-line',
    'cancel-drawing',
    'accept-perimeter',
    'edit-perimeter',
    'cancel-perimeter',
    'measure-line',
    'clear-measurement',
    'locate-me',
    'center-map',
    'registration-prompt',
    'perimeter-confirmation',
    'confirm-area',
    'confirm-and-continue',
    'return-to-edit',
    'confirm-m2',
    'confirm-perimeter',
    'confirm-vertices',
    'map-form-summary',
    'map-summary-area',
    'map-summary-m2',
    'map-summary-perimeter',
    'map-summary-vertices',
    'map-summary-lat',
    'map-summary-lng',
    'map-summary-location',
    'id_nombre',
    'id_ubicacion',
    'id_notas',
    'id_superficie_ha',
    'id_perimetro_aceptado',
)


def block(css, start_marker):
    """Desde el comentario que abre el bloque nuevo hasta el final del archivo."""
    return css[css.rindex('/*', 0, css.index(start_marker)) :]


def selectors(css):
    """Selectores de cada regla (también dentro de @media), sin comentarios."""
    plain = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    found = []
    for chunk in re.split(r'}\s*', plain):
        if '{' not in chunk:
            continue
        head = chunk.split('{')
        head = head[1] if head[0].strip().startswith('@media') else head[0]
        found += [part.strip() for part in head.split(',') if part.strip()]
    return found


class PredioPaso2Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('agricultor', email='a@example.com', password=PASSWORD)
        self.client.force_login(self.user)
        ring = [[-71.216, -33.687], [-71.213, -33.687], [-71.213, -33.684], [-71.216, -33.684]]
        self.feature = {
            'type': 'Feature',
            'properties': {},
            'geometry': {'type': 'Polygon', 'coordinates': [ring + [ring[0]]]},
        }

    def data(self, **extra):
        return {
            'nombre': 'Fundo Paso Dos',
            'ubicacion': 'Camino de prueba',
            'geojson': json.dumps(self.feature),
            'perimetro_aceptado': 'on',
            'latitud': '-33.687',
            'longitud': '-71.216',
            **extra,
        }

    def page(self, url='/predios/nuevo/'):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class EstructuraTests(PredioPaso2Base):
    def test_page_keeps_every_element_once_and_adds_nothing(self):
        html = self.page()
        for element_id in PREDIO_IDS:
            self.assertEqual(html.count(f'id="{element_id}"'), 1, element_id)
        for text, times in (
            ('Confirmar y continuar', 1),
            ('Volver a editar', 1),
            ('Guardar predio', 1),
            ('Ver detalles técnicos', 2),
            ('Sobre este registro', 1),
            ('Agregar más detalles', 1),
            ('Tus datos se guardan de forma privada en tu cuenta.', 1),
        ):
            self.assertEqual(html.count(text), times, text)
        form = html[html.index('id="terrain-form"') :]
        self.assertEqual(form[: form.index('</form>')].count('type="submit"'), 1)
        for added in ('<progress', 'role="progressbar"', 'summary-card', 'stat-card', 'kpi', 'Siguiente paso'):
            self.assertNotIn(added, html)

    def test_form_stays_hidden_until_the_measurement_is_confirmed(self):
        html = self.page()
        self.assertIn('id="terrain-form" hidden', html)
        self.assertLess(html.index('id="perimeter-confirmation"'), html.index('id="terrain-form"'))
        self.assertLess(html.index('id="terrain-form"'), html.index('id="map-form-summary"'))
        self.assertLess(html.index('id="map-form-summary"'), html.index('Guardar predio'))

    def test_heading_carries_the_step_next_to_the_title_and_potrero_is_untouched(self):
        html = self.page()
        self.assertIn('is-predio-heading', html)
        heading = html[html.index('terrain-page-heading') : html.index('<div class="terrain-layout')]
        self.assertLess(heading.index('page-title'), heading.index('page-sub'))
        self.assertIn('id="step-indicator"', heading)
        self.assertIn('Paso 1 de 3', heading)
        parent = Predio.objects.create(propietario=self.user, nombre='Campo', ubicacion='X', superficie_ha=10)
        potrero = self.page(f'/predios/{parent.pk}/potreros/nuevo/')
        self.assertIn('is-potrero-heading', potrero)
        self.assertNotIn('is-predio-heading', potrero)
        self.assertNotIn('is-predio-layout', potrero)

    def test_edit_page_has_no_step_indicator_and_shows_the_form(self):
        predio = Predio.objects.create(
            propietario=self.user, nombre='Existente', ubicacion='Aquí', superficie_ha=3, geojson=self.feature
        )
        html = self.page(f'/predios/{predio.pk}/editar/')
        self.assertNotIn('id="step-indicator"', html)
        self.assertNotIn('id="terrain-form" hidden', html)
        self.assertIn('data-mode="edit"', html)
        self.assertIn('id="map-form-summary"', html)
        self.assertIn('is-predio-heading', html)
        self.assertIn('Corrige el contorno o los datos de tu predio.', html)

    def test_assets_are_versioned_so_browsers_reload_them(self):
        html = self.page()
        self.assertRegex(html, r'predios\.css\?v=(3[3-9]|[4-9]\d)')
        self.assertRegex(html, r'predios-map\.js\?v=(2[6-9]|[3-9]\d)')


class GuardadoTests(PredioPaso2Base):
    def test_valid_registration_saves_and_goes_to_the_new_predio(self):
        response = self.client.post('/predios/nuevo/', self.data())
        predio = Predio.objects.get(nombre='Fundo Paso Dos')
        self.assertRedirects(response, f'/predios/{predio.pk}/?nuevo=1')
        self.assertEqual(predio.ubicacion, 'Camino de prueba')
        self.assertEqual(predio.propietario, self.user)
        self.assertGreater(predio.superficie_ha, 0)
        self.assertEqual(predio.geojson['geometry']['type'], 'Polygon')

    def test_fields_are_still_validated(self):
        for values, field in (({'nombre': ''}, 'nombre'), ({'ubicacion': ''}, 'ubicacion')):
            with self.subTest(field=field):
                response = self.client.post('/predios/nuevo/', self.data(**values))
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context['form'].errors)
        self.assertEqual(Predio.objects.count(), 0)

    def test_unaccepted_perimeter_is_rejected_and_the_form_comes_back_open(self):
        response = self.client.post('/predios/nuevo/', self.data(perimetro_aceptado=''))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Predio.objects.count(), 0)
        self.assertContains(response, 'Acepta el perímetro antes de registrar el predio.')
        self.assertTrue(response.context['registration_open'])
        html = response.content.decode()
        self.assertNotIn('id="terrain-form" hidden', html)

    def test_registration_without_drawing_is_rejected(self):
        response = self.client.post('/predios/nuevo/', self.data(geojson='', perimetro_aceptado=''))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Predio.objects.count(), 0)

    def test_editing_requires_accepting_a_new_perimeter(self):
        predio = Predio.objects.create(
            propietario=self.user, nombre='Existente', ubicacion='Aquí', superficie_ha=3, geojson=self.feature
        )
        moved = json.loads(json.dumps(self.feature))
        moved['geometry']['coordinates'][0][1] = [-71.2, -33.687]
        response = self.client.post(
            f'/predios/{predio.pk}/editar/', self.data(nombre='Existente', geojson=json.dumps(moved), perimetro_aceptado='')
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Acepta el nuevo perímetro antes de guardar.')
        accepted = self.client.post(
            f'/predios/{predio.pk}/editar/', self.data(nombre='Existente', geojson=json.dumps(moved))
        )
        self.assertRedirects(accepted, f'/predios/{predio.pk}/')

    def test_other_users_cannot_edit_the_predio(self):
        predio = Predio.objects.create(propietario=self.user, nombre='Mio', ubicacion='X', superficie_ha=2)
        other = User.objects.create_user('otro', email='o@example.com', password=PASSWORD)
        self.client.force_login(other)
        self.assertEqual(self.client.get(f'/predios/{predio.pk}/editar/').status_code, 404)


class EstilosTests(PredioPaso2Base):
    def test_hidden_form_is_no_longer_overridden_and_confirm_step_shows_only_the_confirmation(self):
        css = re.sub(r'\s+', ' ', CSS)
        self.assertIn('.is-simple-layout .terrain-form-card #terrain-form[hidden],', css)
        self.assertIn('.is-predio-layout[data-reg-state="confirm"] .terrain-form-card #terrain-form { display: none; }', css)
        # La regla que lo rompía sigue existiendo (display:flex del formulario): las nuevas deben ir después.
        broken = css.index('.is-simple-layout .terrain-form-card #terrain-form { display: flex;')
        self.assertGreater(css.index('.is-simple-layout .terrain-form-card #terrain-form[hidden],'), broken)

    def test_new_rules_are_scoped_to_the_predio_layout(self):
        found = selectors(block(CSS, MARKER))
        self.assertGreater(len(found), 60)
        scoped = r'is-predio-layout|is-predio-heading|is-simple-layout \.terrain-form-card #terrain-form\[hidden\]'
        for selector in found:
            self.assertRegex(selector, scoped, selector)

    def test_two_columns_with_a_fixed_data_column_on_wide_screens(self):
        css = re.sub(r'\s+', ' ', block(CSS, MARKER))
        wide = css[css.index('@media (min-width: 1100px)') :]
        self.assertIn(
            '.terrain-layout.is-predio-layout:not([data-reg-state="draw"]) { grid-template-columns: minmax(0, 1fr) clamp(360px, 32%, 440px);',
            wide,
        )

    def test_map_height_is_capped_and_confirmation_is_compact(self):
        css = re.sub(r'\s+', ' ', block(CSS, MARKER))
        self.assertIn('height: clamp(380px, calc(100vh - 400px), 600px);', css)
        self.assertIn('.is-predio-layout .confirm-big { margin: 0 0 12px; font-size: 1.9rem;', css)
        self.assertIn('.is-predio-layout .perimeter-confirmation .confirmation-actions { display: contents; }', css)
        self.assertIn('.is-predio-layout[data-reg-state="editing"] .form-submit-row { display: none; }', css)

    def test_correct_contour_stays_available_when_opening_edit_predio(self):
        css = re.sub(r'\s+', ' ', block(CSS, MARKER))
        self.assertIn(
            '.is-predio-layout[data-reg-state="data"] .map-toolbar.is-predio:has(#accept-perimeter:not([hidden])) #edit-perimeter:not([hidden])',
            css,
        )


class ComportamientoJsTests(PredioPaso2Base):
    def test_drawn_contour_can_be_edited_again(self):
        create = JS[JS.index('function addCurrentLayer') :]
        create = create[: create.index('currentLayer.addTo(map)')]
        self.assertIn('L.geoJSON(feature', create)
        self.assertIn('pmIgnore: false', create)
        self.assertIn("L.PM.setOptIn(true)", JS)  # el modo opt-in sigue: por eso la capa debe pedirlo

    def test_confirm_step_flow_is_unchanged(self):
        confirm = JS[JS.index('function showPerimeterConfirmation') : JS.index('function editConfirmedPerimeter')]
        self.assertIn("perimeterAcceptedInput.value = 'on'", confirm)
        self.assertIn("setRegistrationState('confirm')", confirm)
        edit = JS[JS.index('function editConfirmedPerimeter') : JS.index('function cancelPerimeterChanges')]
        self.assertIn("setRegistrationState('editing')", edit)
        self.assertIn("perimeterAcceptedInput.value = ''", edit)
        self.assertIn('currentLayer.pm.enable', edit)
        cont = JS[JS.index('function continueAfterConfirmation') : JS.index('function stopDrawing')]
        self.assertIn('form.hidden = false', cont)
        self.assertIn("setRegistrationState('data')", cont)
        self.assertIn("form.addEventListener('submit', () => setRegistrationState('save'))", JS)
