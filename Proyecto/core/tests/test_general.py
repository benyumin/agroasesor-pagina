import os
import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings, Client
from django.utils import timezone
from ..geometry import polygon_area, polygon_perimeter
from ..models import (
    AceptacionPoliticas,
    ActividadAgricola,
    CalculoAgricola,
    ConfiguracionAgronomica,
    Cultivo,
    Insumo,
    Predio,
    PredioHistorial,
    Potrero,
    ReferenciaAgronomica,
    TemporadaCultivo,
    TipoInsumo,
    Zona,
    ProblemaAgricola,
)

User = get_user_model()
PASSWORD = 'Campo-Seguro-2026!'
POLYGON = [[-71.215, -33.686], [-71.214, -33.686], [-71.214, -33.685], [-71.215, -33.685]]


class AuthenticationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('farmer@example.com', email='farmer@example.com', password=PASSWORD)

    def registration_data(self, **values):
        return {
            'mode': 'register',
            'nombre_completo': 'maría soto',
            'email': 'maria@example.com',
            'telefono': '+56 9 1234 5678',
            'comuna': 'melipilla',
            'rut': '',
            'password1': PASSWORD,
            'password2': PASSWORD,
            'acepta_politicas': 'on',
            **values,
        }

    def test_registration_persists_hashed_password_logs_in_and_records_policy_acceptance(self):
        response = self.client.post('/login/', self.registration_data())
        self.assertRedirects(response, '/')
        user = User.objects.get(email='maria@example.com')
        self.assertEqual(user.username, 'maria@example.com')
        self.assertEqual(user.first_name, 'María')
        self.assertEqual(user.last_name, 'Soto')
        ficha = user.agricultor
        self.assertEqual(
            (ficha.nombre, ficha.telefono, ficha.comuna, ficha.rut), ('María Soto', '+56912345678', 'Melipilla', None)
        )
        self.assertTrue(user.check_password(PASSWORD))
        self.assertNotEqual(user.password, PASSWORD)
        self.assertFalse(user.is_staff)
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)
        acceptance = user.aceptaciones_politicas.get()
        self.assertEqual(acceptance.version, AceptacionPoliticas.VERSION_ACTUAL)
        self.assertIsNotNone(acceptance.aceptada_en)
        self.assertFalse(self.user.aceptaciones_politicas.exists())

    def test_registration_requires_policy_acceptance(self):
        data = self.registration_data()
        del data['acepta_politicas']
        response = self.client.post('/login/', data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Debes leer y aceptar la Política de privacidad, seguridad y cookies')
        self.assertFalse(User.objects.filter(email='maria@example.com').exists())
        self.assertFalse(AceptacionPoliticas.objects.exists())

    def test_policy_page_explains_cookies_and_browser_storage(self):
        response = self.client.get('/politicas/privacidad-y-cookies/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Cookies necesarias')
        self.assertContains(response, 'almacenamiento local del navegador')
        self.assertContains(response, AceptacionPoliticas.VERSION_ACTUAL)

    def test_registration_rejects_weak_or_mismatched_passwords(self):
        weak_password = self.client.post(
            '/login/',
            self.registration_data(password1='123456', password2='123456'),
        )
        self.assertEqual(weak_password.status_code, 200)
        mismatch = self.client.post(
            '/login/',
            self.registration_data(password1=PASSWORD, password2='diferente'),
        )
        self.assertContains(mismatch, 'Las contraseñas no coinciden.')
        self.assertFalse(User.objects.filter(email='maria@example.com').exists())

    def test_registration_rejects_duplicate_email_case_insensitively(self):
        duplicate_email = self.registration_data(
            email='FARMER@example.com',
        )
        self.assertContains(
            self.client.post('/login/', duplicate_email),
            'Este correo electrónico ya está registrado.',
        )
        self.assertEqual(User.objects.count(), 1)

    def test_login_logout_and_invalid_credentials(self):
        self.assertEqual(self.client.get('/').status_code, 302)
        self.assertContains(self.client.post('/login/', {'email': self.user.email, 'password': 'wrong'}), 'incorrectos')
        self.assertContains(
            self.client.post('/login/', {'username': 'cuenta-inexistente', 'password': PASSWORD}),
            'incorrectos',
        )
        self.assertRedirects(self.client.post('/login/', {'username': 'farmer@example.com', 'password': PASSWORD}), '/')
        self.assertEqual(self.client.get('/logout/').status_code, 405)
        self.assertRedirects(self.client.post('/logout/'), '/login/')
        self.assertEqual(self.client.get('/predios/').status_code, 302)

    def test_registered_user_can_log_out_and_sign_back_in_with_email_in_any_case(self):
        self.client.post('/login/', self.registration_data())
        self.assertRedirects(self.client.post('/logout/'), '/login/')
        self.assertRedirects(
            self.client.post('/login/', {'username': 'maria@example.com', 'password': PASSWORD}),
            '/',
        )
        self.assertRedirects(self.client.post('/logout/'), '/login/')
        self.assertRedirects(
            self.client.post('/login/', {'username': 'MARIA@example.com', 'password': PASSWORD}),
            '/',
        )

    def test_registration_and_predio_data_are_isolated_and_persist_after_login(self):
        existing_farm = Predio.objects.create(
            propietario=self.user,
            nombre='Campo privado existente',
            ubicacion='Melipilla',
            superficie_ha=5,
        )
        self.client.post('/login/', self.registration_data())
        registered_user = User.objects.get(username='maria@example.com')
        empty_farms = self.client.get('/predios/')
        self.assertEqual(empty_farms.status_code, 200)
        self.assertEqual(empty_farms.context['predios_list'].count(), 0)
        self.assertNotContains(empty_farms, existing_farm.nombre)

        boundary = [[-71.215, -33.686], [-71.214, -33.686], [-71.214, -33.685], [-71.215, -33.685]]
        feature = {
            'type': 'Feature',
            'properties': {},
            'geometry': {'type': 'Polygon', 'coordinates': [boundary + [boundary[0]]]},
        }
        response = self.client.post(
            '/predios/nuevo/',
            {
                'nombre': 'Campo de María',
                'ubicacion': 'Melipilla',
                'superficie_ha': '',
                'geojson': json.dumps(feature),
                'perimetro_aceptado': 'on',
            },
        )
        farm = Predio.objects.get(nombre='Campo de María')
        self.assertRedirects(response, f'/predios/{farm.pk}/?nuevo=1')
        self.assertEqual(farm.propietario, registered_user)

        self.assertRedirects(self.client.post('/logout/'), '/login/')
        self.assertRedirects(
            self.client.post('/login/', {'username': 'maria@example.com', 'password': PASSWORD}),
            '/',
        )
        owned_farms = self.client.get('/predios/')
        self.assertContains(owned_farms, farm.nombre)
        self.assertNotContains(owned_farms, existing_farm.nombre)
        self.assertEqual(list(registered_user.predios.values_list('pk', flat=True)), [farm.pk])
        self.assertEqual(self.client.get(f'/predios/{existing_farm.pk}/').status_code, 404)

    def test_inactive_user_cannot_login(self):
        self.user.is_active = False
        self.user.save()
        self.client.post('/login/', {'email': self.user.email, 'password': PASSWORD})
        self.assertEqual(self.client.get('/').status_code, 302)

    def test_admin_shortcut_cannot_escalate_privileges(self):
        self.assertEqual(self.client.post('/entrar-admin/').status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post('/entrar-admin/').status_code, 403)
        self.assertEqual(self.client.get('/panel-admin/').status_code, 403)
        self.assertNotContains(self.client.get('/'), 'Ingresar como administrador')

    def test_admin_real_counts(self):
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)
        response = self.client.get('/panel-admin/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['stats']['predios'], 0)
        self.assertContains(response, '/admin/')

    def test_csrf_required_for_login_registration_and_logout(self):
        client = Client(enforce_csrf_checks=True)
        usuarios = get_user_model().objects.count()
        respuesta = client.post('/login/', self.registration_data())
        self.assertEqual((respuesta.status_code, respuesta['Location']), (302, '/login/'))
        self.assertEqual(get_user_model().objects.count(), usuarios)  # no se registró a nadie
        self.assertNotIn('_auth_user_id', client.session)
        client.force_login(self.user)
        self.assertEqual(client.post('/logout/').status_code, 302)
        self.assertIn('_auth_user_id', client.session)  # sin token, el cierre de sesión NO se ejecutó

    @override_settings(MAILERS={'default': {'BACKEND': 'django.core.mail.backends.locmem.EmailBackend'}})
    def test_reset_requires_verified_single_use_token(self):
        self.assertRedirects(self.client.post('/cuenta/recuperar/', {'email': self.user.email}), '/cuenta/enviado/')
        self.assertEqual(len(mail.outbox), 1)
        url = re.search(r'http://testserver(/cuenta/restablecer/\S+)', mail.outbox[0].body).group(1)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        password = 'Nueva-Clave-Segura-2026!'
        self.assertRedirects(
            self.client.post(response.url, {'new_password1': password, 'new_password2': password}),
            '/cuenta/restablecida/',
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(password))
        another = Client()
        self.assertContains(another.get(url), 'inválido')

    def test_old_reset_flow_cannot_change_password(self):
        self.client.post(
            '/login/', {'mode': 'reset', 'email': self.user.email, 'password': 'hacked', 'password2': 'hacked'}
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))


class TerrainTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('a', email='a@example.com', password=PASSWORD)
        self.other = User.objects.create_user('b', email='b@example.com', password=PASSWORD)
        self.crop = Cultivo.objects.create(nombre='Cultivo de prueba')
        self.predio = Predio.objects.create(
            propietario=self.user, nombre='Mi campo', ubicacion='Melipilla', superficie_ha=10
        )
        self.client.force_login(self.user)
        self.url = f'/predios/{self.predio.pk}/potreros/nuevo/'

    def plot_data(self, **values):
        return {
            'nombre': 'Potrero norte',
            'cultivo': self.crop.pk,
            'variedad': '',
            'superficie_ha': '2',
            'fecha_siembra': '2026-10-02',
            'fecha_cosecha': '2027-03-01',
            'latitud': '',
            'longitud': '',
            'perimetro': '[]',
            **values,
        }

    def test_new_predio_and_potrero_appear_in_siembra(self):
        boundary = [[-71.216, -33.687], [-71.213, -33.687], [-71.213, -33.684], [-71.216, -33.684]]
        feature = {
            'type': 'Feature',
            'properties': {},
            'geometry': {'type': 'Polygon', 'coordinates': [boundary + [boundary[0]]]},
        }
        self.assertNotContains(self.client.get('/calendario/'), 'Campo en siembra')
        response = self.client.post(
            '/predios/nuevo/',
            {
                'nombre': 'Campo en siembra',
                'ubicacion': 'Paine',
                'superficie_ha': '5',
                'geojson': json.dumps(feature),
                'perimetro_aceptado': 'on',
            },
            follow=True,
        )
        # Al registrar no hay aviso de confirmación: la ficha del predio ya trae la única pregunta que sigue.
        self.assertNotContains(response, 'quedó guardado correctamente')
        self.assertContains(response, '¿Cómo deseas organizar tu predio?')
        p = Predio.objects.get(nombre='Campo en siembra')
        page = self.client.get('/calendario/')
        self.assertContains(page, 'Campo en siembra')
        self.assertContains(page, 'aún no tiene potreros')
        response = self.client.post(
            f'/predios/{p.pk}/potreros/nuevo/', self.plot_data(nombre='Potrero siembra'), follow=True
        )
        self.assertContains(response, 'Ya aparece en Siembra')
        page = self.client.get('/calendario/?anio=2026')
        self.assertContains(page, 'Potrero siembra')
        self.assertContains(page, '02/10/2026')
        self.assertContains(page, 'Mis fechas en el año')
        self.assertContains(page, 'tl-bar kind-range')

    def test_siembra_next_steps_guide_the_farmer(self):
        from datetime import timedelta
        from django.utils import timezone

        today = timezone.localdate()
        page = self.client.get('/calendario/?anio=2026')
        self.assertContains(page, 'Lo que sigue en tus potreros')
        self.assertContains(page, 'Agrega el primer potrero de «Mi campo»')
        self.assertContains(page, 'No tienes labores pendientes')
        bare = Potrero.objects.create(predio=self.predio, nombre='Lote sin cultivo', superficie_ha=1)
        nodate = Potrero.objects.create(predio=self.predio, nombre='Lote sin fecha', superficie_ha=1, cultivo=self.crop)
        soon = Potrero.objects.create(
            predio=self.predio,
            nombre='Lote por sembrar',
            superficie_ha=2,
            cultivo=self.crop,
            fecha_siembra=today + timedelta(days=10),
            fecha_cosecha=today + timedelta(days=120),
        )
        page = self.client.get('/calendario/?anio=2026')
        self.assertContains(page, 'Asigna un cultivo a «Lote sin cultivo»')
        self.assertContains(page, 'Registra la fecha de siembra de «Lote sin fecha»')
        self.assertContains(page, 'Programa la labor de siembra de «Lote por sembrar»')
        self.assertContains(page, f'/actividades/nueva/?potrero={soon.pk}')
        ActividadAgricola.objects.create(
            usuario=self.user,
            potrero=soon,
            nombre='Sembrar lote',
            tipo=ActividadAgricola.Tipo.SIEMBRA,
            fecha_programada=today - timedelta(days=2),
        )
        page = self.client.get('/calendario/?anio=2026')
        self.assertContains(page, 'Sembrar lote')
        self.assertContains(page, 'Atrasada 2 días')
        self.assertContains(page, '1 atrasada')
        self.assertNotContains(page, 'Programa la labor de siembra de «Lote por sembrar»')
        self.assertNotContains(page, 'No tienes labores pendientes')

    def test_siembra_next_steps_never_show_other_users_data(self):
        from datetime import timedelta
        from django.utils import timezone

        other_farm = Predio.objects.create(propietario=self.other, nombre='Campo ajeno', superficie_ha=5)
        other_plot = Potrero.objects.create(predio=other_farm, nombre='Lote ajeno', superficie_ha=1)
        ActividadAgricola.objects.create(
            usuario=self.other,
            potrero=other_plot,
            nombre='Labor ajena',
            tipo=ActividadAgricola.Tipo.RIEGO,
            fecha_programada=timezone.localdate() + timedelta(days=1),
        )
        page = self.client.get('/calendario/')
        for text in ('Campo ajeno', 'Lote ajeno', 'Labor ajena'):
            self.assertNotContains(page, text)

    def test_siembra_hides_empty_reference_catalog_from_farmers(self):
        page = self.client.get('/calendario/')
        self.assertNotContains(page, 'Cuándo sembrar según el catálogo')
        self.assertNotContains(page, 'cal-notice')
        self.user.is_staff = True
        self.user.save()
        self.assertContains(self.client.get('/calendario/'), 'cal-notice')

    def test_sidebar_menu_follows_design_and_all_links_work(self):
        import re as regex

        page = self.client.get('/calendario/')
        html = page.content.decode()
        aside = html[html.index('<aside') : html.index('</aside>')]
        self.assertNotIn('id="sidebar-quick-links"', html)  # el diseño nuevo no tiene «Accesos rápidos»
        nav = html[html.index('aria-label="Principal"') : html.index('</nav>', html.index('aria-label="Principal"'))]
        etiquetas = regex.findall(r'<span>([^<]+)</span>', nav)
        self.assertEqual(
            etiquetas,
            [
                'Inicio',
                'Mis predios',
                'Mapa',
                'Catálogo agrícola',
                'Calculadoras',
                'Semillas',
                'Fertilizantes',
                '¿Me alcanza?',
                'Actividades',
                'Toma de decisiones',
                'Reportes',
                'Calendario',
                'Buscar',
            ],
        )
        for href in regex.findall(r'href="([^"#]+)', nav):
            response = self.client.get(href.replace('&amp;', '&'))
            self.assertEqual(response.status_code, 200, f'{href} no abre')
        self.assertEqual(aside.count('is-active'), 1)  # solo «Calendario» queda marcado en esta página
        self.assertIn('<a href="/calendario/" class="nav-link is-active">', re.sub(r'\s+', ' ', aside))
        for url in ('/actividades/', '/actividades/nueva/'):
            self.assertEqual(self.client.get(url).status_code, 200)

    def test_crop_selector_is_grouped_and_base_crops_load_safely(self):
        from ..catalog import crop_group_name, ensure_base_crops, CROP_GROUPS

        self.assertEqual(crop_group_name('Maíz'), 'Cereales y granos')
        self.assertEqual(crop_group_name('maiz grano'), 'Cereales y granos')
        self.assertEqual(crop_group_name('Palto Hass'), 'Frutales')
        self.assertEqual(crop_group_name('Quinoa'), 'Otros cultivos')
        total = sum(len(names) for _group, names in CROP_GROUPS)
        Cultivo.objects.create(nombre='Trigo', densidad_siembra=Decimal('160'))
        created = ensure_base_crops()
        self.assertEqual(created, total - 1)
        self.assertEqual(ensure_base_crops(), 0)
        self.assertEqual(Cultivo.objects.get(nombre='Trigo').densidad_siembra, Decimal('160'))
        html = self.client.get(self.url).content.decode()
        select = html[html.index('name="cultivo"') :]
        select = select[: select.index('</select>')]
        self.assertIn('Sin cultivo por ahora', select)
        for group in ('Cereales y granos', 'Hortalizas', 'Frutales', 'Forrajes y praderas'):
            self.assertIn(f'<optgroup label="{group}">', select)
        self.assertLess(select.index('Cereales y granos'), select.index('Frutales'))
        wheat = Cultivo.objects.get(nombre='Trigo')
        response = self.client.post(self.url, self.plot_data(cultivo=wheat.pk))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Potrero.objects.get(nombre='Potrero norte').cultivo, wheat)

    def test_only_staff_can_load_base_crops_from_admin_panel(self):
        response = self.client.post('/panel-admin/cultivos-base/')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Cultivo.objects.filter(nombre='Maíz').count(), 0)
        self.user.is_staff = True
        self.user.save()
        self.assertContains(self.client.get('/panel-admin/'), 'Cargar cultivos base')
        response = self.client.post('/panel-admin/cultivos-base/', follow=True)
        self.assertContains(response, 'cultivos base')
        self.assertTrue(Cultivo.objects.filter(nombre='Maíz').exists())
        self.assertContains(self.client.post('/panel-admin/cultivos-base/', follow=True), 'ya estaban cargados')
        self.assertEqual(self.client.get('/panel-admin/cultivos-base/').status_code, 405)

    def test_register_forms_explain_predio_and_potrero(self):
        predio_page = self.client.get('/predios/nuevo/')
        self.assertNotContains(predio_page, 'terrain-guide')  # la pantalla de predio ya no repite la guía larga
        self.assertContains(predio_page, 'Dibuja el contorno de tu campo')
        self.assertContains(predio_page, 'id="step-indicator"')  # un solo indicador simple: «Paso X de 3»
        self.assertContains(predio_page, 'Paso 1 de 3')
        self.assertNotContains(predio_page, 'terrain-steps')
        self.assertNotContains(predio_page, 'id="step-confirm"')
        for ruido in ('PASOS 1 Y 2', 'PASOS 3 Y 4', 'En vivo', 'Gestión de predios y potreros', 'Volver a mis predios'):
            self.assertNotContains(predio_page, ruido)
        Cultivo.objects.all().delete()
        potrero_page = self.client.get(self.url)
        self.assertContains(potrero_page, '1 · ¿Qué vas a plantar?')
        self.assertContains(potrero_page, '2 · ¿Dónde queda?')
        self.assertContains(potrero_page, 'Agregar más detalles')
        for ruido in ('terrain-guide', 'Cada lote del predio', 'Aparecerá en Siembra', 'Nombre y cultivo'):
            self.assertNotContains(potrero_page, ruido)
        self.assertNotContains(potrero_page, 'id="step-confirm"')
        self.assertContains(potrero_page, 'Aún no hay cultivos para elegir')

    def test_perimeter_confirmation_lives_in_right_column_without_duplicate_actions(self):
        html = self.client.get('/predios/nuevo/').content.decode()
        for element_id in (
            'perimeter-confirmation',
            'confirm-and-continue',
            'return-to-edit',
            'confirm-area',
            'confirm-m2',
            'confirm-perimeter',
            'confirm-vertices',
            'step-indicator',
        ):
            self.assertEqual(html.count(f'id="{element_id}"'), 1, element_id)
        self.assertEqual(html.count('Confirmar y continuar'), 1)
        self.assertEqual(html.count('Volver a editar'), 1)
        form_card = html.index('terrain-form-card')
        self.assertGreater(html.index('id="perimeter-confirmation"'), form_card)
        self.assertLess(html.index('id="registration-prompt"'), html.index('id="perimeter-confirmation"'))
        self.assertLess(html.index('id="perimeter-confirmation"'), html.index('id="terrain-form"'))
        self.assertContains(self.client.get('/predios/nuevo/'), 'Tu predio mide')
        self.assertContains(self.client.get('/predios/nuevo/'), 'Primero dibuja tu campo')
        # Vértices, perímetro y m² existen, pero plegados bajo «Ver detalles técnicos».
        self.assertEqual(html.count('Ver detalles técnicos'), 2)

    def test_predio_panel_separates_user_data_from_calculated_measurement(self):
        Zona.objects.create(nombre='Zona panel')
        html = self.client.get('/predios/nuevo/').content.decode()
        self.assertIn('Ej: Fundo Santa Rosa', html)
        self.assertIn('Ej: Camino a Bollenar, parcela 12', html)
        self.assertIn('Selecciona una zona', html)
        self.assertIn('Notas del predio (opcional)', html)
        self.assertIn('Agregar más detalles', html)
        # La referencia de ubicación es obligatoria: queda visible, nunca dentro del bloque plegado.
        self.assertLess(html.index('Referencia de ubicación'), html.index('Agregar más detalles'))
        self.assertLess(html.index('Agregar más detalles'), html.index('Notas del predio (opcional)'))
        for hidden in ('superficie_ha', 'latitud', 'longitud'):
            self.assertIn(f'type="hidden" name="{hidden}"', html)
        for gone in (
            'Superficie total (ha)',
            'Fecha de creación',
            'Se asignará automáticamente',
            'Observaciones (opcional)',
            'Descripción (opcional)',
        ):
            self.assertNotIn(gone, html)
        self.assertIn('Tu predio mide', html)
        self.assertIn('Ver detalles técnicos', html)
        self.assertNotIn('Medición confirmada', html)
        self.assertNotIn('Equivalente:', html)
        self.assertEqual(html.count('Guardar predio'), 1)
        for element_id in ('map-summary-m2', 'map-summary-vertices', 'map-summary-lat', 'map-summary-lng'):
            self.assertEqual(html.count(f'id="{element_id}"'), 1)

    def test_zone_is_required_only_when_zones_exist_and_notes_are_unified(self):
        from ..forms import PredioForm

        Zona.objects.all().delete()
        self.assertFalse(PredioForm().fields['zona'].required)
        zone = Zona.objects.create(nombre='Zona obligatoria')
        self.assertTrue(PredioForm().fields['zona'].required)
        boundary = [[-71.216, -33.687], [-71.213, -33.687], [-71.213, -33.684], [-71.216, -33.684]]
        feature = {
            'type': 'Feature',
            'properties': {},
            'geometry': {'type': 'Polygon', 'coordinates': [boundary + [boundary[0]]]},
        }
        data = {
            'nombre': 'Predio notas',
            'ubicacion': 'Camino',
            'geojson': json.dumps(feature),
            'perimetro_aceptado': 'on',
            'latitud': '-33.687',
            'longitud': '-71.216',
            'notas': 'Sector de hortalizas',
        }
        self.assertContains(self.client.post('/predios/nuevo/', data), 'Este campo es obligatorio')
        self.assertFalse(Predio.objects.filter(nombre='Predio notas').exists())
        response = self.client.post('/predios/nuevo/', {**data, 'zona': zone.pk})
        farm = Predio.objects.get(nombre='Predio notas')
        self.assertRedirects(response, f'/predios/{farm.pk}/?nuevo=1')
        self.assertEqual((farm.descripcion, farm.observaciones, farm.zona_id), ('Sector de hortalizas', '', zone.pk))
        self.assertIsNotNone(farm.geojson)
        self.assertGreater(farm.superficie_ha, 0)
        self.assertEqual(farm.latitud, -33.687)

    def test_edit_shows_old_description_and_observations_inside_notes(self):
        from ..forms import PredioForm

        farm = Predio.objects.create(
            propietario=self.user,
            nombre='Con notas',
            ubicacion='X',
            superficie_ha=Decimal('1'),
            descripcion='Descripción vieja',
            observaciones='Observación vieja',
        )
        notes = PredioForm(instance=farm).initial['notas']
        self.assertIn('Descripción vieja', notes)
        self.assertIn('Observación vieja', notes)

    def test_new_predio_detail_offers_next_steps_only_once(self):
        farm = Predio.objects.create(
            propietario=self.user, nombre='Recién creado', ubicacion='X', superficie_ha=Decimal('1')
        )
        response = self.client.get(f'/predios/{farm.pk}/?nuevo=1')
        self.assertContains(response, '¿Cómo deseas organizar tu predio?')
        self.assertNotContains(response, 'Predio registrado correctamente')
        self.assertContains(response, 'Dividir mi predio')
        self.assertContains(response, f'/predios/{farm.pk}/potreros/nuevo/')
        self.assertContains(response, 'Lo haré más tarde')
        self.assertNotContains(response, 'Utilizar todo el predio')  # sin perímetro dibujado no se puede copiar
        # Mientras el predio no tenga potreros la pregunta sigue disponible, con o sin «?nuevo=1»…
        self.assertContains(self.client.get(f'/predios/{farm.pk}/'), '¿Cómo deseas organizar tu predio?')
        # …y desaparece en cuanto existe el primer potrero.
        Potrero.objects.create(predio=farm, nombre='Lote', superficie_ha=Decimal('0.5'))
        for url in (f'/predios/{farm.pk}/?nuevo=1', f'/predios/{farm.pk}/'):
            self.assertNotContains(self.client.get(url), '¿Cómo deseas organizar tu predio?')

    def test_activity_form_explains_product_section_instead_of_greying_it(self):
        self.client.post(self.url, self.plot_data())
        page = self.client.get('/actividades/nueva/')
        self.assertContains(page, 'id="input-section" data-state="pending"')
        self.assertContains(page, 'id="product-fields"')
        self.assertContains(page, 'id="input-callout"')
        self.assertContains(page, 'Producto y dosis')
        self.assertNotContains(page, 'is-muted')

    def test_create_and_edit_predio(self):
        boundary = [[-71.216, -33.687], [-71.213, -33.687], [-71.213, -33.684], [-71.216, -33.684]]
        feature = {
            'type': 'Feature',
            'properties': {},
            'geometry': {'type': 'Polygon', 'coordinates': [boundary + [boundary[0]]]},
        }
        response = self.client.post(
            '/predios/nuevo/',
            {
                'nombre': 'Campo nuevo',
                'ubicacion': 'Paine',
                'superficie_ha': '5',
                'geojson': json.dumps(feature),
                'perimetro_aceptado': 'on',
                'propietario': self.other.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        p = Predio.objects.get(nombre='Campo nuevo')
        self.assertRedirects(response, f'/predios/{p.pk}/?nuevo=1')
        self.assertEqual(p.propietario, self.user)
        self.assertRedirects(
            self.client.post(
                f'/predios/{p.pk}/editar/', {'nombre': 'Actualizado', 'ubicacion': 'Paine', 'superficie_ha': '6'}
            ),
            f'/predios/{p.pk}/',
        )
        p.refresh_from_db()
        self.assertEqual(p.nombre, 'Actualizado')
        self.assertEqual(p.geojson, feature)
        self.assertEqual(p.historial.count(), 2)

    def test_predio_drawn_geojson_is_closed_and_server_recalculates_area(self):
        boundary = [[-71.216, -33.687], [-71.213, -33.687], [-71.213, -33.684], [-71.216, -33.684]]
        feature = {
            'type': 'Feature',
            'properties': {},
            'geometry': {'type': 'Polygon', 'coordinates': [boundary + [boundary[0]]]},
        }
        response = self.client.post(
            '/predios/nuevo/',
            {
                'nombre': 'Campo delimitado',
                'ubicacion': 'Melipilla',
                'superficie_ha': '999',
                'geojson': json.dumps(feature),
                'perimetro_aceptado': 'on',
                'latitud': '',
                'longitud': '',
                'zona': '',
            },
        )
        self.assertEqual(response.status_code, 302)
        farm = Predio.objects.get(nombre='Campo delimitado')
        self.assertRedirects(response, f'/predios/{farm.pk}/?nuevo=1')
        self.assertEqual(farm.geojson, feature)
        self.assertEqual(
            farm.superficie_ha,
            (polygon_area(boundary) / Decimal('10000')).quantize(Decimal('0.0001')),
        )
        self.assertEqual(self.client.get(f'/predios/{farm.pk}/').status_code, 200)
        edit_response = self.client.get(f'/predios/{farm.pk}/editar/')
        self.assertContains(edit_response, 'id="map-form-summary"')
        self.assertContains(edit_response, 'id="map-summary-area"')
        self.assertContains(edit_response, 'name="geojson"')

    def test_new_predio_requires_an_explicit_accepted_closed_polygon(self):
        form_data = {
            'nombre': 'Sin aceptación',
            'ubicacion': 'Melipilla',
            'superficie_ha': '',
            'geojson': json.dumps(
                {
                    'type': 'Feature',
                    'properties': {},
                    'geometry': {'type': 'Polygon', 'coordinates': [POLYGON + [POLYGON[0]]]},
                }
            ),
        }
        response = self.client.post('/predios/nuevo/', form_data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Predio.objects.filter(nombre='Sin aceptación').exists())
        form_data['perimetro_aceptado'] = 'on'
        response = self.client.post('/predios/nuevo/', form_data)
        self.assertEqual(response.status_code, 302)
        farm = Predio.objects.get(nombre='Sin aceptación')
        self.assertEqual(farm.historial.get().accion, PredioHistorial.Accion.CREADO)

        invalid_data = {
            'nombre': 'Anillo abierto',
            'ubicacion': 'Melipilla',
            'superficie_ha': '',
            'geojson': json.dumps(
                {
                    'type': 'Feature',
                    'properties': {},
                    'geometry': {'type': 'Polygon', 'coordinates': [POLYGON]},
                }
            ),
            'perimetro_aceptado': 'on',
        }
        self.assertEqual(self.client.post('/predios/nuevo/', invalid_data).status_code, 200)
        self.assertFalse(Predio.objects.filter(nombre='Anillo abierto').exists())

    def test_perimeter_edits_recalculate_surface_and_keep_immutable_history_snapshots(self):
        old_points = POLYGON
        old_feature = {
            'type': 'Feature',
            'properties': {},
            'geometry': {'type': 'Polygon', 'coordinates': [old_points + [old_points[0]]]},
        }
        create_response = self.client.post(
            '/predios/nuevo/',
            {
                'nombre': 'Trazable',
                'ubicacion': 'Melipilla',
                'superficie_ha': '',
                'geojson': json.dumps(old_feature),
                'perimetro_aceptado': 'on',
            },
        )
        self.assertEqual(create_response.status_code, 302)
        farm = Predio.objects.get(nombre='Trazable')
        old_area = farm.superficie_ha
        new_points = [[-71.215, -33.686], [-71.2135, -33.686], [-71.2135, -33.6845], [-71.215, -33.6845]]
        new_feature = {
            'type': 'Feature',
            'properties': {},
            'geometry': {'type': 'Polygon', 'coordinates': [new_points + [new_points[0]]]},
        }
        response = self.client.post(
            f'/predios/{farm.pk}/editar/',
            {
                'nombre': farm.nombre,
                'ubicacion': farm.ubicacion,
                'superficie_ha': str(old_area),
                'descripcion': 'Actualizada',
                'observaciones': 'Revisión anual',
                'geojson': json.dumps(new_feature),
                'perimetro_aceptado': 'on',
            },
        )
        self.assertRedirects(response, f'/predios/{farm.pk}/')
        farm.refresh_from_db()
        self.assertEqual(
            farm.superficie_ha,
            (polygon_area(new_points) / Decimal('10000')).quantize(Decimal('0.0001')),
        )
        self.assertNotEqual(farm.superficie_ha, old_area)
        perimeter_event = farm.historial.get(accion=PredioHistorial.Accion.PERIMETRO)
        self.assertEqual(perimeter_event.geometria_anterior, old_feature)
        self.assertEqual(perimeter_event.geometria_nueva, new_feature)
        self.assertEqual(perimeter_event.superficie_anterior_ha, old_area)
        self.assertEqual(perimeter_event.superficie_nueva_ha, farm.superficie_ha)
        with self.assertRaises(ValidationError):
            perimeter_event.resumen = 'Alterado'
            perimeter_event.save()
        with self.assertRaises(ValidationError):
            perimeter_event.delete()
        with self.assertRaises(ValidationError):
            PredioHistorial.objects.filter(pk=perimeter_event.pk).update(resumen='Alterado')
        with self.assertRaises(ValidationError):
            PredioHistorial.objects.filter(pk=perimeter_event.pk).delete()
        self.client.logout()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f'/predios/{farm.pk}/').status_code, 404)
        self.assertEqual(self.client.post(f'/predios/{farm.pk}/editar/', {}).status_code, 404)

    def test_predio_ficha_shows_persistent_history_and_agricultural_totals(self):
        self.predio.geojson = {
            'type': 'Feature',
            'properties': {},
            'geometry': {'type': 'Polygon', 'coordinates': [POLYGON + [POLYGON[0]]]},
        }
        self.predio.save()
        history = PredioHistorial.objects.create(
            predio=self.predio,
            usuario=self.user,
            accion=PredioHistorial.Accion.CREADO,
            resumen='Predio importado antes de activar el historial.',
            superficie_nueva_ha=self.predio.superficie_ha,
        )
        self.client.post(self.url, self.plot_data())
        response = self.client.get(f'/predios/{self.predio.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Historial del predio')
        self.assertContains(response, 'Potrero creado')
        self.assertContains(response, 'Superficie utilizada')
        self.assertContains(response, 'Siguiente paso')
        self.assertContains(response, 'Ver planificación')  # el potrero de este flujo ya tiene cultivo
        self.assertNotContains(response, 'Planificación de siembra')
        self.assertContains(response, 'data-map-layer="normal"')
        self.assertContains(response, 'data-map-layer="satellite"')
        self.assertContains(response, 'predio-map-status')
        self.assertEqual(response.context['historial'].first().accion, PredioHistorial.Accion.POTRERO_CREADO)
        self.assertTrue(PredioHistorial.objects.filter(pk=history.pk).exists())

    def test_create_plot_and_associate_crop_persist_across_sessions(self):
        self.assertRedirects(self.client.post(self.url, self.plot_data()), f'/predios/{self.predio.pk}/')
        plot = Potrero.objects.get()
        self.assertEqual(plot.cultivo, self.crop)
        self.client.logout()
        self.client.force_login(self.user)
        response = self.client.get('/predios/')
        self.assertContains(response, 'Potrero norte')
        self.assertEqual(response.context['farm_data']['plots'][0]['id'], str(plot.pk))
        event = self.predio.historial.get(accion=PredioHistorial.Accion.POTRERO_CREADO)
        self.assertEqual(event.superficie_nueva_ha, plot.superficie_ha)
        self.assertEqual(event.potrero, plot)
        self.assertContains(response, 'MIS PREDIOS REGISTRADOS')
        self.assertContains(response, 'POTREROS DE ESTE PREDIO')
        self.assertContains(response, 'HISTORIAL DE POTREROS')
        self.assertContains(response, f'data-select-plot="{plot.pk}"')
        selected_response = self.client.get('/predios/', {'predio': str(self.predio.pk)})
        self.assertContains(selected_response, f'data-initial-farm="{self.predio.pk}"')

    def test_predios_page_lists_all_owned_farms_and_filters_potrero_history(self):
        other_farm = Predio.objects.create(
            propietario=self.user,
            nombre='Campo sur',
            ubicacion='Paine',
            superficie_ha=5,
        )
        more_farms = [
            Predio.objects.create(
                propietario=self.user,
                nombre=f'Campo {number}',
                ubicacion='Zona rural',
                superficie_ha=1,
            )
            for number in range(3, 11)
        ]
        foreign_farm = Predio.objects.create(
            propietario=self.other,
            nombre='Campo privado',
            ubicacion='Otra comuna',
            superficie_ha=8,
        )
        first_plot = Potrero.objects.create(
            predio=self.predio,
            nombre='Ladera norte',
            cultivo=self.crop,
            superficie_ha=Decimal('2'),
        )
        second_plot = Potrero.objects.create(
            predio=other_farm,
            nombre='Bajo sur',
            superficie_ha=Decimal('1'),
        )
        Potrero.objects.create(
            predio=foreign_farm,
            nombre='Potrero ajeno',
            superficie_ha=Decimal('1'),
        )

        response = self.client.get('/predios/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['predios_list'].count(), 10)
        self.assertContains(response, 'Mi campo')
        self.assertContains(response, 'Campo sur')
        for farm in more_farms:
            self.assertContains(response, farm.nombre)
        self.assertNotContains(response, 'Campo privado')
        self.assertContains(response, 'Ladera norte')
        self.assertContains(response, 'Bajo sur')
        self.assertNotContains(response, 'Potrero ajeno')
        self.assertEqual(response.context['potrero_history_page'].paginator.count, 2)

        selected = self.client.get('/predios/', {'predio': str(self.predio.pk)})
        self.assertEqual(selected.context['selected_predio'], self.predio)
        self.assertContains(selected, f'data-farm-plots="{self.predio.pk}"')
        self.assertContains(selected, f'?potrero={first_plot.pk}#predio-history')
        self.assertContains(selected, 'data-select-plot="' + str(first_plot.pk) + '"')

        filtered = self.client.get(
            '/predios/',
            {
                'q': 'norte',
                'filtro_predio': str(self.predio.pk),
                'cultivo': str(self.crop.pk),
                'orden': 'nombre',
            },
        )
        self.assertEqual(filtered.context['potrero_history_page'].paginator.count, 1)
        self.assertEqual(filtered.context['potrero_history_page'].object_list[0], first_plot)

    def test_potrero_history_list_paginates_and_keeps_filters(self):
        for number in range(26):
            Potrero.objects.create(
                predio=self.predio,
                nombre=f'Potrero {number:02}',
                superficie_ha=Decimal('0.1'),
            )
        response = self.client.get(
            '/predios/',
            {
                'q': 'Potrero',
                'filtro_predio': str(self.predio.pk),
                'orden': 'nombre',
            },
        )
        page = response.context['potrero_history_page']
        self.assertEqual(page.paginator.count, 26)
        self.assertEqual(page.paginator.num_pages, 2)
        self.assertEqual(len(page.object_list), 25)
        self.assertContains(response, 'Página 1 de 2')
        self.assertContains(
            response,
            f'?q=Potrero&filtro_predio={self.predio.pk}&cultivo=&orden=nombre&pagina=2',
        )

    def test_predios_empty_state_distinguishes_no_farms_and_farm_without_plots(self):
        response = self.client.get('/predios/')
        self.assertContains(response, 'Selecciona un predio para registrar y administrar sus potreros.')
        self.assertContains(response, 'Este predio todavía no tiene potreros registrados.')
        self.assertContains(response, 'Agregar potrero')
        self.assertNotContains(response, 'Registrar primer potrero')

        new_user = User.objects.create_user('empty', email='empty@example.com', password=PASSWORD)
        self.client.force_login(new_user)
        empty_response = self.client.get('/predios/')
        self.assertContains(empty_response, 'No tienes predios registrados.')
        self.assertContains(empty_response, 'Registrar primer predio')
        self.assertNotContains(empty_response, 'Aún no hay potreros ubicados en el mapa.')

    def test_polygon_area_is_calculated_on_server(self):
        self.assertRedirects(
            self.client.post(self.url, self.plot_data(perimetro=json.dumps(POLYGON), superficie_ha='9')),
            f'/predios/{self.predio.pk}/',
        )
        plot = Potrero.objects.get()
        self.assertEqual(plot.perimetro, POLYGON)
        self.assertEqual(plot.superficie_ha, (polygon_area(POLYGON) / 10000).quantize(Decimal('0.0001')))
        self.assertNotEqual(plot.superficie_ha, Decimal('9'))

    def test_polygon_does_not_need_manual_area(self):
        self.assertRedirects(
            self.client.post(self.url, self.plot_data(perimetro=json.dumps(POLYGON), superficie_ha='')),
            f'/predios/{self.predio.pk}/',
        )

    def test_edit_plot_crop_and_coordinates(self):
        self.client.post(self.url, self.plot_data())
        plot = Potrero.objects.get()
        url = f'/predios/{self.predio.pk}/potreros/{plot.pk}/editar/'
        self.assertRedirects(
            self.client.post(url, self.plot_data(nombre='Sur', cultivo='', latitud='-33.5', longitud='-71.2')),
            f'/predios/{self.predio.pk}/',
        )
        plot.refresh_from_db()
        self.assertIsNone(plot.cultivo)
        self.assertEqual(plot.latitud, -33.5)
        self.assertEqual(plot.nombre, 'Sur')
        event = self.predio.historial.get(accion=PredioHistorial.Accion.POTRERO_ACTUALIZADO)
        self.assertEqual(event.superficie_anterior_ha, Decimal('2'))
        self.assertEqual(event.superficie_nueva_ha, Decimal('2'))
        self.assertEqual(event.potrero, plot)
        self.assertIn('Nombre: Potrero norte → Sur', event.resumen)
        filtered_history = self.client.get(f'/predios/{self.predio.pk}/', {'potrero': plot.pk})
        self.assertContains(filtered_history, 'Historial de Sur')
        self.assertContains(filtered_history, 'Se modificó el potrero')
        self.assertNotContains(filtered_history, 'Predio creado')

    def test_owners_cannot_see_or_modify_other_terrain(self):
        self.client.post(self.url, self.plot_data())
        plot = Potrero.objects.get()
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get('/predios/'), 'Mi campo')
        self.assertEqual(self.client.get('/').context['farm_data']['plots'], [])
        for url in [
            f'/predios/{self.predio.pk}/editar/',
            self.url,
            f'/predios/{self.predio.pk}/potreros/{plot.pk}/editar/',
        ]:
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url, self.plot_data()).status_code, 404)

    def test_reject_invalid_dates_areas_and_coordinates(self):
        for data in [
            self.plot_data(superficie_ha='-1'),
            self.plot_data(superficie_ha='0'),
            self.plot_data(superficie_ha='11'),
            self.plot_data(fecha_cosecha='2026-01-01'),
            self.plot_data(latitud='91', longitud='1'),
            self.plot_data(latitud='-33'),
            self.plot_data(latitud='nan', longitud='1'),
            self.plot_data(perimetro='not-json'),
        ]:
            with self.subTest(data=data):
                self.assertEqual(self.client.post(self.url, data).status_code, 200)
        self.assertEqual(Potrero.objects.count(), 0)

    def test_total_plot_area_and_predio_reduction(self):
        self.client.post(self.url, self.plot_data(superficie_ha='8'))
        response = self.client.post(self.url, self.plot_data(superficie_ha='3'))
        self.assertContains(response, 'superar')
        self.assertEqual(Potrero.objects.count(), 1)
        response = self.client.post(
            f'/predios/{self.predio.pk}/editar/', {'nombre': 'Mi campo', 'ubicacion': 'Melipilla', 'superficie_ha': '7'}
        )
        self.assertContains(response, 'menor')

    def test_inactive_or_unknown_crop_rejected(self):
        self.crop.activo = False
        self.crop.save()
        self.assertEqual(self.client.post(self.url, self.plot_data()).status_code, 200)
        self.assertEqual(self.client.post(self.url, self.plot_data(cultivo='9999')).status_code, 200)
        self.assertEqual(Potrero.objects.count(), 0)

    def test_empty_catalog_stays_empty(self):
        Cultivo.objects.all().delete()
        response = self.client.get('/')
        self.assertEqual(response.context['catalog']['cultivos'], [])
        self.assertEqual(response.context['catalog']['semillas'], [])

    @override_settings(MAP_TILE_URL='https://tiles.example/test-</script><script>bad()</script>/{z}/{x}/{y}.png')
    def test_map_configuration_is_safely_serialized(self):
        response = self.client.get('/predios/')
        self.assertNotContains(response, 'test-</script>')
        self.assertContains(response, '\\u003C/script\\u003E')

    def test_map_uses_leaflet_and_keeps_manual_registration(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'leaflet-geoman')
        self.assertContains(response, 'Dibujar mi potrero')
        self.assertContains(response, 'Medir una línea')
        self.assertRedirects(self.client.post(self.url, self.plot_data()), f'/predios/{self.predio.pk}/')
        predio_form = self.client.get('/predios/nuevo/')
        self.assertContains(predio_form, 'id="map-form-summary"')
        self.assertContains(predio_form, 'id="map-summary-area"')
        self.assertContains(predio_form, 'id="map-summary-perimeter"')
        self.assertContains(predio_form, 'id="map-summary-location"')
        self.assertContains(predio_form, 'id="accept-perimeter"')
        self.assertContains(predio_form, 'id="confirm-and-continue"')
        self.assertContains(predio_form, 'id="terrain-form" hidden')

    def test_potrero_geojson_is_saved_and_recovered(self):
        self.client.post(self.url, self.plot_data(perimetro=json.dumps(POLYGON)))
        plot = Potrero.objects.get()
        self.assertEqual(plot.geojson['type'], 'Feature')
        self.assertEqual(plot.geojson['geometry']['type'], 'Polygon')
        self.assertEqual(plot.geojson['geometry']['coordinates'][0][0], plot.geojson['geometry']['coordinates'][0][-1])
        self.assertEqual(self.client.get('/predios/').context['farm_data']['plots'][0]['geojson'], plot.geojson)

    def test_polygon_perimeter_is_closed_and_included_in_map_data(self):
        self.client.post(self.url, self.plot_data(perimetro=json.dumps(POLYGON)))
        plot = Potrero.objects.get()
        perimeter = polygon_perimeter(POLYGON)
        self.assertGreater(perimeter, Decimal('350'))
        self.assertLess(perimeter, Decimal('450'))
        self.assertEqual(perimeter, polygon_perimeter(list(reversed(POLYGON))))
        map_plot = self.client.get('/predios/').context['farm_data']['plots'][0]
        self.assertEqual(map_plot['perimeterM'], float(perimeter))

    def test_map_initializes_without_turf_and_offers_boundary_drawing(self):
        listing = self.client.get('/predios/')
        form = self.client.get(self.url)
        self.assertContains(listing, 'predios-map.js')
        self.assertContains(listing, 'Información seleccionada')
        self.assertContains(listing, 'selected-terrain-details')
        self.assertNotContains(listing, 'turf.min.js')
        self.assertContains(form, 'id="cancel-drawing"')
        self.assertContains(form, 'finish-line')
        self.assertContains(form, 'clear-measurement')
        self.assertContains(form, 'live-perimeter')
        self.assertContains(form, 'live-area')
        self.assertNotContains(form, 'turf.min.js')

    def test_potrero_must_stay_inside_predio_boundary(self):
        boundary = [[-71.216, -33.687], [-71.213, -33.687], [-71.213, -33.684], [-71.216, -33.684]]
        self.predio.geojson = {
            'type': 'Feature',
            'properties': {},
            'geometry': {'type': 'Polygon', 'coordinates': [boundary + [boundary[0]]]},
        }
        self.predio.save()
        outside = [[-71.219, -33.686], [-71.218, -33.686], [-71.218, -33.685], [-71.219, -33.685]]
        response = self.client.post(self.url, self.plot_data(perimetro=json.dumps(outside)))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'debe quedar dentro del predio')
        self.assertFalse(Potrero.objects.exists())

    def test_potrero_form_shows_remaining_area_and_focuses_parent_boundary(self):
        boundary = [[-71.216, -33.687], [-71.213, -33.687], [-71.213, -33.684], [-71.216, -33.684]]
        self.predio.geojson = {
            'type': 'Feature',
            'properties': {},
            'geometry': {'type': 'Polygon', 'coordinates': [boundary + [boundary[0]]]},
        }
        self.predio.save()
        Potrero.objects.create(
            predio=self.predio,
            nombre='Existente',
            superficie_ha=Decimal('2'),
        )

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['potrero_superficie_disponible'], Decimal('8'))
        self.assertEqual(response.context['form'].fields['superficie_ha'].widget.attrs['max'], '8.0000')
        self.assertContains(response, 'Ver el predio')
        self.assertContains(response, 'dentro del límite verde')

    def test_potrero_surface_offers_remaining_area_shortcut_and_no_silent_block(self):
        Potrero.objects.create(predio=self.predio, nombre='Existente', superficie_ha=Decimal('2'))
        page = self.client.get(self.url)
        self.assertContains(page, 'data-fill-available')
        self.assertContains(page, 'Usar lo que queda (8,00 ha)')
        self.assertContains(page, 'id="terrain-form"')
        self.assertNotContains(page, 'no-space-card')

    def test_potrero_form_explains_when_predio_has_no_free_surface(self):
        Potrero.objects.create(predio=self.predio, nombre='Todo', superficie_ha=self.predio.superficie_ha)
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context['potrero_sin_espacio'])
        self.assertContains(page, 'Este predio ya no tiene superficie libre')
        self.assertContains(page, f'/predios/{self.predio.pk}/editar/')
        self.assertNotContains(page, 'id="terrain-form"')
        self.assertNotContains(page, 'data-fill-available')
        # Con un potrero existente, editarlo sigue mostrando su formulario completo sin un «max» imposible.
        existing = self.predio.potreros.get()
        edit = self.client.get(f'/predios/{self.predio.pk}/potreros/{existing.pk}/editar/')
        self.assertContains(edit, 'id="terrain-form"')
        self.assertNotContains(edit, 'no-space-card')

    def test_icons_script_loads_async_so_menus_work_before_it_arrives(self):
        html = self.client.get('/predios/').content.decode()
        icons = html.index('unpkg.com/lucide')
        self.assertIn('async src="https://unpkg.com/lucide', re.sub(r'\s+', ' ', html))
        self.assertLess(html.index('js/row-menu.js'), icons)
        self.assertLess(html.index('js/layout.js'), icons)

    def test_potrero_overlap_with_same_farm_is_rejected(self):
        first = [[-71.215, -33.686], [-71.214, -33.686], [-71.214, -33.685], [-71.215, -33.685]]
        self.client.post(self.url, self.plot_data(perimetro=json.dumps(first)))
        second = [[-71.2145, -33.686], [-71.2135, -33.686], [-71.2135, -33.685], [-71.2145, -33.685]]
        response = self.client.post(self.url, self.plot_data(nombre='Potrero sur', perimetro=json.dumps(second)))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'se superpone')
        self.assertEqual(Potrero.objects.count(), 1)

    def test_all_existing_module_routes_render(self):
        urls = [
            '/',
            '/predios/',
            '/predios/nuevo/',
            f'/predios/{self.predio.pk}/',
            f'/predios/{self.predio.pk}/editar/',
            '/decisiones/',
            '/productos/',
            '/calendario/',
            '/actividades/',
            '/actividades/nueva/',
            '/reportes/',
            '/busqueda/',
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                main_nav = response.content.decode().split('aria-label="Principal"', 1)[1].split('</nav>', 1)[0]
                for href in (
                    '/',
                    '/predios/',
                    '/calendario/',
                    '/decisiones/',
                    '/busqueda/',
                    '/reportes/',
                    '/actividades/',
                    '/productos/',
                ):
                    self.assertIn(f'href="{href}"', main_nav)

    def test_decision_center_tools_and_single_sidebar_link(self):
        response = self.client.get('/decisiones/')
        self.assertContains(response, 'Centro de decisiones')  # título de la pestaña del navegador
        self.assertNotContains(response, 'Calcula, planifica y optimiza los recursos de tus predios')  # sin encabezado
        for text in ('Cuánto llevar', '¿Me alcanza?'):
            self.assertContains(response, text)
        enlaces = [item for item in response.context['menu'] if item.get('href') == '/decisiones/']
        self.assertEqual(
            [item['label'] for item in enlaces], ['Toma de decisiones']
        )  # un solo enlace al centro de decisiones

    def test_csrf_required_to_save_terrain(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        potreros, predios = Potrero.objects.count(), Predio.objects.count()
        self.assertEqual(client.post(self.url, self.plot_data()).status_code, 302)
        self.assertEqual(client.post('/predios/nuevo/', {}).status_code, 302)
        self.assertEqual((Potrero.objects.count(), Predio.objects.count()), (potreros, predios))  # no se guardó nada


class GeometryTests(TestCase):
    def test_area_reference_and_orientation(self):
        # Approximately a 111 m x 93 m rectangle at latitude 33.686° S.
        area = polygon_area(POLYGON)
        self.assertGreater(area, Decimal('10200'))
        self.assertLess(area, Decimal('10500'))
        self.assertEqual(area, polygon_area(list(reversed(POLYGON))))

    def test_invalid_polygons(self):
        examples = [
            [],
            POLYGON[:2],
            [POLYGON[0]] * 3,
            [POLYGON[0], POLYGON[2], POLYGON[1], POLYGON[3]],
            [[0, 0], [1, 1], [2, 2]],
            [[0, 0], [1, float('nan')], [2, 1]],
            [[True, 0], [1, 0], [1, 1]],
            [[181, 0], [1, 0], [1, 1]],
            [[0, 0], [1, 0], [1, 1]] * 100,
            {'coordinates': POLYGON},
        ]
        for points in examples:
            with self.subTest(points=points), self.assertRaises(ValidationError):
                polygon_area(points)


class CatalogSliceTests(TestCase):
    def test_catalog_contract_exposes_seed_densities(self):
        for nombre, densidad in (('Maíz', '25'), ('Trigo', '160')):
            Cultivo.objects.create(nombre=nombre, densidad_siembra=Decimal(densidad), unidad_siembra='kg')
        user = User.objects.create_user('catalog', password=PASSWORD)
        self.client.force_login(user)
        response = self.client.get('/decisiones/')
        self.assertTrue(response.context['catalog']['fromDb'])
        seeds = {item['crop']: item['density'] for item in response.context['catalog']['semillas']}
        self.assertEqual(seeds['Maíz'], 25.0)
        self.assertEqual(seeds['Trigo'], 160.0)
        maize_seed = next(item for item in response.context['catalog']['semillas'] if item['crop'] == 'Maíz')
        self.assertEqual(maize_seed['densitySource'], Cultivo.objects.get(nombre='Maíz').densidad_fuente)

    def test_catalog_does_not_invent_missing_seed_density(self):
        Cultivo.objects.create(nombre='Cultivo sin dosis')
        user = User.objects.create_user('catalog-empty', email='catalog-empty@example.com', password=PASSWORD)
        self.client.force_login(user)
        response = self.client.get('/decisiones/')
        self.assertEqual(response.context['catalog']['semillas'], [])


class ServerCalculationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('calculator', email='calculator@example.com', password=PASSWORD)
        self.other = User.objects.create_user('other', email='other@example.com', password=PASSWORD)
        self.crop = Cultivo.objects.create(nombre='Maíz')
        self.farm = Predio.objects.create(
            propietario=self.user, nombre='Campo de cálculo', ubicacion='Melipilla', superficie_ha=10
        )
        self.plot = Potrero.objects.create(predio=self.farm, nombre='Lote 1', cultivo=self.crop, superficie_ha=4)
        self.client.force_login(self.user)

    def calculate(self, **payload):
        return self.client.post(
            '/calculos/ejecutar/',
            data=json.dumps(payload),
            content_type='application/json',
        )

    def test_yield_estimation_is_no_longer_offered(self):
        response = self.calculate(
            operation='yield', plot_id=self.plot.pk, crop=self.crop.nombre, area='1', rate='4', source='x'
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CalculoAgricola.objects.exists())

    def test_standard_dose_calculation_keeps_area_times_dose_formula(self):
        response = self.calculate(
            operation='dose',
            plot_id=self.plot.pk,
            crop=self.crop.nombre,
            area='2.5',
            dose='12',
            unit='kg',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['amount'], 30)
        self.assertEqual(response.json()['unit'], 'kg')

    def test_seed_population_uses_explicit_parameters_and_source(self):
        response = self.calculate(
            operation='seed_population',
            plot_id=self.plot.pk,
            area='1',
            plants_m2='100',
            thousand_seed_weight_g='300',
            germination_pct='90',
            purity_pct='95',
            source='Ficha de semilla',
        )
        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(response.json()['rate'], 100 * 300 / (100 * 0.9 * 0.95))
        self.assertEqual(
            self.calculate(
                operation='seed_population',
                plot_id=self.plot.pk,
                area='1',
                plants_m2='100',
                thousand_seed_weight_g='300',
                germination_pct='101',
                purity_pct='95',
                source='Ficha de semilla',
            ).status_code,
            400,
        )

    def test_coverage_preserves_unregistered_units_and_computes_availability(self):
        kind = TipoInsumo.objects.create(nombre='Fertilizante')
        product = Insumo.objects.create(tipo=kind, nombre='Producto sin unidad')
        response = self.calculate(
            operation='coverage',
            plot_id=self.plot.pk,
            crop=self.crop.nombre,
            product_id=product.pk,
            area='2',
            dose='3',
            stock='3',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['needed'], 6)
        self.assertEqual(response.json()['coveredHa'], 1)
        self.assertEqual(response.json()['unit'], '')

    def test_calculation_rejects_another_users_plot(self):
        other_farm = Predio.objects.create(
            propietario=self.other, nombre='Otro campo', ubicacion='Paine', superficie_ha=2
        )
        other_plot = Potrero.objects.create(predio=other_farm, nombre='Privado', superficie_ha=1)
        response = self.calculate(
            operation='dose', plot_id=other_plot.pk, crop=self.crop.nombre, area='1', dose='4', unit='kg'
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('no pertenece', response.json()['error'])
        self.assertFalse(CalculoAgricola.objects.exists())

    def test_calculations_are_saved_and_report_history_is_user_scoped(self):
        response = self.calculate(
            operation='dose',
            calculation_type='semillas',
            plot_id=self.plot.pk,
            crop=self.crop.nombre,
            area='2',
            dose='15',
            unit='kg',
        )
        self.assertEqual(response.status_code, 200)
        calculation = CalculoAgricola.objects.get(pk=response.json()['calculation_id'])
        self.assertEqual(calculation.usuario, self.user)
        self.assertEqual(calculation.potrero, self.plot)
        self.assertEqual(calculation.operacion, CalculoAgricola.Operacion.DOSIS)
        self.assertEqual(calculation.entrada['area_ha'], '2')
        self.assertEqual(calculation.entrada['calculation_type'], 'semillas')
        self.assertEqual(calculation.resultado['amount'], 30)

        other_farm = Predio.objects.create(
            propietario=self.other,
            nombre='Campo ajeno',
            ubicacion='Paine',
            superficie_ha=2,
        )
        other_plot = Potrero.objects.create(
            predio=other_farm,
            nombre='Lote ajeno',
            superficie_ha=1,
        )
        private_calculation = CalculoAgricola.objects.create(
            usuario=self.other,
            potrero=other_plot,
            operacion=CalculoAgricola.Operacion.RENDIMIENTO,
            entrada={'calculation_type': 'rendimiento'},
            resultado={'production': 10},
        )
        report = self.client.get('/reportes/')
        self.assertEqual(
            [item['id'] for item in report.context['report_records']],
            [calculation.pk],
        )
        self.assertNotContains(report, 'Campo ajeno')
        self.assertContains(report, 'Cálculo guardado')

    def test_calculation_history_cannot_be_deleted(self):
        calculation = CalculoAgricola.objects.create(
            usuario=self.user,
            potrero=self.plot,
            operacion=CalculoAgricola.Operacion.DOSIS,
            entrada={},
            resultado={},
        )
        with self.assertRaises(ValidationError):
            calculation.delete()
        with self.assertRaises(ValidationError):
            CalculoAgricola.objects.filter(pk=calculation.pk).delete()

    def test_phytosanitary_coverage_requires_verified_authorized_target(self):
        kind = TipoInsumo.objects.create(nombre='Herbicida')
        product = Insumo.objects.create(
            tipo=kind,
            cultivo=self.crop,
            nombre='Producto autorizado',
            dosis_hectarea=Decimal('1.5'),
            unidad_dosis='L/ha',
            estado_verificacion='verified',
            fuente_nombre='Etiqueta',
            fuente_url='https://example.com/source',
            fecha_verificacion='2026-01-01',
            dosis_verificada=True,
            dosis_fuente='Etiqueta',
            etiqueta_url='https://example.com/label',
            numero_autorizacion_sag='REG-1',
            cultivos_autorizados=[self.crop.nombre],
            objetivos_control=['Maleza A'],
        )
        payload = {
            'operation': 'coverage',
            'plot_id': self.plot.pk,
            'crop': self.crop.nombre,
            'product_id': product.pk,
            'area': '2',
            'dose': '99',
            'stock': '3',
        }
        invalid = self.calculate(**payload)
        self.assertEqual(invalid.status_code, 400)
        valid = self.calculate(**payload, target='Maleza A')
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.json()['dose'], 1.5)
        self.assertEqual(valid.json()['coveredHa'], 2)

    def test_verified_maximum_dose_is_enforced_and_expired_products_are_rejected(self):
        kind = TipoInsumo.objects.create(nombre='Fertilizante')
        product = Insumo.objects.create(
            tipo=kind,
            cultivo=self.crop,
            nombre='Fertilizante con máximo',
            dosis_hectarea=Decimal('1'),
            unidad_dosis='kg/ha',
            dosis_maxima_hectarea=Decimal('1.5'),
            unidad_dosis_maxima='kg/ha',
            estado_verificacion='verified',
            fuente_nombre='Etiqueta técnica',
            fuente_url='https://example.com/max-dose',
            fecha_verificacion=timezone.localdate(),
            dosis_verificada=True,
            dosis_fuente='Etiqueta técnica',
        )
        excessive = self.calculate(
            operation='dose',
            plot_id=self.plot.pk,
            crop=self.crop.nombre,
            product_id=product.pk,
            area='1',
            dose='1.6',
            unit='kg',
        )
        # Superar la dosis máxima documentada ya no bloquea: calcula y entrega una advertencia.
        self.assertEqual(excessive.status_code, 200)
        data = excessive.json()
        self.assertTrue(data['above_max'])
        self.assertEqual((data['max_dose'], data['max_unit']), (1.5, 'kg/ha'))
        self.assertEqual(data['origin'], 'adjusted')

        product.vigencia_hasta = timezone.localdate() - timedelta(days=1)
        product.save(update_fields=['vigencia_hasta'])
        expired = self.calculate(
            operation='dose',
            plot_id=self.plot.pk,
            crop=self.crop.nombre,
            product_id=product.pk,
            area='1',
            dose='1',
            unit='kg',
        )
        self.assertEqual(expired.status_code, 400)
        self.assertIn('no existe o no está vigente', expired.json()['error'])

    def test_fertilizer_nutrition_requires_verified_composition_and_need_source(self):
        kind = TipoInsumo.objects.create(nombre='Fertilizante')
        product = Insumo.objects.create(
            tipo=kind,
            nombre='Fertilizante N',
            unidad_dosis='kg/ha',
            estado_verificacion='verified',
            fuente_nombre='Ficha técnica',
            fuente_url='https://example.com/fertilizer',
            fecha_verificacion='2026-01-01',
            composicion_nutricional='10% N',
            nitrogeno_pct=Decimal('10'),
        )
        response = self.calculate(
            operation='fertilizer_nutrition',
            plot_id=self.plot.pk,
            area='2',
            product_id=product.pk,
            nutrient='N',
            need_per_ha='5',
            need_source='Análisis de suelo',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['amount'], 100)
        self.assertEqual(
            self.calculate(
                operation='fertilizer_nutrition',
                plot_id=self.plot.pk,
                area='2',
                product_id=product.pk,
                nutrient='N',
                need_per_ha='5',
            ).status_code,
            400,
        )

    def test_calculation_endpoint_enforces_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(
            '/calculos/ejecutar/',
            data=json.dumps(
                {'operation': 'dose', 'plot_id': self.plot.pk, 'area': '1', 'dose': '4', 'unit': 'kg', 'crop': 'Maíz'}
            ),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)


class ActividadAgricolaTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            'activity-owner@example.com',
            email='activity-owner@example.com',
            password=PASSWORD,
        )
        self.other = User.objects.create_user(
            'activity-other@example.com',
            email='activity-other@example.com',
            password=PASSWORD,
        )
        self.predio = Predio.objects.create(
            propietario=self.user,
            nombre='Campo de actividades',
            ubicacion='Melipilla',
            superficie_ha=Decimal('8'),
        )
        self.potrero = Potrero.objects.create(
            predio=self.predio,
            nombre='Lote norte',
            superficie_ha=Decimal('2'),
        )
        other_farm = Predio.objects.create(
            propietario=self.other,
            nombre='Campo ajeno',
            ubicacion='Paine',
            superficie_ha=Decimal('4'),
        )
        self.other_plot = Potrero.objects.create(
            predio=other_farm,
            nombre='Lote ajeno',
            superficie_ha=Decimal('1'),
        )
        self.client.force_login(self.user)
        self.create_url = '/actividades/nueva/'

    def activity_data(self, **values):
        return {
            'nombre': 'Aplicar riego',
            'tipo': 'riego',
            'potrero': self.potrero.pk,
            'fecha_programada': '2026-11-05',
            'estado': 'pendiente',
            'observaciones': 'Revisar humedad del suelo.',
            **values,
        }

    def test_create_activity_persists_across_a_new_authenticated_session(self):
        response = self.client.post(self.create_url, self.activity_data())
        activity = ActividadAgricola.objects.get()
        self.assertRedirects(response, f'/actividades/{activity.pk}/')
        self.assertEqual(activity.usuario, self.user)
        self.assertEqual(activity.potrero, self.potrero)
        self.assertEqual(activity.estado, ActividadAgricola.Estado.PENDIENTE)

        self.client.logout()
        another_session = Client()
        another_session.force_login(self.user)
        self.assertContains(another_session.get('/actividades/'), 'Aplicar riego')
        self.assertContains(another_session.get('/'), 'Aplicar riego')

    def test_dashboard_counters_use_owned_farm_data(self):
        crop = Cultivo.objects.create(nombre='Cultivo del potrero')
        self.potrero.cultivo = crop
        self.potrero.save(update_fields=['cultivo'])
        response = self.client.get('/')
        stats = response.context['farm_data']['stats']
        self.assertEqual(stats['predios'], 1)
        self.assertEqual(stats['potreros'], 1)
        self.assertEqual(stats['superficie_predios'], 8.0)
        self.assertEqual(stats['superficie'], 2.0)
        self.assertEqual(stats['superficie_sin_asignar'], 6.0)
        self.assertEqual(stats['distribucion_pct'], 25.0)
        self.assertEqual(stats['cultivos'], 1)
        self.assertEqual(response.context['pending_activity_count'], 0)
        self.assertContains(response, '8,00')
        self.assertContains(response, '2,00')
        self.assertContains(response, 'Mis predios')
        self.assertContains(response, 'Potreros activos')
        self.assertContains(response, 'Distribución de superficie')
        self.assertContains(response, 'Calculadoras')
        self.assertContains(response, 'Fertilizantes')
        self.assertNotContains(response, '<nav class="card quick-links"')
        self.assertContains(response, f'/predios/{self.predio.pk}/')
        self.assertContains(response, f'/predios/{self.predio.pk}/potreros/{self.potrero.pk}/editar/')

    def test_dashboard_surfaces_and_counts_are_isolated_and_refresh_after_registration(self):
        response = self.client.get('/')
        stats = response.context['farm_data']['stats']
        self.assertEqual(stats['predios'], 1)
        self.assertEqual(stats['potreros'], 1)
        self.assertEqual(stats['superficie_predios'], 8.0)
        self.assertEqual(stats['superficie'], 2.0)
        self.assertEqual(stats['cultivos'], 0)
        self.assertNotContains(response, 'Campo ajeno')

        boundary = [[-71.216, -33.687], [-71.213, -33.687], [-71.213, -33.684], [-71.216, -33.684]]
        feature = {
            'type': 'Feature',
            'properties': {},
            'geometry': {'type': 'Polygon', 'coordinates': [boundary + [boundary[0]]]},
        }
        create_response = self.client.post(
            '/predios/nuevo/',
            {
                'nombre': 'Nuevo dashboard',
                'ubicacion': 'Paine',
                'superficie_ha': '',
                'geojson': json.dumps(feature),
                'perimetro_aceptado': 'on',
            },
        )
        self.assertEqual(create_response.status_code, 302)
        dashboard = self.client.get('/')
        self.assertEqual(dashboard.context['farm_data']['stats']['predios'], 2)
        self.assertAlmostEqual(
            dashboard.context['farm_data']['stats']['superficie_predios'],
            8 + float(polygon_area(boundary) / 10000),
            places=3,
        )
        self.assertContains(dashboard, 'Nuevo dashboard')

    def test_dashboard_refreshes_assigned_area_and_active_crop_after_registering_plot(self):
        crop = Cultivo.objects.create(nombre='Trigo de dashboard')
        response = self.client.post(
            f'/predios/{self.predio.pk}/potreros/nuevo/',
            {
                'nombre': 'Lote nuevo',
                'cultivo': crop.pk,
                'variedad': '',
                'fecha_siembra': '',
                'fecha_cosecha': '',
                'superficie_ha': '1.5',
                'latitud': '',
                'longitud': '',
                'perimetro': '[]',
            },
        )
        self.assertEqual(response.status_code, 302)
        dashboard = self.client.get('/')
        stats = dashboard.context['farm_data']['stats']
        self.assertEqual(stats['potreros'], 2)
        self.assertEqual(stats['superficie'], 3.5)
        self.assertEqual(stats['superficie_sin_asignar'], 4.5)
        self.assertEqual(stats['cultivos'], 1)
        self.assertContains(dashboard, 'Lote nuevo')
        self.assertContains(dashboard, 'Trigo de dashboard')

    def test_dashboard_empty_states_and_calculation_history_do_not_use_fictional_data(self):
        self.client.force_login(self.other)
        response = self.client.get('/')
        stats = response.context['farm_data']['stats']
        self.assertEqual(stats['predios'], 1)
        self.assertEqual(stats['potreros'], 1)
        self.assertContains(response, 'No hay actividades pendientes guardadas')
        self.assertContains(response, 'Aún no has guardado cálculos')
        self.assertNotContains(response, 'Aún no hay historial persistente de cálculos')
        self.assertContains(response, 'Centro de decisiones')

        self.client.force_login(
            User.objects.create_user(
                'dashboard-empty@example.com',
                email='dashboard-empty@example.com',
                password=PASSWORD,
            )
        )
        empty_response = self.client.get('/')
        self.assertEqual(empty_response.context['farm_data']['stats']['predios'], 0)
        self.assertEqual(empty_response.context['farm_data']['stats']['potreros'], 0)
        # Sin predios, Inicio muestra una sola tarjeta de arranque (un único «Registrar predio»), no cinco
        # contadores en cero ni cuatro tarjetas vacías que repiten el mismo mensaje.
        visible = re.sub(r'<script.*?</script>', '', empty_response.content.decode(), flags=re.S)
        self.assertEqual(visible.count('class="btn-primary-agro" href="/predios/nuevo/"'), 1)
        self.assertIn('Empieza registrando tu primer predio', visible)
        self.assertIn('Explorar el catálogo agrícola', visible)
        for removed in ('Superficie asignada a potreros', 'Potreros activos', 'Distribución de superficie', 'Actividades pendientes'):
            self.assertNotIn(removed, visible)
        self.assertNotIn('class="journey no-print"', visible)  # la ruta de trabajo no repite el mismo CTA

    def test_edit_and_status_updates_are_persistent_and_dashboard_count_is_real(self):
        activity = ActividadAgricola.objects.create(
            usuario=self.user,
            potrero=self.potrero,
            nombre='Preparar suelo',
            tipo=ActividadAgricola.Tipo.PREPARACION,
            fecha_programada='2026-10-30',
        )
        self.assertContains(
            self.client.get(f'/actividades/{activity.pk}/editar/'),
            'value="2026-10-30"',
        )
        response = self.client.post(
            f'/actividades/{activity.pk}/editar/',
            self.activity_data(nombre='Riego actualizado', tipo='riego'),
        )
        self.assertRedirects(response, f'/actividades/{activity.pk}/')
        activity.refresh_from_db()
        self.assertEqual(activity.nombre, 'Riego actualizado')
        self.assertEqual(activity.tipo, ActividadAgricola.Tipo.RIEGO)

        dashboard = self.client.get('/')
        self.assertEqual(dashboard.context['pending_activity_count'], 1)
        self.assertContains(dashboard, 'Actividades pendientes')
        self.assertContains(dashboard, 'Riego actualizado')
        self.assertContains(dashboard, 'Pendiente')
        response = self.client.post(
            f'/actividades/{activity.pk}/estado/',
            {'estado': 'completada', 'next': 'dashboard'},
        )
        self.assertRedirects(response, '/')
        activity.refresh_from_db()
        self.assertEqual(activity.estado, ActividadAgricola.Estado.COMPLETADA)
        self.assertEqual(self.client.get('/').context['pending_activity_count'], 0)
        self.assertContains(self.client.get('/actividades/'), 'Riego actualizado')

    def test_invalid_activity_fields_and_foreign_potrero_are_rejected(self):
        invalid_values = [
            self.activity_data(nombre=''),
            self.activity_data(tipo='fumigación'),
            self.activity_data(fecha_programada='no-es-fecha'),
            self.activity_data(potrero=self.other_plot.pk),
        ]
        for data in invalid_values:
            with self.subTest(data=data):
                response = self.client.post(self.create_url, data)
                self.assertEqual(response.status_code, 200)
        self.assertFalse(ActividadAgricola.objects.exists())

    def test_user_cannot_read_edit_change_or_delete_another_users_activity(self):
        activity = ActividadAgricola.objects.create(
            usuario=self.other,
            potrero=self.other_plot,
            nombre='Actividad privada',
            tipo=ActividadAgricola.Tipo.SIEMBRA,
            fecha_programada='2026-11-01',
        )
        detail_url = f'/actividades/{activity.pk}/'
        for url in [detail_url, f'/actividades/{activity.pk}/editar/', f'/actividades/{activity.pk}/eliminar/']:
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(
            self.client.post(f'/actividades/{activity.pk}/estado/', {'estado': 'completada'}).status_code,
            404,
        )
        self.assertNotContains(self.client.get('/actividades/'), 'Actividad privada')
        self.assertNotContains(self.client.get('/'), 'Actividad privada')
        self.assertNotContains(self.client.get('/calendario/'), 'Actividad privada')
        activity.refresh_from_db()
        self.assertEqual(activity.estado, ActividadAgricola.Estado.PENDIENTE)

    def test_delete_requires_confirmation_and_post(self):
        activity = ActividadAgricola.objects.create(
            usuario=self.user,
            potrero=self.potrero,
            nombre='Actividad a eliminar',
            tipo=ActividadAgricola.Tipo.MONITOREO,
            fecha_programada='2026-11-10',
        )
        delete_url = f'/actividades/{activity.pk}/eliminar/'
        self.assertContains(self.client.get(delete_url), 'Esta acción no se puede deshacer')
        self.assertTrue(ActividadAgricola.objects.filter(pk=activity.pk).exists())
        response = self.client.post(delete_url)
        self.assertRedirects(response, '/actividades/')
        self.assertFalse(ActividadAgricola.objects.filter(pk=activity.pk).exists())

    def test_invalid_status_is_rejected_and_activity_routes_require_authentication(self):
        activity = ActividadAgricola.objects.create(
            usuario=self.user,
            potrero=self.potrero,
            nombre='Regar',
            tipo=ActividadAgricola.Tipo.RIEGO,
            fecha_programada='2026-11-10',
        )
        response = self.client.post(
            f'/actividades/{activity.pk}/estado/',
            {'estado': 'inventado'},
        )
        self.assertRedirects(response, f'/actividades/{activity.pk}/')
        activity.refresh_from_db()
        self.assertEqual(activity.estado, ActividadAgricola.Estado.PENDIENTE)

        self.client.logout()
        for url in [
            '/actividades/',
            '/actividades/nueva/',
            f'/actividades/{activity.pk}/',
            f'/actividades/{activity.pk}/editar/',
            f'/actividades/{activity.pk}/eliminar/',
        ]:
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_status_endpoint_enforces_csrf(self):
        activity = ActividadAgricola.objects.create(
            usuario=self.user,
            potrero=self.potrero,
            nombre='Revisar cultivo',
            tipo=ActividadAgricola.Tipo.MONITOREO,
            fecha_programada='2026-11-10',
        )
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(
            f'/actividades/{activity.pk}/estado/',
            {'estado': 'completada'},
        )
        # Sin token CSRF nada se ejecuta: se vuelve a la página con un aviso (no la pantalla de error de Django).
        self.assertEqual(response.status_code, 302)
        activity.refresh_from_db()
        self.assertEqual(activity.estado, ActividadAgricola.Estado.PENDIENTE)


class AgronomicCalendarTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='calendar-user',
            email='calendar@example.com',
            password=PASSWORD,
        )
        self.client.force_login(self.user)
        self.crop = Cultivo.objects.create(nombre='Trigo de prueba')
        self.zone = Zona.objects.create(nombre='Zona de prueba')

    def create_reference(self, **values):
        data = {
            'cultivo': self.crop,
            'zona': self.zone,
            'siembra_inicio_mes': 2,
            'siembra_inicio_dia': 1,
            'siembra_fin_mes': 2,
            'siembra_fin_dia': 10,
            'cosecha_inicio_mes': 7,
            'cosecha_inicio_dia': 1,
            'cosecha_fin_mes': 8,
            'cosecha_fin_dia': 31,
            'duracion_ciclo_dias': 120,
            'institucion_fuente': 'Institución técnica',
            'documento_fuente': 'Manual agronómico',
            'url_fuente': 'https://example.org/manual',
            'fecha_consulta_fuente': '2026-10-01',
            'estado': ReferenciaAgronomica.Estado.VERIFICADA,
        }
        data.update(values)
        return ReferenciaAgronomica.objects.create(**data)

    def evaluate(self, **values):
        return self.client.post(
            '/calendario/evaluar/',
            data=json.dumps(
                {
                    'evaluation': 'sowing',
                    'crop_id': self.crop.pk,
                    'zone_id': self.zone.pk,
                    'proposed_date': '2026-02-05',
                    **values,
                }
            ),
            content_type='application/json',
        )

    def test_calendar_without_own_zone_opens_the_zone_with_most_windows(self):
        zone_a = Zona.objects.create(nombre='Zona A')
        zone_b = Zona.objects.create(nombre='Zona B')
        # 'Zona de prueba' tiene 1 ventana, 'Zona B' tiene 2 y 'Zona A' ninguna.
        self.create_reference()
        self.create_reference(zona=zone_b)
        self.create_reference(zona=zone_b, variedad='Otra')
        response = self.client.get('/calendario/')
        self.assertEqual(response.context['calendar_default_zone'], zone_b.pk)
        # Una zona indicada en la URL sigue teniendo prioridad.
        self.assertEqual(
            self.client.get(f'/calendario/?zone={zone_a.pk}').context['calendar_default_zone'],
            zone_a.pk,
        )
        # Con la misma cantidad de ventanas gana la primera por nombre.
        self.create_reference(zona=zone_a)
        self.create_reference(zona=zone_a, variedad='Otra')
        response = self.client.get('/calendario/')
        self.assertEqual(response.context['calendar_default_zone'], zone_a.pk)

    def test_calendar_displays_only_documented_verified_references(self):
        verified = self.create_reference()
        self.create_reference(
            variedad='Sin fuente',
            estado=ReferenciaAgronomica.Estado.BORRADOR,
            institucion_fuente='',
            documento_fuente='',
            url_fuente='',
            fecha_consulta_fuente=None,
        )
        response = self.client.get('/calendario/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Cuándo sembrar')
        self.assertContains(response, 'calendar-references')
        self.assertEqual(
            [reference['id'] for reference in response.context['calendar_references']],
            [verified.pk, verified.pk + 1],
        )
        self.assertNotContains(response, 'Referencia general para la Zona Central')

    def test_calendar_marks_unverified_references_and_ignores_invalid_filters(self):
        self.create_reference()
        response = self.client.get('/calendario/')
        self.assertFalse(response.context['calendar_has_unverified'])
        self.assertNotContains(response, 'sin fuente verificada')
        self.create_reference(
            variedad='Sin fuente',
            estado=ReferenciaAgronomica.Estado.BORRADOR,
            institucion_fuente='',
            documento_fuente='',
            url_fuente='',
            fecha_consulta_fuente=None,
        )
        response = self.client.get('/calendario/?crop=abc&zone=xyz&year=zzz')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['calendar_has_unverified'])
        self.assertContains(response, 'sin fuente verificada')
        self.assertEqual(response.context['calendar_selected_crop'], '')
        self.assertEqual(response.context['calendar_default_zone'], self.zone.pk)
        self.assertEqual(response.context['calendar_current_year'], timezone.localdate().year)

    def test_missing_verified_reference_returns_insufficient_information(self):
        response = self.evaluate()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'insufficient')
        self.assertIsNone(response.json()['source'])

    def test_sowing_window_classifies_inside_near_and_outside_using_configuration(self):
        self.create_reference()
        ConfiguracionAgronomica.objects.create(margen_ventana_dias=15)
        cases = [
            ('2026-02-05', 'within'),
            ('2026-01-17', 'near'),
            ('2026-01-16', 'outside'),
        ]
        for proposed_date, expected_status in cases:
            with self.subTest(proposed_date=proposed_date):
                response = self.evaluate(proposed_date=proposed_date)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['status'], expected_status)
                self.assertEqual(response.json()['margin_days'], 15)
                self.assertEqual(response.json()['source']['institution'], 'Institución técnica')

    def test_sowing_window_crossing_calendar_year_is_classified_correctly(self):
        self.create_reference(
            siembra_inicio_mes=11,
            siembra_inicio_dia=20,
            siembra_fin_mes=2,
            siembra_fin_dia=10,
        )
        response = self.evaluate(proposed_date='2027-01-15')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'within')

    def test_variety_specific_reference_is_preferred_over_general_reference(self):
        self.create_reference(institucion_fuente='Fuente general')
        self.create_reference(
            variedad='Línea A',
            institucion_fuente='Fuente de variedad',
            siembra_inicio_mes=3,
            siembra_inicio_dia=1,
            siembra_fin_mes=3,
            siembra_fin_dia=10,
        )
        response = self.evaluate(
            variety='Línea A',
            proposed_date='2026-03-05',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'within')
        self.assertEqual(response.json()['source']['institution'], 'Fuente de variedad')

    def test_harvest_compares_window_and_documented_cycle(self):
        reference = self.create_reference()
        response = self.client.post(
            '/calendario/evaluar/',
            data=json.dumps(
                {
                    'evaluation': 'harvest',
                    'crop_id': self.crop.pk,
                    'zone_id': self.zone.pk,
                    'sowing_date': '2026-03-01',
                    'proposed_date': '2026-07-01',
                }
            ),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'within')
        self.assertEqual(response.json()['expected_cycle_days'], reference.duracion_ciclo_dias)
        self.assertEqual(response.json()['elapsed_days'], 122)
        self.assertIn('madurez real', response.json()['disclaimer'])

    def test_harvest_reports_near_and_cycle_outside_states(self):
        self.create_reference()
        base_payload = {
            'evaluation': 'harvest',
            'crop_id': self.crop.pk,
            'zone_id': self.zone.pk,
            'sowing_date': '2026-03-01',
        }
        near = self.client.post(
            '/calendario/evaluar/',
            data=json.dumps({**base_payload, 'proposed_date': '2026-06-16'}),
            content_type='application/json',
        )
        self.assertEqual(near.status_code, 200)
        self.assertEqual(near.json()['status'], 'near')
        outside_cycle = self.client.post(
            '/calendario/evaluar/',
            data=json.dumps({**base_payload, 'proposed_date': '2026-08-31'}),
            content_type='application/json',
        )
        self.assertEqual(outside_cycle.status_code, 200)
        self.assertEqual(outside_cycle.json()['status'], 'outside')

    def test_harvest_rejects_invalid_order_and_endpoint_requires_login_and_post(self):
        self.create_reference()
        invalid_order = self.client.post(
            '/calendario/evaluar/',
            data=json.dumps(
                {
                    'evaluation': 'harvest',
                    'crop_id': self.crop.pk,
                    'zone_id': self.zone.pk,
                    'sowing_date': '2026-07-01',
                    'proposed_date': '2026-03-01',
                }
            ),
            content_type='application/json',
        )
        self.assertEqual(invalid_order.status_code, 400)
        self.assertContains(invalid_order, 'no puede ser anterior', status_code=400)

        self.assertEqual(self.client.get('/calendario/evaluar/').status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post('/calendario/evaluar/').status_code, 302)

    def test_reference_requires_complete_valid_windows_and_source_when_verified(self):
        reference = ReferenciaAgronomica(
            cultivo=self.crop,
            zona=self.zone,
            siembra_inicio_mes=2,
            siembra_inicio_dia=30,
        )
        with self.assertRaises(ValidationError):
            reference.full_clean()

        incomplete_source = ReferenciaAgronomica(
            cultivo=self.crop,
            zona=self.zone,
            estado=ReferenciaAgronomica.Estado.VERIFICADA,
        )
        with self.assertRaises(ValidationError):
            incomplete_source.full_clean()

    def test_evaluator_rejects_non_json_and_invalid_catalog_ids(self):
        self.assertEqual(
            self.client.post('/calendario/evaluar/', {'evaluation': 'sowing'}).status_code,
            415,
        )
        response = self.client.post(
            '/calendario/evaluar/',
            data=json.dumps(
                {
                    'evaluation': 'sowing',
                    'crop_id': 99999,
                    'zone_id': self.zone.pk,
                    'proposed_date': '2026-02-05',
                }
            ),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_evaluation_endpoint_enforces_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(
            '/calendario/evaluar/',
            data=json.dumps(
                {
                    'evaluation': 'sowing',
                    'crop_id': self.crop.pk,
                    'zone_id': self.zone.pk,
                    'proposed_date': '2026-02-05',
                }
            ),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)


class TemporadaCultivoTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='season-user',
            email='season@example.com',
            password=PASSWORD,
        )
        self.other = User.objects.create_user(
            username='other-season-user',
            email='other-season@example.com',
            password=PASSWORD,
        )
        self.crop = Cultivo.objects.create(nombre='Cultivo legado')
        self.new_crop = Cultivo.objects.create(nombre='Cultivo nuevo')
        self.predio = Predio.objects.create(
            propietario=self.user,
            nombre='Predio temporadas',
            ubicacion='Melipilla',
            superficie_ha=5,
        )
        self.potrero = Potrero.objects.create(
            predio=self.predio,
            nombre='Potrero rotación',
            cultivo=self.crop,
            variedad='Variedad antigua',
            fecha_siembra='2025-09-01',
            fecha_cosecha='2026-02-01',
            superficie_ha=2,
        )
        self.client.force_login(self.user)
        self.create_url = f'/predios/{self.predio.pk}/potreros/{self.potrero.pk}/temporadas/nueva/'

    def season_data(self, **values):
        return {
            'cultivo': self.new_crop.pk,
            'variedad': 'Variedad nueva',
            'fecha_siembra': '2026-09-15',
            'fecha_cosecha': '2027-02-10',
            'estado': TemporadaCultivo.Estado.PLANIFICADA,
            'observaciones': 'Rotación de este ciclo.',
            **values,
        }

    def test_each_new_season_persists_without_overwriting_legacy_potrero_data(self):
        first_response = self.client.post(self.create_url, self.season_data())
        self.assertRedirects(first_response, f'/predios/{self.predio.pk}/')
        first = TemporadaCultivo.objects.get()
        self.assertEqual(first.potrero, self.potrero)
        self.assertEqual(first.cultivo, self.new_crop)
        self.assertEqual(first.variedad, 'Variedad nueva')
        self.assertEqual(first.observaciones, 'Rotación de este ciclo.')

        second_response = self.client.post(
            self.create_url,
            self.season_data(
                cultivo=self.crop.pk,
                variedad='Otra temporada',
                fecha_siembra='2027-09-15',
                fecha_cosecha='2028-02-10',
            ),
        )
        self.assertRedirects(second_response, f'/predios/{self.predio.pk}/')
        self.potrero.refresh_from_db()
        self.assertEqual(self.potrero.cultivo, self.crop)
        self.assertEqual(self.potrero.variedad, 'Variedad antigua')
        self.assertEqual(self.potrero.fecha_siembra.isoformat(), '2025-09-01')
        self.assertEqual(self.potrero.fecha_cosecha.isoformat(), '2026-02-01')
        self.assertEqual(self.potrero.temporadas.count(), 2)
        self.assertContains(self.client.get(f'/predios/{self.predio.pk}/'), 'Variedad nueva')
        self.assertContains(self.client.get(f'/predios/{self.predio.pk}/'), 'Otra temporada')

        self.client.logout()
        self.assertRedirects(
            self.client.post(
                '/login/',
                {'username': 'season-user', 'password': PASSWORD},
            ),
            '/',
        )
        self.assertEqual(
            list(self.potrero.temporadas.values_list('cultivo__nombre', flat=True)),
            ['Cultivo legado', 'Cultivo nuevo'],
        )

    def test_season_validation_and_non_deletion(self):
        response = self.client.post(
            self.create_url,
            self.season_data(
                fecha_siembra='2027-03-01',
                fecha_cosecha='2027-02-28',
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'La cosecha no puede ser anterior a la siembra.')
        self.assertFalse(TemporadaCultivo.objects.exists())

        response = self.client.post(
            self.create_url,
            self.season_data(
                fecha_siembra='',
                fecha_cosecha='',
                estado=TemporadaCultivo.Estado.SEMBRADA,
            ),
        )
        self.assertContains(response, 'Registra la fecha de siembra')
        self.assertFalse(TemporadaCultivo.objects.exists())

        season = TemporadaCultivo.objects.create(
            potrero=self.potrero,
            cultivo=self.new_crop,
            fecha_siembra='2026-09-15',
        )
        with self.assertRaises(ValidationError):
            season.delete()
        with self.assertRaises(ValidationError):
            TemporadaCultivo.objects.filter(pk=season.pk).delete()

    def test_user_cannot_create_or_edit_seasons_for_another_users_plot(self):
        other_farm = Predio.objects.create(
            propietario=self.other,
            nombre='Predio privado',
            ubicacion='Paine',
            superficie_ha=4,
        )
        other_plot = Potrero.objects.create(
            predio=other_farm,
            nombre='Potrero privado',
            superficie_ha=1,
        )
        other_season = TemporadaCultivo.objects.create(
            potrero=other_plot,
            cultivo=self.new_crop,
            fecha_siembra='2026-09-15',
        )
        create_other_url = f'/predios/{other_farm.pk}/potreros/{other_plot.pk}/temporadas/nueva/'
        edit_other_url = f'/predios/{other_farm.pk}/potreros/{other_plot.pk}/' f'temporadas/{other_season.pk}/editar/'
        self.assertEqual(self.client.get(create_other_url).status_code, 404)
        self.assertEqual(self.client.post(create_other_url, self.season_data()).status_code, 404)
        self.assertEqual(self.client.get(edit_other_url).status_code, 404)
        self.assertNotContains(self.client.get(f'/predios/{self.predio.pk}/'), 'Potrero privado')

    def test_calendar_default_zone_uses_only_the_current_users_farm(self):
        first_zone = Zona.objects.create(nombre='Zona A')
        own_zone = Zona.objects.create(nombre='Zona B')
        self.predio.zona = own_zone
        self.predio.save(update_fields=['zona'])
        other_farm = Predio.objects.create(
            propietario=self.other,
            nombre='Predio privado',
            ubicacion='Paine',
            superficie_ha=4,
            zona=first_zone,
        )
        other_plot = Potrero.objects.create(
            predio=other_farm,
            nombre='Potrero privado',
            superficie_ha=1,
        )
        TemporadaCultivo.objects.create(
            potrero=other_plot,
            cultivo=self.crop,
            fecha_siembra='2026-09-15',
        )
        response = self.client.get('/calendario/')
        self.assertEqual(response.context['calendar_default_zone'], own_zone.pk)
        self.assertNotContains(response, 'Predio privado')
        self.assertNotContains(response, 'Potrero privado')
        # Una zona indicada en la URL tiene prioridad; una inexistente se ignora.
        self.assertEqual(
            self.client.get(f'/calendario/?zone={first_zone.pk}').context['calendar_default_zone'],
            first_zone.pk,
        )
        self.assertEqual(
            self.client.get('/calendario/?zone=9999').context['calendar_default_zone'],
            own_zone.pk,
        )


# ---------------------------------------------------------------------------
# Integración de calendario, temporadas, centro de decisiones y catálogo.
# Todos los datos de estas pruebas son ficticios y viven solo en la base de pruebas.
# ---------------------------------------------------------------------------
from datetime import date as _date


def _verified_source():
    return dict(
        institucion_fuente='Institución de prueba',
        documento_fuente='Documento de prueba',
        url_fuente='https://example.org/prueba',
        fecha_consulta_fuente=_date(2026, 10, 1),
        estado=ReferenciaAgronomica.Estado.VERIFICADA,
    )


class IntegrationBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('owner', email='owner@example.com', password=PASSWORD)
        self.other = User.objects.create_user('intruder', email='intruder@example.com', password=PASSWORD)
        self.zone = Zona.objects.create(nombre='Zona prueba')
        self.zone2 = Zona.objects.create(nombre='Zona prueba 2')
        self.maize = Cultivo.objects.create(nombre='Maíz prueba')
        self.wheat = Cultivo.objects.create(nombre='Trigo prueba')
        self.farm = Predio.objects.create(
            propietario=self.user, nombre='Campo', ubicacion='X', superficie_ha=10, zona=self.zone
        )
        self.plot = Potrero.objects.create(predio=self.farm, nombre='Lote A', superficie_ha=4)
        self.other_farm = Predio.objects.create(propietario=self.other, nombre='Ajeno', ubicacion='Y', superficie_ha=5)
        self.other_plot = Potrero.objects.create(predio=self.other_farm, nombre='Lote ajeno', superficie_ha=1)
        self.client.force_login(self.user)

    def post_json(self, url, payload):
        return self.client.post(url, data=json.dumps(payload), content_type='application/json')


class CropAssignmentTests(IntegrationBase):
    def url(self, plot=None):
        return f'/api/potreros/{(plot or self.plot).pk}/cultivo/'

    def test_assign_creates_season_updates_snapshot_and_history(self):
        response = self.post_json(
            self.url(),
            {
                'crop_id': self.maize.pk,
                'variety': 'H1',
                'season': '2026-2027',
                'sowing_date': '2026-10-10',
                'harvest_date': '2027-03-30',
                'status': 'sown',
                'notes': 'Nota',
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.plot.refresh_from_db()
        self.assertEqual(self.plot.cultivo, self.maize)
        self.assertEqual(self.plot.variedad, 'H1')
        self.assertEqual(self.plot.fecha_siembra, _date(2026, 10, 10))
        season = self.plot.temporadas.get()
        self.assertEqual(season.temporada_agricola, '2026-2027')
        self.assertEqual(season.estado, 'sown')
        self.assertTrue(PredioHistorial.objects.filter(potrero=self.plot, accion='crop_assigned').exists())
        data = response.json()['plot']
        self.assertEqual(data['crop'], 'Maíz prueba')
        self.assertEqual(len(data['seasons']), 1)

    def test_changing_crop_keeps_previous_season_and_legacy_crop(self):
        self.plot.cultivo = self.wheat
        self.plot.fecha_siembra = _date(2026, 5, 1)
        self.plot.save()
        response = self.post_json(self.url(), {'crop_id': self.maize.pk, 'sowing_date': '2026-10-01'})
        self.assertEqual(response.status_code, 200, response.content)
        seasons = list(self.plot.temporadas.order_by('creado', 'pk'))
        self.assertEqual([s.cultivo for s in seasons], [self.wheat, self.maize])
        self.assertEqual(seasons[0].fecha_siembra, _date(2026, 5, 1))
        self.assertEqual(seasons[0].estado, 'closed')
        response = self.post_json(self.url(), {'crop_id': self.wheat.pk, 'close_previous': False})
        self.assertEqual(self.plot.temporadas.count(), 3)
        self.assertEqual(self.plot.temporadas.get(cultivo=self.maize).estado, 'planned')

    def test_update_dates_mode_keeps_crop_and_requires_same_crop(self):
        self.post_json(self.url(), {'crop_id': self.maize.pk})
        season = self.plot.temporadas.get()
        response = self.post_json(
            self.url(),
            {
                'mode': 'update_dates',
                'season_id': season.pk,
                'crop_id': self.maize.pk,
                'sowing_date': '2026-10-05',
                'harvest_date': '2027-03-01',
                'status': 'planned',
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.plot.temporadas.count(), 1)
        season.refresh_from_db()
        self.assertEqual(season.fecha_siembra, _date(2026, 10, 5))
        self.plot.refresh_from_db()
        self.assertEqual(self.plot.fecha_cosecha, _date(2027, 3, 1))
        response = self.post_json(
            self.url(), {'mode': 'update_dates', 'season_id': season.pk, 'crop_id': self.wheat.pk}
        )
        self.assertEqual(response.status_code, 400)

    def test_validation_and_isolation(self):
        self.assertEqual(self.post_json(self.url(self.other_plot), {'crop_id': self.maize.pk}).status_code, 404)
        self.assertEqual(self.other_plot.temporadas.count(), 0)
        bad_dates = self.post_json(
            self.url(), {'crop_id': self.maize.pk, 'sowing_date': '2026-10-10', 'harvest_date': '2026-01-01'}
        )
        self.assertEqual(bad_dates.status_code, 400)
        self.assertIn('cosecha', bad_dates.json()['error'])
        self.maize.activo = False
        self.maize.save()
        self.assertEqual(self.post_json(self.url(), {'crop_id': self.maize.pk}).status_code, 400)
        self.assertEqual(self.post_json(self.url(), {'crop_id': self.wheat.pk, 'season': '2026-2030'}).status_code, 400)
        self.assertEqual(self.plot.temporadas.count(), 0)
        self.assertEqual(self.client.get(self.url()).status_code, 405)

    def test_csrf_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(self.url(), data=json.dumps({'crop_id': self.maize.pk}), content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_potrero_form_crop_change_preserves_traceability(self):
        url = f'/predios/{self.farm.pk}/potreros/{self.plot.pk}/editar/'
        base = {
            'nombre': 'Lote A',
            'superficie_ha': '4',
            'perimetro': '[]',
            'variedad': '',
            'fecha_siembra': '',
            'fecha_cosecha': '',
            'latitud': '',
            'longitud': '',
        }
        self.client.post(url, {**base, 'cultivo': self.wheat.pk})
        self.client.post(url, {**base, 'cultivo': self.maize.pk})
        self.assertEqual([s.cultivo for s in self.plot.temporadas.order_by('creado', 'pk')], [self.wheat, self.maize])

    def test_predios_page_selects_plot_and_isolates_other_users(self):
        response = self.client.get(f'/predios/?potrero={self.plot.pk}')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-initial-plot="%s"' % self.plot.pk)
        self.assertContains(response, 'id="crop-dialog"')
        self.assertEqual(self.client.get(f'/predios/?potrero={self.other_plot.pk}').status_code, 404)
        self.assertEqual(self.client.get(f'/api/potreros/{self.other_plot.pk}/').status_code, 404)


class CalendarIntegrationTests(IntegrationBase):
    def setUp(self):
        super().setUp()
        self.ref = ReferenciaAgronomica.objects.create(
            cultivo=self.maize,
            zona=self.zone,
            siembra_inicio_mes=9,
            siembra_inicio_dia=15,
            siembra_fin_mes=11,
            siembra_fin_dia=30,
            cosecha_inicio_mes=3,
            cosecha_inicio_dia=1,
            cosecha_fin_mes=5,
            cosecha_fin_dia=15,
            duracion_ciclo_dias=170,
            **_verified_source(),
        )
        ReferenciaAgronomica.objects.create(
            cultivo=self.wheat,
            zona=self.zone2,
            variedad='Invernal',
            siembra_inicio_mes=5,
            siembra_inicio_dia=1,
            siembra_fin_mes=7,
            siembra_fin_dia=15,
            **_verified_source(),
        )

    def evaluate(self, **payload):
        return self.post_json('/calendario/evaluar/', payload)

    def test_calendar_sends_all_zones_and_references(self):
        response = self.client.get('/calendario/')
        self.assertEqual(response.status_code, 200)
        crops = {item['crop'] for item in response.context['calendar_references']}
        self.assertEqual(crops, {'Maíz prueba', 'Trigo prueba'})
        sent = [
            {'cropId': item['cropId'], 'zoneId': item['zoneId'], 'variety': item['variety']}
            for item in response.context['calendar_references']
        ]
        self.assertIn({'cropId': self.wheat.pk, 'zoneId': self.zone2.pk, 'variety': 'Invernal'}, sent)
        self.assertEqual([zone.pk for zone in response.context['calendar_zones']], [self.zone.pk, self.zone2.pk])
        self.assertEqual(response.context['calendar_default_zone'], self.zone.pk)
        for marker in ('id="cal-board"', 'id="cal-detail"', 'id="zone"', 'id="crop-filter"', 'id="year"'):
            self.assertContains(response, marker)

    def test_year_specific_reference_has_priority(self):
        ReferenciaAgronomica.objects.create(
            cultivo=self.maize,
            zona=self.zone,
            anio_referencia=2027,
            siembra_inicio_mes=10,
            siembra_inicio_dia=20,
            siembra_fin_mes=10,
            siembra_fin_dia=31,
            **_verified_source(),
        )
        within_recurring = self.evaluate(
            evaluation='sowing', crop_id=self.maize.pk, zone_id=self.zone.pk, proposed_date='2026-10-01'
        )
        self.assertEqual(within_recurring.json()['status'], 'within')
        specific = self.evaluate(
            evaluation='sowing', crop_id=self.maize.pk, zone_id=self.zone.pk, proposed_date='2027-10-10'
        )
        self.assertEqual(specific.json()['status'], 'near')
        self.assertEqual(specific.json()['reference_context']['year'], 2027)

    def test_harvest_across_years_and_leap_year(self):
        normal = self.evaluate(
            evaluation='harvest',
            crop_id=self.maize.pk,
            zone_id=self.zone.pk,
            sowing_date='2026-10-01',
            proposed_date='2027-03-20',
        ).json()
        leap = self.evaluate(
            evaluation='harvest',
            crop_id=self.maize.pk,
            zone_id=self.zone.pk,
            sowing_date='2027-10-01',
            proposed_date='2028-03-19',
        ).json()
        self.assertEqual(normal['elapsed_days'], 170)
        self.assertEqual(leap['elapsed_days'], 170)
        self.assertEqual(normal['status'], 'within')
        self.assertEqual(normal['sowing_date'], '2026-10-01')

    def test_harvest_without_documented_cycle_uses_window_only_and_insufficient_without_data(self):
        self.ref.duracion_ciclo_dias = None
        self.ref.save()
        result = self.evaluate(
            evaluation='harvest',
            crop_id=self.maize.pk,
            zone_id=self.zone.pk,
            sowing_date='2026-10-01',
            proposed_date='2027-04-01',
        ).json()
        self.assertEqual(result['status'], 'within')
        self.assertIsNone(result['expected_cycle_days'])
        self.assertIn('no documenta la duración', result['explanation'])
        none = self.evaluate(
            evaluation='harvest',
            crop_id=self.wheat.pk,
            zone_id=self.zone.pk,
            sowing_date='2026-05-01',
            proposed_date='2026-12-01',
        ).json()
        self.assertEqual(none['status'], 'insufficient')
        self.assertNotIn('100', none['explanation'])


class DecisionCenterIntegrationTests(IntegrationBase):
    def setUp(self):
        super().setUp()
        self.plot.cultivo = self.maize
        self.plot.save()
        self.seed_type = TipoInsumo.objects.create(nombre='Semilla')
        self.fert_type = TipoInsumo.objects.create(nombre='Fertilizante')
        verified = dict(
            estado_verificacion='verified',
            fuente_nombre='Fuente',
            fuente_url='https://example.org/f',
            fecha_verificacion=_date(2026, 10, 1),
        )
        self.seed = Insumo.objects.create(
            nombre='Semilla P',
            tipo=self.seed_type,
            cultivo=self.maize,
            dosis_hectarea=Decimal('22'),
            unidad_dosis='kg/ha',
            dosis_verificada=True,
            dosis_fuente='Ficha',
            presentacion_cantidad=Decimal('20'),
            presentacion_unidad='kg',
            variedad_semilla='V1',
            **verified,
        )
        self.fert = Insumo.objects.create(nombre='Fert P', tipo=self.fert_type, cultivo=self.maize)
        self.wheat_fert = Insumo.objects.create(nombre='Fert trigo', tipo=self.fert_type, cultivo=self.wheat)

    def calc(self, **payload):
        return self.post_json('/calculos/ejecutar/', {'plot_id': self.plot.pk, 'crop': 'Maíz prueba', **payload})

    def test_seed_dose_marks_documented_source_and_packages(self):
        data = self.calc(
            operation='dose', calculation_type='semillas', area='3', dose='22', unit='kg/ha', product_id=self.seed.pk
        ).json()
        self.assertEqual(data['amount'], 66)
        self.assertTrue(data['verified'])
        self.assertFalse(data['simulation'])
        self.assertEqual(data['package']['count'], 4)
        self.assertEqual(data['source'], 'Ficha')

    def test_modified_or_undocumented_dose_is_simulation(self):
        data = self.calc(
            operation='dose', calculation_type='semillas', area='3', dose='30', unit='kg/ha', product_id=self.seed.pk
        ).json()
        self.assertTrue(data['simulation'])
        manual = self.calc(
            operation='dose', calculation_type='insumos', area='1', dose='100', unit='kg/ha', product_id=self.fert.pk
        ).json()
        self.assertTrue(manual['simulation'])

    def test_changing_a_catalog_dose_marks_the_result_as_adjusted_manually(self):
        from ..views import describe_calculation

        same = self.calc(
            operation='dose', calculation_type='semillas', area='3', dose='22', unit='kg/ha', product_id=self.seed.pk
        ).json()
        self.assertTrue(same['verified'])
        self.assertFalse(same['adjusted'])
        changed = self.calc(
            operation='dose', calculation_type='semillas', area='3', dose='30', unit='kg/ha', product_id=self.seed.pk
        ).json()
        self.assertTrue(changed['adjusted'])
        self.assertTrue(changed['simulation'])
        self.assertEqual((changed['catalog_dose'], changed['catalog_unit']), (22, 'kg/ha'))
        # Sin dato en el catálogo no hay nada que «ajustar»: es una simulación ingresada a mano.
        manual = self.calc(
            operation='dose', calculation_type='insumos', area='1', dose='100', unit='kg/ha', product_id=self.fert.pk
        ).json()
        self.assertFalse(manual['adjusted'])
        self.assertTrue(manual['simulation'])
        origin = describe_calculation(CalculoAgricola.objects.get(pk=changed['calculation_id']))['origin']
        self.assertIn('Ajustado manualmente', origin)
        self.assertIn('kg/ha', origin)
        self.assertNotIn('Documentado', origin)
        self.assertEqual(
            (same['origin'], changed['origin'], manual['origin']), ('documented', 'adjusted', 'simulation')
        )

    def test_catalog_value_without_full_support_is_labelled_catalog_not_documented(self):
        from ..views import describe_calculation

        kind = TipoInsumo.objects.create(nombre='Otros')
        loose = Insumo.objects.create(
            tipo=kind, nombre='Producto de catálogo sin fuente', dosis_hectarea=Decimal('4'), unidad_dosis='kg/ha'
        )
        data = self.calc(
            operation='dose', calculation_type='insumos', area='2', dose='4', unit='kg/ha', product_id=loose.pk
        ).json()
        self.assertEqual(data['origin'], 'catalog')
        self.assertFalse(data['verified'])
        self.assertFalse(data['above_max'])
        self.assertEqual(data['amount'], 8)
        self.assertIn(
            'Dato de catálogo', describe_calculation(CalculoAgricola.objects.get(pk=data['calculation_id']))['origin']
        )
        # Un valor máximo sin fuente no genera advertencias: solo se advierte contra referencias documentadas.
        loose.dosis_maxima_hectarea = Decimal('3')
        loose.unidad_dosis_maxima = 'kg/ha'
        loose.save()
        again = self.calc(
            operation='dose', calculation_type='insumos', area='2', dose='4', unit='kg/ha', product_id=loose.pk
        ).json()
        self.assertFalse(again['above_max'])

    def test_category_and_crop_mismatches_are_rejected(self):
        self.assertEqual(
            self.calc(
                operation='dose', calculation_type='semillas', area='1', dose='5', unit='kg/ha', product_id=self.fert.pk
            ).status_code,
            400,
        )
        self.assertEqual(
            self.calc(
                operation='dose', calculation_type='insumos', area='1', dose='5', unit='kg/ha', product_id=self.seed.pk
            ).status_code,
            400,
        )
        self.assertEqual(
            self.calc(
                operation='dose',
                calculation_type='insumos',
                area='1',
                dose='5',
                unit='kg/ha',
                product_id=self.wheat_fert.pk,
            ).status_code,
            400,
        )
        self.fert.estado_verificacion = 'restricted'
        self.fert.save()
        self.assertEqual(
            self.calc(
                operation='dose', calculation_type='insumos', area='1', dose='5', unit='kg/ha', product_id=self.fert.pk
            ).status_code,
            400,
        )

    def test_coverage_is_simulation_without_inventory(self):
        data = self.calc(
            operation='coverage',
            calculation_type='coverage',
            area='4',
            dose='22',
            unit='kg/ha',
            stock='44',
            product_id=self.seed.pk,
        ).json()
        self.assertEqual(data['coveredHa'], 2)
        self.assertEqual(data['missingQty'], 44)
        self.assertEqual(data['inventory'], 'simulation')

    def test_catalog_payload_stats_categories_and_varieties(self):
        response = self.client.get('/decisiones/')
        catalog = response.context['catalog']
        self.assertEqual(catalog['stats']['seeds'], 1)
        self.assertEqual(catalog['stats']['fertilizers'], 2)
        self.assertFalse(catalog['stats']['inventoryTracked'])
        self.assertEqual({item['category'] for item in catalog['insumos']}, {'seed', 'fertilizer'})
        self.assertEqual(catalog['varieties'][str(self.maize.pk)], ['V1'])
        for tool in ('data-tool="llevar"', 'data-tool="alcanza"', 'Cálculo avanzado'):
            self.assertContains(response, tool)

    def test_calculator_panel_and_initial_tool_come_from_the_url(self):
        response = self.client.get('/decisiones/')
        self.assertEqual(response.context['initial_tool'], 'llevar')
        for marker in ('id="calc-form"', 'id="calc-modes"', 'id="calc-result"', 'calc-panel.js'):
            self.assertContains(response, marker)
        self.assertEqual(self.client.get('/decisiones/?herramienta=alcanza').context['initial_tool'], 'alcanza')
        self.assertEqual(self.client.get('/decisiones/?herramienta=ficha').context['initial_tool'], 'llevar')
        self.assertEqual(self.client.get('/decisiones/?herramienta=<script>').context['initial_tool'], 'llevar')

    def test_empty_catalog_is_reported_without_fake_options(self):
        Insumo.objects.all().delete()
        response = self.client.get('/decisiones/')
        self.assertContains(response, 'El catálogo no tiene insumos registrados')
        self.assertNotContains(response, '<option value="1">')


class CatalogImportCommandTests(TestCase):
    def write(self, data):
        import tempfile

        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
        json.dump(data, handle)
        handle.close()
        return handle.name

    def test_dry_run_then_apply(self):
        path = self.write({'zonas': [{'nombre': 'Zona import'}], 'cultivos': [{'nombre': 'Cultivo import'}]})
        call_command('importar_catalogo', path, verbosity=0, stdout=open(os.devnull, 'w', encoding='utf-8'))
        self.assertFalse(Cultivo.objects.filter(nombre='Cultivo import').exists())
        call_command('importar_catalogo', path, '--aplicar', stdout=open(os.devnull, 'w', encoding='utf-8'))
        self.assertTrue(Cultivo.objects.filter(nombre='Cultivo import').exists())

    def test_verified_reference_without_source_rolls_back_everything(self):
        from django.core.management.base import CommandError

        path = self.write(
            {
                'zonas': [{'nombre': 'Z'}],
                'cultivos': [{'nombre': 'C'}],
                'referencias': [{'cultivo': 'C', 'zona': 'Z', 'estado': 'verified'}],
            }
        )
        with self.assertRaises(CommandError):
            call_command('importar_catalogo', path, '--aplicar', stdout=open(os.devnull, 'w', encoding='utf-8'))
        self.assertFalse(Zona.objects.filter(nombre='Z').exists())

    def test_template_has_no_agronomic_values(self):
        import tempfile

        path = tempfile.mktemp(suffix='.json')
        call_command('importar_catalogo', '--plantilla', path, stdout=open(os.devnull, 'w', encoding='utf-8'))
        data = json.load(open(path, encoding='utf-8'))
        self.assertIsNone(data['cultivos'][0]['densidad_siembra'])
        self.assertIsNone(data['insumos'][0]['dosis_hectarea'])


class ProfessionalModulesTests(IntegrationBase):
    def setUp(self):
        super().setUp()
        self.plot.cultivo = self.maize
        self.plot.fecha_siembra = _date(2026, 10, 1)
        self.plot.fecha_cosecha = _date(2026, 10, 20)
        self.plot.save()
        self.insect_type = TipoInsumo.objects.create(nombre='Insecticida')
        self.fert_type = TipoInsumo.objects.create(nombre='Fertilizante')
        self.insecticide = Insumo.objects.create(
            nombre='Insecticida X',
            tipo=self.insect_type,
            cultivo=self.maize,
            periodo_carencia='21 días',
            dosis_hectarea=Decimal('0.5'),
            unidad_dosis='L/ha',
        )
        self.fert = Insumo.objects.create(nombre='Fert trigo', tipo=self.fert_type, cultivo=self.wheat)

    def activity(self, **values):
        data = {
            'nombre': 'Aplicación',
            'tipo': 'control_fitosanitario',
            'potrero': self.plot.pk,
            'fecha_programada': '2026-10-05',
            'estado': 'pendiente',
            'prioridad': 'alta',
        }
        data.update(values)
        return self.client.post('/actividades/nueva/', data)

    def test_field_log_record_with_product_dose_total_and_withholding_alert(self):
        response = self.activity(
            insumo=self.insecticide.pk,
            dosis='0.5',
            unidad_dosis='L/ha',
            superficie_ha='2',
            estado='completada',
            fecha_realizada='2026-10-05',
            responsable='Juan',
        )
        self.assertEqual(response.status_code, 302, response.content.decode()[:3000])
        item = ActividadAgricola.objects.get()
        self.assertEqual(item.cantidad_total, Decimal('1.000'))
        self.assertEqual(item.fecha_fin_carencia, _date(2026, 10, 26))
        self.assertTrue(item.conflicto_carencia)
        detail = self.client.get(f'/actividades/{item.pk}/')
        self.assertContains(detail, 'anterior al fin del período de carencia')

    def test_activity_validations(self):
        self.assertEqual(self.activity(estado='completada').status_code, 200)  # sin fecha ni producto
        self.assertEqual(self.activity(insumo=self.fert.pk, tipo='fertilizacion').status_code, 200)  # otro cultivo
        self.assertEqual(self.activity(superficie_ha='99', tipo='riego').status_code, 200)
        self.assertEqual(self.activity(dosis='2', tipo='fertilizacion').status_code, 200)  # dosis sin unidad
        self.assertEqual(ActividadAgricola.objects.count(), 0)
        self.assertEqual(self.activity(tipo='riego', dosis='5', unidad_dosis='kg/ha').status_code, 302)
        self.assertIsNone(ActividadAgricola.objects.get().dosis)

    def test_list_filters_calendar_and_prefill(self):
        self.activity(tipo='riego', nombre='Riego norte')
        response = self.client.get('/actividades/?vista=calendario&mes=2026-10')
        self.assertContains(response, 'Riego norte')
        self.assertEqual(response.context['activity_stats']['pendientes'], 1)
        self.assertEqual(list(self.client.get('/actividades/?tipo=siembra').context['activities']), [])
        form = self.client.get(
            f'/actividades/nueva/?potrero={self.plot.pk}&tipo=control_fitosanitario&insumo={self.insecticide.pk}&dosis=0.5&unidad=L/ha'
        )
        self.assertEqual(form.context['form'].initial['insumo'], self.insecticide.pk)
        self.assertEqual(form.context['form'].initial['unidad_dosis'], 'L/ha')

    def test_season_plan_uses_registered_dates_without_duplicates(self):
        payload = {
            'potrero': self.plot.pk,
            'incluir_preparacion': '1',
            'dias_preparacion': '10',
            'incluir_siembra': '1',
            'incluir_monitoreo': '1',
            'frecuencia_monitoreo': '7',
            'incluir_cosecha': '1',
        }
        self.client.post('/actividades/plan/', payload)
        types = sorted(ActividadAgricola.objects.values_list('tipo', flat=True))
        self.assertEqual(types, ['cosecha', 'monitoreo', 'monitoreo', 'preparacion', 'siembra'])
        self.assertEqual(ActividadAgricola.objects.get(tipo='preparacion').fecha_programada, _date(2026, 9, 21))
        self.client.post('/actividades/plan/', payload)
        self.assertEqual(ActividadAgricola.objects.count(), 5)
        self.assertEqual(
            self.client.post('/actividades/plan/', {**payload, 'potrero': self.other_plot.pk}).status_code, 404
        )

    def test_product_catalog_filters_and_sheet(self):
        names = lambda url: [item.nombre for item in self.client.get(url).context['products_page']]
        self.assertEqual(names('/productos/?categoria=insecticide'), ['Insecticida X'])
        self.assertEqual(names('/productos/?q=trigo'), ['Fert trigo'])
        self.assertEqual(names(f'/productos/?cultivo={self.maize.pk}'), ['Insecticida X'])
        self.assertEqual(self.client.get('/productos/?solo_calculables=1').context['product_count'], 0)
        sheet = self.client.get(f'/productos/{self.insecticide.pk}/')
        self.assertContains(sheet, 'número de autorización SAG')
        self.assertContains(sheet, '21 días')
        self.assertNotContains(sheet, 'Calcular cantidad</a>')

    def test_reports_types_csv_and_isolation(self):
        self.activity(
            insumo=self.insecticide.pk,
            dosis='0.5',
            unidad_dosis='L/ha',
            superficie_ha='2',
            estado='completada',
            fecha_realizada='2026-10-05',
            responsable='Juan',
        )
        ActividadAgricola.objects.create(
            usuario=self.other,
            potrero=self.other_plot,
            nombre='Ajena',
            tipo='riego',
            fecha_programada=_date(2026, 10, 1),
            estado='completada',
            fecha_realizada=_date(2026, 10, 1),
        )
        for kind in ('resumen', 'cuaderno', 'cultivos', 'calculos'):
            with self.subTest(kind=kind):
                response = self.client.get(f'/reportes/?tipo={kind}')
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, 'Ajena')
                self.assertNotContains(response, 'Lote ajeno')
        log = self.client.get('/reportes/?tipo=cuaderno')
        self.assertContains(log, 'Insecticida X')
        self.assertEqual(len(log.context['field_log']), 1)
        csv_response = self.client.get('/reportes/?tipo=cuaderno&formato=csv')
        self.assertEqual(csv_response['Content-Type'], 'text/csv; charset=utf-8')
        content = csv_response.content.decode('utf-8-sig')
        self.assertIn('Insecticida X', content)
        self.assertIn('26/10/2026', content)
        self.assertNotIn('Ajena', content)


class CatalogCsvAndUsoTests(TestCase):
    """Importador CSV, categorías base y relación cultivo → problema → insumo."""

    def folder(self, files):
        import tempfile
        from pathlib import Path

        base = Path(tempfile.mkdtemp())
        for name, text in files.items():
            (base / name).write_text(text, encoding='utf-8')
        return str(base)

    def run_import(self, *args):
        call_command('importar_catalogo', *args, stdout=open(os.devnull, 'w', encoding='utf-8'))

    def files(self):
        return {
            'zonas.csv': 'nombre;descripcion\nZona TEST;solo pruebas\n',
            'cultivos.csv': 'nombre;densidad_siembra;unidad_siembra\nCultivo TEST;1,5;kg\n',
            'problemas.csv': 'nombre;tipo;cultivos\nProblema TEST;pest;Cultivo TEST\n',
            'insumos.csv': 'nombre;tipo;dosis_hectarea;unidad_dosis\nInsumo TEST;Fungicida;2,5;L/ha\n',
            'usos.csv': 'cultivo;problema;insumo;fuente_nombre;fecha_verificacion\nCultivo TEST;Problema TEST;Insumo TEST;Fuente TEST;2026-01-15\n',
            'calendario.csv': 'cultivo;zona;siembra_inicio_mes;siembra_inicio_dia;siembra_fin_mes;siembra_fin_dia\nCultivo TEST;Zona TEST;9;1;11;30\n',
            'rendimientos.csv': 'cultivo;zona;rendimiento_ton_ha\nCultivo TEST;Zona TEST;12,5\n',
        }

    def test_base_categories_load_without_duplicates(self):
        self.run_import('--categorias')
        self.run_import('--categorias')
        names = set(TipoInsumo.objects.values_list('nombre', flat=True))
        self.assertTrue({'Fertilizante', 'Herbicida', 'Fungicida', 'Insecticida', 'Acaricida', 'Otros'} <= names)
        self.assertEqual(TipoInsumo.objects.filter(nombre='Otros').count(), 1)

    def test_csv_folder_is_idempotent_and_merges_calendar_with_yield(self):
        folder = self.folder(self.files())
        self.run_import('--categorias')
        self.run_import(folder)  # simulación
        self.assertFalse(Cultivo.objects.filter(nombre='Cultivo TEST').exists())
        self.run_import(folder, '--aplicar')
        self.run_import(folder, '--aplicar')
        self.run_import(folder, '--aplicar', '--actualizar')
        from ..models import UsoInsumoCultivo

        crop = Cultivo.objects.get(nombre='Cultivo TEST')
        self.assertEqual(crop.densidad_siembra, Decimal('1.5'))
        self.assertEqual(ReferenciaAgronomica.objects.count(), 1)
        ref = ReferenciaAgronomica.objects.get()
        self.assertEqual((ref.siembra_inicio_mes, ref.rendimiento_ton_ha), (9, Decimal('12.5')))
        self.assertEqual(Insumo.objects.get().dosis_hectarea, Decimal('2.5'))
        self.assertEqual(UsoInsumoCultivo.objects.count(), 1)
        self.assertTrue(UsoInsumoCultivo.objects.get().recomendable)

    def test_initial_csv_loads_only_crop_names_and_zone_without_invented_technical_data(self):
        from pathlib import Path
        from django.conf import settings

        data_dir = Path(settings.BASE_DIR) / 'data'
        self.run_import(str(data_dir), '--aplicar')
        self.run_import(str(data_dir), '--aplicar')  # repetir no duplica ni modifica
        self.assertGreater(Cultivo.objects.count(), 40)
        self.assertEqual(Cultivo.objects.filter(nombre='Maíz').count(), 1)
        self.assertEqual(Zona.objects.filter(nombre='Melipilla', activo=True).count(), 1)
        # Sin densidades, rendimientos, insumos ni referencias inventadas.
        self.assertFalse(Cultivo.objects.filter(densidad_siembra__isnull=False).exists())
        self.assertFalse(Cultivo.objects.filter(rendimiento_ton_ha__isnull=False).exists())
        self.assertEqual(Insumo.objects.count(), 0)

    def test_invalid_csv_value_is_rejected_and_rolls_back(self):
        from django.core.management.base import CommandError

        files = self.files()
        files['rendimientos.csv'] = 'cultivo;zona;rendimiento_ton_ha\nCultivo TEST;Zona TEST;mucho\n'
        with self.assertRaises(CommandError):
            self.run_import(self.folder(files), '--aplicar')
        self.assertFalse(Zona.objects.filter(nombre='Zona TEST').exists())

    def test_uso_requires_source_and_stops_being_recommended_when_inactive(self):
        from datetime import date
        from django.core.exceptions import ValidationError
        from ..models import UsoInsumoCultivo

        self.run_import('--categorias')
        self.run_import(self.folder(self.files()), '--aplicar')
        uso = UsoInsumoCultivo.objects.get()
        uso.fuente_nombre = ' '
        with self.assertRaises(ValidationError):
            uso.full_clean()
        uso.refresh_from_db()
        uso.fecha_verificacion = date(2999, 1, 1)
        with self.assertRaises(ValidationError):
            uso.full_clean()
        uso.refresh_from_db()
        uso.insumo.vigente = False
        self.assertFalse(uso.recomendable)
        uso.insumo.vigente = True
        uso.activo = False
        self.assertFalse(uso.recomendable)


class PredioPotreroFlowAndPrivacyTests(TestCase):
    """Flujo Predio → Potrero sin volver a dibujar y aislamiento entre usuarios."""

    def setUp(self):
        self.a = User.objects.create_user('ua', email='ua@example.com', password=PASSWORD)
        self.b = User.objects.create_user('ub', email='ub@example.com', password=PASSWORD)
        self.client.force_login(self.a)
        self.ring = [[-71.216, -33.687], [-71.213, -33.687], [-71.213, -33.684], [-71.216, -33.684]]
        self.feature = {
            'type': 'Feature',
            'properties': {},
            'geometry': {'type': 'Polygon', 'coordinates': [self.ring + [self.ring[0]]]},
        }

    def create_predio(self, **extra):
        data = {
            'nombre': 'Predio A',
            'ubicacion': 'Melipilla',
            'geojson': json.dumps(self.feature),
            'perimetro_aceptado': 'on',
            'latitud': '-33.687',
            'longitud': '-71.216',
            **extra,
        }
        response = self.client.post('/predios/nuevo/', data)
        return Predio.objects.get(nombre=data['nombre']), response

    def test_ownership_checkbox_removed_and_discreet_privacy_notes_shown(self):
        html = self.client.get('/predios/nuevo/').content.decode()
        self.assertNotIn('autorizacion_predio', html)
        self.assertNotIn('Declaro que soy propietario', html)
        self.assertNotIn('consent-box', html)
        self.assertNotIn('type="checkbox"', html)
        self.assertIn('Tus datos se guardan de forma privada en tu cuenta.', html)
        self.assertIn(
            'Registra únicamente predios que administras o tienes autorización para gestionar. Este trazado no acredita propiedad legal.',
            html,
        )
        self.assertNotIn('Propiedad verificada', html)
        self.assertLess(html.index('id="map-form-summary"'), html.index('Guardar predio'))
        self.assertLess(html.index('Guardar predio'), html.index('Registra únicamente predios'))

    def test_predio_saves_without_ownership_checkbox_and_keeps_geometry(self):
        farm, response = self.create_predio()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(farm.propietario, self.a)
        self.assertEqual(farm.geojson['geometry']['type'], 'Polygon')
        self.assertIsNotNone(farm.latitud)
        self.assertIsNotNone(farm.longitud)
        self.assertGreater(farm.superficie_ha, 0)
        resumen = farm.historial.get().resumen
        self.assertTrue(resumen.startswith('Predio creado con una superficie de'))
        self.assertNotIn('declaró', resumen)

    def test_map_tools_grouped_and_help_accordion_is_closed_with_five_steps(self):
        html = self.client.get('/predios/nuevo/').content.decode()
        self.assertIn('map-toolbar is-predio', html)
        for button in (
            'draw-perimeter',
            'measure-line',
            'undo-point',
            'finish-perimeter',
            'edit-perimeter',
            'locate-me',
            'center-map',
            'accept-perimeter',
            'cancel-perimeter',
            'cancel-drawing',
            'finish-line',
            'clear-measurement',
        ):
            self.assertEqual(html.count(f'id="{button}"'), 1, button)  # el JS del mapa los necesita: no se eliminan
        # Un solo botón principal al empezar; las herramientas secundarias van plegadas.
        self.assertIn('Dibujar mi campo', html)
        self.assertIn('<details class="more-tools">', html)
        self.assertNotIn('<details class="more-tools" open', html)
        # Una sola ayuda corta (la línea de estado); sin acordeones de «¿Cómo…?».
        self.assertNotIn('¿Cómo delimito mi predio?', html)
        self.assertNotIn('map-help', html)
        self.assertNotIn('map-privacy-note', html)
        self.assertNotIn('id="map-confirmed-line"', html)  # la medida no se repite bajo el mapa
        self.assertNotIn('id="confirm-location"', html)
        self.assertNotIn('<details class="more-details" open', html)
        order = [
            html.index(x)
            for x in (
                'Nombre del predio',
                'Referencia de ubicación',
                'Notas del predio (opcional)',
                'id="map-form-summary"',
                'Guardar predio',
            )
        ]
        self.assertEqual(order, sorted(order))

    def test_predio_hides_zone_when_there_is_only_one_and_shows_it_when_there_are_several(self):
        # Hay una sola zona (preseleccionada): el campo sigue en el formulario, pero oculto.
        Zona.objects.all().delete()
        Zona.objects.create(nombre='Melipilla')
        html = self.client.get('/predios/nuevo/').content.decode()
        self.assertIn('class="zone-fixed" hidden', html)
        self.assertNotIn('<span>Zona', html)
        # Con varias zonas se vuelve a mostrar para elegir.
        Zona.objects.create(nombre='Maule')
        html = self.client.get('/predios/nuevo/').content.decode()
        self.assertNotIn('zone-fixed', html)
        self.assertIn('<span>Zona', html)

    def sur_feature(self):
        ring = [[-71.300, -33.800], [-71.296, -33.800], [-71.296, -33.796], [-71.300, -33.796]]
        return {'type': 'Feature', 'properties': {}, 'geometry': {'type': 'Polygon', 'coordinates': [ring + [ring[0]]]}}

    def test_new_predio_map_starts_clean_when_user_already_has_predios(self):
        norte, _ = self.create_predio(nombre='Predio Norte')
        response = self.client.get('/predios/nuevo/')
        html = response.content.decode()
        self.assertContains(response, 'data-mode="create"')
        # Ni el trazado ni las coordenadas del predio anterior llegan al mapa de creación.
        self.assertNotIn('-71.216', html)
        self.assertNotIn('-33.687', html)
        self.assertNotIn('"Polygon"', html)
        for item in response.context['farm_data']['predios']:
            self.assertIsNone(item['geojson'])
            self.assertIsNone(item['lat'])
            self.assertIsNone(item['lng'])
        for field in ('id_latitud', 'id_longitud', 'id_superficie_ha'):
            self.assertRegex(html, rf'id="{field}"(?! value)')
        self.assertIn('id="id_geojson"', html)
        self.assertNotIn(norte.nombre + '</', html.split('id="terrain-form"')[1])
        # El predio sigue existiendo y se ve en «Mis predios y potreros».
        self.assertContains(self.client.get('/predios/'), 'Predio Norte')

    def test_two_predios_same_user_keep_only_their_own_geometry(self):
        norte, _ = self.create_predio(nombre='Predio Norte')
        sur_feature = self.sur_feature()
        sur, response = self.create_predio(
            nombre='Predio Sur', geojson=json.dumps(sur_feature), latitud='-33.800', longitud='-71.300'
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Predio.objects.filter(propietario=self.a).count(), 2)
        norte.refresh_from_db()
        self.assertEqual(norte.geojson['geometry']['coordinates'], self.feature['geometry']['coordinates'])
        self.assertEqual(sur.geojson['geometry']['coordinates'], sur_feature['geometry']['coordinates'])
        self.assertNotEqual(norte.geojson, sur.geojson)
        self.assertNotEqual(norte.superficie_ha, 0)
        listing = self.client.get('/predios/')
        self.assertContains(listing, 'Predio Norte')
        self.assertContains(listing, 'Predio Sur')

    def test_editing_shows_only_that_predios_geometry(self):
        norte, _ = self.create_predio(nombre='Predio Norte')
        sur, _ = self.create_predio(
            nombre='Predio Sur', geojson=json.dumps(self.sur_feature()), latitud='-33.800', longitud='-71.300'
        )
        response = self.client.get(f'/predios/{sur.pk}/editar/')
        html = response.content.decode()
        self.assertContains(response, 'data-mode="edit"')
        self.assertIn('-71.3', html)  # su propia geometría llega por el formulario
        self.assertNotIn('-71.216', html)  # la del otro predio no
        for item in response.context['farm_data']['predios']:
            self.assertIsNone(item['geojson'])

    def test_other_user_never_receives_geometry_of_user_a_in_new_predio_form(self):
        self.create_predio(nombre='Predio Norte')
        self.client.force_login(self.b)
        response = self.client.get('/predios/nuevo/')
        html = response.content.decode()
        self.assertEqual(response.context['farm_data']['predios'], [])
        self.assertNotIn('-71.216', html)
        self.assertNotIn('Predio Norte', html)
        self.assertEqual(
            self.client.get(f'/predios/{Predio.objects.get(nombre="Predio Norte").pk}/editar/').status_code, 404
        )

    def test_map_script_resets_state_uses_overview_zoom_and_fits_new_polygon(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / 'static' / 'js' / 'predios-map.js').read_text(
            encoding='utf-8'
        )
        self.assertIn('NEW_PREDIO_ZOOM = 12', source)
        self.assertIn('MY_LOCATION_ZOOM = 16', source)
        self.assertIn('resetNewPredioState', source)
        self.assertIn("if (!form || isPredioForm) return;", source)
        self.assertIn("fitBounds(layerBounds(currentLayer), { padding: [56, 56], maxZoom: 17 })", source)
        self.assertIn("['Normal', normalTiles], ['Satélite', satelliteTiles]", re.sub(r'\s+', ' ', source))
        self.assertNotIn('localStorage', source)
        self.assertNotIn('sessionStorage', source)

    def test_case_a_use_whole_predio_creates_main_potrero_without_redrawing(self):
        farm, response = self.create_predio()
        self.assertRedirects(response, f'/predios/{farm.pk}/?nuevo=1')
        page = self.client.get(f'/predios/{farm.pk}/?nuevo=1')
        self.assertNotContains(page, 'Predio registrado correctamente')
        self.assertNotContains(page, 'Ya registraste el límite completo de tu predio')
        for text in (
            '¿Cómo deseas organizar tu predio?',
            'Puedes utilizar toda la superficie como un solo potrero o dividirla en sectores para gestionar tus '
            'cultivos y actividades agrícolas por separado.',
            'Un solo potrero',
            'Utilizar todo el predio',
            'Dividir en potreros',
            'Dividir mi predio',
            'Lo haré más tarde',
        ):
            self.assertContains(page, text)
        response = self.client.post(f'/predios/{farm.pk}/usar-completo/', {'nombre': ''})
        plot = Potrero.objects.get(predio=farm)
        self.assertRedirects(response, f'/predios/{farm.pk}/potreros/{plot.pk}/editar/')
        self.assertEqual(plot.nombre, 'Potrero principal')
        self.assertEqual(plot.superficie_ha, farm.superficie_ha)
        self.assertEqual(plot.geojson['geometry'], farm.geojson['geometry'])
        self.assertEqual(plot.perimetro, [list(point) for point in self.ring])
        self.assertEqual((plot.latitud, plot.longitud), (farm.latitud, farm.longitud))
        crop = Cultivo.objects.create(nombre='Cultivo flujo')
        plot.cultivo = crop
        plot.full_clean()
        plot.save()
        self.assertEqual(Potrero.objects.get(pk=plot.pk).cultivo, crop)
        # No se crea un segundo potrero por doble envío y la acción solo acepta POST.
        second = self.client.post(f'/predios/{farm.pk}/usar-completo/', {'nombre': 'Otro'})
        self.assertEqual(Potrero.objects.filter(predio=farm).count(), 1)
        self.assertRedirects(second, f'/predios/{farm.pk}/potreros/{plot.pk}/editar/', fetch_redirect_response=False)
        self.assertEqual(self.client.get(f'/predios/{farm.pk}/usar-completo/').status_code, 405)
        detail = self.client.get(f'/predios/{farm.pk}/?nuevo=1')
        self.assertNotContains(detail, 'Predio registrado correctamente')
        self.assertNotContains(detail, '¿Cómo deseas organizar tu predio?')

    def test_case_b_split_shows_reference_and_accepts_several_potreros(self):
        farm, _ = self.create_predio()
        page = self.client.get(f'/predios/{farm.pk}/potreros/nuevo/')
        self.assertContains(page, 'Predio: <strong>Predio A</strong>')
        self.assertNotContains(page, 'Estás creando potreros dentro de tu predio')
        self.assertNotContains(page, 'No necesitas volver a marcar todo el predio')
        west = [[-71.216, -33.687], [-71.2145, -33.687], [-71.2145, -33.684], [-71.216, -33.684]]
        east = [[-71.2144, -33.687], [-71.213, -33.687], [-71.213, -33.684], [-71.2144, -33.684]]
        for name, ring in (('Oeste', west), ('Este', east)):
            response = self.client.post(
                f'/predios/{farm.pk}/potreros/nuevo/',
                {
                    'nombre': name,
                    'cultivo': '',
                    'variedad': '',
                    'superficie_ha': '',
                    'fecha_siembra': '',
                    'fecha_cosecha': '',
                    'latitud': '',
                    'longitud': '',
                    'perimetro': json.dumps(ring),
                },
            )
            self.assertRedirects(response, f'/predios/{farm.pk}/')
        self.assertEqual(set(farm.potreros.values_list('nombre', flat=True)), {'Oeste', 'Este'})
        outside = [[-71.3, -33.7], [-71.29, -33.7], [-71.29, -33.69], [-71.3, -33.69]]
        response = self.client.post(
            f'/predios/{farm.pk}/potreros/nuevo/',
            {'nombre': 'Afuera', 'superficie_ha': '', 'latitud': '', 'longitud': '', 'perimetro': json.dumps(outside)},
        )
        self.assertContains(response, 'debe quedar dentro del predio')
        self.assertFalse(Potrero.objects.filter(nombre='Afuera').exists())

    def test_use_whole_requires_a_drawn_perimeter(self):
        farm = Predio.objects.create(propietario=self.a, nombre='Sin trazo', ubicacion='X', superficie_ha=Decimal('2'))
        response = self.client.post(f'/predios/{farm.pk}/usar-completo/', follow=True)
        self.assertContains(response, 'no tiene un perímetro dibujado')
        self.assertFalse(farm.potreros.exists())

    def test_case_c_user_b_cannot_reach_user_a_data_by_direct_url(self):
        farm, _ = self.create_predio(nombre='Predio privado A')
        self.client.post(f'/predios/{farm.pk}/usar-completo/', {'nombre': 'Lote secreto'})
        plot = farm.potreros.get()
        crop = Cultivo.objects.create(nombre='Cultivo privado')
        act = ActividadAgricola.objects.create(
            usuario=self.a, potrero=plot, nombre='Labor privada', tipo='riego', fecha_programada=_date(2026, 5, 1)
        )
        client_b = self.client_class()
        client_b.force_login(self.b)
        get_urls = [
            f'/predios/{farm.pk}/',
            f'/predios/{farm.pk}/editar/',
            f'/predios/{farm.pk}/potreros/nuevo/',
            f'/predios/{farm.pk}/potreros/{plot.pk}/editar/',
            f'/predios/{farm.pk}/potreros/{plot.pk}/temporadas/nueva/',
            f'/api/potreros/{plot.pk}/',
            f'/actividades/{act.pk}/',
            f'/actividades/{act.pk}/editar/',
            f'/actividades/{act.pk}/eliminar/',
        ]
        for url in get_urls:
            self.assertIn(client_b.get(url).status_code, (403, 404), url)
        post_urls = [
            f'/predios/{farm.pk}/usar-completo/',
            f'/predios/{farm.pk}/editar/',
            f'/predios/{farm.pk}/potreros/nuevo/',
            f'/predios/{farm.pk}/potreros/{plot.pk}/editar/',
            f'/api/potreros/{plot.pk}/cultivo/',
            f'/actividades/{act.pk}/estado/',
            f'/actividades/{act.pk}/eliminar/',
        ]
        for url in post_urls:
            self.assertIn(client_b.post(url, {'estado': 'completada', 'cultivo': crop.pk}).status_code, (403, 404), url)
        self.assertEqual(
            client_b.post(
                '/calculos/ejecutar/',
                json.dumps({'operation': 'dose', 'plot_id': plot.pk, 'area': '1'}),
                content_type='application/json',
            ).status_code,
            400,
        )
        # Nada de A llega a las páginas de B: ni HTML, ni JSON embebido, ni reportes.
        lat_text = '-33.687'
        for url in (
            '/',
            '/predios/',
            '/calendario/',
            '/actividades/',
            '/decisiones/',
            '/reportes/',
            '/reportes/?tipo=resumen&formato=csv',
            '/busqueda/?q=privado',
            '/panel-admin/',
        ):
            response = client_b.get(url)
            body = response.content.decode()
            for secret in ('Predio privado A', 'Lote secreto', 'Labor privada', lat_text):
                self.assertNotIn(secret, body, f'{secret} en {url}')
        # Los datos de A siguen intactos.
        self.assertTrue(Predio.objects.filter(pk=farm.pk).exists())
        self.assertTrue(ActividadAgricola.objects.filter(pk=act.pk).exists())
        self.assertEqual(farm.potreros.count(), 1)

    def test_anonymous_users_and_policy_page(self):
        farm, _ = self.create_predio()
        anonymous = self.client_class()
        for url in (f'/predios/{farm.pk}/', f'/api/potreros/{farm.potreros.count()}/', '/predios/'):
            self.assertIn(anonymous.get(url).status_code, (302, 403, 404))
        self.assertEqual(anonymous.post(f'/predios/{farm.pk}/usar-completo/').status_code, 302)


class DemoCatalogEndToEndTests(TestCase):
    """data_demo/ (catálogo ficticio) debe importarse sin errores y reproducir los resultados calculados a mano."""

    @classmethod
    def setUpTestData(cls):
        from io import StringIO
        from pathlib import Path

        out = StringIO()
        call_command('importar_catalogo', '--categorias', stdout=out)
        cls.demo_dir = Path(__file__).resolve().parent.parent.parent / 'data_demo'
        call_command('importar_catalogo', str(cls.demo_dir), '--aplicar', stdout=out)
        cls.import_output = out.getvalue()

    def setUp(self):
        from ..models import Zona

        self.user = User.objects.create_user('demo-user', email='demo@example.com', password=PASSWORD)
        self.client.force_login(self.user)
        self.papa = Cultivo.objects.get(nombre='PAPA DEMO')
        self.predio = Predio.objects.create(
            propietario=self.user,
            nombre='Predio DEMO',
            ubicacion='DEMO',
            superficie_ha=5,
            zona=Zona.objects.get(nombre='ZONA DEMO MELIPILLA'),
        )
        self.potrero = Potrero.objects.create(
            predio=self.predio, nombre='Potrero Norte DEMO', superficie_ha=2, cultivo=self.papa
        )

    def calc(self, **payload):
        return self.client.post(
            '/calculos/ejecutar/',
            data=json.dumps({'plot_id': self.potrero.pk, 'crop': 'PAPA DEMO', 'area': '2', **payload}),
            content_type='application/json',
        )

    def test_import_counts_and_demo_labels(self):
        from ..models import ProblemaAgricola, UsoInsumoCultivo, Zona

        self.assertEqual(Zona.objects.filter(activo=True).count(), 2)
        self.assertEqual(Cultivo.objects.count(), 3)
        self.assertEqual(ProblemaAgricola.objects.count(), 3)
        self.assertEqual(ReferenciaAgronomica.objects.count(), 3)
        self.assertEqual(Insumo.objects.count(), 4)
        self.assertEqual(UsoInsumoCultivo.objects.count(), 3)
        for model in (Zona, Cultivo, ProblemaAgricola, Insumo):
            self.assertFalse(model.objects.exclude(nombre__contains='DEMO').exists(), model)
        for reference in ReferenciaAgronomica.objects.all():  # calendario + rendimientos se fusionaron
            self.assertEqual(reference.estado, 'verified')
            self.assertIsNotNone(reference.rendimiento_ton_ha)
            self.assertIsNotNone(reference.siembra_inicio_mes)
            self.assertEqual(reference.zona.nombre, 'ZONA DEMO MELIPILLA')
        self.assertFalse(Insumo.objects.filter(nombre='SEMILLA DEMO PAPA').exclude(numero_autorizacion_sag='').exists())
        # Reimportar no duplica nada.
        from io import StringIO

        call_command('importar_catalogo', str(self.demo_dir), '--aplicar', stdout=StringIO())
        self.assertEqual(Insumo.objects.count(), 4)
        self.assertEqual(ReferenciaAgronomica.objects.count(), 3)

    def test_catalog_counters_are_no_longer_zero(self):
        from ..catalog import catalog_payload

        stats = catalog_payload()['stats']
        self.assertEqual((stats['crops'], stats['zones'], stats['seeds'], stats['cropsWithDensity']), (3, 2, 1, 3))
        self.assertEqual(
            (stats['fertilizers'], stats['otherInputs'], stats['products'], stats['problems']), (1, 2, 4, 3)
        )

    def test_seed_calculation_two_ha_by_2500(self):
        data = self.calc(operation='dose', calculation_type='semillas', dose='2500', unit='kg').json()
        self.assertEqual(data['amount'], 5000)
        self.assertEqual(data['unit'], 'kg')
        self.assertTrue(data['verified'])
        self.assertFalse(data['simulation'])

    def test_insecticide_quantity_and_coverage(self):
        product = Insumo.objects.get(nombre='INSUMO DEMO INSECTICIDA')
        need = self.calc(
            operation='dose', calculation_type='proteccion', product_id=product.pk, target='PLAGA DEMO A'
        ).json()
        self.assertEqual((need['amount'], need['unit'], need['dose']), (4, 'L', 2))
        self.assertEqual(need['package'], {'size': 5.0, 'unit': 'L', 'count': 1})
        coverage = self.calc(
            operation='coverage', calculation_type='coverage', product_id=product.pk, target='PLAGA DEMO A', stock='3'
        ).json()
        self.assertEqual(
            (coverage['needed'], coverage['percent'], coverage['missingQty'], coverage['missingHa']), (4, 75, 1, 0.5)
        )

    def test_dose_limits_and_fungicide_belongs_to_other_crop(self):
        insecticide = Insumo.objects.get(nombre='INSUMO DEMO INSECTICIDA')
        # En fitosanitarios la dosis sale de la ficha: una dosis escrita a mano (4 L/ha) se ignora.
        forced = self.calc(
            operation='dose',
            calculation_type='proteccion',
            product_id=insecticide.pk,
            target='PLAGA DEMO A',
            dose='4',
            unit='L/ha',
        ).json()
        self.assertEqual(forced['dose'], 2)
        # En el fertilizante, superar el máximo de 150 kg/ha calcula igual pero con advertencia.
        fert = Insumo.objects.get(nombre='FERTILIZANTE DEMO NPK')
        over = self.calc(operation='dose', calculation_type='insumos', product_id=fert.pk, dose='200', unit='kg/ha')
        self.assertEqual(over.status_code, 200)
        self.assertTrue(over.json()['above_max'])
        self.assertEqual(over.json()['origin'], 'adjusted')
        ok = self.calc(
            operation='dose', calculation_type='insumos', product_id=fert.pk, dose='100', unit='kg/ha'
        ).json()
        self.assertEqual((ok['amount'], ok['package']['count']), (200, 8))
        fungicide = Insumo.objects.get(nombre='INSUMO DEMO FUNGICIDA')
        wrong = self.calc(
            operation='dose', calculation_type='proteccion', product_id=fungicide.pk, target='ENFERMEDAD DEMO B'
        )
        self.assertEqual(wrong.status_code, 400)

    def test_catalog_lists_insecticide_for_papa_and_pest(self):
        from ..catalog import catalog_payload

        catalog = catalog_payload()
        problem = next(item for item in catalog['problemas'] if item['name'] == 'PLAGA DEMO A')
        matches = [
            item['name']
            for item in catalog['insumos']
            if item['cropId'] == str(self.papa.pk)
            and str(problem['id']) in item['problemIds']
            and item['type'] == 'Insecticida'
        ]
        self.assertEqual(matches, ['INSUMO DEMO INSECTICIDA'])


class ButtonHierarchyAndNextStepTests(TestCase):
    """Jerarquía de acciones (principal/secundaria/terciaria) y «Siguiente paso» contextual."""

    def setUp(self):
        self.user = User.objects.create_user('jerarquia', email='jerarquia@example.com', password=PASSWORD)
        self.client.force_login(self.user)
        self.ring = [[-71.216, -33.687], [-71.213, -33.687], [-71.213, -33.684], [-71.216, -33.684]]
        self.feature = {
            'type': 'Feature',
            'properties': {},
            'geometry': {'type': 'Polygon', 'coordinates': [self.ring + [self.ring[0]]]},
        }

    def farm(self, **extra):
        return Predio.objects.create(
            propietario=self.user, nombre='Predio flujo', ubicacion='Melipilla', superficie_ha=Decimal('2'), **extra
        )

    def page(self, url):
        # Solo el HTML visible: los JSON de los <script> no cuentan como contenido de la pantalla.
        return re.sub(r'<script.*?</script>', '', self.client.get(url).content.decode(), flags=re.S)

    def test_1_user_without_predios_sees_only_register_first_predio(self):
        html = self.page('/predios/')
        self.assertEqual(html.count('Registrar primer predio'), 1)
        self.assertEqual(html.count('class="btn-primary-agro" href="/predios/nuevo/"'), 1)
        self.assertNotIn('Mostrar todos los predios</a>', html)
        calendar = self.page('/calendario/')
        self.assertIn('No tienes predios registrados.', calendar)
        self.assertEqual(calendar.count('Registrar primer predio'), 1)
        self.assertNotIn('Nuevo predio', calendar)

    def test_2_predio_without_potreros_recommends_adding_one_potrero(self):
        farm = self.farm()
        listing = self.page(f'/predios/?predio={farm.pk}')
        self.assertEqual(listing.count('Registrar predio</a>'), 1)  # solo la cabecera (el menú lateral queda aparte)
        self.assertIn('id="register-selected-plot"', listing)
        self.assertRegex(
            listing, r'id="register-selected-plot"\s+hidden'
        )  # sin potreros, el estado vacío tiene el único CTA
        self.assertEqual(listing.count('Configurar potrero'), 1)
        self.assertNotIn('Registrar primer potrero', listing)
        detail = self.page(f'/predios/{farm.pk}/')
        # Sin potreros la ficha ofrece una sola guía («¿Cómo deseas organizar tu predio?»), no además «Siguiente paso».
        self.assertIn('¿Cómo deseas organizar tu predio?', detail)
        self.assertNotIn('Siguiente paso', detail)
        self.assertIn(f'/predios/{farm.pk}/potreros/nuevo/', detail)
        self.assertNotIn('Registrar potrero', detail)
        self.assertNotIn('Planificación de siembra', detail)
        calendar = self.page('/calendario/')
        self.assertIn('Agregar potrero', calendar)
        self.assertIn('Ver predio', calendar)

    def test_3_potrero_without_crop_recommends_assigning_crop(self):
        farm = self.farm()
        Potrero.objects.create(predio=farm, nombre='Lote sin cultivo', superficie_ha=Decimal('1'))
        detail = self.page(f'/predios/{farm.pk}/')
        self.assertIn('Siguiente paso', detail)
        self.assertIn('Asigna un cultivo a «Lote sin cultivo»', detail)
        self.assertNotIn(
            'class="btn-primary-agro"', detail.split('<main>')[1]
        )  # el siguiente paso es una llamada discreta
        self.assertEqual(
            detail.count('Agregar potrero'), 1
        )  # cabecera: un solo «Agregar potrero» una vez que hay potreros
        listing = self.page(f'/predios/?predio={farm.pk}')
        self.assertRegex(listing, r'data-assign-crop="\d+"\s+data-quick\s*>')  # acción rápida visible sin cultivo
        calendar = self.page('/calendario/')
        self.assertIn('btn-primary-agro btn-sm" href', re.sub(r'\s+', ' ', calendar))
        self.assertIn('Asignar cultivo', calendar)
        self.assertIn('Predio registrado', calendar)  # checklist informativo
        self.assertIn('class="check-mark"', calendar)

    def test_4_potrero_with_crop_recommends_planning_and_labor(self):
        farm = self.farm()
        crop = Cultivo.objects.create(nombre='Cultivo jerarquía')
        plot = Potrero.objects.create(
            predio=farm,
            nombre='Lote con cultivo',
            superficie_ha=Decimal('1'),
            cultivo=crop,
            fecha_siembra=timezone.localdate() + timedelta(days=30),
            fecha_cosecha=timezone.localdate() + timedelta(days=150),
        )
        detail = self.page(f'/predios/{farm.pk}/')
        self.assertIn('→ Calcula lo que necesitas para «Lote con cultivo»', detail)
        self.assertNotIn('Asignar cultivo', detail)
        listing = self.page(f'/predios/?predio={farm.pk}')
        self.assertIn('Cambiar cultivo', listing)
        self.assertRegex(listing, r'data-quick\s+hidden')  # con cultivo no hay acción rápida de asignar
        self.assertIn(f'data-plot-planning="{plot.pk}"', listing)
        calendar = self.page('/calendario/')
        self.assertNotIn('Asignar cultivo', calendar)
        # Sin labores: solo el CTA dentro del estado vacío; con labores: «Programar labor» arriba.
        self.assertIn('Programar primera labor', calendar)
        ActividadAgricola.objects.create(
            usuario=self.user,
            potrero=plot,
            nombre='Riego flujo',
            tipo=ActividadAgricola.Tipo.RIEGO,
            fecha_programada=timezone.localdate() + timedelta(days=2),
        )
        calendar = self.page('/calendario/')
        self.assertNotIn('Programar primera labor', calendar)
        self.assertIn('Programar labor</a>', calendar)
        self.assertIn('→ Calcula lo que necesitas para «Lote con cultivo»', self.page(f'/predios/{farm.pk}/'))

    def test_5_new_predio_shows_only_the_usage_decision(self):
        farm = self.farm(geojson=self.feature)
        html = self.page(f'/predios/{farm.pk}/?nuevo=1')
        self.assertIn('¿Cómo deseas organizar tu predio?', html)
        self.assertNotIn('¿Cómo utilizas este predio?', html)
        self.assertIn('Utilizar todo el predio', html)
        self.assertIn('Dividir mi predio', html)
        self.assertIn('Lo haré más tarde', html)
        self.assertNotIn('Siguiente paso', html)
        self.assertNotIn('Agregar potrero', html)
        self.assertNotIn('Registrar potrero', html)
        self.assertEqual(html.count('Utilizar todo el predio'), 1)

    def test_6_predio_with_potreros_does_not_show_onboarding_again(self):
        farm = self.farm(geojson=self.feature)
        Potrero.objects.create(predio=farm, nombre='Principal', superficie_ha=Decimal('1'))
        html = self.page(f'/predios/{farm.pk}/?nuevo=1')
        self.assertNotIn('¿Cómo deseas organizar tu predio?', html)
        self.assertNotIn('Utilizar todo el predio', html)
        self.assertNotIn('Predio registrado correctamente', html)
        self.assertNotIn('use-card', html)
        self.assertIn('Gestión agrícola', html)

    def test_selected_panel_is_informative_and_script_has_no_duplicate_actions(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / 'static' / 'js' / 'predios-map.js').read_text(
            encoding='utf-8'
        )
        for old_label in (
            "'Registrar potrero'",
            "'Registrar primer potrero'",
            "'Ficha e historial del predio'",
            "'Editar predio'",
        ):
            self.assertNotIn(old_label, source)
        self.assertIn('createPlotLink.hidden = farmPlots.length === 0', source)

    def test_button_system_classes_exist_in_css(self):
        from pathlib import Path

        css = (Path(__file__).resolve().parent.parent / 'static' / 'css' / 'body.css').read_text(encoding='utf-8')
        for name in (
            '.btn-primary-agro',
            '.btn-secondary-agro',
            '.btn-ghost-agro',
            '.btn-danger-agro',
            '.btn-disabled',
        ):
            self.assertIn(name, css)


class VisualVarietyTests(TestCase):
    """Tarjetas de entidad, menús ⋯, chips, selector de vista, línea de meses, labores y herramientas."""

    def setUp(self):
        self.user = User.objects.create_user('variedad', email='variedad@example.com', password=PASSWORD)
        self.other = User.objects.create_user('ajeno', email='ajeno@example.com', password=PASSWORD)
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.crop = Cultivo.objects.create(nombre='Papa visual')
        self.farm = Predio.objects.create(
            propietario=self.user, nombre='Fundo visual', ubicacion='Chocalán', superficie_ha=Decimal('10')
        )
        self.planned = Potrero.objects.create(
            predio=self.farm,
            nombre='Potrero planificado',
            superficie_ha=Decimal('2'),
            cultivo=self.crop,
            fecha_siembra=self.today + timedelta(days=20),
            fecha_cosecha=self.today + timedelta(days=140),
        )
        self.bare = Potrero.objects.create(predio=self.farm, nombre='Potrero pelado', superficie_ha=Decimal('1'))

    def labor(self, plot, days, tipo=ActividadAgricola.Tipo.RIEGO, user=None):
        return ActividadAgricola.objects.create(
            usuario=user or self.user,
            potrero=plot,
            nombre=f'Labor {days}',
            tipo=tipo,
            fecha_programada=self.today + timedelta(days=days),
        )

    def html(self, url):
        return re.sub(r'<script.*?</script>', '', self.client.get(url).content.decode(), flags=re.S)

    def test_farm_and_plot_cards_have_context_menus_without_delete(self):
        html = self.html('/predios/')
        self.assertGreaterEqual(html.count('class="row-menu-trigger"'), 3)  # predio + 2 potreros
        for text in (
            'Ver predio',
            'Editar información',
            'Ver en mapa',
            'Agregar potrero',
            'Ver historial',
            'Ver planificación',
            'Generar reporte',
            'Programar labor',
            'Ver ficha e historial',
            'Editar potrero',
        ):
            self.assertIn(text, html)
        self.assertIn(f'/reportes/?predio={self.farm.pk}', html)
        self.assertNotIn('Eliminar predio', html)  # no existe aún un flujo de borrado con confirmación
        self.assertNotIn('Eliminar potrero', html)
        # El menú del potrero cambia según el estado: sin cultivo «Asignar», con cultivo «Cambiar».
        self.assertEqual(html.count('data-assign-label>Asignar cultivo'), 1)
        self.assertEqual(html.count('data-assign-label>Cambiar cultivo'), 1)

    def test_state_chips_and_plan_meter_use_real_data_only(self):
        self.labor(self.planned, 3)
        self.labor(self.planned, -2, ActividadAgricola.Tipo.MONITOREO)
        html = self.html('/predios/')
        for text in (
            'Con cultivo',
            'Sin cultivo',
            'Sin planificar',
            'Planificación completa',
            'Labores vencidas · 1',
            'Próxima labor',
        ):
            self.assertIn(text, html)
        self.assertIn('Planificación <strong>3 de 3</strong>', html)
        self.assertRegex(html, rf'data-plot-plan="{self.bare.pk}"\s+hidden')  # sin cultivo no se inventa un avance
        self.assertNotRegex(html, r'Planificación <strong>\d+%')
        self.assertIn('Siguiente paso', html)
        self.assertIn('→ Asigna un cultivo a «Potrero pelado»', html)

    def test_payload_exposes_task_and_plan_data_for_the_side_panel(self):
        self.labor(self.planned, 3)
        self.labor(self.planned, -2, ActividadAgricola.Tipo.MONITOREO)
        self.labor(
            Potrero.objects.create(
                predio=Predio.objects.create(propietario=self.other, nombre='Ajeno', superficie_ha=1),
                nombre='Lote ajeno',
                superficie_ha=1,
            ),
            1,
            user=self.other,
        )
        data = self.client.get('/predios/').context['farm_data']
        farm = data['predios'][0]
        self.assertEqual((farm['pendingTasks'], farm['overdueTasks'], farm['cropCount']), (2, 1, 1))
        self.assertEqual(farm['nextStep']['cta'], 'Asignar cultivo')
        plot = next(item for item in data['plots'] if item['name'] == 'Potrero planificado')
        self.assertEqual((plot['planDone'], plot['planTotal'], plot['pendingTasks']), (3, 3, 2))
        self.assertEqual(plot['nextTask']['label'], 'Monitoreo')  # la más cercana en el tiempo, aunque esté vencida
        bare = next(item for item in data['plots'] if item['name'] == 'Potrero pelado')
        self.assertEqual((bare['planDone'], bare['pendingTasks'], bare['nextTask']), (0, 0, None))
        self.assertNotIn('Lote ajeno', json.dumps(data))

    def test_next_step_is_a_single_suggestion_with_five_states(self):
        from ..views import _farm_next_step

        farm = Predio.objects.create(propietario=self.user, nombre='Estados', superficie_ha=1)
        self.assertEqual(_farm_next_step(farm, [])['key'], 'potrero')
        plot = Potrero.objects.create(predio=farm, nombre='A', superficie_ha=1)
        self.assertEqual(_farm_next_step(farm, [plot])['key'], 'cultivo')
        plot.cultivo = self.crop
        self.assertEqual(_farm_next_step(farm, [plot])['key'], 'planificacion')
        plot.fecha_siembra, plot.fecha_cosecha = self.today, self.today + timedelta(days=90)
        # Con la planificación completa el siguiente paso es calcular (las labores se programan después).
        self.assertEqual(_farm_next_step(farm, [plot])['key'], 'decisiones')
        self.assertEqual(_farm_next_step(farm, [plot], pending=2)['key'], 'decisiones')
        self.assertEqual(_farm_next_step(farm, [plot])['url'], f'/decisiones/?tipo=semillas&potrero={plot.pk}')

    def test_view_switch_is_front_end_only_and_remembered_in_local_storage(self):
        html = self.html('/predios/')
        for marker in (
            'data-view-mode="list"',
            'data-view-mode="cards"',
            'data-view-focus="map"',
            'id="farm-picker-list"',
        ):
            self.assertIn(marker, html)
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / 'static' / 'js' / 'predios-view.js').read_text(
            encoding='utf-8'
        )
        self.assertIn('localStorage', source)
        self.assertNotIn('fetch(', source)
        self.assertNotIn('XMLHttpRequest', source)

    def test_context_menu_script_handles_keyboard_and_aria(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / 'static' / 'js' / 'row-menu.js').read_text(encoding='utf-8')
        for marker in ("'Escape'", "'ArrowDown'", 'aria-expanded', 'closest'):
            self.assertIn(marker, source)

    def test_selected_panel_script_shows_summary_and_single_next_action(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / 'static' / 'js' / 'predios-map.js').read_text(
            encoding='utf-8'
        )
        for marker in ("'Próxima acción'", "'Abrir predio'", "'Labores pendientes'", "'Potreros'", 'farm.nextStep'):
            self.assertIn(marker, source)

    def test_siembra_shows_month_line_menus_and_no_new_predio_button(self):
        html = self.html('/calendario/')
        self.assertIn('data-role="months"', html)
        self.assertIn('class="row-menu-trigger"', html)
        self.assertIn('Programar labor', html)
        self.assertNotIn('Nuevo predio', html)
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / 'static' / 'js' / 'siembra-registro.js').read_text(
            encoding='utf-8'
        )
        self.assertIn('renderMonths', source)
        self.assertIn('if (!holder || (!sow && !harvest)) return;', source)  # sin fechas no se dibuja nada

    def test_labores_show_type_icon_color_bar_and_useful_empty_state(self):
        empty = self.html('/actividades/')
        self.assertIn('No tienes labores', empty)
        self.assertIn('Programa una tarea como riego, fertilización o monitoreo.', empty)
        self.assertEqual(empty.count('Programar primera labor'), 1)
        self.assertNotIn('Programar labor</a>', empty)  # un solo CTA en el estado vacío
        self.labor(self.planned, 2)
        html = self.html('/actividades/')
        self.assertIn('activity-table-row type-riego', html)
        self.assertIn('data-lucide="droplets"', html)
        self.assertIn('Programar labor</a>', html)
        self.assertNotIn('Programar primera labor', html)

    def test_decision_center_tools_are_differentiated_cards(self):
        html = self.html('/decisiones/')
        self.assertNotIn('tool-card-tag', html)  # las etiquetas «Calculadora»/«Cobertura» repetían el nombre
        for label in ('Cuánto llevar', '¿Me alcanza?'):
            self.assertIn(f'<strong>{label}</strong>', html)
        self.assertEqual(html.count('class="tool-ico tone-'), 2)

    def test_ficha_predio_shows_summary_chips_and_plot_menus(self):
        self.labor(self.planned, -1)
        html = self.html(f'/predios/{self.farm.pk}/')
        self.assertIn('detail-summary-row', html)
        self.assertIn('Labores vencidas · 1', html)
        self.assertGreaterEqual(html.count('class="row-menu-trigger"'), 3)  # predio + 2 potreros
        self.assertIn('Registrar nueva temporada', html)
        self.assertIn('Siguiente paso', html)
        self.assertEqual(html.count('class="next-step-line"'), 1)


class CropPickerTests(TestCase):
    """Elegir cultivo: solo cultivos y datos reales, sin listas fijas ni cifras inventadas."""

    def setUp(self):
        self.user = User.objects.create_user('selector', email='selector@example.com', password=PASSWORD)
        self.other = User.objects.create_user('selector_ajeno', email='selector_ajeno@example.com', password=PASSWORD)
        self.client.force_login(self.user)
        self.zone = Zona.objects.create(nombre='Zona Central')
        self.maiz = Cultivo.objects.create(nombre='Maíz')
        self.papa = Cultivo.objects.create(nombre='Papa')
        self.lechuga = Cultivo.objects.create(nombre='Lechuga')
        self.raro = Cultivo.objects.create(nombre='Cultivo exótico del catálogo')
        self.inactivo = Cultivo.objects.create(nombre='Cultivo retirado', activo=False)
        self.farm = Predio.objects.create(
            propietario=self.user,
            nombre='Fundo selector',
            ubicacion='Chocalán',
            superficie_ha=Decimal('20'),
            zona=self.zone,
        )
        self.plot = Potrero.objects.create(predio=self.farm, nombre='Potrero Norte', superficie_ha=Decimal('4.8'))
        self.today = timezone.localdate()

    def picker(self, url='/decisiones/'):
        html = self.client.get(url).content.decode()
        match = re.search(r'<script id="agro-crop-picker" type="application/json">(.*?)</script>', html, re.S)
        self.assertIsNotNone(match, 'La página debe incluir los datos del selector de cultivos.')
        return json.loads(match.group(1)), html

    def season(self, plot, crop):
        return TemporadaCultivo.objects.create(potrero=plot, cultivo=crop, temporada_agricola='2026-2027')

    def test_crop_data_only_loads_on_the_decision_center(self):
        self.picker()
        for url in ('/predios/', '/actividades/'):
            self.assertNotIn('agro-crop-picker', self.client.get(url).content.decode(), url)

    def test_without_history_only_catalog_crops_are_suggested_and_no_statistics_are_sent(self):
        data, _html = self.picker()
        self.assertEqual(data['frequentBasis'], 'suggested')
        suggested = {Cultivo.objects.get(pk=int(pk)).nombre for pk in data['frequent']}
        self.assertEqual(suggested, {'Maíz', 'Papa'})  # solo los sugeridos que existen y están activos en el catálogo
        self.assertNotIn(str(self.inactivo.pk), data['frequent'])
        self.assertNotIn(str(self.raro.pk), data['frequent'])
        self.assertFalse(any(key in data for key in ('counts', 'usage', 'stats', 'percent')))

    def test_frequent_crops_come_from_own_history_and_never_from_other_users(self):
        farm2 = Predio.objects.create(propietario=self.other, nombre='Fundo ajeno', superficie_ha=Decimal('5'))
        plot2 = Potrero.objects.create(predio=farm2, nombre='Potrero ajeno', superficie_ha=Decimal('1'))
        for _ in range(3):
            self.season(plot2, self.lechuga)
        self.season(self.plot, self.papa)
        self.season(self.plot, self.papa)
        self.season(self.plot, self.maiz)
        data, _html = self.picker()
        self.assertEqual(data['frequentBasis'], 'used')
        self.assertEqual(data['frequent'], [str(self.papa.pk), str(self.maiz.pk)])
        self.assertNotIn(str(self.lechuga.pk), data['frequent'])

    def test_groups_use_catalog_constants_and_short_filter_names(self):
        data, _html = self.picker()
        shorts = [group['short'] for group in data['groups']]
        for name in ('Cereales', 'Hortalizas', 'Leguminosas', 'Tubérculos', 'Forrajeros'):
            self.assertIn(name, shorts)
        self.assertEqual(data['groups'][-1]['name'], 'Otros cultivos')

    def test_crop_dialog_has_dates_and_no_hardcoded_crop_names(self):
        html = self.client.get('/predios/').content.decode()
        html = re.sub(r'<script.*?</script>', '', html, flags=re.S)
        dialog = html[html.index('id="crop-dialog"') : html.index('</dialog>')]
        for text in (
            'id="crop-select"',
            '¿Cuándo quieres sembrar?',
            'id="crop-sowing"',
            'id="crop-harvest"',
            'Más opciones de la temporada',
            'Guardar cultivo',
        ):
            self.assertIn(text, dialog)
        for name in ('Maíz', 'Papa', 'Lechuga', 'Trigo', 'Tomate'):
            self.assertNotIn(name, dialog)  # los cultivos salen de la base de datos, no de la plantilla

    def test_forms_use_a_simple_select_with_active_crops(self):
        for url in (
            f'/predios/{self.farm.pk}/potreros/nuevo/',
            f'/predios/{self.farm.pk}/potreros/{self.plot.pk}/temporadas/nueva/',
        ):
            html = self.client.get(url).content.decode()
            self.assertIn('name="cultivo"', html)
            for crop in (self.maiz, self.papa, self.lechuga):
                self.assertIn(f'<option value="{crop.pk}">{crop.nombre}</option>', html)
            self.assertNotIn(f'<option value="{self.inactivo.pk}">', html)

    def test_saving_through_the_form_still_works(self):
        response = self.client.post(
            f'/predios/{self.farm.pk}/potreros/{self.plot.pk}/temporadas/nueva/',
            {
                'cultivo': self.maiz.pk,
                'variedad': '',
                'temporada_agricola': '2026-2027',
                'estado': 'planned',
                'fecha_siembra': '',
                'fecha_cosecha': '',
                'observaciones': '',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(TemporadaCultivo.objects.filter(potrero=self.plot, cultivo=self.maiz).exists())


class PrediosSideLayoutTests(TestCase):
    """/predios/: mapa a la izquierda; Información seleccionada e Historial de potreros apilados a la derecha."""

    def setUp(self):
        self.user = User.objects.create_user('lateral', email='lateral@example.com', password=PASSWORD)
        self.client.force_login(self.user)
        self.farm = Predio.objects.create(
            propietario=self.user, nombre='Fundo lateral', ubicacion='Chocalán', superficie_ha=Decimal('30')
        )
        self.north = Potrero.objects.create(predio=self.farm, nombre='Potrero Norte', superficie_ha=Decimal('4.8'))
        self.south = Potrero.objects.create(predio=self.farm, nombre='Potrero Sur', superficie_ha=Decimal('2'))

    def page(self, url='/predios/'):
        return re.sub(r'<script.*?</script>', '', self.client.get(url).content.decode(), flags=re.S)

    def test_history_lives_in_the_right_column_under_selected_info(self):
        html = self.page()
        workspace = html.index('terrain-workspace')
        map_card = html.index('class="card map-card"')
        side = html.index('terrain-side')
        selected = html.index('id="selected-terrain"')
        history = html.index('id="potrero-history"')
        dialog = html.index('id="crop-dialog"')
        self.assertTrue(
            workspace < map_card < side < selected < history < dialog
        )  # mapa primero; historial dentro del lateral
        self.assertEqual(html.count('class="card potrero-history"'), 1)
        self.assertEqual(html.count('HISTORIAL DE POTREROS'), 1)

    def test_history_keeps_search_filters_order_pagination_and_actions(self):
        html = self.page()
        section = html[html.index('id="potrero-history"') : html.index('id="crop-dialog"')]
        for text in (
            'name="q"',
            'name="filtro_predio"',
            'name="cultivo"',
            'name="orden"',
            'Filtrar',
            'Ver en mapa',
            'Ficha e historial',
            'Editar',
            'Potrero Norte',
            'Potrero Sur',
            'Paginación del historial de potreros',
            'Página 1 de 1',
            '2 registrados',
        ):
            self.assertIn(text, section)
        # Las acciones pasaron a un menú ⋯ (una por potrero) y ya no son botones permanentes del listado.
        self.assertEqual(section.count('class="row-menu-trigger"'), 2)
        self.assertNotIn('btn-compact', section)
        self.assertIn(f'data-select-plot="{self.north.pk}"', section)
        self.assertIn(f'/predios/{self.farm.pk}/potreros/{self.north.pk}/editar/', section)
        # Fechas completas, una por línea para que no se corten en el ancho lateral.
        self.assertRegex(
            section,
            r'<small>Creado \d\d/\d\d/\d{4} \d\d:\d\d</small>\s*<small>Actualizado \d\d/\d\d/\d{4} \d\d:\d\d</small>',
        )

    def test_filters_still_work_in_the_side_panel(self):
        html = self.page('/predios/?q=Sur')
        section = html[html.index('id="potrero-history"') : html.index('id="crop-dialog"')]
        self.assertIn('Potrero Sur', section)
        self.assertNotIn('Potrero Norte', section)
        self.assertIn('value="Sur"', section)
        empty = self.page('/predios/?q=inexistente')
        self.assertIn('No hay potreros que coincidan con esos filtros.', empty)

    def test_layout_styles_two_columns_internal_scroll_and_responsive_stack(self):
        css = (Path(__file__).resolve().parent.parent / 'static' / 'css' / 'predios.css').read_text(encoding='utf-8')
        # Sin depender del formato: se comparan las reglas con los espacios y saltos de línea unificados.
        block = re.sub(r'\s+', ' ', css[css.index('/* ---------- /predios/: mapa a la izquierda') :])
        self.assertIn('.terrain-workspace { grid-template-columns: minmax(0, 2fr) minmax(340px, 1fr);', block)
        self.assertIn('.terrain-side .potrero-history-list { max-height:', block)
        self.assertIn('overflow-y: auto', block)
        self.assertIn('@media (max-width: 1100px)', block)
        self.assertIn('.terrain-workspace .map-card { order: -1; }', block)  # móvil/tablet: el mapa primero
        self.assertIn(
            '@media (min-width: 760px) and (max-width: 1100px)', block
        )  # tablet ancha: info e historial lado a lado
        self.assertNotIn('overflow-x: scroll', block)


class CalculatorGuidedFlowTests(IntegrationBase):
    """«Cuánto llevar»: herramienta guiada paso a paso (solo presentación; el cálculo sigue en el servidor)."""

    def page(self, url='/decisiones/?herramienta=llevar'):
        return self.client.get(url).content.decode()

    def test_steps_status_and_button_hierarchy(self):
        html = self.page()
        for step in ('plot', 'crop', 'area', 'dose', 'unit', 'go'):
            self.assertIn(f'data-step="{step}"', html)
        llevar_panel = html.split('id="tool-llevar"', 1)[1].split('id="tool-alcanza"', 1)[0]
        self.assertEqual(llevar_panel.count('class="calc-step"'), 6)
        for marker in (
            'id="calc-status"',
            'id="calc-summary"',
            'id="calc-plot-picker"',
            'id="calc-crop-picker"',
            'id="calc-area-tools"',
            'id="calc-submit-hint"',
            'Opciones avanzadas',
            'Cálculo avanzado de semillas',
            'calc-flow.js',
            'decisiones.css',
        ):
            self.assertIn(marker, html)
        for removed in ('id="calc-stats"', 'info-strip', 'calc-strip', 'id="ctx-line"', 'class="calc-head"'):
            self.assertNotIn(removed, html)  # sin estadísticas decorativas ni cabeceras repetidas
        # Prioridad: «Calcular ahora» es el botón principal; PDF, Reportes y «¿Me alcanza?» son secundarios.
        compact = re.sub(r'\s+', ' ', html)  # sin depender de cómo se formatearon los atributos
        self.assertRegex(compact, r'<button class="btn-primary-agro calc-submit" type="submit" id="calc-submit">')
        self.assertRegex(compact, r'<button class="btn-secondary-agro btn-sm" type="button" id="calc-pdf">')
        self.assertIn('Ver en Reportes', html)
        self.assertIn('id="calc-go-cover"', html)
        self.assertIn('Ir a «¿Me alcanza?»', html)

    def test_native_form_controls_remain_the_source_of_truth(self):
        html = self.page()
        for field in (
            'calc-plot',
            'calc-crop',
            'calc-product',
            'calc-target',
            'calc-area',
            'calc-dose',
            'calc-unit',
            'calc-error',
            'calc-result',
            'calc-notes',
            'adv-seed',
            'adv-nutrient',
            'calc-advanced',
        ):
            self.assertIn(f'id="{field}"', html)
        self.assertIn('data-mode="semillas"', html)
        self.assertIn('data-mode="insumos"', html)
        self.assertEqual(self.client.get('/decisiones/?herramienta=llevar&tipo=insumos').status_code, 200)

    def test_visual_crop_data_reaches_the_page(self):
        response = self.client.get('/decisiones/?herramienta=llevar')
        self.assertIn('groups', response.context['crop_picker'])
        html = response.content.decode()
        self.assertIn('id="agro-crop-picker"', html)

    def test_incomplete_catalog_notice_is_actionable_without_invented_routes(self):
        html = self.page()
        self.assertIn('Catálogo en construcción', html)
        self.assertIn('El catálogo no tiene insumos registrados', html)
        self.assertIn('class="catalog-alert"', html)
        self.assertIn('href="/productos/"', html)
        self.assertIn('data-calc-jump', html)  # hay cultivos: se puede seguir como simulación
        self.assertNotIn('Completar catálogo', html)  # solo para administradores
        self.user.is_staff = True
        self.user.save()
        self.assertIn('Completar catálogo', self.page())

    def test_notice_disappears_with_a_complete_catalog_and_changes_without_crops(self):
        TipoInsumo.objects.create(nombre='Semilla')
        Insumo.objects.create(nombre='Cualquiera', tipo=TipoInsumo.objects.get(nombre='Semilla'), cultivo=self.maize)
        self.assertNotIn('Catálogo en construcción', self.page())
        Insumo.objects.all().delete()
        Cultivo.objects.all().delete()
        html = self.page()
        self.assertIn('El catálogo no tiene cultivos registrados', html)
        self.assertNotIn('data-calc-jump', html)  # sin cultivos no hay simulación posible

    def test_user_without_plots_gets_actionable_empty_state_hooks(self):
        loner = User.objects.create_user('sinpotreros', email='sin@example.com', password=PASSWORD)
        self.client.force_login(loner)
        response = self.client.get('/decisiones/?herramienta=llevar')
        self.assertEqual(response.context['farm_data']['plots'], [])
        html = response.content.decode()
        self.assertIn('data-plots-url="/predios/"', html)
        self.assertIn('id="calc-plot-picker"', html)

    def test_presentation_layer_does_not_calculate_or_call_the_server(self):
        base = Path(__file__).resolve().parent.parent / 'static'
        flow = (base / 'js' / 'calc-flow.js').read_text(encoding='utf-8')
        panel = (base / 'js' / 'calc-panel.js').read_text(encoding='utf-8')
        self.assertNotIn('fetch(', flow)
        self.assertNotIn('XMLHttpRequest', flow)
        self.assertIn('calc:update', flow)
        self.assertIn("new CustomEvent('calc:update'", panel)
        self.assertIn('function readiness()', panel)
        # El cálculo sigue validándose en el servidor.
        self.assertIn("operation: 'dose'", panel)
        self.assertIn('calculateUrl', panel)
        for name in ('calc-flow.js', 'calc-panel.js', 'decisiones.css'):
            self.assertTrue((base / ('js' if name.endswith('.js') else 'css') / name).exists(), name)

    def test_tool_selector_keeps_two_tools_and_no_catalog_strip(self):
        html = self.page()
        for tool in ('llevar', 'alcanza'):
            self.assertIn(f'data-tool="{tool}"', html)
        self.assertEqual(html.count('class="tool-ico tone-'), 2)
        # Sin tira de estadísticas del catálogo: ni cuando el catálogo está completo ni cuando le faltan datos.
        TipoInsumo.objects.create(nombre='Fertilizante')
        Insumo.objects.create(nombre='Abono de prueba', tipo=TipoInsumo.objects.get(nombre='Fertilizante'))
        for page in (html, self.page()):
            for label in ('Semillas registradas', 'Fertilizantes en catálogo', 'Otros insumos registrados'):
                self.assertNotIn(label, page)
        for name in ('calc-flow.js', 'tool-flow.js', 'calc-panel.js'):
            source = (Path(__file__).resolve().parent.parent / 'static' / 'js' / name).read_text(encoding='utf-8')
            for label in ('Semillas registradas', 'Fertilizantes en catálogo', 'Otros insumos registrados'):
                self.assertNotIn(label, source, name)


class ToolFlowDesignTests(IntegrationBase):
    """«¿Me alcanza?» usa el mismo diseño guiado que «Cuánto llevar» (solo presentación)."""

    def page(self, tool):
        return self.client.get(f'/decisiones/?herramienta={tool}').content.decode()

    def static_source(self, kind, name):
        return (Path(__file__).resolve().parent.parent / 'static' / kind / name).read_text(encoding='utf-8')

    def test_cover_has_guided_steps_and_flow_hooks(self):
        html = self.page('alcanza')
        for marker in (
            'id="cover-form"',
            'data-flow="cover"',
            'data-picker="plot"',
            'data-picker="crop"',
            'data-flow-status',
            'data-flow-summary',
            'data-flow-summary-body',
            'data-flow-hint',
            'data-flow-submit',
            'data-flow-area-tools',
            'tool-flow.js',
            'decisiones.css',
            'Comprobar disponibilidad',
        ):
            self.assertIn(marker, html)
        self.assertNotIn('data-flow-strip', html)  # sin tira de estadísticas decorativas
        self.assertIn('data-result-for="cover-form"', html)

    def test_native_controls_and_field_names_are_preserved(self):
        html = self.page('alcanza')
        for name in ('plot', 'crop', 'product', 'stock', 'unit', 'dose', 'area'):
            self.assertRegex(html, rf'name="{name}"')

    def test_flow_layer_is_presentation_only(self):
        js = self.static_source('js', 'tool-flow.js')
        self.assertNotIn('fetch(', js)
        self.assertNotIn('XMLHttpRequest', js)
        css = self.static_source('css', 'decisiones.css')
        self.assertTrue(css.strip())

    def test_both_tools_still_render_for_user_without_plots(self):
        empty = get_user_model().objects.create_user('sin_potreros', 'sp@example.com', 'Clave12345!')
        client = Client()
        client.force_login(empty)
        for tool in ('llevar', 'alcanza'):
            response = client.get(f'/decisiones/?herramienta={tool}')
            self.assertEqual(response.status_code, 200, tool)
            self.assertIn('data-tool="alcanza"', response.content.decode())

    def test_deep_link_parameters_still_accepted(self):
        for query in (
            'herramienta=alcanza&plot=1&crop=1&insumo=1',
            'herramienta=ficha&crop=1&insumo=1',
            'herramienta=cosecha&plot=1',
        ):
            self.assertEqual(self.client.get('/decisiones/?' + query).status_code, 200, query)


class GuidedJourneyTests(IntegrationBase):
    """Ruta de trabajo, zona por defecto (Melipilla) y coherencia entre secciones (solo presentación y datos existentes)."""

    def journey(self, url='/'):
        return self.client.get(url).context['journey']

    def assign(self, **fields):
        for name, value in fields.items():
            setattr(self.plot, name, value)
        self.plot.save()

    def test_new_user_is_guided_to_register_the_first_farm(self):
        newcomer = User.objects.create_user('nuevo', email='nuevo@example.com', password=PASSWORD)
        self.client.force_login(newcomer)
        journey = self.journey()
        self.assertEqual(
            [step['state'] for step in journey['steps']], ['current', 'todo', 'todo', 'todo', 'todo', 'todo']
        )
        self.assertEqual(journey['next']['cta'], 'Registrar predio')
        self.assertEqual(journey['next']['url'], '/predios/nuevo/')

    def test_journey_follows_the_data_step_by_step(self):
        # Predio con potrero pero sin cultivo → siguiente paso: asignar cultivo a ese potrero.
        journey = self.journey()
        self.assertEqual([step['state'] for step in journey['steps']][:3], ['done', 'done', 'current'])
        self.assertEqual(journey['next']['title'], 'Selecciona qué quieres plantar')
        self.assertIn('Potrero listo', journey['next']['status'])
        self.assertEqual(journey['next']['cta'], 'Asignar cultivo')
        self.assertEqual(journey['next']['url'], f'/predios/{self.farm.pk}/potreros/{self.plot.pk}/editar/')
        # Con cultivo pero sin fecha de siembra → definir siembra.
        self.assign(cultivo=self.maize)
        journey = self.journey()
        self.assertEqual(journey['steps'][3]['state'], 'current')
        self.assertEqual(journey['next']['title'], 'Planifica la siembra')
        self.assertEqual(journey['next']['cta'], 'Continuar planificación')
        self.assertEqual('Cultivo seleccionado', journey['next']['status'])
        # Con siembra → ya puede usar el Centro de decisiones, con el potrero y el cultivo precargados.
        self.assign(fecha_siembra=_date(2026, 10, 10))
        journey = self.journey()
        self.assertEqual(journey['steps'][4]['state'], 'current')
        self.assertEqual(journey['next']['status'], 'Ya puedes utilizar las herramientas agrícolas')
        self.assertEqual(
            [item['label'] for item in journey['next']['actions']],
            ['Calcular semillas', 'Calcular insumos', 'Ver si me alcanza', 'Consultar ficha de producto'],
        )
        self.assertEqual(journey['next']['url'], f'/decisiones/?tipo=semillas&potrero={self.plot.pk}')
        self.assertEqual(journey['next']['actions'][1]['url'], f'/decisiones/?tipo=insumos&potrero={self.plot.pk}')
        self.assertEqual(
            journey['next']['actions'][2]['url'], f'/decisiones/?herramienta=alcanza&potrero={self.plot.pk}'
        )
        # Con un cálculo guardado → seguimiento y reportes.
        CalculoAgricola.objects.create(
            usuario=self.user,
            potrero=self.plot,
            operacion='dose',
            entrada={'area_ha': 4, 'crop': 'Maíz prueba'},
            resultado={'amount': 100, 'unit': 'kg', 'dose': 25, 'dose_unit': 'kg/ha'},
        )
        journey = self.journey()
        self.assertEqual(journey['steps'][4]['state'], 'done')
        self.assertEqual(journey['steps'][5]['state'], 'current')
        self.assertEqual(journey['next']['url'], '/reportes/')

    def test_farm_without_plots_asks_to_create_one(self):
        empty_farm = Predio.objects.create(
            propietario=self.user, nombre='Parcela vacía', ubicacion='Z', superficie_ha=2
        )
        journey = self.journey()
        self.assertEqual(journey['steps'][1]['state'], 'current')
        self.assertEqual(journey['next']['title'], '¿Cómo quieres trabajar este terreno?')
        self.assertEqual('Tu predio ya está registrado', journey['next']['status'])
        self.assertIn('Parcela vacía', journey['next']['text'])
        self.assertEqual(journey['next']['url'], f'/predios/{empty_farm.pk}/potreros/nuevo/')

    def test_journey_is_shown_on_main_sections_but_not_on_forms_or_farm_detail(self):
        for url in ('/', '/predios/', '/calendario/', '/reportes/'):
            html = self.client.get(url).content.decode()
            self.assertIn('class="journey no-print"', html, url)
            self.assertIn('Siguiente paso', html, url)
        # El Centro de decisiones y las calculadoras no llevan la ruta de seis pasos.
        self.assertNotIn('class="journey no-print"', self.client.get('/decisiones/').content.decode())
        for url in (f'/predios/{self.farm.pk}/', '/predios/nuevo/', f'/predios/{self.farm.pk}/potreros/nuevo/'):
            self.assertNotIn('class="journey no-print"', self.client.get(url).content.decode(), url)

    def test_each_section_explains_what_comes_next(self):
        # El Centro de decisiones ya no explica «qué sigue»: arranca directo en la calculadora.
        self.assertNotIn(
            'Para calcular necesitas: potrero + cultivo + superficie + dosis',
            self.client.get('/decisiones/').content.decode(),
        )
        self.assertIn('completa la fecha de siembra', self.client.get('/calendario/').content.decode())
        self.assertIn('quedan guardados y aparecen aquí', self.client.get('/reportes/').content.decode())

    def test_dashboard_lists_saved_calculations_instead_of_claiming_there_are_none(self):
        self.assertIn('Aún no has guardado cálculos', self.client.get('/').content.decode())
        CalculoAgricola.objects.create(
            usuario=self.user,
            potrero=self.plot,
            operacion='yield',
            entrada={'area_ha': 4, 'crop': 'Maíz prueba'},
            resultado={'production': 120, 'rate': 30, 'source': 'Registro propio'},
        )
        html = self.client.get('/').content.decode()
        self.assertNotIn('Aún no hay historial persistente', html)
        self.assertIn('120 ton', html)
        self.assertIn('Lote A', html)
        self.assertIn('/reportes/?tipo=calculos', html)

    def test_dashboard_calculations_belong_to_the_current_user_only(self):
        CalculoAgricola.objects.create(
            usuario=self.other,
            potrero=self.other_plot,
            operacion='yield',
            entrada={'area_ha': 1},
            resultado={'production': 999, 'rate': 1},
        )
        self.assertNotIn('999 ton', self.client.get('/').content.decode())


class MelipillaDefaultZoneTests(IntegrationBase):
    def test_default_zone_helpers_are_idempotent_and_carry_no_agronomic_data(self):
        from ..catalog import default_zone, ensure_default_zone

        self.assertIsNone(default_zone())
        zone, created = ensure_default_zone()
        self.assertTrue(created)
        self.assertEqual(zone.nombre, 'Melipilla')
        self.assertEqual(ensure_default_zone(), (zone, False))
        self.assertEqual(default_zone(), zone)
        self.assertFalse(ReferenciaAgronomica.objects.exists())

    def test_deactivated_melipilla_is_respected(self):
        from ..catalog import ensure_default_zone

        Zona.objects.create(nombre='Melipilla', activo=False)
        self.assertEqual(ensure_default_zone(), (None, False))
        self.assertEqual(Zona.objects.filter(nombre__iexact='Melipilla').count(), 1)

    def test_new_farm_form_lists_melipilla_first_and_preselected(self):
        from ..forms import PredioForm

        melipilla = Zona.objects.create(nombre='Melipilla')
        form = PredioForm()
        self.assertEqual(form['zona'].value(), melipilla.pk)
        self.assertEqual(form.fields['zona'].label, 'Zona agrícola')
        self.assertEqual(list(form.fields['zona'].queryset)[0], melipilla)
        self.assertIn('Por defecto: Melipilla', form.fields['zona'].help_text)
        html = self.client.get('/predios/nuevo/').content.decode()
        self.assertRegex(html, rf'<option value="{melipilla.pk}" selected>Melipilla</option>')

    def test_editing_a_farm_keeps_its_own_zone(self):
        from ..forms import PredioForm

        Zona.objects.create(nombre='Melipilla')
        self.assertEqual(PredioForm(instance=self.farm)['zona'].value(), self.zone.pk)

    def test_farm_can_still_be_saved_when_the_catalog_has_no_zones(self):
        Predio.objects.update(zona=None)
        Zona.objects.all().delete()
        boundary = [[-71.216, -33.687], [-71.213, -33.687], [-71.213, -33.684], [-71.216, -33.684]]
        feature = {
            'type': 'Feature',
            'properties': {},
            'geometry': {'type': 'Polygon', 'coordinates': [boundary + [boundary[0]]]},
        }
        response = self.client.post(
            '/predios/nuevo/',
            {
                'nombre': 'Predio local',
                'ubicacion': 'Melipilla',
                'geojson': json.dumps(feature),
                'perimetro_aceptado': 'on',
                'latitud': '-33.687',
                'longitud': '-71.216',
            },
        )
        farm = Predio.objects.get(nombre='Predio local')
        self.assertRedirects(response, f'/predios/{farm.pk}/?nuevo=1')
        self.assertIsNone(farm.zona_id)
        self.assertFalse(Zona.objects.exists())  # guardar un predio nunca crea zonas por su cuenta

    def test_sowing_calendar_opens_on_melipilla_for_users_without_a_zone(self):
        melipilla = Zona.objects.create(nombre='Melipilla')
        self.farm.zona = None
        self.farm.save(update_fields=['zona'])
        response = self.client.get('/calendario/')
        self.assertEqual(response.context['calendar_default_zone'], melipilla.pk)

    def test_base_loader_command_creates_zone_and_crops_without_agronomic_data(self):
        from django.core.management import call_command
        from io import StringIO

        Predio.objects.update(zona=None)
        Zona.objects.all().delete()
        out = StringIO()
        call_command('cargar_cultivos_base', stdout=out)
        call_command('cargar_cultivos_base', stdout=out)
        self.assertEqual(Zona.objects.filter(nombre='Melipilla').count(), 1)
        self.assertTrue(Cultivo.objects.filter(nombre='Papa').exists())
        self.assertFalse(Cultivo.objects.filter(nombre='Papa', densidad_siembra__isnull=False).exists())
        self.assertFalse(ReferenciaAgronomica.objects.exists())

    def test_entrypoint_loads_base_crops_and_zone_on_start(self):
        source = (Path(__file__).resolve().parent.parent.parent / 'docker-entrypoint.sh').read_text(encoding='utf-8')
        self.assertIn('python manage.py importar_catalogo data --aplicar', source)
        self.assertLess(source.index('migrate'), source.index('importar_catalogo data --aplicar'))
        self.assertNotIn('\r', source)  # el script debe tener saltos de línea LF para ejecutarse en Linux


class DecisionsContinuityStaticTests(TestCase):
    """Presentación del Centro de decisiones y de Buscar: sin cultivo heredado de otro potrero y con siguiente paso."""

    def source(self, kind, name):
        return (Path(__file__).resolve().parent.parent / 'static' / kind / name).read_text(encoding='utf-8')

    def test_plot_without_crop_does_not_inherit_the_previous_crop(self):
        self.assertIn("else if (!plot.cropId) ui.crop.value = '';", self.source('js', 'calc-panel.js'))
        self.assertIn("else if (!plot.cropId) cropSelect.value = '';", self.source('js', 'decisiones.js'))

    def test_plot_summary_shows_next_step_and_links_to_the_plot_form(self):
        for name in ('calc-flow.js', 'tool-flow.js'):
            js = self.source('js', name)
            self.assertIn('Siguiente paso: ', js, name)
            self.assertIn('plot.editUrl', js, name)
            self.assertIn('Asignar cultivo', js, name)

    def test_search_examples_come_from_the_real_catalog(self):
        # Buscar se resuelve en el servidor: los ejemplos salen de los datos reales de la cuenta, nunca de textos fijos.
        js = self.source('js', 'busqueda.js')
        template = (Path(__file__).resolve().parent.parent / 'templates' / 'busqueda.html').read_text(encoding='utf-8')
        for source in (js, template):
            self.assertNotIn('gusano', source)
            self.assertNotIn('catálogo demo', source)
        self.assertIn('search-examples', template)
        self.assertIn('search_explore.examples', template)


class FlowStatesTests(IntegrationBase):
    """Estados del recorrido (sin predio, sin potrero, sin cultivo, planificado) y enlaces directos entre secciones."""

    FEATURE = {
        'type': 'Feature',
        'properties': {},
        'geometry': {
            'type': 'Polygon',
            'coordinates': [[[-71.2, -33.7], [-71.2, -33.69], [-71.19, -33.69], [-71.19, -33.7], [-71.2, -33.7]]],
        },
    }

    def source(self, kind, name):
        return (Path(__file__).resolve().parent.parent / 'static' / kind / name).read_text(encoding='utf-8')

    def plan(self):
        self.plot.cultivo = self.maize
        self.plot.fecha_siembra = _date(2026, 10, 10)
        self.plot.fecha_cosecha = _date(2027, 3, 30)
        self.plot.save()

    def test_state_1_without_farm_invites_to_register_one_and_decisions_do_not_pose_as_available(self):
        newcomer = User.objects.create_user('sinpredio', email='sinpredio@example.com', password=PASSWORD)
        self.client.force_login(newcomer)
        response = self.client.get('/')
        journey = response.context['journey']
        self.assertEqual(journey['next']['title'], 'Comienza registrando tu predio')
        self.assertEqual(journey['next']['cta'], 'Registrar predio')
        html = self.client.get('/decisiones/').content.decode()
        self.assertNotIn('Comienza registrando tu predio', html)  # la ruta ya no se dibuja en las calculadoras
        self.assertIn('data-farm-create-url="/predios/nuevo/"', html)  # el botón único de la tarjeta lleva aquí
        self.assertNotIn('Regístralo en <a', html)

    def test_state_2_farm_without_plots_offers_whole_farm_or_split(self):
        self.plot.delete()
        Predio.objects.filter(pk=self.farm.pk).update(geojson=self.FEATURE)
        journey = self.client.get('/').context['journey']
        self.assertEqual(journey['next']['status'], 'Tu predio ya está registrado')
        self.assertEqual(
            [(item['label'], item['post']) for item in journey['next']['actions']],
            [('Utilizar todo el predio', True), ('Dividir en potreros', False)],
        )
        html = self.client.get('/').content.decode()
        self.assertIn(f'action="/predios/{self.farm.pk}/usar-completo/"', html)
        self.assertIn('Utilizar todo el predio', html)
        self.assertIn('Dividir en potreros', html)
        # Con un predio sin perímetro no se puede copiar su geometría: solo se ofrece dividir.
        Predio.objects.filter(pk=self.farm.pk).update(geojson=None)
        labels = [item['label'] for item in self.client.get('/').context['journey']['next']['actions']]
        self.assertEqual(labels, ['Dividir en potreros'])
        # En el Centro de decisiones el enlace lleva a elegir cómo trabajar el predio.
        decisions = self.client.get('/decisiones/').content.decode()
        self.assertIn(f'/predios/{self.farm.pk}/?nuevo=1', decisions)

    def test_state_3_and_4_use_the_requested_wording(self):
        journey = self.client.get('/').context['journey']
        self.assertEqual(journey['next']['title'], 'Selecciona qué quieres plantar')
        self.assertEqual(journey['next']['cta'], 'Asignar cultivo')
        self.plot.cultivo = self.maize
        self.plot.save()
        journey = self.client.get('/').context['journey']
        self.assertEqual(journey['next']['title'], 'Planifica la siembra')
        self.assertEqual(journey['next']['cta'], 'Continuar planificación')

    def test_state_5_offers_the_three_tools_with_the_plot_preselected(self):
        self.plan()
        html = self.client.get('/').content.decode()
        self.assertIn('Ya puedes utilizar las herramientas agrícolas', html)
        for label in ('Calcular semillas', 'Calcular insumos', 'Ver si me alcanza', 'Consultar ficha de producto'):
            self.assertIn(label, html)
        self.assertIn(f'/decisiones/?tipo=semillas&amp;potrero={self.plot.pk}', html)
        self.assertIn(f'/decisiones/?herramienta=alcanza&amp;potrero={self.plot.pk}', html)

    def test_sowing_and_farm_detail_link_each_plot_to_its_next_action(self):
        calendar = self.client.get('/calendario/').content.decode()
        detail = self.client.get(f'/predios/{self.farm.pk}/').content.decode()
        for html in (calendar, detail):
            self.assertIn('Asignar cultivo', html)
            self.assertNotIn(f'tipo=semillas&amp;potrero={self.plot.pk}', html)  # aún no se puede calcular
        self.plot.cultivo = self.maize
        self.plot.save()
        self.assertIn('Continuar planificación', self.client.get('/calendario/').content.decode())
        self.plan()
        link = f'/decisiones/?tipo=semillas&amp;potrero={self.plot.pk}'
        calendar = self.client.get('/calendario/').content.decode()
        detail = self.client.get(f'/predios/{self.farm.pk}/').content.decode()
        for html in (calendar, detail):
            self.assertIn(link, html)
            self.assertIn(f'/decisiones/?tipo=insumos&amp;potrero={self.plot.pk}', html)
            self.assertIn(f'/decisiones/?herramienta=alcanza&amp;potrero={self.plot.pk}', html)
        self.assertIn('Cambiar cultivo o fechas', calendar)

    def test_farm_payload_exposes_direct_links_for_the_map_panel(self):
        self.plan()
        html = self.client.get('/predios/').content.decode()
        self.assertIn('seedsUrl', html)
        self.assertIn('setupUrl', html)
        js = self.source('js', 'predios-map.js')
        self.assertIn('plot.seedsUrl', js)
        self.assertIn("'Calcular semillas'", js)

    def test_breadcrumbs_tell_where_the_user_is(self):
        detail = self.client.get(f'/predios/{self.farm.pk}/?potrero={self.plot.pk}').content.decode()
        self.assertIn('class="crumbs"', detail)
        self.assertRegex(
            detail, r'Mis predios</a><span aria-hidden="true">›</span><a href="/predios/\d+/">Campo</a>.*Lote A'
        )
        form = self.client.get(f'/predios/{self.farm.pk}/potreros/{self.plot.pk}/editar/').content.decode()
        self.assertIn('class="crumbs"', form)
        self.assertIn('Editar potrero', form)
        self.assertNotIn('id="ctx-line"', self.client.get('/decisiones/').content.decode())  # sin línea de contexto

    def test_decisions_accept_the_public_query_parameters_and_label_the_value_origin(self):
        self.plan()
        self.assertEqual(self.client.get(f'/decisiones/?tipo=semillas&potrero={self.plot.pk}').status_code, 200)
        self.assertEqual(self.client.get(f'/decisiones/?tipo=insumos&potrero={self.plot.pk}').status_code, 200)
        self.assertEqual(self.client.get(f'/decisiones/?herramienta=alcanza&potrero={self.plot.pk}').status_code, 200)
        for name in ('calc-panel.js', 'decisiones.js'):
            self.assertIn("params.set('plot', params.get('potrero'))", self.source('js', name), name)
        panel = self.source('js', 'calc-panel.js')
        for text in (
            'Dato documentado del catálogo',
            'Ajustado manualmente',
            'Valor ingresado manualmente. Este cálculo corresponde a una simulación.',
            'No puede superar las ',
        ):
            self.assertIn(text, panel)
        self.assertNotIn('ctx-line', self.source('js', 'decisiones.js'))

    def test_base_catalog_has_the_extra_central_chile_crops_by_name_only(self):
        from ..catalog import ensure_base_crops

        ensure_base_crops()
        for name in ('Alcachofa', 'Durazno', 'Frutilla'):
            crop = Cultivo.objects.get(nombre=name)
            self.assertIsNone(crop.densidad_siembra)
            self.assertIsNone(crop.rendimiento_ton_ha)
