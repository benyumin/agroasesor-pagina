"""Predio recién registrado: un solo mensaje, sin avisos repetidos y sin envíos duplicados.

Causa que se corrige: el formulario podía enviarse dos veces (doble clic), cada envío creaba un registro y dejaba su
aviso en la cookie de mensajes, y la ficha además repetía la confirmación en una tarjeta propia. Aquí se comprueba que
lo que llega a pantalla es una sola pregunta («¿Cómo deseas organizar tu predio?») y ningún aviso de confirmación.
"""

import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.utils import timezone

from core.models import Potrero, Predio

User = get_user_model()
PASSWORD = 'Clave-segura-123'
CORE = Path(__file__).resolve().parent.parent
TITLE = '¿Cómo deseas organizar tu predio?'
INTRO = (
    'Puedes utilizar toda la superficie como un solo potrero o dividirla en sectores para gestionar tus cultivos '
    'y actividades agrícolas por separado.'
)


def visible(response):
    """Solo el HTML que se ve: los JSON de los <script> no cuentan como contenido de la pantalla."""
    return re.sub(r'<script.*?</script>', '', response.content.decode(), flags=re.S)


def shown_messages(response):
    return [str(m) for m in response.context['messages']]


def message_callouts(html):
    """Avisos que salen de django.contrib.messages. El recordatorio de completar el perfil es otro componente."""
    found = re.findall(r'<p class="callout" role="status">(.*?)</p>', html, flags=re.S)
    return [text for text in found if 'Completa tu perfil' not in text]


class PredioNuevoBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('agricultor', email='agri@example.com', password=PASSWORD)
        self.other = User.objects.create_user('vecino', email='vecino@example.com', password=PASSWORD)
        self.client.force_login(self.user)
        self.ring = [[-71.216, -33.687], [-71.213, -33.687], [-71.213, -33.684], [-71.216, -33.684]]
        self.feature = {
            'type': 'Feature',
            'properties': {},
            'geometry': {'type': 'Polygon', 'coordinates': [self.ring + [self.ring[0]]]},
        }

    def predio_data(self, **extra):
        return {
            'nombre': 'Pomaire',
            'ubicacion': 'Melipilla',
            'geojson': json.dumps(self.feature),
            'perimetro_aceptado': 'on',
            'latitud': '-33.687',
            'longitud': '-71.216',
            **extra,
        }

    def potrero_data(self, name, ring):
        return {
            'nombre': name,
            'cultivo': '',
            'variedad': '',
            'superficie_ha': '',
            'fecha_siembra': '',
            'fecha_cosecha': '',
            'latitud': '',
            'longitud': '',
            'perimetro': json.dumps(ring),
        }

    def new_predio(self):
        response = self.client.post('/predios/nuevo/', self.predio_data())
        return Predio.objects.get(nombre='Pomaire', propietario=self.user), response


class SingleMessageAfterRegisteringTests(PredioNuevoBase):
    def test_registering_a_predio_shows_the_question_and_no_confirmation_anywhere(self):
        farm, response = self.new_predio()
        self.assertRedirects(response, f'/predios/{farm.pk}/?nuevo=1', fetch_redirect_response=False)
        page = self.client.get(response['Location'])
        html = visible(page)
        self.assertEqual(shown_messages(page), [])  # ningún aviso de django.contrib.messages
        self.assertEqual(message_callouts(html), [])
        self.assertEqual(html.count(TITLE), 1)
        self.assertEqual(html.count(INTRO), 1)
        for repeated in (
            'Predio registrado correctamente',
            'quedó guardado correctamente',
            'ya aparece en Siembra',
            'Ya registraste el límite completo',
            '¿Cómo utilizas este predio?',
        ):
            self.assertNotIn(repeated, html)

    def test_card_keeps_both_options_and_later_link_without_definitions(self):
        farm, response = self.new_predio()
        html = visible(self.client.get(response['Location']))
        self.assertEqual(html.count('Un solo potrero'), 1)
        self.assertEqual(html.count('Dividir en potreros'), 1)
        self.assertEqual(html.count('Utilizar todo el predio'), 1)
        self.assertEqual(html.count('Dividir mi predio'), 1)
        self.assertEqual(html.count('Lo haré más tarde'), 1)
        self.assertIn('name="nombre"', html)
        self.assertIn('value="Potrero principal"', html)
        self.assertIn(f'action="/predios/{farm.pk}/usar-completo/"', html)
        self.assertIn(f'href="/predios/{farm.pk}/potreros/nuevo/"', html)
        # Sin explicaciones repetidas de qué es un predio o un potrero.
        for definition in ('Es el terreno o propiedad completa', 'Es un sector dentro del predio', 'use-help'):
            self.assertNotIn(definition, html)

    def test_follow_through_redirect_leaves_the_message_queue_empty(self):
        _, response = self.new_predio()
        page = self.client.get(response['Location'])
        self.assertEqual(shown_messages(page), [])
        reload = self.client.get(response['Location'])
        self.assertEqual(shown_messages(reload), [])
        self.assertEqual(message_callouts(visible(reload)), [])

    def test_editing_a_predio_still_confirms_exactly_once(self):
        farm, _ = self.new_predio()
        response = self.client.post(
            f'/predios/{farm.pk}/editar/',
            self.predio_data(nombre='Pomaire Norte', perimetro_aceptado=''),
            follow=True,
        )
        self.assertEqual(shown_messages(response), ['El predio quedó guardado correctamente.'])
        self.assertEqual(visible(response).count('El predio quedó guardado correctamente.'), 1)
        self.assertEqual(visible(self.client.get(f'/predios/{farm.pk}/')).count('quedó guardado'), 0)

    def test_only_body_template_and_login_render_messages(self):
        found = {
            path.name
            for path in (CORE / 'templates').glob('*.html')
            if 'for message in messages' in path.read_text(encoding='utf-8')
        }
        self.assertEqual(found, {'body.html', 'login.html'})
        body = (CORE / 'templates' / 'body.html').read_text(encoding='utf-8')
        self.assertEqual(body.count('for message in messages'), 1)


class DoubleSubmitTests(PredioNuevoBase):
    def test_second_identical_submission_does_not_create_another_predio(self):
        first = self.client.post('/predios/nuevo/', self.predio_data())
        second = self.client.post('/predios/nuevo/', self.predio_data())
        self.assertEqual(Predio.objects.filter(propietario=self.user, nombre='Pomaire').count(), 1)
        self.assertEqual(first['Location'], second['Location'])
        page = self.client.get(second['Location'])
        self.assertEqual(shown_messages(page), [])
        self.assertEqual(visible(page).count(TITLE), 1)
        self.assertEqual(message_callouts(visible(page)), [])

    def test_same_name_with_other_perimeter_or_after_the_window_is_a_new_predio(self):
        self.client.post('/predios/nuevo/', self.predio_data())
        moved = [[x + 0.01, y] for x, y in self.ring]
        feature = {**self.feature, 'geometry': {'type': 'Polygon', 'coordinates': [moved + [moved[0]]]}}
        self.client.post('/predios/nuevo/', self.predio_data(geojson=json.dumps(feature)))
        self.assertEqual(Predio.objects.filter(propietario=self.user, nombre='Pomaire').count(), 2)
        Predio.objects.filter(propietario=self.user).update(creado=timezone.now() - timedelta(minutes=10))
        self.client.post('/predios/nuevo/', self.predio_data())
        self.assertEqual(Predio.objects.filter(propietario=self.user, nombre='Pomaire').count(), 3)

    def test_another_person_registering_the_same_data_is_not_a_duplicate(self):
        self.client.post('/predios/nuevo/', self.predio_data())
        self.client.force_login(self.other)
        self.client.post('/predios/nuevo/', self.predio_data())
        self.assertEqual(Predio.objects.filter(nombre='Pomaire').count(), 2)
        self.assertEqual(Predio.objects.filter(propietario=self.other).count(), 1)

    def test_using_the_whole_predio_twice_creates_one_potrero_and_one_alert(self):
        farm, _ = self.new_predio()
        # Dos envíos seguidos sin pasar por la página intermedia: la cookie del primero llega con el segundo.
        first = self.client.post(f'/predios/{farm.pk}/usar-completo/', {'nombre': 'Lote único'})
        second = self.client.post(f'/predios/{farm.pk}/usar-completo/', {'nombre': 'Lote único'})
        plot = Potrero.objects.get(predio=farm)
        self.assertEqual(plot.nombre, 'Lote único')
        self.assertEqual(first['Location'], second['Location'])
        page = self.client.get(second['Location'])
        self.assertEqual(len(shown_messages(page)), 1)
        self.assertIn('usa todo tu predio', shown_messages(page)[0])
        self.assertNotIn('ya tiene potreros', visible(page))
        self.assertEqual(len(message_callouts(visible(page))), 1)

    def test_using_the_whole_predio_when_it_already_had_potreros_explains_why_once(self):
        farm, _ = self.new_predio()
        Potrero.objects.create(predio=farm, nombre='Antiguo', superficie_ha=Decimal('1'))
        Potrero.objects.filter(predio=farm).update(creado=timezone.now() - timedelta(days=3))
        response = self.client.post(f'/predios/{farm.pk}/usar-completo/', {'nombre': 'Otro'}, follow=True)
        self.assertEqual(Potrero.objects.filter(predio=farm).count(), 1)
        self.assertEqual(len(shown_messages(response)), 1)
        self.assertIn('ya tiene potreros', shown_messages(response)[0])

    def test_creating_a_potrero_twice_creates_one_potrero_and_one_alert(self):
        farm, _ = self.new_predio()
        west = [[-71.216, -33.687], [-71.2145, -33.687], [-71.2145, -33.684], [-71.216, -33.684]]
        url = f'/predios/{farm.pk}/potreros/nuevo/'
        first = self.client.post(url, self.potrero_data('Oeste', west))
        second = self.client.post(url, self.potrero_data('Oeste', west))
        self.assertEqual(Potrero.objects.filter(predio=farm, nombre='Oeste').count(), 1)
        self.assertEqual(first['Location'], second['Location'])
        page = self.client.get(second['Location'])
        self.assertEqual(len(shown_messages(page)), 1)
        self.assertEqual(len(message_callouts(visible(page))), 1)

    def test_two_different_potreros_in_a_row_are_both_created(self):
        farm, _ = self.new_predio()
        west = [[-71.216, -33.687], [-71.2145, -33.687], [-71.2145, -33.684], [-71.216, -33.684]]
        east = [[-71.2144, -33.687], [-71.213, -33.687], [-71.213, -33.684], [-71.2144, -33.684]]
        url = f'/predios/{farm.pk}/potreros/nuevo/'
        self.client.post(url, self.potrero_data('Oeste', west))
        self.client.post(url, self.potrero_data('Este', east))
        self.assertEqual(set(farm.potreros.values_list('nombre', flat=True)), {'Oeste', 'Este'})


class SetupVisibilityTests(PredioNuevoBase):
    def test_question_is_shown_while_the_predio_has_no_potreros_and_hides_afterwards(self):
        farm, _ = self.new_predio()
        for url in (f'/predios/{farm.pk}/?nuevo=1', f'/predios/{farm.pk}/'):
            self.assertEqual(visible(self.client.get(url)).count(TITLE), 1)
        self.client.post(f'/predios/{farm.pk}/usar-completo/', {'nombre': 'Principal'})
        for url in (f'/predios/{farm.pk}/?nuevo=1', f'/predios/{farm.pk}/'):
            html = visible(self.client.get(url))
            for gone in (TITLE, 'Un solo potrero', 'Utilizar todo el predio', 'Lo haré más tarde', 'use-card'):
                self.assertNotIn(gone, html)
            self.assertIn('Gestión agrícola', html)
            self.assertIn('Agregar potrero', html)  # acción de gestión en lugar del asistente

    def test_map_and_general_information_are_present_on_the_new_predio_page(self):
        farm, response = self.new_predio()
        html = visible(self.client.get(response['Location']))
        self.assertIn('id="predio-detail-map"', html)
        self.assertIn('Información general', html)
        # La pregunta va antes del mapa y en un solo bloque (no repite la ficha en dos tarjetas).
        self.assertLess(html.index(TITLE), html.index('id="predio-detail-map"'))
        self.assertEqual(html.count('class="card use-card"'), 1)


class StaticGuardTests(TestCase):
    def test_layout_script_blocks_a_second_submission_of_the_same_form(self):
        source = (CORE / 'static' / 'js' / 'layout.js').read_text(encoding='utf-8')
        self.assertIn("addEventListener('submit'", source)
        self.assertIn("form.dataset.sending === '1'", source)
        self.assertIn('event.defaultPrevented', source)
        self.assertIn("addEventListener('pageshow'", source)
        body = (CORE / 'templates' / 'body.html').read_text(encoding='utf-8')
        self.assertIn("js/layout.js' %}?v=3", body)

    def test_old_confirmation_card_styles_are_gone_and_new_card_is_compact(self):
        css = (CORE / 'static' / 'css' / 'predios.css').read_text(encoding='utf-8')
        for old in ('.use-help', '.use-confirm', '.use-head', '.inline-use-form'):
            self.assertNotIn(old, css)
        self.assertIn('.use-card {', css)
        template = (CORE / 'templates' / 'predio_detail.html').read_text(encoding='utf-8')
        self.assertNotIn('Predio registrado correctamente', template)
        self.assertNotIn('use-confirm', template)
