"""Calculadora agrícola unificada: Semillas, Insumos y ¿Me alcanza? en un solo selector.

El rediseño es solo de presentación: se comprueba que el marcado conserva los contratos que usan los scripts,
que el servidor sigue calculando y guardando igual, y que no hay cambios de esquema pendientes.
"""

import json
import re
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from core.models import CalculoAgricola, Cultivo, Potrero, Predio

User = get_user_model()
PASSWORD = 'Clave-segura-123'
STATIC = Path(__file__).resolve().parent.parent / 'static'


def fuente(tipo, nombre):
    return (STATIC / tipo / nombre).read_text(encoding='utf-8').replace('\r\n', '\n')


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('agricultora', email='a@example.com', password=PASSWORD)
        self.crop = Cultivo.objects.create(nombre='Tomate')
        self.predio = Predio.objects.create(propietario=self.user, nombre='Fundo Uno', ubicacion='Melipilla', superficie_ha=Decimal('10'))
        self.plot = Potrero.objects.create(predio=self.predio, nombre='Norte', superficie_ha=Decimal('3'), cultivo=self.crop)
        self.client.login(username='agricultora', password=PASSWORD)

    def html(self, url='/decisiones/'):
        return self.client.get(url).content.decode()

    def pestanas(self, url):
        selector = self.html(url).split('id="calc-modes"', 1)[1].split('</div>', 1)[0]
        return re.findall(r'<button(.*?)</button>', selector, re.S)


class SelectorDeTresHerramientasTests(Base):
    def test_un_solo_selector_con_semillas_insumos_y_cobertura(self):
        pestanas = self.pestanas('/decisiones/')
        self.assertEqual(len(pestanas), 3)
        esperado = (
            ('Semillas', 'data-tool="llevar"', 'data-mode="semillas"', 'aria-controls="tool-llevar"'),
            ('Insumos', 'data-tool="llevar"', 'data-mode="insumos"', 'aria-controls="tool-llevar"'),
            ('¿Me alcanza?', 'data-tool="alcanza"', '', 'aria-controls="tool-alcanza"'),
        )
        for pestana, (nombre, herramienta, modo, panel) in zip(pestanas, esperado):
            self.assertIn(f'<strong>{nombre}</strong>', pestana)
            self.assertIn('role="tab"', pestana)
            for atributo in (herramienta, modo, panel):
                self.assertIn(atributo, pestana)
        self.assertNotIn('data-mode', pestanas[2])  # la cobertura no es un modo de «cuánto llevar»
        html = self.html()
        self.assertEqual(html.count('role="tablist"'), 1)
        self.assertNotIn('mode-cards', html)  # ya no hay un segundo selector dentro del formulario
        self.assertIn('Calculadora agrícola', html)

    def test_la_pestana_activa_depende_de_la_url(self):
        casos = {
            '/decisiones/': 0,
            '/decisiones/?herramienta=llevar&tipo=semillas': 0,
            '/decisiones/?tipo=insumos': 1,
            '/decisiones/?herramienta=llevar&tipo=insumos': 1,
            '/decisiones/?herramienta=alcanza': 2,
            '/decisiones/?herramienta=alcanza&tipo=insumos': 2,
        }
        for url, activa in casos.items():
            estados = ['class="is-active"' in p and 'aria-selected="true"' in p for p in self.pestanas(url)]
            self.assertEqual(estados, [i == activa for i in range(3)], url)

    def test_paneles_y_contratos_de_los_scripts_se_conservan(self):
        html = self.html()
        for marca in (
            'id="tool-llevar"', 'id="tool-alcanza"', 'id="calc-form"', 'id="cover-form"', 'data-flow="cover"',
            'id="calc-plot"', 'id="calc-crop"', 'id="calc-area"', 'id="calc-product"', 'id="calc-target"',
            'id="calc-dose"', 'id="calc-unit"', 'id="calc-submit"', 'id="calc-result"', 'id="calc-pdf"',
            'id="calc-go-reports"', 'id="calc-summary"', 'id="adv-plants"', 'id="adv-tsw"', 'id="adv-germ"',
            'id="adv-purity"', 'id="adv-source"', 'id="adv-nutrient-key"', 'id="adv-need"', 'id="adv-need-source"',
            'data-result-for="cover-form"', 'data-flow-submit', 'data-flow-summary', 'data-calculate-url="/calculos/ejecutar/"',
        ):
            self.assertIn(marca, html)
        for campo in ('plot', 'crop', 'product', 'target', 'stock', 'unit', 'dose', 'area'):
            self.assertRegex(html, rf'name="{campo}"')
        self.assertIn('no descuenta existencias ni registra inventario', html)

    def test_los_scripts_sincronizan_las_tres_pestanas(self):
        panel, centro = fuente('js', 'calc-panel.js'), fuente('js', 'decisiones.js')
        self.assertIn("document.querySelectorAll('#calc-modes [data-mode]')", panel)
        self.assertIn('window.AgroCalcTabs = { sync: syncToolTabs }', panel)
        self.assertIn("tab.dataset.tool === tool && (!tab.dataset.mode || tab.dataset.mode === mode)", panel)
        self.assertIn('window.AgroCalcTabs.sync()', centro)
        self.assertNotIn("setAttribute('aria-pressed', String(active))", panel.split('function setMode')[0][-900:])
        # El cálculo sigue en el servidor: el rediseño no añadió cálculos en la capa visual.
        for nombre in ('calc-flow.js', 'tool-flow.js'):
            self.assertNotIn('fetch(', fuente('js', nombre))

    def test_estilos_del_rediseno(self):
        css = re.sub(r'\s+', ' ', fuente('css', 'decisiones.css'))
        for regla in (
            '.tool-tabs-shell {',
            '.tool-tabs { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));',
            '.calc-step-body { display: grid; grid-template-columns: minmax(150px, 210px) minmax(0, 1fr);',
            '.decision-center .calc-side { position: sticky;',
            '@media (max-width: 640px)',
            '.cover-bar { height: 18px;',
        ):
            self.assertIn(regla, css)
        self.assertEqual(css.count('{'), css.count('}'))
        self.assertNotIn('.calc-modes button', css)  # se retiraron los estilos del selector antiguo


class ElServidorNoCambioTests(Base):
    def calcular(self, **datos):
        return self.client.post('/calculos/ejecutar/', data=json.dumps({'plot_id': self.plot.pk, **datos}), content_type='application/json')

    def test_las_tres_herramientas_guardan_en_calculo_agricola(self):
        casos = (
            ({'operation': 'dose', 'calculation_type': 'semillas', 'area': '2', 'dose': '25', 'unit': 'kg/ha'}, 'amount', 50),
            ({'operation': 'dose', 'calculation_type': 'insumos', 'area': '2', 'dose': '100', 'unit': 'kg/ha'}, 'amount', 200),
            ({'operation': 'coverage', 'calculation_type': 'coverage', 'area': '3', 'dose': '100', 'unit': 'kg/ha', 'stock': '250'}, 'needed', 300),
        )
        for i, (datos, clave, esperado) in enumerate(casos, 1):
            r = self.calcular(**datos)
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual(r.json()[clave], esperado)
            self.assertEqual(CalculoAgricola.objects.count(), i)
            guardado = CalculoAgricola.objects.latest('id')
            self.assertEqual((guardado.usuario, guardado.potrero, guardado.operacion), (self.user, self.plot, datos['operation']))
            self.assertTrue(guardado.entrada and guardado.resultado)
        self.assertEqual({f.name for f in CalculoAgricola._meta.fields}, {'id', 'usuario', 'potrero', 'operacion', 'entrada', 'resultado', 'creado'})

    def test_validaciones_del_servidor_siguen_activas(self):
        for datos in (
            {'operation': 'dose', 'calculation_type': 'semillas', 'area': '0', 'dose': '25', 'unit': 'kg/ha'},
            {'operation': 'dose', 'calculation_type': 'semillas', 'area': '-1', 'dose': '25', 'unit': 'kg/ha'},
            {'operation': 'dose', 'calculation_type': 'semillas', 'area': '99', 'dose': '25', 'unit': 'kg/ha'},
            {'operation': 'dose', 'calculation_type': 'insumos', 'area': '1', 'dose': '0', 'unit': 'kg/ha'},
            {'operation': 'coverage', 'calculation_type': 'coverage', 'area': '1', 'dose': '10', 'unit': 'kg/ha', 'stock': '-5'},
        ):
            self.assertEqual(self.calcular(**datos).status_code, 400, datos)
        self.assertEqual(CalculoAgricola.objects.count(), 0)

    def test_reportes_muestra_el_calculo_guardado(self):
        self.calcular(operation='dose', calculation_type='semillas', area='2', dose='25', unit='kg/ha')
        r = self.client.get('/reportes/?tipo=calculos')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Norte')

    def test_el_rediseno_no_deja_cambios_de_esquema(self):
        salida = StringIO()
        call_command('makemigrations', '--check', '--dry-run', stdout=salida)  # falla si hubiera migraciones pendientes
        self.assertIn('No changes detected', salida.getvalue())
