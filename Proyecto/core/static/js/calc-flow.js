/* «Cuánto llevar»: capa de presentación guiada (paso a paso).
 *
 * No calcula nada ni guarda nada: usa el motor de calc-panel.js (window.AgroCalculator) y los datos reales
 * del servidor (#agro-farms y #agro-catalog). Los <select> originales de potrero y cultivo siguen siendo la
 * fuente de verdad del formulario: aquí solo se presentan como tarjetas seleccionables, se marcan los pasos
 * completados, se bloquea lo que aún no se puede usar y se resume la selección en la columna derecha.
 * Si este archivo no carga, el formulario sigue funcionando con los selects normales.
 */
(function () {
  'use strict';

  const root = document.getElementById('tool-llevar');
  const calc = window.AgroCalculator;
  if (!root || !calc || !calc.state) return;

  const $ = (id) => document.getElementById(id);
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
      if (value === false || value == null) return;
      if (key === 'class') node.className = value;
      else node.setAttribute(key, value === true ? '' : value);
    });
    kids.flat(Infinity).forEach((kid) => {
      if (kid === null || kid === undefined || kid === false) return;
      node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    });
    return node;
  }
  const fmt = (value, digits) =>
    Number(value).toLocaleString('es-CL', { maximumFractionDigits: digits === undefined ? 2 : digits });
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
  const groupShort = new Map((hints.groups || []).map((group) => [group.name, group.short || group.name]));
  const groupOrder = (hints.groups || []).map((group) => group.name);

  const plotSelect = $('calc-plot');
  const cropSelect = $('calc-crop');
  const plotHost = $('calc-plot-picker');
  const cropHost = $('calc-crop-picker');
  const submit = $('calc-submit');
  const pdf = $('calc-pdf');
  const reportsLink = $('calc-go-reports');
  const coverLink = $('calc-go-cover');
  const summary = $('calc-summary');
  const notes = $('calc-notes');
  const plotSummary = $('calc-plot-summary');
  const resultCard = root.querySelector('.calc-result-card');
  if (!plotSelect || !cropSelect || !plotHost || !cropHost) return;

  const steps = {};
  root.querySelectorAll('.calc-step[data-step]').forEach((node) => {
    steps[node.dataset.step] = node;
  });
  const STEP_ORDER = ['plot', 'crop', 'area', 'dose', 'unit', 'go'];

  const view = { plotChoosing: false, cropChoosing: false, plotQuery: '', cropQuery: '', cropFilter: 'all' };
  const signatures = { plot: '', crop: '' };
  let latest = calc.state();

  const plotById = (id) => plots.find((plot) => String(plot.id) === String(id)) || null;
  const cropById = (id) => crops.find((crop) => String(crop.id) === String(id)) || null;
  const emoji = (crop) => calc.cropEmoji(crop);
  const shortGroup = (crop) => groupShort.get(crop.group) || crop.group || '';

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

  // ---------- Potrero ----------
  function plotMeta(plot) {
    return plot.farmName + ' · ' + fmt(plot.areaHa, 2) + ' ha';
  }
  function plotIcon(plot) {
    const crop = plot.cropId ? cropById(plot.cropId) : null;
    return crop ? emoji(crop) : '🟩';
  }

  function renderPlots() {
    const selected = plotById(plotSelect.value);
    const choosing = view.plotChoosing && plots.length > 1;
    const signature = [plots.length, selected ? selected.id : '', choosing, view.plotQuery].join('|');
    if (signature === signatures.plot) return;
    signatures.plot = signature;
    steps.plot.classList.add('is-enhanced');
    plotHost.hidden = false;
    plotHost.replaceChildren();

    if (!plots.length) {
      const farmWithSetup = predios.find((item) => item.setupUrl);
      if (farmWithSetup) {
        plotHost.append(
          emptyCard(
            '🗺️',
            'Aún no tienes potreros',
            'Los cálculos se hacen por potrero.',
            [h('a', { class: 'btn-primary-agro btn-sm', href: farmWithSetup.setupUrl }, 'Definir potreros')],
          ),
        );
      } else {
        plotHost.append(
          emptyCard(
            '🗺️',
            'Aún no tienes predios',
            'Regístralo para empezar a calcular.',
            [
              h(
                'a',
                { class: 'btn-primary-agro btn-sm', href: plotHost.dataset.farmCreateUrl || '/predios/nuevo/' },
                'Registrar predio',
              ),
            ],
          ),
        );
      }
      return;
    }

    if (selected && !choosing) {
      plotHost.append(
        h(
          'div',
          { class: 'calc-chosen' },
          h('span', { class: 'calc-chosen-ico', 'aria-hidden': 'true' }, plotIcon(selected)),
          h(
            'div',
            { class: 'calc-chosen-copy' },
            h('strong', null, selected.name),
            h('small', null, plotMeta(selected)),
          ),
          plots.length > 1
            ? h('button', { class: 'btn-ghost-agro btn-sm', type: 'button', 'data-change': 'plot' }, 'Cambiar potrero')
            : null,
        ),
      );
      return;
    }

    const grid = h('div', { class: 'calc-opts', role: 'group', 'aria-label': 'Potreros' });
    const fill = () => {
      const query = norm(view.plotQuery);
      grid.replaceChildren();
      const list = plots.filter(
        (plot) => !query || norm(plot.name + ' ' + plot.farmName + ' ' + plot.crop).includes(query),
      );
      list.forEach((plot) =>
        grid.append(
          h(
            'button',
            {
              class: 'calc-opt',
              type: 'button',
              'data-plot': plot.id,
              'aria-pressed': String(String(plot.id) === plotSelect.value),
            },
            h('span', { class: 'calc-opt-ico', 'aria-hidden': 'true' }, plotIcon(plot)),
            h(
              'span',
              { class: 'calc-opt-copy' },
              h('strong', null, plot.name),
              h('small', null, plotMeta(plot)),
              h('small', null, plot.crop ? 'Cultivo: ' + plot.crop : 'Sin cultivo'),
            ),
          ),
        ),
      );
      if (!list.length) grid.append(h('p', { class: 'calc-opts-empty' }, 'Ningún potrero coincide con la búsqueda.'));
    };
    if (plots.length > 6) {
      const search = h('input', {
        class: 'calc-search',
        type: 'search',
        placeholder: 'Buscar potrero o predio…',
        'aria-label': 'Buscar potrero',
        value: view.plotQuery,
      });
      search.addEventListener('input', () => {
        view.plotQuery = search.value;
        fill();
      });
      plotHost.append(h('div', { class: 'calc-picker-tools' }, search));
    }
    fill();
    plotHost.append(grid);
  }

  // ---------- Cultivo ----------
  function cropChoices() {
    const present = new Set(
      Array.from(cropSelect.options)
        .filter((option) => option.value && !option.disabled)
        .map((option) => option.value),
    );
    return crops
      .filter((crop) => present.has(String(crop.id)))
      .sort((a, b) => {
        const byGroup = (groupOrder.indexOf(a.group) + 1 || 99) - (groupOrder.indexOf(b.group) + 1 || 99);
        return byGroup || a.name.localeCompare(b.name, 'es');
      });
  }

  function renderCrops() {
    const selected = cropById(cropSelect.value);
    const choices = cropChoices();
    const choosing = !selected || view.cropChoosing;
    const signature = [choices.length, selected ? selected.id : '', choosing, view.cropQuery, view.cropFilter].join(
      '|',
    );
    if (signature === signatures.crop) return;
    signatures.crop = signature;
    steps.crop.classList.add('is-enhanced');
    steps.crop.classList.toggle('is-empty', !choices.length);
    cropHost.hidden = false;
    cropHost.replaceChildren();

    if (!choices.length) {
      const adminUrl = cropHost.dataset.adminUrl;
      cropHost.append(
        emptyCard(
          '🌱',
          'No hay cultivos activos en el catálogo',
          'Sin cultivos no se puede calcular, y AgroAsesor no inventa datos. ' +
            (adminUrl ? 'Regístralos desde administración.' : 'Pide al administrador que los cargue.'),
          adminUrl ? [h('a', { class: 'btn-secondary-agro btn-sm', href: adminUrl }, 'Ir al catálogo')] : [],
        ),
      );
      return;
    }

    if (selected && !choosing) {
      const hasDensity = selected.density !== null && selected.density !== undefined;
      cropHost.append(
        h(
          'div',
          { class: 'calc-chosen' },
          h('span', { class: 'calc-chosen-ico', 'aria-hidden': 'true' }, emoji(selected)),
          h(
            'div',
            { class: 'calc-chosen-copy' },
            h('strong', null, selected.name),
            h(
              'small',
              null,
              [
                shortGroup(selected),
                hasDensity
                  ? 'Densidad ' + fmt(selected.density) + ' ' + (selected.unit || 'kg') + '/ha'
                  : 'Sin densidad documentada',
              ]
                .filter(Boolean)
                .join(' · '),
            ),
          ),
          h('button', { class: 'btn-ghost-agro btn-sm', type: 'button', 'data-change': 'crop' }, 'Cambiar cultivo'),
        ),
      );
      return;
    }

    const many = choices.length > 8;
    const grid = h('div', { class: 'calc-opts', role: 'group', 'aria-label': 'Cultivos del catálogo' });
    const fill = () => {
      const query = norm(view.cropQuery);
      grid.replaceChildren();
      const list = choices.filter(
        (crop) =>
          (view.cropFilter === 'all' || shortGroup(crop) === view.cropFilter) &&
          (!query || norm(crop.name + ' ' + crop.group).includes(query)),
      );
      list.forEach((crop) =>
        grid.append(
          h(
            'button',
            {
              class: 'calc-opt',
              type: 'button',
              'data-crop': crop.id,
              'aria-pressed': String(String(crop.id) === cropSelect.value),
            },
            h('span', { class: 'calc-opt-ico', 'aria-hidden': 'true' }, emoji(crop)),
            h('span', { class: 'calc-opt-copy' }, h('strong', null, crop.name), h('small', null, shortGroup(crop))),
          ),
        ),
      );
      if (!list.length) grid.append(h('p', { class: 'calc-opts-empty' }, 'Ningún cultivo coincide con la búsqueda.'));
    };

    if (many) {
      const tools = h('div', { class: 'calc-picker-tools' });
      const search = h('input', {
        class: 'calc-search',
        type: 'search',
        placeholder: 'Buscar cultivo…',
        'aria-label': 'Buscar cultivo',
        value: view.cropQuery,
      });
      search.addEventListener('input', () => {
        view.cropQuery = search.value;
        fill();
      });
      tools.append(search);
      const groups = Array.from(new Set(choices.map(shortGroup))).filter(Boolean);
      if (groups.length > 1) {
        const filters = h('div', { class: 'calc-filters', role: 'group', 'aria-label': 'Filtrar por categoría' });
        ['all'].concat(groups).forEach((name) => {
          const chip = h(
            'button',
            {
              class: 'calc-chip' + (view.cropFilter === name ? ' is-active' : ''),
              type: 'button',
              'data-filter': name,
              'aria-pressed': String(view.cropFilter === name),
            },
            name === 'all' ? 'Todos' : name,
          );
          filters.append(chip);
        });
        filters.addEventListener('click', (event) => {
          const chip = event.target.closest('[data-filter]');
          if (!chip) return;
          view.cropFilter = chip.dataset.filter;
          filters.querySelectorAll('[data-filter]').forEach((node) => {
            const active = node.dataset.filter === view.cropFilter;
            node.classList.toggle('is-active', active);
            node.setAttribute('aria-pressed', String(active));
          });
          fill();
        });
        tools.append(filters);
      }
      cropHost.append(tools);
      const frequent = (hints.frequent || [])
        .map((id) => choices.find((crop) => String(crop.id) === String(id)))
        .filter(Boolean);
      if (frequent.length) {
        cropHost.append(
          h(
            'p',
            { class: 'calc-freq-label' },
            hints.frequentBasis === 'used' ? 'Frecuentes en tus potreros' : 'Sugeridos',
          ),
        );
        cropHost.append(
          h(
            'div',
            { class: 'calc-freq' },
            frequent.map((crop) =>
              h('button', { class: 'calc-chip', type: 'button', 'data-crop': crop.id }, emoji(crop) + ' ' + crop.name),
            ),
          ),
        );
      }
    }
    fill();
    cropHost.append(grid);
  }

  plotHost.addEventListener('click', (event) => {
    const change = event.target.closest('[data-change="plot"]');
    if (change) {
      view.plotChoosing = true;
      view.plotQuery = '';
      update(latest);
      return;
    }
    const option = event.target.closest('[data-plot]');
    if (!option) return;
    view.plotChoosing = false;
    view.plotQuery = '';
    fireChange(plotSelect, option.dataset.plot);
    update(calc.state());
  });
  cropHost.addEventListener('click', (event) => {
    const change = event.target.closest('[data-change="crop"]');
    if (change) {
      view.cropChoosing = true;
      view.cropQuery = '';
      view.cropFilter = 'all';
      update(latest);
      return;
    }
    const option = event.target.closest('[data-crop]');
    if (!option) return;
    view.cropChoosing = false;
    view.cropQuery = '';
    view.cropFilter = 'all';
    fireChange(cropSelect, option.dataset.crop);
    update(calc.state());
  });

  // ---------- Pasos, bloqueos y estado ----------
  function updateSteps(state) {
    const ok = {};
    state.items.forEach((item) => {
      ok[item.key] = item.ok;
    });
    const done = {
      plot: ok.plot,
      crop: ok.crop,
      area: ok.area,
      dose: ok.dose && ok.product !== false,
      unit: ok.unit,
      go: state.saved,
    };
    const current = STEP_ORDER.find((key) => !done[key] && key !== 'go') || 'go';
    // Sin potrero no se puede elegir cultivo; sin cultivo no tiene sentido pedir superficie, dosis ni unidad.
    let lockFrom = STEP_ORDER.length;
    if (!ok.plot) lockFrom = 1;
    else if (!ok.crop) lockFrom = 2;
    STEP_ORDER.forEach((key, index) => {
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
    submit.disabled = !state.ready;
    pdf.disabled = !state.ready;
    if (reportsLink) {
      reportsLink.setAttribute('aria-disabled', String(!state.saved));
      if (state.saved) reportsLink.removeAttribute('tabindex');
      else reportsLink.setAttribute('tabindex', '-1');
    }
    const ctx = Boolean(ok.plot && ok.crop);
    coverLink.setAttribute('aria-disabled', String(!ctx));
    if (!ctx) coverLink.setAttribute('tabindex', '-1');
    else coverLink.removeAttribute('tabindex');
  }

  function statusFor(state) {
    if (!plots.length) return ['blocked', 'Necesitas un potrero'];
    if (!crops.length) return ['blocked', 'El catálogo no tiene cultivos'];
    if (state.saved) return state.simulation ? ['sim', 'Simulación guardada'] : ['done', 'Cálculo guardado'];
    if (state.ready) return ['ready', 'Listo para calcular'];
    const missing = state.missing;
    return [
      'pending',
      missing.length === 1 ? 'Falta: ' + missing[0].label.toLowerCase() : 'Faltan ' + missing.length + ' datos',
    ];
  }

  function updateStatus(state) {
    const status = $('calc-status');
    const [kind, text] = statusFor(state);
    status.dataset.state = kind;
    status.querySelector('.calc-status-text').textContent = text;
    const hint = $('calc-submit-hint');
    if (!plots.length) hint.textContent = 'Necesitas al menos un potrero registrado para calcular.';
    else if (!crops.length) hint.textContent = 'Sin cultivos activos en el catálogo no se puede calcular.';
    else if (state.saved)
      hint.textContent = 'Cálculo guardado. Puedes descargarlo en PDF o ajustar los datos y volver a calcular.';
    else if (state.ready)
      hint.textContent = 'Todo listo. «Calcular ahora» valida el cálculo con el catálogo y lo guarda en tu cuenta.';
    else hint.textContent = 'Te falta: ' + state.missing.map((item) => item.label.toLowerCase()).join(', ') + '.';
  }

  // ---------- Superficie: atajo para usar todo el potrero ----------
  function renderAreaTools() {
    const host = $('calc-area-tools');
    const plot = plotById(plotSelect.value);
    const area = Number(String($('calc-area').value).replace(',', '.'));
    host.replaceChildren();
    if (!plot) return;
    if (!Number.isFinite(area) || Math.abs(area - plot.areaHa) > 1e-6) {
      host.append(
        h(
          'button',
          { class: 'calc-chip', type: 'button', 'data-use-all': '1' },
          'Usar todo el potrero (' + fmt(plot.areaHa, 2) + ' ha)',
        ),
      );
    }
  }
  $('calc-area-tools').addEventListener('click', (event) => {
    if (!event.target.closest('[data-use-all]')) return;
    const plot = plotById(plotSelect.value);
    if (!plot) return;
    const input = $('calc-area');
    input.value = Number(plot.areaHa.toFixed(4));
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });

  // ---------- Resumen de la selección ----------
  function renderSummary() {
    const plot = plotById(plotSelect.value);
    plotSummary.replaceChildren();
    if (plot) {
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
      plotSummary.append(
        h(
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
        ),
      );
    }
    summary.hidden = !plot && notes.hidden;
  }

  // ---------- Actualización general ----------
  function update(state) {
    latest = state;
    // Sin potreros o sin cultivos no hay nada que calcular: se ocultan también las opciones avanzadas.
    root.classList.toggle('is-blocked', !plots.length || !crops.length);
    renderPlots();
    renderCrops();
    updateSteps(state);
    updateStatus(state);
    renderAreaTools();
    renderSummary();
  }

  root.addEventListener('calc:update', (event) => {
    update(event.detail);
    // En pantallas angostas el resultado queda debajo del formulario: se lleva a la vista al guardar.
    if (event.detail.painted && event.detail.saved && window.matchMedia('(max-width: 1080px)').matches && resultCard) {
      const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      resultCard.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
    }
  });

  // ---------- Acciones ----------
  coverLink.addEventListener('click', (event) => {
    event.preventDefault();
    if (coverLink.getAttribute('aria-disabled') === 'true') return;
    const info = calc.availabilityInfo();
    if (window.AgroDecisiones) window.AgroDecisiones.goToAvailability(info);
    else window.location.href = coverLink.getAttribute('href') + '&plot=' + encodeURIComponent(info.plot || '');
  });

  // «Continuar como simulación» (aviso de catálogo incompleto): lleva al formulario sin inventar datos.
  document.querySelectorAll('[data-calc-jump]').forEach((link) =>
    link.addEventListener('click', (event) => {
      event.preventDefault();
      if (window.AgroDecisiones) window.AgroDecisiones.showTool('llevar');
      const form = $('calc-form');
      const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      if (form) form.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
    }),
  );

  update(latest);
})();
