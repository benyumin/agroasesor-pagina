/* «¿Me alcanza?»: capa de presentación guiada (paso a paso), igual que «Cuánto llevar».
 *
 * No calcula nada ni llama al servidor: el motor de cada formulario sigue en decisiones.js (que valida con el
 * servidor). Aquí solo se leen los <select> e <input> reales, se marcan los pasos completados, se bloquea lo que aún
 * no se puede usar, se resume la selección y se muestra qué falta antes de calcular. Los <select> originales de
 * potrero y cultivo siguen siendo la fuente de verdad: si este archivo no carga, el formulario funciona igual.
 */
(function () {
  'use strict';

  const root = document.querySelector('.decision-center');
  if (!root) return;

  function readJson(id, fallback) {
    const node = document.getElementById(id);
    if (!node) return fallback;
    try {
      return JSON.parse(node.textContent) || fallback;
    } catch (error) {
      return fallback;
    }
  }
  function h(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value === false || value === null || value === undefined) return;
      if (key === 'class') node.className = value;
      else node.setAttribute(key, value === true ? '' : value);
    });
    kids.flat(Infinity).forEach((kid) => {
      if (kid === null || kid === undefined || kid === false) return;
      node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    });
    return node;
  }
  const fmt = (value, digits) => {
    const number = Number(value);
    return Number.isFinite(number)
      ? number.toLocaleString('es-CL', { maximumFractionDigits: digits === undefined ? 2 : digits })
      : '—';
  };
  const norm = (text) =>
    String(text || '')
      .normalize('NFD')
      .replace(/[̀-ͯ]/g, '')
      .toLowerCase()
      .trim();

  const farm = readJson('agro-farms', { predios: [], plots: [] });
  const catalog = readJson('agro-catalog', {});
  const hints = readJson('agro-crop-picker', {});
  const plots = farm.plots || [];
  const predios = farm.predios || [];
  const crops = (catalog.cultivos || []).filter((crop) => crop.active !== false);
  const allProducts = catalog.insumos || [];
  const groupShort = new Map((hints.groups || []).map((group) => [group.name, group.short || group.name]));
  const groupOrder = (hints.groups || []).map((group) => group.name);

  const plotById = (id) => plots.find((plot) => String(plot.id) === String(id)) || null;
  const cropById = (id) => crops.find((crop) => String(crop.id) === String(id)) || null;
  const productById = (id) => allProducts.find((item) => String(item.id) === String(id)) || null;
  const shortGroup = (crop) => groupShort.get(crop.group) || crop.group || '';
  function emoji(crop) {
    return window.AgroCalculator ? window.AgroCalculator.cropEmoji(crop) : '🌱';
  }
  function plotIcon(plot) {
    const crop = plot.cropId ? cropById(plot.cropId) : null;
    return crop ? emoji(crop) : '🟩';
  }
  const plotMeta = (plot) => plot.farmName + ' · ' + fmt(plot.areaHa, 2) + ' ha';

  function fireChange(select, value) {
    select.value = String(value);
    select.dispatchEvent(new Event('change', { bubbles: true }));
  }
  function emptyCard(icon, title, text, actions) {
    return h(
      'div',
      { class: 'calc-empty-card' },
      h('span', { class: 'calc-empty-ico', 'aria-hidden': 'true' }, icon),
      h('strong', null, title),
      h('p', null, text),
      actions && actions.length ? h('div', { class: 'calc-empty-actions' }, actions) : null,
    );
  }

  // ---------- Selector visual genérico (tarjetas con buscador y filtros) ----------
  // cfg: host, step, items(), selected(), pick(id), chosen(item) -> {icon, title, sub}, option(item) -> {icon, title, subs},
  //      text(item), group(item), frequent(), frequentLabel, empty(), label, searchFrom, canChange(), onChange()
  function createPicker(cfg) {
    const view = { choosing: false, query: '', filter: 'all' };
    let signature = '';

    function render(force) {
      const items = cfg.items();
      const selected = cfg.selected();
      const canChange = cfg.canChange ? cfg.canChange(items) : items.length > 1;
      const choosing = !selected || (view.choosing && canChange);
      const sig = [
        items.map((item) => item.id).join(','),
        selected ? selected.id : '',
        choosing,
        view.query,
        view.filter,
      ].join('|');
      if (!force && sig === signature) return;
      signature = sig;
      if (cfg.step) {
        cfg.step.classList.add('is-enhanced');
        cfg.step.classList.toggle('is-empty', !items.length);
      }
      cfg.host.hidden = false;
      cfg.host.replaceChildren();

      if (!items.length) {
        cfg.host.append(cfg.empty());
        return;
      }

      if (selected && !choosing) {
        const info = cfg.chosen(selected);
        cfg.host.append(
          h(
            'div',
            { class: 'calc-chosen' },
            h('span', { class: 'calc-chosen-ico', 'aria-hidden': 'true' }, info.icon),
            h('div', { class: 'calc-chosen-copy' }, h('strong', null, info.title), h('small', null, info.sub)),
            canChange
              ? h(
                  'button',
                  { class: 'btn-ghost-agro btn-sm', type: 'button', 'data-change': '1' },
                  cfg.changeLabel || 'Cambiar',
                )
              : null,
          ),
        );
        return;
      }

      const grid = h('div', { class: 'calc-opts', role: 'group', 'aria-label': cfg.label || 'Opciones' });
      const fill = () => {
        const query = norm(view.query);
        grid.replaceChildren();
        const list = items.filter(
          (item) =>
            (view.filter === 'all' || !cfg.group || cfg.group(item) === view.filter) &&
            (!query || norm(cfg.text(item)).includes(query)),
        );
        list.forEach((item) => {
          const info = cfg.option(item);
          grid.append(
            h(
              'button',
              {
                class: 'calc-opt',
                type: 'button',
                'data-pick': item.id,
                'aria-pressed': String(Boolean(selected) && String(item.id) === String(selected.id)),
              },
              h('span', { class: 'calc-opt-ico', 'aria-hidden': 'true' }, info.icon),
              h(
                'span',
                { class: 'calc-opt-copy' },
                h('strong', null, info.title),
                (info.subs || []).filter(Boolean).map((sub) => h('small', null, sub)),
              ),
            ),
          );
        });
        if (!list.length) grid.append(h('p', { class: 'calc-opts-empty' }, 'Nada coincide con la búsqueda.'));
      };

      if (items.length > (cfg.searchFrom === undefined ? 6 : cfg.searchFrom)) {
        const tools = h('div', { class: 'calc-picker-tools' });
        const search = h('input', {
          class: 'calc-search',
          type: 'search',
          placeholder: cfg.searchPlaceholder || 'Buscar…',
          'aria-label': cfg.searchPlaceholder || 'Buscar',
          value: view.query,
        });
        search.addEventListener('input', () => {
          view.query = search.value;
          fill();
        });
        tools.append(search);
        const groups = cfg.group ? Array.from(new Set(items.map(cfg.group))).filter(Boolean) : [];
        if (groups.length > 1) {
          const filters = h('div', { class: 'calc-filters', role: 'group', 'aria-label': 'Filtrar por categoría' });
          ['all'].concat(groups).forEach((name) =>
            filters.append(
              h(
                'button',
                {
                  class: 'calc-chip' + (view.filter === name ? ' is-active' : ''),
                  type: 'button',
                  'data-filter': name,
                  'aria-pressed': String(view.filter === name),
                },
                name === 'all' ? 'Todos' : name,
              ),
            ),
          );
          filters.addEventListener('click', (event) => {
            const chip = event.target.closest('[data-filter]');
            if (!chip) return;
            view.filter = chip.dataset.filter;
            filters.querySelectorAll('[data-filter]').forEach((node) => {
              const active = node.dataset.filter === view.filter;
              node.classList.toggle('is-active', active);
              node.setAttribute('aria-pressed', String(active));
            });
            fill();
          });
          tools.append(filters);
        }
        cfg.host.append(tools);
        const frequent = cfg.frequent ? cfg.frequent(items) : [];
        if (frequent.length) {
          cfg.host.append(h('p', { class: 'calc-freq-label' }, cfg.frequentLabel || 'Sugeridos'));
          cfg.host.append(
            h(
              'div',
              { class: 'calc-freq' },
              frequent.map((item) =>
                h(
                  'button',
                  { class: 'calc-chip', type: 'button', 'data-pick': item.id },
                  cfg.option(item).icon + ' ' + cfg.option(item).title,
                ),
              ),
            ),
          );
        }
      }
      fill();
      cfg.host.append(grid);
    }

    cfg.host.addEventListener('click', (event) => {
      if (event.target.closest('[data-change]')) {
        if (cfg.onChange) {
          cfg.onChange();
          return;
        }
        view.choosing = true;
        view.query = '';
        view.filter = 'all';
        render(true);
        return;
      }
      const option = event.target.closest('[data-pick]');
      if (!option) return;
      view.choosing = false;
      view.query = '';
      view.filter = 'all';
      cfg.pick(option.dataset.pick);
      render(true);
    });
    return { render, view };
  }

  // Cultivos ordenados por categoría y nombre (mismo orden que «Cuánto llevar»).
  function sortCrops(list) {
    return list.slice().sort((a, b) => {
      const byGroup = (groupOrder.indexOf(a.group) + 1 || 99) - (groupOrder.indexOf(b.group) + 1 || 99);
      return byGroup || a.name.localeCompare(b.name, 'es');
    });
  }

  // Configuración base del selector de cultivo.
  function cropPickerConfig(base) {
    return Object.assign(
      {
        searchFrom: 8,
        searchPlaceholder: 'Buscar cultivo…',
        label: 'Cultivos del catálogo',
        changeLabel: 'Cambiar cultivo',
        canChange: () => true,
        text: (crop) => crop.name + ' ' + crop.group,
        group: shortGroup,
        frequent: (items) =>
          (hints.frequent || []).map((id) => items.find((crop) => String(crop.id) === String(id))).filter(Boolean),
        frequentLabel: hints.frequentBasis === 'used' ? 'Frecuentes en tus potreros' : 'Sugeridos',
        chosen: (crop) => ({
          icon: emoji(crop),
          title: crop.name,
          sub: [
            shortGroup(crop),
            crop.density !== null && crop.density !== undefined
              ? 'Densidad ' + fmt(crop.density) + ' ' + (crop.unit || 'kg') + '/ha'
              : 'Sin densidad documentada',
          ]
            .filter(Boolean)
            .join(' · '),
        }),
        option: (crop) => ({ icon: emoji(crop), title: crop.name, subs: [shortGroup(crop)] }),
        empty: () =>
          emptyCard(
            '🌱',
            'No hay cultivos activos en el catálogo',
            'Sin cultivos no se puede continuar, y AgroAsesor no inventa datos. ' +
              (root.dataset.staff === '1'
                ? 'Regístralos desde administración.'
                : 'Pide al administrador que los cargue.'),
          ),
      },
      base,
    );
  }

  // ---------- Resumen del potrero y del cultivo (columna derecha) ----------
  function plotCard(plot) {
    const rows = [
      ['Predio', plot.farmName],
      ['Superficie', fmt(plot.areaHa, 3) + ' ha'],
      ['Cultivo actual', plot.crop ? plot.crop + (plot.variety ? ' · ' + plot.variety : '') : 'Sin cultivo'],
      ['Estado', plot.status || ''],
      ['Temporada', plot.season || ''],
    ].filter((row) => row[1]);
    let next = null;
    if (!plot.crop) {
      next = h(
        'p',
        { class: 'calc-mini-next' },
        h('strong', null, 'Siguiente paso: '),
        'este potrero no tiene cultivo. Asígnalo para planificar la siembra; mientras tanto puedes simular con otro cultivo. ',
        plot.editUrl ? h('a', { href: plot.editUrl }, 'Asignar cultivo') : null,
      );
    } else if (!plot.sowing) {
      next = h(
        'p',
        { class: 'calc-mini-next' },
        h('strong', null, 'Siguiente paso: '),
        'registra la fecha de siembra de este potrero. ',
        plot.editUrl ? h('a', { href: plot.editUrl }, 'Definir siembra') : null,
      );
    }
    return h(
      'div',
      { class: 'calc-mini' },
      h('p', { class: 'calc-mini-kicker' }, 'Potrero'),
      h('div', { class: 'calc-mini-head' }, h('h3', null, plot.name)),
      h(
        'dl',
        null,
        rows.map((row) => [h('dt', null, row[0]), h('dd', null, row[1])]),
      ),
      next,
    );
  }
  function metric(label, value) {
    return h('div', { class: 'metric' }, h('span', null, label), h('strong', null, value));
  }
  function cropCard(crop) {
    const hasDensity = crop.density !== null && crop.density !== undefined;
    const hasYield = crop.yield !== null && crop.yield !== undefined;
    const sources = [];
    if (hasDensity)
      sources.push(crop.densitySource ? 'Densidad: ' + crop.densitySource : 'Densidad sin fuente registrada');
    if (hasYield)
      sources.push(crop.yieldSource ? 'Rendimiento: ' + crop.yieldSource : 'Rendimiento sin fuente registrada');
    return h(
      'div',
      { class: 'calc-mini' },
      h(
        'div',
        { class: 'calc-crop-head' },
        h('span', { class: 'calc-crop-emoji', 'aria-hidden': 'true' }, emoji(crop)),
        h('div', null, h('h3', null, crop.name), crop.group ? h('span', { class: 'badge' }, crop.group) : null),
      ),
      crop.description ? h('p', { class: 'calc-crop-desc' }, crop.description) : null,
      h(
        'div',
        { class: 'metric-grid calc-metrics' },
        metric('Unidad base', crop.unit || 'kg'),
        metric('Densidad de siembra', hasDensity ? fmt(crop.density) + ' ' + (crop.unit || 'kg') + '/ha' : 'Sin dato'),
        metric('Rendimiento ref.', hasYield ? fmt(crop.yield, 1) + ' t/ha' : '—'),
      ),
      h(
        'p',
        { class: 'calc-source-note' },
        h('strong', null, 'Fuente de datos: '),
        sources.length ? sources.join(' · ') + '.' : 'el catálogo no tiene datos de siembra de este cultivo.',
      ),
    );
  }
  function productCard(item) {
    if (!item) return null;
    const dose =
      item.dose !== null && item.dose !== undefined
        ? fmt(item.dose, 3) + ' ' + (item.unit || '') + (item.doseVerified ? ' (verificada)' : ' (sin verificar)')
        : 'No documentada';
    return h(
      'div',
      { class: 'calc-mini' },
      h('p', { class: 'calc-mini-kicker' }, 'Producto'),
      h('div', { class: 'calc-mini-head' }, h('h3', null, item.name)),
      h(
        'dl',
        null,
        h('dt', null, 'Categoría'),
        h('dd', null, item.categoryLabel || '—'),
        h('dt', null, 'Dosis'),
        h('dd', null, dose),
        h('dt', null, 'Fuente'),
        h('dd', null, item.sourceName || 'Sin fuente registrada'),
      ),
    );
  }

  // ---------- Lista de lo que falta (estado vacío del resultado) ----------
  function checklist(items) {
    return h(
      'ul',
      { class: 'calc-checklist', 'aria-label': 'Qué falta para continuar' },
      items.map((item) =>
        h(
          'li',
          { class: item.ok ? 'is-ok' : 'is-todo' },
          h('span', null, item.label),
          item.ok ? null : h('small', null, item.hint),
        ),
      ),
    );
  }
  function pendingResult(panel, spec) {
    panel.replaceChildren(
      h(
        'div',
        { class: 'calc-result-top' },
        h('p', { class: 'result-kicker' }, spec.kicker || 'Resultado'),
        h('span', { class: 'badge amber' }, 'Pendiente'),
      ),
      h('p', { class: 'result-value is-empty', 'aria-hidden': 'true' }, h('strong', null, '—')),
      h('h2', { class: 'calc-empty-title' }, spec.title),
      h('p', { class: 'muted calc-empty-text' }, spec.text),
      spec.items && !spec.ready ? checklist(spec.items) : null,
    );
  }

  // ---------- Formulario guiado de «¿Me alcanza?» ----------
  const FLOWS = {
    'cover-form': {
      tool: 'alcanza',
      verb: 'comprobar',
      action: 'Comprobar disponibilidad',
      resultName: 'la disponibilidad',
      steps: ['plot', 'crop', 'product', 'stock', 'dose', 'go'],
      stepItems: { plot: ['plot'], crop: ['crop'], product: ['product'], stock: ['stock'], dose: ['dose', 'area'] },
      items(ctx) {
        const { f, plot, crop, form } = ctx;
        const targetField = form.querySelector('[data-role=target-field]');
        const needsTarget = Boolean(targetField && !targetField.hidden);
        const stock = f.stock.value;
        return [
          ctx.plotItem,
          ctx.cropItem,
          {
            key: 'product',
            label: 'Producto',
            ok: f.product.value !== '' && (!needsTarget || f.target.value !== ''),
            hint: !crop
              ? 'Primero elige un cultivo'
              : f.product.value === ''
                ? 'Elige un producto o «no registrado»'
                : 'Selecciona el objetivo de control',
          },
          {
            key: 'stock',
            label: 'Cantidad disponible',
            ok: stock !== '' && Number(stock) >= 0,
            hint: 'Indica cuánto producto tienes (0 o más)',
          },
          {
            key: 'dose',
            label: 'Dosis o requerimiento',
            ok: Number(f.dose.value) > 0,
            hint: 'Ingresa una dosis mayor que 0',
          },
          ctx.areaItem,
        ];
      },
      pendingText: 'En cuanto lo completes, aquí verás si te alcanza el producto y qué superficie alcanzas a cubrir.',
      readyText: 'Pulsa «Comprobar disponibilidad» para ver si te alcanza.',
      extraSummary(ctx) {
        return productCard(productById(ctx.f.product.value));
      },
    },
  };

  function mountFlow(formId, spec) {
    const form = document.getElementById(formId);
    if (!form) return;
    const panel = form.closest('.tool-flow');
    const f = form.elements;
    const steps = {};
    form.querySelectorAll('.calc-step[data-step]').forEach((node) => {
      steps[node.dataset.step] = node;
    });
    const status = panel.querySelector('[data-flow-status]');
    const hint = form.querySelector('[data-flow-hint]');
    const submit = form.querySelector('[data-flow-submit]');
    const result = panel.querySelector('[data-result-for="' + formId + '"]');
    const summary = panel.querySelector('[data-flow-summary]');
    const summaryBody = panel.querySelector('[data-flow-summary-body]');
    const areaTools = form.querySelector('[data-flow-area-tools]');
    const plotHost = form.querySelector('[data-picker="plot"]');
    const cropHost = form.querySelector('[data-picker="crop"]');

    const plotSelect = f.plot;
    const cropSelect = f.crop;
    const currentPlot = () => plotById(plotSelect.value);
    const currentCrop = () => cropById(cropSelect.value);

    const plotPicker = createPicker({
      host: plotHost,
      step: steps.plot,
      label: 'Potreros',
      searchFrom: 6,
      searchPlaceholder: 'Buscar potrero o predio…',
      items: () => plots,
      selected: currentPlot,
      pick: (id) => fireChange(plotSelect, id),
      text: (plot) => plot.name + ' ' + plot.farmName + ' ' + plot.crop,
      chosen: (plot) => ({ icon: plotIcon(plot), title: plot.name, sub: plotMeta(plot) }),
      option: (plot) => ({
        icon: plotIcon(plot),
        title: plot.name,
        subs: [plotMeta(plot), plot.crop ? 'Cultivo: ' + plot.crop : 'Sin cultivo'],
      }),
      changeLabel: 'Cambiar potrero',
      empty: () => {
        const withSetup = predios.find((item) => item.setupUrl);
        if (withSetup) {
          return emptyCard(
            '🗺️',
            'Aún no tienes potreros',
            'La comprobación se hace por potrero.',
            [h('a', { class: 'btn-primary-agro btn-sm', href: withSetup.setupUrl }, 'Definir potreros')],
          );
        }
        return emptyCard(
          '🗺️',
          'Aún no tienes predios',
          'Regístralo para empezar a comprobar.',
          [
            h(
              'a',
              { class: 'btn-primary-agro btn-sm', href: plotHost.dataset.farmCreateUrl || '/predios/nuevo/' },
              'Registrar predio',
            ),
          ],
        );
      },
    });
    const availableCrops = () => {
      const present = new Set(
        Array.from(cropSelect.options)
          .filter((option) => option.value && !option.disabled)
          .map((option) => option.value),
      );
      return crops.filter((crop) => present.has(String(crop.id)));
    };
    const cropCfg = cropPickerConfig({
      host: cropHost,
      step: steps.crop,
      items: () => sortCrops(availableCrops()),
      selected: currentCrop,
      pick: (id) => fireChange(cropSelect, id),
    });
    const cropPicker = createPicker(cropCfg);

    function context() {
      const plot = currentPlot();
      const crop = currentCrop();
      const area = Number(String(f.area.value).replace(',', '.'));
      const areaOk = Boolean(plot) && Number.isFinite(area) && area > 0 && area <= plot.areaHa + 1e-9;
      return {
        form,
        f,
        plot,
        crop,
        plotItem: {
          key: 'plot',
          label: 'Potrero',
          ok: Boolean(plot),
          hint: plots.length ? 'Selecciona un potrero' : 'Registra un potrero en Mis predios y potreros',
        },
        cropItem: {
          key: 'crop',
          label: 'Cultivo',
          ok: Boolean(crop),
          hint: crops.length ? 'Selecciona un cultivo' : 'El catálogo no tiene cultivos activos',
        },
        areaItem: {
          key: 'area',
          label: 'Superficie',
          ok: areaOk,
          hint: !plot
            ? 'Primero elige un potrero'
            : !(area > 0)
              ? 'Ingresa una superficie mayor que 0'
              : 'No puede superar ' + fmt(plot.areaHa, 3) + ' ha',
        },
      };
    }

    function stateFor(items) {
      if (!plots.length) return ['blocked', 'Necesitas un potrero'];
      if (!crops.length) return ['blocked', 'El catálogo no tiene cultivos'];
      if (result.dataset.state === 'done')
        return result.dataset.simulation === '1' ? ['sim', 'Simulación guardada'] : ['done', 'Resultado guardado'];
      const missing = items.filter((item) => !item.ok);
      if (!missing.length) return ['ready', 'Listo para ' + spec.verb];
      return [
        'pending',
        missing.length === 1 ? 'Falta: ' + missing[0].label.toLowerCase() : 'Faltan ' + missing.length + ' datos',
      ];
    }

    function refresh() {
      const ctx = context();
      const items = spec.items(ctx);
      const ok = {};
      items.forEach((item) => {
        ok[item.key] = item.ok;
      });
      const done = {};
      spec.steps.forEach((key) => {
        if (spec.stepItems[key]) done[key] = spec.stepItems[key].every((k) => ok[k]);
      });
      done.go = result.dataset.state === 'done';
      const current = spec.steps.find((key) => key !== 'go' && !done[key]) || 'go';
      // Sin potrero no se elige cultivo; sin cultivo no tiene sentido pedir lo demás (los productos dependen del cultivo).
      let lockFrom = spec.steps.length;
      if (!ok.plot) lockFrom = 1;
      else if (!ok.crop) lockFrom = 2;
      spec.steps.forEach((key, index) => {
        const node = steps[key];
        if (!node) return;
        const locked = index >= lockFrom;
        node.classList.toggle('is-done', Boolean(done[key]) && !locked);
        node.classList.toggle('is-current', key === current && !locked);
        node.classList.toggle('is-locked', locked);
        const body = node.querySelector('.calc-step-body');
        if (body) {
          body.toggleAttribute('inert', locked);
          if (locked) body.setAttribute('aria-disabled', 'true');
          else body.removeAttribute('aria-disabled');
        }
      });

      plotPicker.render();
      cropPicker.render();

      const ready = items.every((item) => item.ok) && plots.length > 0 && crops.length > 0;
      submit.disabled = !ready;
      const [kind, text] = stateFor(items);
      status.dataset.state = kind;
      status.querySelector('.calc-status-text').textContent = text;
      const missing = items.filter((item) => !item.ok);
      if (!plots.length) hint.textContent = 'Necesitas al menos un potrero registrado para continuar.';
      else if (!crops.length) hint.textContent = 'Sin cultivos activos en el catálogo no se puede continuar.';
      else if (result.dataset.state === 'done')
        hint.textContent = 'Resultado guardado. Puedes ajustar los datos y volver a ' + spec.verb + '.';
      else if (ready)
        hint.textContent =
          'Todo listo. «' + spec.action + '» valida el cálculo con el catálogo y lo guarda en tu cuenta.';
      else hint.textContent = 'Te falta: ' + missing.map((item) => item.label.toLowerCase()).join(', ') + '.';

      // Superficie: atajo para usar todo el potrero.
      if (areaTools) {
        areaTools.replaceChildren();
        const plot = ctx.plot;
        const area = Number(String(f.area.value).replace(',', '.'));
        if (plot && (!Number.isFinite(area) || Math.abs(area - plot.areaHa) > 1e-6)) {
          areaTools.append(
            h(
              'button',
              { class: 'calc-chip', type: 'button', 'data-use-all': '1' },
              'Usar todo el potrero (' + fmt(plot.areaHa, 2) + ' ha)',
            ),
          );
        }
      }

      // Resumen de la selección.
      summaryBody.replaceChildren();
      if (ctx.plot) summaryBody.append(plotCard(ctx.plot));
      if (ctx.crop) summaryBody.append(cropCard(ctx.crop));
      const extra = spec.extraSummary(ctx);
      if (extra) summaryBody.append(extra);
      summary.hidden = !ctx.plot && !ctx.crop;

      // Resultado: mientras no haya uno vigente se muestra qué falta.
      if (result.dataset.state !== 'done' && result.dataset.state !== 'stale') {
        let title;
        let body;
        if (!plots.length) {
          title = 'Necesitas un potrero';
          body = 'Registra tu predio y define sus potreros en «Mis predios y potreros» para continuar.';
        } else if (!crops.length) {
          title = 'El catálogo no tiene cultivos';
          body = 'Sin cultivos activos no se puede calcular y AgroAsesor no inventa datos.';
        } else if (ready) {
          title = 'Todo listo';
          body = spec.readyText;
        } else {
          title = missing.length === 1 ? 'Aún falta un dato' : 'Aún faltan ' + missing.length + ' datos';
          body = spec.pendingText;
        }
        pendingResult(result, { title, text: body, items, ready });
      }
    }

    // Un cambio en el formulario deja el resultado anterior como «desactualizado» (sin borrarlo).
    function markStale() {
      if (result.dataset.state !== 'done') return;
      result.dataset.state = 'stale';
      result.classList.add('is-stale');
      result.prepend(
        h('p', { class: 'flow-stale' }, 'Cambiaste datos: vuelve a ' + spec.verb + ' para actualizar este resultado.'),
      );
    }
    form.addEventListener('input', (event) => {
      if (event.target.matches('input, select')) markStale();
      refresh();
    });
    form.addEventListener('change', (event) => {
      if (event.target.matches('input, select')) markStale();
      refresh();
    });
    form.addEventListener('flow:refresh', refresh);
    form.addEventListener('flow:result', (event) => {
      result.classList.remove('is-stale');
      result.dataset.state = 'done';
      result.dataset.simulation = event.detail && event.detail.simulation ? '1' : '';
      refresh();
    });
    if (areaTools) {
      areaTools.addEventListener('click', (event) => {
        if (!event.target.closest('[data-use-all]')) return;
        const plot = currentPlot();
        if (!plot) return;
        f.area.value = Number(plot.areaHa.toFixed(4));
        f.area.dispatchEvent(new Event('input', { bubbles: true }));
      });
    }
    refresh();
  }

  function mountAll() {
    Object.keys(FLOWS).forEach((id) => mountFlow(id, FLOWS[id]));
    // El aviso «Continuar como simulación» lleva a «Cuánto llevar» (ya existente); no se toca aquí.
  }

  // decisiones.js (se carga después) rellena los <select> al ejecutarse; los formularios se montan al terminar la carga.
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mountAll);
  else mountAll();
})();
