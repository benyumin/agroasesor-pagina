"""Vista oficial del acceso (login, recuperar contraseña y pantallas siguientes).

Verifican que las pantallas usan los textos y la estructura del diseño de Figma, que los parámetros del
formulario (nombres de campos, token CSRF, rutas, comportamiento) siguen intactos y que el CSS conserva las
medidas y colores del diseño.
"""

import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse

User = get_user_model()
PASSWORD = 'Campo-Seguro-2026!'
MAIL = {'default': {'BACKEND': 'django.core.mail.backends.locmem.EmailBackend'}}
CSS = Path(__file__).resolve().parent.parent / 'static' / 'css' / 'login.css'
HEADLINE_LOGIN = 'Optimiza tus decisiones agrícolas con datos reales de tu campo.'
HEADLINE_RECUPERAR = 'Protegemos la seguridad de tus datos y la administración de tus cultivos.'


def css_text():
    return CSS.read_text(encoding='utf-8').replace('\r\n', '\n')


def css_rule(selector):
    """Cuerpo de la regla CSS cuyo selector es exactamente ``selector`` (primera aparición)."""
    coincidencia = re.search(r'(?m)^' + re.escape(selector) + r' \{\n(.*?)\n\}', css_text(), re.S)
    assert coincidencia, f'no existe la regla {selector}'
    return coincidencia.group(1)


class LoginVistaOficialTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('agricultora', email='ana@example.com', password=PASSWORD)

    def test_login_muestra_los_textos_del_diseno(self):
        r = self.client.get('/login/')
        self.assertEqual(r.status_code, 200)
        for texto in (
            '<h1>Iniciar Sesión</h1>',
            'Ingresa tus credenciales profesionales para acceder a la plataforma agrícola.',
            '<span>Correo electrónico</span>',
            'placeholder="ejemplo@agroasesor.cl"',
            '<span>Contraseña</span>',
            '¿Olvidaste tu contraseña?',
            'Ingresar</button>',
            '¿No tienes cuenta?',
            'Regístrate aquí',
            HEADLINE_LOGIN,
            'AgroAsesor',
            'Gestión agrícola inteligente',
            'Melipilla, Chile',
        ):
            self.assertContains(r, texto)
        self.assertNotContains(r, 'Módulo de Acceso')  # frase retirada del pie del panel

    def test_login_conserva_los_parametros_del_formulario(self):
        html = self.client.get('/login/').content.decode()
        self.assertIn('<form method="post" class="access-form">', html)
        self.assertIn('name="csrfmiddlewaretoken"', html)
        self.assertIn('<input type="hidden" name="mode" value="login">', html)
        usuario = re.search(r'<input[^>]*id="username"[^>]*>', html, re.S).group(0)
        for atributo in ('name="username"', 'type="text"', 'required', 'autocomplete="username"'):
            self.assertIn(atributo, usuario)  # sigue aceptando usuario O correo (type="text", no "email")
        clave = re.search(r'<input[^>]*id="password"[^>]*>', html, re.S).group(0)
        for atributo in ('name="password"', 'type="password"', 'required', 'autocomplete="current-password"'):
            self.assertIn(atributo, clave)
        self.assertIn('data-eye="password"', html)
        self.assertIn('href="/cuenta/recuperar/"', html)
        self.assertIn('href="?mode=register"', html)

    def test_login_sigue_funcionando_con_correo_y_con_usuario(self):
        for identificador in ('ana@example.com', 'agricultora'):
            c = Client()
            r = c.post('/login/', {'mode': 'login', 'username': identificador, 'password': PASSWORD})
            self.assertRedirects(r, '/', fetch_redirect_response=False)
            self.assertEqual(int(c.session['_auth_user_id']), self.user.pk)

    def test_login_incorrecto_muestra_el_error_y_conserva_lo_escrito(self):
        r = self.client.post('/login/', {'mode': 'login', 'username': 'ana@example.com', 'password': 'mala'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Usuario, correo electrónico o contraseña incorrectos.')
        self.assertContains(r, 'role="alert"')
        self.assertContains(r, 'value="ana@example.com"')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_registro_usa_la_misma_vista_con_todos_sus_campos(self):
        r = self.client.get('/login/?mode=register')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, '<h1>Crear Cuenta</h1>')
        self.assertContains(r, 'Registra tu cuenta de agricultor.')
        self.assertContains(r, 'access-visual')
        for campo in (
            'nombre_completo',
            'email',
            'telefono',
            'comuna',
            'rut',
            'password1',
            'password2',
            'acepta_politicas',
        ):
            self.assertContains(r, f'name="{campo}"')
        self.assertContains(r, '<input type="hidden" name="mode" value="register">', html=True)
        self.assertContains(r, f'href="{reverse("politica_privacidad")}"')
        self.assertContains(r, 'Inicia sesión')
        self.assertContains(r, 'href="?mode=login"')

    def test_registro_sigue_creando_la_cuenta(self):
        datos = {
            'mode': 'register',
            'nombre_completo': 'Rosa Pérez',
            'email': 'rosa@example.com',
            'telefono': '+56 9 8765 4321',
            'comuna': 'Melipilla',
            'rut': '',
            'password1': PASSWORD,
            'password2': PASSWORD,
            'acepta_politicas': 'on',
        }
        r = self.client.post('/login/', datos)
        self.assertRedirects(r, '/', fetch_redirect_response=False)
        self.assertTrue(User.objects.filter(email='rosa@example.com').exists())

    def test_login_y_recuperar_comparten_el_panel_y_las_fuentes_y_hoja_de_estilos(self):
        for url in ('/login/', '/cuenta/recuperar/'):
            html = self.client.get(url).content.decode()
            self.assertIn('class="access-shell', html)
            self.assertIn('<section class="access-visual" aria-hidden="true">', html)
            self.assertIn('class="access-logo"', html)
            self.assertIn('css/login.css?v=4', html)
            self.assertRegex(html, r'(?s)<link href="[^"]*(inter\.css|family=Inter)[^"]*"')
            self.assertRegex(html, r'(?s)<link href="[^"]*(outfit\.css|family=Outfit)[^"]*"')

    def test_login_y_registro_tienen_foto_de_fondo_detras_de_la_tarjeta(self):
        for url in ('/login/', '/login/?mode=register'):
            html = self.client.get(url).content.decode()
            self.assertIn('<body class="access-body access-body--foto">', html, url)
        foto = css_rule('.access-body--foto')
        self.assertIn('url("../img/login-campo.jpg")', foto)
        self.assertIn('cover', foto)
        self.assertIn('--acc-pad: clamp(16px, 6vw, 96px);', foto)  # margen suficiente para que la foto se vea
        self.assertIn('box-shadow', css_rule('.access-body--foto .access-shell'))
        self.assertTrue(finders.find('img/login-campo.jpg'))

    def test_panel_izquierdo_usa_la_foto_aerea_del_diseno(self):
        visual = css_rule('.access-visual')
        self.assertIn('url("../img/login-campo.png")', visual)  # la foto aérea de Figma, no la del dashboard
        self.assertNotIn('dash-hero', css_text())
        self.assertTrue(finders.find('img/login-campo.png'))
        self.assertIn('rgba(33, 42, 26, 0.67)', visual)  # capa verde oscura medida del diseño

    def test_todo_el_flujo_de_recuperar_tambien_lleva_la_foto_de_fondo(self):
        urls = (
            '/cuenta/recuperar/',
            '/cuenta/enviado/',
            '/cuenta/restablecida/',
            '/cuenta/restablecer/xx/token-falso/',  # enlace inválido
        )
        for url in urls:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertContains(r, '<body class="access-body access-body--foto">', msg_prefix=url)

    def test_pie_del_panel_solo_muestra_la_ciudad(self):
        for url in ('/login/', '/login/?mode=register', '/cuenta/recuperar/', '/cuenta/enviado/', '/cuenta/restablecida/'):
            r = self.client.get(url)
            self.assertContains(r, '<span>Melipilla, Chile</span>', msg_prefix=url)
            self.assertNotContains(r, 'Módulo de Acceso', msg_prefix=url)
            self.assertNotContains(r, 'v2.1', msg_prefix=url)


class RecuperarContrasenaVistaOficialTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('farmer@example.com', email='farmer@example.com', password=PASSWORD)

    def test_recuperar_muestra_los_textos_del_diseno(self):
        r = self.client.get('/cuenta/recuperar/')
        self.assertEqual(r.status_code, 200)
        for texto in (
            '<h1>Recuperar Contraseña</h1>',
            'Ingresa tu correo electrónico y te enviaremos las instrucciones para restablecer tu contraseña.',
            '<span>Correo electrónico</span>',
            'placeholder="ejemplo@agroasesor.cl"',
            'Enviar instrucciones</button>',
            'Volver al inicio de sesión',
            HEADLINE_RECUPERAR,
            'Melipilla, Chile',
            'access-shell--recuperar',
        ):
            self.assertContains(r, texto)
        self.assertContains(r, 'href="/login/"')
        self.assertContains(r, '<path d="m12 19-7-7 7-7" />')  # flecha ← del enlace «Volver»
        self.assertNotContains(r, 'Módulo de Acceso')

    def test_recuperar_conserva_el_campo_y_el_token(self):
        html = self.client.get('/cuenta/recuperar/').content.decode()
        self.assertIn('name="csrfmiddlewaretoken"', html)
        correo = re.search(r'<input[^>]*name="email"[^>]*>', html, re.S).group(0)
        for atributo in ('type="email"', 'autocomplete="email"', 'maxlength="254"', 'required'):
            self.assertIn(atributo, correo)

    def test_correo_invalido_vuelve_con_error_y_conserva_lo_escrito(self):
        r = self.client.post('/cuenta/recuperar/', {'email': 'no-es-correo'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'class="errorlist"')
        self.assertContains(r, 'value="no-es-correo"')

    @override_settings(MAILERS=MAIL)
    def test_flujo_completo_con_la_vista_nueva(self):
        r = self.client.post('/cuenta/recuperar/', {'email': self.user.email}, follow=True)
        self.assertEqual(r.redirect_chain[-1][0], '/cuenta/enviado/')
        for texto in (
            '<h1>Solicitud Recibida</h1>',
            'class="access-status"',
            'te enviamos las instrucciones para restablecer tu contraseña. Revisa tu bandeja de entrada y la carpeta de spam.',
            '<a class="submit" href="/login/">Volver al inicio de sesión</a>',
            HEADLINE_RECUPERAR,
        ):
            self.assertContains(r, texto)
        self.assertNotContains(r, 'consola del servidor')
        self.assertEqual(len(mail.outbox), 1)
        url = re.search(r'http://testserver(/cuenta/restablecer/\S+)', mail.outbox[0].body).group(1)

        paso = self.client.get(url)  # Django guarda el token en sesión y redirige a /set-password/
        self.assertEqual(paso.status_code, 302)
        formulario = self.client.get(paso.url)
        for texto in (
            '<h1>Restablecer Contraseña</h1>',
            'Elige una contraseña nueva y segura para tu cuenta.',
            'name="new_password1"',
            'name="new_password2"',
            'Guardar contraseña</button>',
            'class="field-help"',
            'Volver al inicio de sesión',
        ):
            self.assertContains(formulario, texto)

        nueva = 'Otra-Clave-Segura-2027!'
        hecho = self.client.post(paso.url, {'new_password1': nueva, 'new_password2': nueva}, follow=True)
        self.assertEqual(hecho.redirect_chain[-1][0], '/cuenta/restablecida/')
        for texto in (
            '<h1>Contraseña Actualizada</h1>',
            'class="access-status"',
            'Tu nueva contraseña quedó guardada.',
            'Iniciar sesión',
        ):
            self.assertContains(hecho, texto)
        self.assertContains(hecho, 'class="submit" href="/login/"')
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(nueva))
        entra = Client().post('/login/', {'mode': 'login', 'username': self.user.email, 'password': nueva})
        self.assertRedirects(entra, '/', fetch_redirect_response=False)

    def test_enlace_invalido_muestra_enlace_no_disponible(self):
        r = self.client.get('/cuenta/restablecer/xx/token-falso/')
        self.assertContains(r, '<h1>Enlace No Disponible</h1>')
        self.assertContains(r, 'El enlace es inválido o venció.')
        self.assertContains(r, 'Volver al inicio de sesión')
        self.assertNotContains(r, '<form')

    def test_contrasenas_distintas_muestran_el_error_en_la_pantalla_de_restablecer(self):
        with override_settings(MAILERS=MAIL):
            self.client.post('/cuenta/recuperar/', {'email': self.user.email})
        url = re.search(r'http://testserver(/cuenta/restablecer/\S+)', mail.outbox[0].body).group(1)
        paso = self.client.get(url)
        r = self.client.post(paso.url, {'new_password1': 'Otra-Clave-Segura-2027!', 'new_password2': 'distinta'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'class="errorlist"')
        self.assertContains(r, '<h1>Restablecer Contraseña</h1>')

    def test_politica_de_privacidad_sigue_usando_la_tarjeta_simple(self):
        r = self.client.get(reverse('politica_privacidad'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'recovery-shell')
        self.assertContains(r, 'policy-content')
        self.assertContains(r, 'css/login.css?v=4')


class DisenoOficialCssTests(TestCase):
    """El CSS conserva los colores y las medidas tomados del diseño (marco de 1440 × 900)."""

    def test_paleta_del_diseno(self):
        raiz = css_rule(':root')
        for variable, valor in (
            ('--acc-bg', '#f7f6f2'),
            ('--acc-ink', '#1c241e'),
            ('--acc-muted', '#5c685e'),
            ('--acc-line', '#e1e8e3'),
            ('--acc-field', '#f9faf9'),
            ('--acc-orange', '#c26217'),
            ('--acc-green', '#2b4c37'),
        ):
            self.assertIn(f'{variable}: {valor};', raiz)
        self.assertIn('Outfit', css_rule(':root'))

    def test_tarjeta_partida_41_59_con_margen_del_diseno(self):
        self.assertIn('--acc-pad: 40px;', css_rule('.access-body'))
        self.assertIn('padding: var(--acc-pad);', css_rule('.access-body'))
        self.assertIn('background: var(--acc-bg);', css_rule('.access-body'))
        shell = css_rule('.access-shell')
        self.assertIn('minmax(0, 41fr) minmax(0, 59fr)', shell)
        self.assertIn('min-height: calc(100vh - 2 * var(--acc-pad));', shell)
        self.assertIn('border-radius: 8px;', shell)

    def test_panel_izquierdo(self):
        visual = css_rule('.access-visual')
        self.assertIn('padding: 64px;', visual)
        self.assertIn('justify-content: space-between;', visual)
        self.assertIn('font-size: 32px;', css_rule('.access-copy h2'))
        self.assertIn('width: 40px;', css_rule('.access-logo'))
        self.assertIn('background: var(--acc-orange);', css_rule('.access-logo'))

    def test_bloque_del_formulario_de_440px_arriba_y_centrado(self):
        self.assertIn('max-width: 440px;', css_rule('.access-content'))
        panel = css_rule('.access-panel')
        self.assertIn('align-items: center;', panel)
        self.assertIn('justify-content: center;', panel)  # centrado también en vertical
        self.assertIn('padding: 56px 64px;', panel)
        self.assertNotIn('padding-top: 100px', css_text())

    def test_controles_del_formulario(self):
        campo = css_rule('.field input,\n.field select')
        self.assertIn('height: 44px;', campo)
        self.assertIn('border: 1px solid var(--acc-line);', campo)
        self.assertIn('background: var(--acc-field);', campo)
        boton = css_rule('.submit')
        self.assertIn('height: 48px;', boton)
        self.assertIn('background: var(--acc-orange);', boton)
        self.assertIn('rgba(194, 98, 23', boton)
        self.assertIn('font-size: 34px;', css_rule('.access-panel h1'))
        self.assertIn('font-size: 14px;', css_rule('.lead'))
        self.assertIn('line-height: 22px;', css_rule('.lead'))
        estado = css_rule('.access-status')
        self.assertIn('width: 56px;', estado)
        self.assertIn('border-radius: 50%;', estado)
        self.assertIn('margin-top: 40px;', css_rule('.field + .submit'))  # 40 px entre el último campo y el botón
        self.assertIn('margin: 0 0 40px;', css_rule('.forgot'))
        self.assertIn('color: var(--acc-orange);', css_rule('.forgot'))
        self.assertIn('color: var(--acc-green);', css_rule('.switch a'))

    def test_responsive_oculta_el_titular_y_apila_en_tablet(self):
        css = css_text()
        self.assertIn('@media (max-width: 960px)', css)
        self.assertIn('--acc-pad: 16px;', css)
        self.assertIn('@media (max-width: 560px)', css)
        tablet = css[css.index('@media (max-width: 960px)') : css.index('@media (max-width: 560px)')]
        self.assertIn('grid-template-columns: minmax(0, 1fr);', tablet)
        self.assertIn('display: none;', tablet)
