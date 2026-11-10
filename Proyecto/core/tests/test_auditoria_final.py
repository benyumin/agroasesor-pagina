"""Auditoría final: formato de cifras, indicadores del inicio sin duplicados y mensajes de la calculadora."""

import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from core.models import Cultivo, Potrero, Predio
from core.templatetags.agro_assets import cifra

User = get_user_model()
PASSWORD = 'Clave-segura-123'


class CifraTests(SimpleTestCase):
    def test_formato_chileno_sin_ceros_de_relleno(self):
        casos = {
            Decimal('12.5000'): '12,5',
            Decimal('5.0000'): '5',
            Decimal('32000.00'): '32.000',
            Decimal('8.2000'): '8,2',
            Decimal('1234567.891'): '1.234.567,89',
            0: '0',
            Decimal('-3.50'): '-3,5',
        }
        for valor, esperado in casos.items():
            self.assertEqual(cifra(valor), esperado, valor)

    def test_superficies_pequenas_no_se_redondean_a_cero(self):
        self.assertEqual(cifra(Decimal('0.0125')), '0,0125')
        self.assertEqual(cifra(Decimal('0.004')), '0,004')

    def test_decimales_configurables_y_valores_no_numericos(self):
        self.assertEqual(cifra(Decimal('32000.49'), 0), '32.000')
        self.assertEqual(cifra(None), '')
        self.assertEqual(cifra('sin dato'), 'sin dato')


class PantallasTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('agricultora', email='a@example.com', password=PASSWORD)
        self.crop = Cultivo.objects.create(nombre='Tomate')
        self.predio = Predio.objects.create(propietario=self.user, nombre='Fundo Uno', ubicacion='Melipilla', superficie_ha=Decimal('12.5'))
        self.potrero = Potrero.objects.create(predio=self.predio, nombre='Norte', superficie_ha=Decimal('3.2'), cultivo=self.crop)
        self.client.login(username='agricultora', password=PASSWORD)

    def test_ficha_del_predio_muestra_cifras_limpias(self):
        r = self.client.get(f'/predios/{self.predio.pk}/')
        self.assertContains(r, '12,5 ha')
        self.assertContains(r, '3,2 ha (32.000 m²)')
        for feo in ('12,5000', '3,2000', '32000,00'):
            self.assertNotContains(r, feo)

    def test_lista_de_predios_muestra_cifras_limpias(self):
        r = self.client.get('/predios/')
        self.assertNotContains(r, '3,2000')
        self.assertNotContains(r, '32000,00')

    def test_inicio_no_repite_las_superficies_en_los_indicadores(self):
        r = self.client.get('/')
        html = r.content.decode()
        indicadores = html[html.index('id="stats"') : html.index('class="dash-desk"')]
        self.assertEqual(indicadores.count('class="card dash-stat'), 3)
        for texto in ('Predios registrados', 'Potreros registrados', 'Cultivos activos'):
            self.assertIn(texto, indicadores)
        self.assertNotIn('Superficie', indicadores)
        self.assertContains(r, 'Distribución de superficie')  # las superficies siguen, una sola vez
        self.assertContains(r, 'Sin asignar')

    def test_calculadora_avisa_el_limite_de_superficie_sin_ceros_de_relleno(self):
        r = self.client.post(
            '/calculos/ejecutar/',
            data=json.dumps({'operation': 'dose', 'calculation_type': 'insumos', 'plot_id': self.potrero.pk, 'area': '99', 'dose': '1', 'unit': 'kg/ha'}),
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['error'], 'La superficie no puede superar 3,2 ha registradas.')

    def test_calculos_de_dosis_cobertura_y_semillas_dan_resultados_coherentes(self):
        def calcular(**datos):
            r = self.client.post('/calculos/ejecutar/', data=json.dumps({'plot_id': self.potrero.pk, **datos}), content_type='application/json')
            self.assertEqual(r.status_code, 200, r.content)
            return r.json()

        dosis = calcular(operation='dose', calculation_type='insumos', area='2.5', dose='120', unit='kg/ha', bag_kg='25', bag_price_clp='18000')
        self.assertEqual(dosis['amount'], 300)  # 2,5 ha × 120 kg/ha
        self.assertEqual(dosis['user_bag']['count'], 12)
        self.assertEqual(dosis['user_bag']['leftover'], 0)
        self.assertEqual(dosis['user_bag']['total_cost_clp'], 216000)

        cobertura = calcular(operation='coverage', calculation_type='coverage', area='3', dose='100', unit='kg/ha', stock='250')
        self.assertEqual(cobertura['needed'], 300)
        self.assertEqual(cobertura['coveredHa'], 2.5)
        self.assertEqual(cobertura['missingQty'], 50)
        self.assertAlmostEqual(cobertura['percent'], 83.33, places=2)

        semillas = calcular(operation='seed_population', calculation_type='semillas', area='2', plants_m2='300', thousand_seed_weight_g='45', germination_pct='90', purity_pct='98', source='Ficha técnica')
        self.assertAlmostEqual(semillas['rate'], 300 * 45 / (100 * 0.90 * 0.98), places=4)  # kg/ha
        self.assertAlmostEqual(semillas['amount'], semillas['rate'] * 2, places=4)
