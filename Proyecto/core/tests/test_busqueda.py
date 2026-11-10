"""Buscar: una sola búsqueda en el servidor (cultivos, semillas, insumos, problemas y predios propios)."""

import re
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Cultivo, Insumo, Potrero, Predio, ProblemaAgricola, TipoInsumo, Zona
from core.search import fold, highlight, run_search

User = get_user_model()
PASSWORD = 'Clave-segura-123'


class BusquedaBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('agricultora', email='a@example.com', password=PASSWORD)
        self.other = User.objects.create_user('vecino', email='v@example.com', password=PASSWORD)
        self.zone = Zona.objects.create(nombre='Melipilla prueba')
        self.maize = Cultivo.objects.create(nombre='Maíz', descripcion='Cereal de verano')
        self.wheat = Cultivo.objects.create(nombre='Trigo')
        self.tomato = Cultivo.objects.create(
            nombre='Tomate',
            densidad_siembra=Decimal('0.300'),
            unidad_siembra='kg',
            densidad_fuente='INIA prueba',
        )
        self.fert = TipoInsumo.objects.create(nombre='Fertilizante')
        self.seed_type = TipoInsumo.objects.create(nombre='Semilla')
        self.herb = TipoInsumo.objects.create(nombre='Herbicida')
        self.urea = Insumo.objects.create(
            tipo=self.fert,
            nombre='Urea granulada',
            descripcion='Fertilizante nitrogenado',
            dosis_hectarea=Decimal('100'),
            unidad_dosis='kg/ha',
            estado_verificacion='verified',
            dosis_verificada=True,
            dosis_fuente='INIA prueba',
            fuente_url='https://example.com/urea',
            fecha_verificacion=date(2026, 1, 1),
        )
        self.seed = Insumo.objects.create(tipo=self.seed_type, nombre='Semilla de maíz híbrido', cultivo=self.maize)
        self.weedkiller = Insumo.objects.create(tipo=self.herb, nombre='Herbicida para maíz', cultivo=self.maize)
        self.expired = Insumo.objects.create(tipo=self.fert, nombre='Abono caducado', vigente=False)
        self.weed = ProblemaAgricola.objects.create(nombre='Maleza de hoja ancha', tipo='weed', sintomas='Cubre el cultivo')
        self.weed.cultivos.add(self.maize)
        self.weedkiller.problemas.add(self.weed)
        self.lonely = ProblemaAgricola.objects.create(nombre='Pulgón del trigo', tipo='pest')
        self.lonely.cultivos.add(self.wheat)
        self.farm = Predio.objects.create(
            propietario=self.user, nombre='Fundo Las Acacias', ubicacion='Melipilla', superficie_ha=12, zona=self.zone
        )
        self.plot = Potrero.objects.create(predio=self.farm, nombre='Potrero Norte', superficie_ha=3, cultivo=self.maize)
        self.secret_farm = Predio.objects.create(
            propietario=self.other, nombre='Predio privado ajeno', ubicacion='Lejos', superficie_ha=5
        )
        Potrero.objects.create(predio=self.secret_farm, nombre='Lote secreto ajeno', superficie_ha=1)
        self.client.force_login(self.user)

    def get(self, query=''):
        response = self.client.get('/busqueda/' + query)
        self.assertEqual(response.status_code, 200, query)
        return response

    def visible(self, query=''):
        """HTML que ve la persona: sin los <script> con JSON que comparten todas las páginas."""
        html = self.get(query).content.decode()
        return re.sub(r'<script.*?</script>', '', html, flags=re.S)


class MotorDeBusquedaTests(BusquedaBase):
    def test_fold_ignores_accents_and_case_and_keeps_length(self):
        self.assertEqual(fold('MAÍZ Pimentón ñuble'), 'maiz pimenton nuble')
        self.assertEqual(len(fold('Cebolla de año')), len('Cebolla de año'))

    def test_highlight_marks_matches_without_breaking_html(self):
        html = str(highlight('Maíz <b>', ['mai']))
        self.assertIn('<mark>Maí</mark>z', html)
        self.assertIn('&lt;b&gt;', html)

    def test_partial_match_ignores_accents_and_uppercase(self):
        for term in ('mai', 'MAIZ', 'maíz', 'ma'):
            response = self.get(f'?q={term}&tab=cultivos')
            titles = [item['title'] for item in response.context['search_items']]
            self.assertIn('Maíz', titles, term)
        self.assertNotIn('Trigo', [i['title'] for i in self.get('?q=mai&tab=cultivos').context['search_items']])

    def test_multiple_words_must_all_match(self):
        titles = [i['title'] for i in self.get('?q=semilla+maiz&tab=semillas').context['search_items']]
        self.assertEqual(titles, ['Semilla de maíz híbrido'])
        self.assertEqual(self.get('?q=semilla+trigo&tab=semillas').context['search_total'], 0)

    def test_tab_counts_are_real_and_consistent_with_results(self):
        response = self.get('?q=maiz')
        counts = {tab['key']: tab['count'] for tab in response.context['search_tabs']}
        self.assertEqual(counts['cultivos'], 1)
        self.assertEqual(counts['semillas'], 1)
        self.assertEqual(counts['insumos'], 1)
        self.assertEqual(counts['problemas'], 1)  # la maleza que afecta al maíz
        self.assertGreaterEqual(counts['predios'], 1)  # el potrero con maíz
        for key in ('cultivos', 'semillas', 'insumos', 'predios'):
            tab_total = self.get(f'?q=maiz&tab={key}').context['search_total']
            self.assertEqual(tab_total, counts[key], key)
        sections_total = response.context['search_total']
        self.assertEqual(sections_total, counts['todos'])

    def test_expired_products_and_other_users_data_never_appear(self):
        self.assertNotIn('Abono caducado', self.visible('?q=caducado'))
        self.assertEqual(self.get('?q=caducado').context['search_total'], 0)
        for term in ('privado', 'secreto', 'ajeno', 'Lejos'):
            html = self.visible(f'?q={term}')
            self.assertNotIn('Predio privado ajeno', html, term)
            self.assertNotIn('Lote secreto ajeno', html, term)
            self.assertEqual(self.get(f'?q={term}').context['search_total'], 0, term)
        self.assertNotIn('Predio privado ajeno', self.visible('?tab=predios'))
        # El JSON que comparten todas las páginas tampoco trae predios de otras cuentas.
        full = self.get('?q=privado').content.decode()
        self.assertNotIn('Predio privado ajeno', full)
        self.assertNotIn('Lote secreto ajeno', full)

    def test_own_farms_and_plots_are_found(self):
        response = self.get('?q=acacias&tab=predios')
        # El predio primero; su potrero también aparece porque muestra a qué predio pertenece.
        self.assertEqual([i['kind'] for i in response.context['search_items']], ['predio', 'potrero'])
        item = response.context['search_items'][0]
        self.assertEqual(item['primary_action']['url'], f'/predios/{self.farm.pk}/')
        plot = self.get('?q=norte&tab=predios').context['search_items'][0]
        self.assertEqual(plot['kind'], 'potrero')

    def test_each_kind_has_its_own_contextual_action(self):
        crop = self.get('?q=maiz&tab=cultivos').context['search_items'][0]
        self.assertEqual(crop['primary_action']['label'], 'Ver calendario')
        self.assertIn(f'crop={self.maize.pk}', crop['primary_action']['url'])
        problem = self.get('?q=maleza&tab=problemas').context['search_items'][0]
        self.assertEqual(problem['primary_action']['label'], 'Ver productos')
        self.assertIn(f'problema={self.weed.pk}', problem['primary_action']['url'])
        product = self.get('?q=urea&tab=insumos').context['search_items'][0]
        self.assertEqual(product['primary_action']['label'], 'Ver ficha')
        self.assertEqual(product['primary_action']['url'], f'/productos/{self.urea.pk}/')
        calc = [a for a in product['menu_actions'] if a['label'] == 'Calcular cuánto llevar']
        self.assertEqual(len(calc), 1)  # la urea tiene dosis verificada: se puede calcular

    def test_problem_without_products_has_disabled_action_with_reason(self):
        problem = self.get('?q=pulgon&tab=problemas').context['search_items'][0]
        action = problem['primary_action']
        self.assertTrue(action['disabled'])
        self.assertTrue(action['reason'])
        self.assertEqual(action['url'], '')

    def test_unverified_product_does_not_offer_calculation(self):
        product = self.get('?q=herbicida&tab=insumos').context['search_items'][0]
        labels = [a['label'] for a in product['menu_actions']]
        self.assertNotIn('Calcular cuánto llevar', labels)
        disabled = [a for a in product['menu_actions'] if a['disabled']]
        self.assertEqual(len(disabled), 1)
        self.assertTrue(disabled[0]['reason'])

    def test_crop_with_documented_density_is_listed_once_in_all(self):
        response = self.get('?q=tomate')
        sections = {s['key']: s for s in response.context['search_sections']}
        self.assertEqual(sections['cultivos']['total'], 1)
        self.assertNotIn('semillas', sections)  # ya aparece como cultivo, sin duplicado
        seeds = self.get('?q=tomate&tab=semillas').context['search_items']
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]['primary_action']['label'], 'Calcular semillas')


class FiltrosYPaginacionTests(BusquedaBase):
    def test_filters_only_apply_to_their_tab(self):
        # «categoria» solo existe en Insumos: en Cultivos se ignora.
        response = self.get('?tab=cultivos&categoria=herbicide')
        self.assertEqual(response.context['search_total'], 3)
        self.assertEqual(self.get('?tab=insumos&categoria=herbicide').context['search_total'], 1)
        self.assertEqual(self.get('?tab=insumos&categoria=fertilizer').context['search_total'], 1)

    def test_filters_combine(self):
        response = self.get(f'?tab=insumos&cultivo={self.maize.pk}&categoria=herbicide')
        self.assertEqual([i['title'] for i in response.context['search_items']], ['Herbicida para maíz'])
        response = self.get(f'?tab=insumos&cultivo={self.maize.pk}&categoria=fertilizer')
        self.assertEqual(response.context['search_total'], 0)
        response = self.get(f'?tab=insumos&problema={self.weed.pk}')
        self.assertEqual([i['title'] for i in response.context['search_items']], ['Herbicida para maíz'])

    def test_only_calculable_filter(self):
        titles = [i['title'] for i in self.get('?tab=insumos&calculables=1').context['search_items']]
        self.assertEqual(titles, ['Urea granulada'])

    def test_crop_filter_in_all_limits_every_category(self):
        response = self.get(f'?q=&cultivo={self.maize.pk}')
        keys = {s['key'] for s in response.context['search_sections']}
        self.assertEqual(keys, {'cultivos', 'semillas', 'insumos', 'problemas', 'predios'})
        for section in response.context['search_sections']:
            for item in section['items']:
                self.assertIn(str(self.maize.pk), item['crop_ids'], item['title'])

    def test_problem_type_filter(self):
        titles = [i['title'] for i in self.get('?tab=problemas&tipo=pest').context['search_items']]
        self.assertEqual(titles, ['Pulgón del trigo'])

    def test_group_filter_for_crops(self):
        group = self.get('?tab=cultivos&grupo=cereales').context['search_items']
        self.assertEqual({i['title'] for i in group}, {'Maíz', 'Trigo'})

    def test_invalid_parameters_do_not_break_the_page(self):
        for query in ('?tab=inventado', '?pagina=abc', '?pagina=-4', '?orden=xx', '?cultivo=zzz', '?q=' + 'a' * 500):
            self.get(query)

    def test_pagination_keeps_search_parameters(self):
        for index in range(30):
            Cultivo.objects.create(nombre=f'Cultivo prueba {index:02d}')
        response = self.get('?tab=cultivos&q=prueba&orden=nombre')
        page = response.context['search_page']
        self.assertEqual(page.paginator.count, 30)
        self.assertEqual(len(response.context['search_items']), 12)
        html = response.content.decode()
        self.assertIn('q=prueba', html)
        self.assertIn('tab=cultivos', html)
        self.assertIn('orden=nombre', html)
        self.assertIn('pagina=2', html)
        second = self.get('?tab=cultivos&q=prueba&orden=nombre&pagina=3')
        self.assertEqual(len(second.context['search_items']), 6)
        self.assertEqual(len(self.get('?tab=cultivos&q=prueba&pagina=99').context['search_items']), 6)

    def test_order_by_name_and_by_relevance(self):
        by_name = [i['title'] for i in self.get('?q=maiz&tab=semillas&orden=nombre').context['search_items']]
        self.assertEqual(by_name, sorted(by_name, key=fold))
        relevant = self.get('?q=trigo&tab=problemas').context['search_items']
        self.assertEqual(relevant[0]['title'], 'Pulgón del trigo')


class ExploracionYVacioTests(BusquedaBase):
    def test_landing_has_no_long_list_and_uses_real_counts(self):
        response = self.get()
        self.assertEqual(response.context['search_mode'], 'explore')
        explore = response.context['search_explore']
        by_key = {c['key']: c for c in explore['categories']}
        self.assertEqual(by_key['cultivos']['count'], 3)
        self.assertEqual(by_key['insumos']['count'], 2)  # urea + herbicida vigentes; el caducado no cuenta
        self.assertEqual(by_key['semillas']['count'], 2)  # semilla del catálogo + tomate con densidad documentada
        self.assertEqual(by_key['predios']['count'], 1)
        self.assertEqual([c['name'] for c in explore['crops_in_use']], ['Maíz'])
        html = response.content.decode()
        self.assertNotIn('result-row', html)
        self.assertIn('id="search-examples"', html)
        for text in explore['examples']:
            self.assertTrue(
                text in (self.farm.nombre, 'Maíz', 'Urea granulada', 'Herbicida para maíz', 'Tomate', 'Trigo'), text
            )
        self.assertNotIn('gusano', html)

    def test_account_without_farms_gets_a_clear_next_step(self):
        self.client.force_login(self.other)
        Potrero.objects.filter(predio__propietario=self.other).delete()
        Predio.objects.filter(propietario=self.other).delete()
        explore = self.get().context['search_explore']
        farms = next(c for c in explore['categories'] if c['key'] == 'predios')
        self.assertEqual(farms['count'], 0)
        self.assertEqual(farms['cta'], 'Registrar predio')
        self.assertEqual(farms['url'], '/predios/nuevo/')
        self.assertContains(self.get('?tab=predios'), 'Aún no tienes predios registrados')

    def test_empty_results_explain_and_offer_a_way_out(self):
        response = self.get('?q=zzzz')
        self.assertContains(response, 'Sin resultados para «zzzz»')
        self.assertContains(response, 'Calcula a mano')
        filtered = self.get(f'?tab=insumos&categoria=fertilizer&cultivo={self.maize.pk}')
        self.assertContains(filtered, 'Ningún resultado con estos filtros')
        self.assertContains(filtered, 'Limpiar filtros')

    def test_clear_filters_keeps_query_and_tab(self):
        response = self.get(f'?q=urea&tab=insumos&cultivo={self.maize.pk}')
        self.assertEqual(response.context['search_clear_url'], '/busqueda/?q=urea&tab=insumos')

    def test_global_search_box_and_page_share_the_same_endpoint(self):
        html = self.visible('?q=maiz')
        self.assertEqual(html.count('action="/busqueda/"'), 2)  # barra superior y buscador de la página
        self.assertEqual(html.count('name="q"'), 2)
        self.assertEqual(html.count('value="maiz"'), 2)  # ambos muestran la misma consulta

    def test_anonymous_users_are_sent_to_login(self):
        self.client.logout()
        response = self.client.get('/busqueda/?q=maiz')
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])


class BusquedaToleranteTests(BusquedaBase):
    """Errores habituales de escritura: se busca la palabra más parecida y se avisa qué se buscó."""

    def search(self, **params):
        self.client.login(username='agricultora', password=PASSWORD)
        return self.client.get('/busqueda/', params)

    def test_letra_faltante_invertida_o_sobrante_encuentra_el_cultivo(self):
        for escrito in ('trgo', 'tirgo', 'triigo', 'trigp'):
            r = self.search(q=escrito)
            self.assertEqual(r.context['search_corrected'], 'trigo', escrito)
            titulos = [item['title'] for s in r.context['search_sections'] for item in s['items']]
            self.assertIn('Trigo', titulos, escrito)
            self.assertContains(r, f'No encontramos «{escrito}»: mostramos lo más parecido.')

    def test_error_en_palabra_larga_y_en_una_de_varias_palabras(self):
        r = self.search(q='uera granulda')
        self.assertEqual(r.context['search_corrected'], 'urea granulada')
        titulos = [item['title'] for s in r.context['search_sections'] for item in s['items']]
        self.assertIn('Urea granulada', titulos)
        r = self.search(q='fertlizante')
        self.assertIn(r.context['search_corrected'], ('fertilizante', 'fertilizantes'))
        self.assertGreater(r.context['search_total'], 0)

    def test_busqueda_correcta_no_se_corrige_ni_muestra_aviso(self):
        for escrito in ('trigo', 'mai', 'Urea'):
            r = self.search(q=escrito)
            self.assertEqual(r.context['search_corrected'], '', escrito)
            self.assertNotContains(r, 'mostramos lo más parecido')

    def test_palabras_cortas_o_sin_parecido_no_inventan_resultados(self):
        for escrito in ('zzzz', 'xq', 'tgo', 'qwertyuiop'):
            r = self.search(q=escrito)
            self.assertEqual(r.context['search_corrected'], '', escrito)
            self.assertEqual(r.context['search_total'], 0, escrito)
            self.assertContains(r, f'Sin resultados para «{escrito}»')

    def test_la_correccion_no_muestra_predios_de_otra_cuenta(self):
        predio = Predio.objects.create(propietario=self.other, nombre='Hacienda Quillay', ubicacion='Lejos', superficie_ha=5)
        r = self.search(q='quilay')
        self.assertEqual(r.context['search_corrected'], '')
        self.assertNotContains(r, predio.nombre)

    def test_correccion_sin_coincidencias_en_la_pestana_ofrece_quisiste_decir(self):
        r = self.search(q='trgo', tab='insumos')
        self.assertEqual(r.context['search_total'], 0)
        self.assertContains(r, '¿Quisiste decir')
        self.assertContains(r, 'href="/busqueda/?q=trigo"')  # lleva a todas las categorías, no a la misma pestaña vacía

    def test_distancia_de_edicion(self):
        from core.search import _distance, suggest_word

        self.assertEqual(_distance('trigo', 'trigo', 2), 0)
        self.assertEqual(_distance('tirgo', 'trigo', 2), 1)  # letras invertidas cuentan como un solo error
        self.assertEqual(_distance('trgo', 'trigo', 2), 1)
        self.assertGreater(_distance('papa', 'trigo', 2), 2)
        self.assertIsNone(suggest_word('pap', {'papa': 3}))  # menos de 4 letras: no se corrige
        self.assertEqual(suggest_word('tomatte', {'tomate': 2, 'tomillo': 1}), 'tomate')
