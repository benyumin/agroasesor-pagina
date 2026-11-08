(() => {
  'use strict';
  const configNode = document.getElementById('maps-config');
  const mapElement = document.getElementById('farm-map');
  const status = document.getElementById('map-status');
  if (!configNode || !mapElement || !status) return;

  const config = JSON.parse(configNode.textContent);
  const form = document.getElementById('terrain-form');
  const isPlotForm = Boolean(document.getElementById('id_perimetro'));
  const isPredioForm = Boolean(document.getElementById('accept-perimeter'));
  // «Registrar predio» (/predios/nuevo/) es siempre un registro nuevo: el mapa y los campos de dibujo parten vacíos.
  const isCreatingPredio = isPredioForm && Boolean(form) && form.dataset.mode === 'create';
  const NEW_PREDIO_ZOOM = 12; // vista general (~20 km de ancho): se reconoce Melipilla y su entorno
  const MY_LOCATION_ZOOM = 16; // suficiente para distinguir caminos, parcelas y límites aproximados
  const dataNode = document.getElementById('agro-farms');
  const farmData = dataNode ? JSON.parse(dataNode.textContent) : { predios: [], plots: [] };
  const areaInput = document.getElementById('id_superficie_ha');
  const latitudeInput = document.getElementById('id_latitud');
  const longitudeInput = document.getElementById('id_longitud');
  const legacyPerimeterInput = document.getElementById('id_perimetro');
  const geojsonInput = document.getElementById('id_geojson');
  const mapSummary = document.getElementById('map-form-summary');
  const mapSummaryArea = document.getElementById('map-summary-area');
  const mapSummaryPerimeter = document.getElementById('map-summary-perimeter');
  const mapSummaryLocation = document.getElementById('map-summary-location');
  const mapSummaryM2 = document.getElementById('map-summary-m2');
  const mapSummaryVertices = document.getElementById('map-summary-vertices');
  const mapSummaryLat = document.getElementById('map-summary-lat');
  const mapSummaryLng = document.getElementById('map-summary-lng');
  const perimeterAcceptedInput = document.getElementById('id_perimetro_aceptado');
  const confirmationPanel = document.getElementById('perimeter-confirmation');
  const registrationPrompt = document.getElementById('registration-prompt');
  const confirmArea = document.getElementById('confirm-area');
  const confirmPerimeter = document.getElementById('confirm-perimeter');
  const confirmVertices = document.getElementById('confirm-vertices');
  const promptTitle = document.getElementById('registration-prompt-title');
  const promptText = document.getElementById('registration-prompt-text');
  const promptDefaults = {
    title: promptTitle ? promptTitle.textContent : '',
    text: promptText ? promptText.textContent : '',
  };
  const melipillaCenter = [-33.6866, -71.2167];

  // Superficie escrita a mano: avisa en español si se pasa de lo disponible y ofrece usar justo lo que queda.
  (function setupSurfaceInput() {
    if (!isPlotForm || !areaInput) return;
    const fmt = (n) => n.toLocaleString('es-CL', { maximumFractionDigits: 2 });
    const check = () => {
      areaInput.setCustomValidity('');
      if (areaInput.readOnly || areaInput.value === '') return;
      const max = Number(areaInput.max);
      if (areaInput.max !== '' && Number.isFinite(max) && Number(areaInput.value) > max) {
        areaInput.setCustomValidity('En este predio solo quedan ' + fmt(max) + ' ha disponibles.');
      }
    };
    areaInput.addEventListener('input', check);
    areaInput.addEventListener('change', check);
    const fill = document.querySelector('[data-fill-available]');
    if (fill) {
      fill.addEventListener('click', () => {
        if (areaInput.readOnly) return;
        areaInput.value = Number(areaInput.max).toFixed(4);
        check();
        areaInput.focus();
      });
    }
  })();
  let map;
  let currentLayer = null;
  let marker = null;
  let drawing = false;
  let drawingMode = null;
  let drawPoints = [];
  let previewLine = null;
  let savedMeasurement = null;
  let drawMarkers = [];
  let boundaryMarkers = [];
  let viewBounds;
  let originalFeature = null;
  let parentFarm = null;
  let parentFarmLayer = null;
  const farmLayers = new Map();
  const plotLayers = new Map();
  let selectedFarmId = '';
  let selectedPlotId = '';

  function setStatus(message) {
    status.textContent = message;
  }

  function configureTiles() {
    const normalTiles = L.tileLayer(config.tile_url, {
      attribution: config.attribution,
      maxZoom: 19,
      crossOrigin: true,
    });
    const satelliteTiles = L.tileLayer(
      'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      {
        attribution: '&copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community',
        maxZoom: 19,
        crossOrigin: true,
      },
    );
    let activeTiles = normalTiles;

    [normalTiles, satelliteTiles].forEach((tiles) => {
      tiles.on('tileerror', () => {
        if (tiles === activeTiles) {
          setStatus(
            'No se cargaron algunas teselas del mapa. Puedes seguir dibujando; comprueba la conexión si el fondo aparece vacío.',
          );
        }
      });
      tiles.on('tileload', () => {
        if (tiles === activeTiles && status.textContent.startsWith('No se cargaron algunas teselas del mapa.')) {
          setStatus(
            form
              ? isPredioForm
                ? 'Mapa listo. Toca «Dibujar mi campo».'
                : 'Mapa listo. Toca «Dibujar mi potrero».'
              : 'Mapa listo.',
          );
        }
      });
    });

    const viewControl = L.control({ position: 'topright' });
    viewControl.onAdd = () => {
      const container = L.DomUtil.create('div', 'leaflet-control map-view-switch');
      container.setAttribute('role', 'group');
      container.setAttribute('aria-label', 'Cambiar vista del mapa');
      const buttons = new Map();

      function selectView(tiles) {
        if (tiles !== activeTiles) {
          map.removeLayer(activeTiles);
          activeTiles = tiles;
          activeTiles.addTo(map);
        }
        buttons.forEach((button, layer) => {
          const isActive = layer === activeTiles;
          button.classList.toggle('is-active', isActive);
          button.setAttribute('aria-pressed', String(isActive));
        });
      }

      [
        ['Normal', normalTiles],
        ['Satélite', satelliteTiles],
      ].forEach(([label, tiles]) => {
        const button = L.DomUtil.create('button', '', container);
        button.type = 'button';
        button.textContent = label;
        button.setAttribute('aria-pressed', 'false');
        button.addEventListener('click', () => selectView(tiles));
        buttons.set(tiles, button);
      });

      L.DomEvent.disableClickPropagation(container);
      L.DomEvent.disableScrollPropagation(container);
      selectView(normalTiles);
      return container;
    };
    viewControl.addTo(map);
    normalTiles.addTo(map);
  }

  function featureOf(layer) {
    const feature = layer.toGeoJSON();
    feature.properties = {};
    return feature;
  }

  function featureCoordinates(feature) {
    if (!feature) return [];
    const geometry = feature.type === 'Feature' ? feature.geometry : feature;
    if (!geometry || geometry.type !== 'Polygon' || !Array.isArray(geometry.coordinates)) return [];
    const ring = (geometry.coordinates[0] || []).map((point) => point.slice());
    if (ring.length > 1 && JSON.stringify(ring[0]) === JSON.stringify(ring[ring.length - 1])) ring.pop();
    return ring;
  }

  function distanceMetres(first, second) {
    const radians = (degrees) => (degrees * Math.PI) / 180;
    const [longitude1, latitude1] = first;
    const [longitude2, latitude2] = second;
    const deltaLatitude = radians(latitude2 - latitude1);
    const deltaLongitude = radians(longitude2 - longitude1);
    const latitude1Radians = radians(latitude1);
    const latitude2Radians = radians(latitude2);
    const haversine =
      Math.sin(deltaLatitude / 2) ** 2 +
      Math.cos(latitude1Radians) * Math.cos(latitude2Radians) * Math.sin(deltaLongitude / 2) ** 2;
    return 2 * 6378137 * Math.atan2(Math.sqrt(haversine), Math.sqrt(Math.max(0, 1 - haversine)));
  }

  function perimeterMetres(points, closeRing) {
    const edgeCount = points.length - 1 + (closeRing && points.length > 2 ? 1 : 0);
    let length = 0;
    for (let index = 0; index < edgeCount; index += 1) {
      length += distanceMetres(points[index], points[(index + 1) % points.length]);
    }
    return length;
  }

  function polygonAreaSquareMetres(points) {
    const vectors = points.map(([longitude, latitude]) => {
      const lng = (longitude * Math.PI) / 180;
      const lat = (latitude * Math.PI) / 180;
      return [Math.cos(lat) * Math.cos(lng), Math.cos(lat) * Math.sin(lng), Math.sin(lat)];
    });
    const dot = (a, b) => a.reduce((sum, value, index) => sum + value * b[index], 0);
    const origin = vectors[0];
    let excess = 0;
    for (let index = 1; index < vectors.length - 1; index += 1) {
      const b = vectors[index];
      const c = vectors[index + 1];
      const determinant =
        origin[0] * (b[1] * c[2] - b[2] * c[1]) +
        origin[1] * (b[2] * c[0] - b[0] * c[2]) +
        origin[2] * (b[0] * c[1] - b[1] * c[0]);
      excess += 2 * Math.atan2(determinant, 1 + dot(origin, b) + dot(b, c) + dot(c, origin));
    }
    return Math.abs(excess) * 6378137 ** 2;
  }

  function orientation(a, b, c) {
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  }

  function segmentsIntersect(a, b, c, d) {
    const epsilon = 1e-12;
    const onSegment = (first, second, point) =>
      Math.min(first[0], second[0]) - epsilon <= point[0] &&
      point[0] <= Math.max(first[0], second[0]) + epsilon &&
      Math.min(first[1], second[1]) - epsilon <= point[1] &&
      point[1] <= Math.max(first[1], second[1]) + epsilon;
    const values = [orientation(a, b, c), orientation(a, b, d), orientation(c, d, a), orientation(c, d, b)];
    if (values[0] * values[1] < 0 && values[2] * values[3] < 0) return true;
    const points = [c, d, a, b];
    return values.some(
      (value, index) => Math.abs(value) < epsilon && onSegment(index < 2 ? a : c, index < 2 ? b : d, points[index]),
    );
  }

  function hasSelfIntersection(points) {
    const edges = points.map((point, index) => [point, points[(index + 1) % points.length]]);
    for (let first = 0; first < edges.length; first += 1) {
      for (let second = first + 1; second < edges.length; second += 1) {
        if (second === first + 1 || (first === 0 && second === edges.length - 1)) continue;
        if (segmentsIntersect(...edges[first], ...edges[second])) return true;
      }
    }
    return false;
  }

  function pointInRing(point, ring) {
    let inside = false;
    for (let index = 0, previous = ring.length - 1; index < ring.length; previous = index, index += 1) {
      const [x, y] = point;
      const [x1, y1] = ring[index];
      const [x2, y2] = ring[previous];
      const cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1);
      if (
        Math.abs(cross) < 1e-12 &&
        x >= Math.min(x1, x2) - 1e-12 &&
        x <= Math.max(x1, x2) + 1e-12 &&
        y >= Math.min(y1, y2) - 1e-12 &&
        y <= Math.max(y1, y2) + 1e-12
      )
        return true;
      if (y1 > y !== y2 > y && x < ((x2 - x1) * (y - y1)) / (y2 - y1) + x1) {
        inside = !inside;
      }
    }
    return inside;
  }

  function polygonInsideParent(points) {
    if (!parentFarm?.geojson) return true;
    const boundary = featureCoordinates(parentFarm.geojson);
    if (boundary.length < 3 || points.some((point) => !pointInRing(point, boundary))) return false;
    for (let index = 0; index < points.length; index += 1) {
      const first = points[index];
      const second = points[(index + 1) % points.length];
      for (let edge = 0; edge < boundary.length; edge += 1) {
        const third = boundary[edge];
        const fourth = boundary[(edge + 1) % boundary.length];
        const crossesBoundary =
          orientation(first, second, third) * orientation(first, second, fourth) < 0 &&
          orientation(third, fourth, first) * orientation(third, fourth, second) < 0;
        if (crossesBoundary) return false;
      }
    }
    return true;
  }

  function formatLength(metres) {
    return metres < 1000
      ? metres.toLocaleString('es-CL', { maximumFractionDigits: 1 }) + ' m'
      : (metres / 1000).toLocaleString('es-CL', { maximumFractionDigits: 3 }) + ' km';
  }

  function updateFormMapSummary(points) {
    if (!mapSummary) return;
    const latitude = latitudeInput?.value;
    const longitude = longitudeInput?.value;
    const hasCoordinates = Boolean(latitude && longitude);
    const setText = (element, text) => {
      if (element) element.textContent = text;
    };
    const showCoordinates = (lat, lng) => {
      setText(mapSummaryLat, Number(lat).toFixed(6));
      setText(mapSummaryLng, Number(lng).toFixed(6));
    };
    if (points && points.length >= 3) {
      const area = polygonAreaSquareMetres(points);
      if (areaInput) {
        areaInput.value = (area / 10000).toFixed(4);
        areaInput.readOnly = true;
      }
      setText(mapSummaryArea, (area / 10000).toLocaleString('es-CL', { maximumFractionDigits: 4 }) + ' ha');
      setText(mapSummaryM2, area.toLocaleString('es-CL', { maximumFractionDigits: 2 }) + ' m²');
      setText(mapSummaryPerimeter, formatLength(perimeterMetres(points, true)));
      setText(mapSummaryVertices, String(points.length));
      setText(mapSummaryLocation, '📍 Ubicación registrada desde el mapa');
      showCoordinates(hasCoordinates ? latitude : points[0][1], hasCoordinates ? longitude : points[0][0]);
      mapSummary.hidden = false;
      return;
    }
    if (hasCoordinates) {
      mapSummary.hidden = false;
      setText(
        mapSummaryArea,
        areaInput?.value
          ? Number(areaInput.value).toLocaleString('es-CL', { maximumFractionDigits: 4 }) + ' ha'
          : 'Pendiente de calcular',
      );
      setText(
        mapSummaryM2,
        areaInput?.value
          ? (Number(areaInput.value) * 10000).toLocaleString('es-CL', { maximumFractionDigits: 2 }) + ' m²'
          : '—',
      );
      setText(mapSummaryPerimeter, 'Pendiente de delimitar');
      setText(mapSummaryVertices, '—');
      setText(mapSummaryLocation, '📍 Ubicación registrada desde el mapa');
      showCoordinates(latitude, longitude);
      return;
    }
    mapSummary.hidden = true;
    [
      mapSummaryArea,
      mapSummaryM2,
      mapSummaryPerimeter,
      mapSummaryVertices,
      mapSummaryLocation,
      mapSummaryLat,
      mapSummaryLng,
    ].forEach((element) => setText(element, ''));
  }

  function fmtHa(value, digits) {
    return Number(value || 0).toLocaleString('es-CL', { maximumFractionDigits: digits ?? 2 });
  }

  function fmtDate(value) {
    if (!value) return 'Sin fecha registrada';
    const [year, month, day] = String(value).split('-');
    return day && month && year ? day + '/' + month + '/' + year : value;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function plotActionUrl(kind, plot, farm) {
    const params = new URLSearchParams();
    if (kind === 'calendar') {
      params.set('potrero', plot.id);
      if (plot.cropId) params.set('crop', plot.cropId);
      if (farm.zoneId) params.set('zone', farm.zoneId);
      return '/calendario/?' + params.toString();
    }
    params.set('plot', plot.id);
    if (kind === 'seeds') {
      params.set('herramienta', 'llevar');
      params.set('tipo', 'semillas');
    }
    if (kind === 'inputs') {
      params.set('herramienta', 'llevar');
      params.set('tipo', 'insumos');
    }
    return '/decisiones/?' + params.toString();
  }

  function stateChip(text, tone) {
    return el('span', 'state-chip ' + tone, text);
  }

  function nextAction(title, cta, target) {
    const box = el('div', 'drawer-next');
    box.append(el('span', 'next-hint-label', 'Próxima acción'), el('p', '', title));
    let action;
    if (target.assignId) {
      action = el('button', 'btn-ghost-agro btn-compact', '→ ' + cta);
      action.type = 'button';
      action.dataset.assignCrop = target.assignId;
    } else {
      action = el('a', 'btn-ghost-agro btn-compact', '→ ' + cta);
      action.href = target.href;
    }
    box.appendChild(action);
    return box;
  }

  function plotNextAction(plot) {
    if (!plot.crop) return nextAction('Asignar cultivo a ' + plot.name, 'Asignar cultivo', { assignId: plot.id });
    if (Number(plot.planDone) < Number(plot.planTotal)) {
      return nextAction('Definir la planificación de ' + plot.name, 'Definir planificación', { href: plot.editUrl });
    }
    if (!plot.nextTask)
      return nextAction('Programar una labor en ' + plot.name, 'Programar labor', { href: plot.taskUrl });
    return nextAction('Revisar la planificación de ' + plot.name, 'Ver planificación', { href: plot.planUrl });
  }

  function setSelectedTerrain(farm, plot) {
    const details = document.getElementById('selected-terrain-details');
    const emptyState = document.getElementById('selected-terrain-empty');
    if (!details || !emptyState) return;
    details.replaceChildren();
    const list = (parent) => {
      const dl = el('dl', 'terrain-facts');
      parent.appendChild(dl);
      return (label, value) => dl.append(el('dt', '', label), el('dd', '', value));
    };
    // Panel contextual: informa y propone UNA próxima acción. Las acciones habituales están en el menú «⋯» de cada tarjeta.
    const tools = el('div', 'terrain-tool-links');
    tools.appendChild(el('span', 'terrain-tool-label', 'Usar este potrero en:'));
    const addLink = (label, href) => {
      const link = el('a', 'btn-ghost-agro btn-compact', label);
      link.href = href;
      tools.appendChild(link);
      return link;
    };
    const farmPlots = farmData.plots.filter((item) => String(item.predio) === String(farm.id));
    const assignedArea = farmPlots.reduce((sum, item) => sum + Number(item.areaHa || 0), 0);

    if (plot) {
      const head = el('div', 'terrain-panel-head');
      head.append(el('span', 'terrain-eyebrow', 'Potrero seleccionado'), el('h3', '', plot.name));
      details.appendChild(head);
      const chips = el('div', 'chip-row drawer-chips');
      chips.appendChild(stateChip(plot.crop ? 'Con cultivo' : 'Sin cultivo', plot.crop ? 'is-green' : 'is-neutral'));
      if (plot.crop && Number(plot.planDone) === Number(plot.planTotal))
        chips.appendChild(stateChip('Planificación completa', 'is-dark'));
      else if (!plot.crop) chips.appendChild(stateChip('Sin planificar', 'is-neutral'));
      if (plot.overdueTasks) chips.appendChild(stateChip('Labores vencidas · ' + plot.overdueTasks, 'is-red'));
      else if (plot.pendingTasks) chips.appendChild(stateChip('Labores pendientes · ' + plot.pendingTasks, 'is-amber'));
      details.appendChild(chips);
      const add = list(details);
      add('Predio', farm.name);
      add('Superficie', fmtHa(plot.areaHa, 2) + ' ha (' + fmtHa(plot.areaM2, 0) + ' m²)');
      if (plot.perimeterM !== null && plot.perimeterM !== undefined) add('Perímetro', formatLength(plot.perimeterM));
      if (plot.crop) {
        add('Cultivo actual', plot.crop);
        add('Variedad', plot.variety || 'No registrada');
        add('Temporada', plot.season || 'Sin rótulo');
        add('Fecha de siembra', fmtDate(plot.sowing));
        add('Cosecha estimada', fmtDate(plot.harvest));
        add('Estado', plot.status || 'No disponible');
        add('Planificación', plot.planDone + ' de ' + plot.planTotal + ' pasos');
      } else {
        const notice = el('div', 'terrain-no-crop');
        notice.append(el('p', '', 'Este potrero todavía no tiene un cultivo asignado'));
        details.appendChild(notice);
      }
      if (plot.nextTask) add('Próxima labor', plot.nextTask.label + ' · ' + plot.nextTask.date);
      details.appendChild(plotNextAction(plot));
      addLink('Planificar siembra', plotActionUrl('calendar', plot, farm));
      addLink('Calcular semillas', plotActionUrl('seeds', plot, farm));
      addLink('Calcular fertilizantes', plotActionUrl('inputs', plot, farm));
      details.appendChild(tools);

      const history = el('details', 'terrain-season-history');
      history.open = (plot.seasons || []).length > 0 && (plot.seasons || []).length <= 3;
      history.append(el('summary', '', 'Historial de cultivos (' + (plot.seasons || []).length + ')'));
      if ((plot.seasons || []).length) {
        const ol = el('ol', 'season-list');
        plot.seasons.forEach((season, index) => {
          const item = el('li', index === 0 ? 'is-current' : '');
          item.append(
            el(
              'strong',
              '',
              (season.season ? season.season + ' · ' : '') +
                season.crop +
                (season.variety ? ' · ' + season.variety : ''),
            ),
          );
          item.append(
            el(
              'span',
              '',
              (index === 0 ? 'Actual · ' : 'Anterior · ') +
                season.statusLabel +
                ' · siembra ' +
                fmtDate(season.sowingDate) +
                ' · cosecha ' +
                fmtDate(season.harvestDate),
            ),
          );
          if (season.notes) item.append(el('small', '', season.notes));
          ol.appendChild(item);
        });
        history.appendChild(ol);
      } else {
        history.appendChild(el('p', 'muted', 'Aún no hay temporadas registradas para este potrero.'));
      }
      details.appendChild(history);
      const plotActions = el('div', 'plot-actions');
      const plotNext = el(
        'a',
        'btn-primary-agro btn-sm',
        !plot.crop ? 'Asignar cultivo' : !plot.sowing ? 'Continuar planificación' : 'Calcular semillas',
      );
      plotNext.href = !plot.crop || !plot.sowing ? plot.editUrl : plot.seedsUrl;
      const plotOpen = el('a', 'btn-secondary-agro btn-sm', 'Ver ficha e historial');
      plotOpen.href = plot.detailUrl;
      plotActions.append(plotNext, plotOpen);
      details.appendChild(plotActions);
      details.appendChild(el('hr'));
    }

    const farmHead = el('div', 'terrain-panel-head');
    farmHead.append(
      el('span', 'terrain-eyebrow', plot ? 'Predio del potrero' : 'Predio seleccionado'),
      el(plot ? 'h4' : 'h3', '', farm.name),
    );
    details.appendChild(farmHead);
    if (!plot) {
      details.appendChild(el('p', 'drawer-area', fmtHa(farm.area, 2) + ' ha'));
      details.appendChild(el('p', 'drawer-place', farm.location || 'Ubicación no registrada'));
      const stats = el('div', 'drawer-stats');
      const addStat = (label, value, tone) => {
        const box = el('div', 'drawer-stat' + (tone ? ' ' + tone : ''));
        box.append(el('strong', '', String(value)), el('span', '', label));
        stats.appendChild(box);
      };
      addStat('Potreros', farmPlots.length);
      addStat('Cultivos', farm.cropCount || 0);
      addStat('Labores pendientes', farm.pendingTasks || 0, farm.overdueTasks ? 'is-alert' : '');
      details.appendChild(stats);
    }
    const addFarm = list(details);
    if (plot) addFarm('Ubicación', farm.location || 'No registrada');
    addFarm('Superficie total', fmtHa(farm.area, 2) + ' ha');
    if (!plot) {
      if (farm.perimeterM !== null && farm.perimeterM !== undefined)
        addFarm('Perímetro', formatLength(farm.perimeterM));
      addFarm(
        'Superficie en potreros',
        fmtHa(assignedArea, 2) + ' ha · sin asignar ' + fmtHa(Math.max(0, Number(farm.area) - assignedArea), 2) + ' ha',
      );
      addFarm('Última modificación', farm.updated || 'No disponible');
    } else {
      const crops = [...new Set(farmPlots.map((item) => item.crop).filter(Boolean))];
      addFarm('Cultivos actuales', crops.length ? crops.join(', ') : 'Sin cultivos asignados');
    }
    if (!plot && farm.nextStep) {
      details.appendChild(nextAction(farm.nextStep.title, farm.nextStep.cta, { href: farm.nextStep.url }));
    }
    const farmOpen = el('a', plot ? 'btn-ghost-agro btn-compact' : 'btn-secondary-agro btn-sm', 'Abrir predio');
    farmOpen.href = farm.detailUrl;
    details.appendChild(farmOpen);

    emptyState.hidden = true;
    details.hidden = false;
    document.querySelectorAll('[data-select-farm]').forEach((button) => {
      const selected = String(button.dataset.selectFarm) === String(farm.id);
      button.classList.toggle('is-selected', selected);
      button.setAttribute('aria-pressed', String(selected));
      const card = button.classList.contains('entity-card-select') ? button.closest('.entity-card') : null;
      if (card) card.classList.toggle('is-selected', selected);
    });
    document.querySelectorAll('[data-farm-plots]').forEach((group) => {
      group.hidden = String(group.dataset.farmPlots) !== String(farm.id);
    });
    const plotsPanel = document.getElementById('farm-plots-picker');
    if (plotsPanel) plotsPanel.hidden = false;
    document.querySelectorAll('[data-plot-card]').forEach((card) => {
      card.classList.toggle('is-selected', Boolean(plot && String(card.dataset.plotCard) === String(plot.id)));
    });
    document.querySelectorAll('[data-select-plot]').forEach((button) => {
      const selected = Boolean(plot && String(button.dataset.selectPlot) === String(plot.id));
      button.classList.toggle('is-selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    const createPlotLink = document.getElementById('register-selected-plot');
    if (createPlotLink) {
      createPlotLink.href = farm.potreroCreateUrl;
      // «Agregar potrero» solo aparece cuando el predio ya tiene potreros; si no, el estado vacío ofrece «Configurar potrero».
      createPlotLink.hidden = farmPlots.length === 0;
    }
    const showPlotsButton = document.getElementById('show-selected-farm-plots');
    if (showPlotsButton) showPlotsButton.disabled = farmPlots.length === 0;
    const params = new URLSearchParams(window.location.search);
    params.set('predio', farm.id);
    if (plot) params.set('potrero', plot.id);
    else params.delete('potrero');
    window.history.replaceState(null, '', window.location.pathname + '?' + params.toString());
  }

  function updateMeasurements(points, closeRing) {
    const perimeterOutput = document.getElementById('live-perimeter');
    if (perimeterOutput) {
      perimeterOutput.textContent =
        (closeRing ? 'Perímetro' : 'Longitud trazada') + ': ' + formatLength(perimeterMetres(points, closeRing));
    }
    const areaOutput = document.getElementById('live-area');
    if (!areaOutput) return;
    if (!closeRing || points.length < 3) {
      areaOutput.textContent = '';
      areaOutput.classList.remove('is-error');
      return;
    }
    if (hasSelfIntersection(points)) {
      areaOutput.textContent = 'Las líneas se cruzan. Mueve las esquinas.';
      areaOutput.classList.add('is-error');
      return;
    }
    const squareMetres = polygonAreaSquareMetres(points);
    const hectares = squareMetres / 10000;
    if (isPlotForm && !polygonInsideParent(points)) {
      areaOutput.textContent = 'Tu potrero debe quedar dentro del límite verde.';
      areaOutput.classList.add('is-error');
      return;
    }
    const availableHectares = Number(areaInput?.max);
    if (isPlotForm && Number.isFinite(availableHectares) && Number(hectares.toFixed(4)) > availableHectares) {
      areaOutput.textContent =
        'Dibujaste ' +
        hectares.toLocaleString('es-CL', { maximumFractionDigits: 2 }) +
        ' ha, pero solo quedan ' +
        availableHectares.toLocaleString('es-CL', { maximumFractionDigits: 2 }) +
        ' ha en este predio.';
      areaOutput.classList.add('is-error');
      return;
    }
    areaOutput.classList.remove('is-error');
    areaOutput.textContent =
      (isPlotForm ? 'Tu potrero mide ' : 'Tu campo mide ') +
      hectares.toLocaleString('es-CL', { maximumFractionDigits: 2 }) +
      ' ha';
  }

  function setAreaAndGeometry() {
    const coordinates = featureCoordinates(featureOf(currentLayer));
    if (geojsonInput) geojsonInput.value = JSON.stringify(featureOf(currentLayer));
    if (legacyPerimeterInput) legacyPerimeterInput.value = JSON.stringify(coordinates);

    if (coordinates.length < 3) {
      document.getElementById('live-area').textContent = 'Marca al menos tres esquinas.';
      return;
    }
    updateMeasurements(coordinates, true);
    const squareMetres = polygonAreaSquareMetres(coordinates);
    const hectares = squareMetres / 10000;
    if (areaInput) {
      areaInput.value = hectares.toFixed(4);
      areaInput.readOnly = true;
    }
    if (coordinates[0]) {
      if (latitudeInput) latitudeInput.value = coordinates[0][1];
      if (longitudeInput) longitudeInput.value = coordinates[0][0];
    }
    updateFormMapSummary(coordinates);
  }

  function renderBoundaryVertices() {
    boundaryMarkers.forEach((item) => map.removeLayer(item));
    boundaryMarkers = [];
    if (!isPredioForm || !currentLayer) return;
    featureCoordinates(featureOf(currentLayer)).forEach(([longitude, latitude]) => {
      boundaryMarkers.push(
        L.circleMarker([latitude, longitude], {
          radius: 4,
          color: '#1e3d28',
          fillColor: '#fff',
          fillOpacity: 1,
          weight: 2,
          interactive: false,
        }).addTo(map),
      );
    });
  }

  // Estado visible del registro de predio: draw → confirm → data → save (y «editing» al reajustar el trazo).
  // Una sola función decide qué se muestra en la columna derecha y qué paso está activo.
  function setRegistrationState(state) {
    if (!isPredioForm) return;
    // Indicador único «Paso X de 3» (solo al crear un predio; al editar no existe).
    const stepTexts = {
      draw: 'Paso 1 de 3 · Dibuja tu campo',
      editing: 'Paso 1 de 3 · Dibuja tu campo',
      confirm: 'Paso 2 de 3 · Confirma la medida',
      data: 'Paso 3 de 3 · Ponle nombre',
      save: 'Paso 3 de 3 · Ponle nombre',
    };
    const stepIndicator = document.getElementById('step-indicator');
    if (stepIndicator && stepTexts[state]) stepIndicator.textContent = stepTexts[state];
    // Solo presentación: el CSS usa este estado para mostrar solo lo que corresponde en cada paso.
    const layoutElement = document.querySelector('.terrain-layout');
    const previousState = layoutElement ? layoutElement.dataset.regState : '';
    if (layoutElement) layoutElement.dataset.regState = state;
    // El ancho del mapa cambia al pasar de un paso a otro: se reajusta y, al confirmar, se vuelve a encuadrar el campo.
    if (map && previousState !== state) {
      map.invalidateSize({ animate: false, pan: false });
      if (state === 'confirm' && currentLayer) {
        map.fitBounds(layerBounds(currentLayer), { padding: [56, 56], maxZoom: 17 });
      }
    }
    if (confirmationPanel) confirmationPanel.hidden = state !== 'confirm';
    // Solo presentación: el estado se refleja en el contenedor del mapa (CSS decide qué botón destaca).
    const mapCard = document.querySelector('.map-card');
    if (mapCard) mapCard.dataset.regState = state;
    const confirmedLine = document.getElementById('map-confirmed-line');
    if (confirmedLine)
      confirmedLine.hidden = !(state === 'data' || state === 'save') || !(form && !form.hidden) || !currentLayer;
    // El resumen principal vive en «Medición confirmada»: bajo el mapa no se repite superficie ni perímetro.
    if (state === 'confirm' || state === 'data' || state === 'save') {
      ['live-perimeter', 'live-area'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.textContent = '';
      });
    }
    if (registrationPrompt) {
      const formVisible = Boolean(form && !form.hidden);
      registrationPrompt.hidden =
        state === 'confirm' || state === 'data' || state === 'save' || (state === 'editing' && formVisible);
      if (promptTitle && promptText) {
        promptTitle.textContent = state === 'editing' ? 'Ajusta tu contorno' : promptDefaults.title;
        promptText.textContent =
          state === 'editing'
            ? 'Mueve las esquinas en el mapa y toca «Listo» para volver a medir.'
            : promptDefaults.text;
      }
    }
    if (state === 'confirm' && confirmationPanel && window.matchMedia('(max-width: 900px)').matches) {
      confirmationPanel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }

  function setPerimeterControls(available, accepted) {
    const accept = document.getElementById('accept-perimeter');
    const edit = document.getElementById(isPredioForm ? 'edit-perimeter' : 'toggle-edit');
    const cancel = document.getElementById('cancel-perimeter');
    if (accept) accept.hidden = !available || accepted;
    if (edit) edit.hidden = !available || accepted;
    if (cancel) cancel.hidden = !available || accepted;
  }

  function showPerimeterConfirmation() {
    if (!currentLayer) {
      setStatus('Primero dibuja el contorno de tu predio.');
      return;
    }
    const points = featureCoordinates(featureOf(currentLayer));
    if (points.length < 3 || points.length > 200 || hasSelfIntersection(points)) {
      setStatus('El contorno necesita entre 3 y 200 esquinas y sus líneas no deben cruzarse.');
      return;
    }
    const area = polygonAreaSquareMetres(points);
    if (!Number.isFinite(area) || area < 1) {
      setStatus('El contorno debe encerrar una superficie de al menos 1 m².');
      return;
    }
    if (perimeterAcceptedInput) perimeterAcceptedInput.value = 'on';
    if (confirmArea) {
      confirmArea.textContent = (area / 10000).toLocaleString('es-CL', { maximumFractionDigits: 4 }) + ' ha';
    }
    const confirmM2 = document.getElementById('confirm-m2');
    if (confirmM2) confirmM2.textContent = area.toLocaleString('es-CL', { maximumFractionDigits: 2 }) + ' m²';
    if (confirmPerimeter) confirmPerimeter.textContent = formatLength(perimeterMetres(points, true));
    if (confirmVertices) confirmVertices.textContent = String(points.length);
    setRegistrationState('confirm');
    setPerimeterControls(true, true);
    if (currentLayer.pm) currentLayer.pm.disable();
    renderBoundaryVertices();
    setStatus('Revisa la medida y confirma para continuar.');
  }

  function editConfirmedPerimeter() {
    if (!currentLayer || !currentLayer.pm) return;
    setRegistrationState('editing');
    if (perimeterAcceptedInput) perimeterAcceptedInput.value = '';
    setPerimeterControls(true, false);
    boundaryMarkers.forEach((item) => map.removeLayer(item));
    boundaryMarkers = [];
    currentLayer.pm.enable({ allowSelfIntersection: false });
    setStatus('Mueve las esquinas en el mapa y toca «Listo».');
  }

  function cancelPerimeterChanges() {
    if (drawing) stopDrawing();
    boundaryMarkers.forEach((item) => map.removeLayer(item));
    boundaryMarkers = [];
    if (currentLayer) map.removeLayer(currentLayer);
    currentLayer = null;
    if (originalFeature) {
      addCurrentLayer(originalFeature, false);
    } else {
      if (geojsonInput) geojsonInput.value = '';
      if (legacyPerimeterInput) legacyPerimeterInput.value = '[]';
      if (areaInput) {
        areaInput.value = '';
        areaInput.readOnly = isPredioForm;
      }
      if (latitudeInput) latitudeInput.value = '';
      if (longitudeInput) longitudeInput.value = '';
      if (mapSummary) mapSummary.hidden = true;
      setPerimeterControls(false, false);
      const perimeterOutput = document.getElementById('live-perimeter');
      const areaOutput = document.getElementById('live-area');
      if (perimeterOutput) perimeterOutput.textContent = '';
      if (areaOutput) areaOutput.textContent = '';
    }
    if (perimeterAcceptedInput) perimeterAcceptedInput.value = '';
    setRegistrationState(originalFeature ? 'data' : 'draw');
    setStatus(
      originalFeature
        ? 'Cambios descartados. Se mantiene el perímetro guardado.'
        : 'Dibujo descartado. Los predios existentes no se modificaron.',
    );
  }

  function continueAfterConfirmation() {
    if (form) form.hidden = false;
    const editButton = document.getElementById('edit-perimeter');
    if (editButton) editButton.hidden = false;
    setRegistrationState('data');
    const nameInput = document.getElementById('id_nombre');
    if (nameInput) nameInput.focus({ preventScroll: true });
    setStatus('Contorno confirmado. Ponle nombre a tu predio y guárdalo.');
  }

  function stopDrawing() {
    drawing = false;
    drawingMode = null;
    drawPoints = [];
    if (previewLine) map.removeLayer(previewLine);
    previewLine = null;
    drawMarkers.forEach((item) => map.removeLayer(item));
    drawMarkers = [];
    document.getElementById('undo-point').disabled = true;
    document.getElementById('finish-perimeter').disabled = true;
    document.getElementById('finish-line').hidden = true;
    document.getElementById('finish-line').disabled = true;
    document.getElementById('cancel-drawing').hidden = true;
    document.getElementById('draw-perimeter').classList.remove('is-active');
    document.getElementById('measure-line').classList.remove('is-active');
  }

  function enableEditing() {
    if (currentLayer && currentLayer.pm) {
      if (isPredioForm) {
        editConfirmedPerimeter();
      } else {
        currentLayer.pm.enable({ allowSelfIntersection: false });
        setStatus('Mueve las esquinas en el mapa. Cuando termines, guarda el potrero.');
      }
    }
  }

  function addCurrentLayer(feature, askToReplace) {
    if (currentLayer && askToReplace && !window.confirm('¿Reemplazar el perímetro existente?')) return false;
    boundaryMarkers.forEach((item) => map.removeLayer(item));
    boundaryMarkers = [];
    if (currentLayer) map.removeLayer(currentLayer);
    currentLayer = L.geoJSON(feature, {
      style: { color: '#255b37', weight: 3, fillColor: '#74a567', fillOpacity: 0.24 },
      // Con setOptIn(true) Geoman solo actúa sobre capas con pmIgnore:false; sin esto, «Volver a editar» y
      // «Corregir contorno» no hacían nada sobre un contorno recién dibujado (sí funcionaban al recargar uno guardado).
      pmIgnore: false,
    }).getLayers()[0];
    if (!currentLayer) return false;
    currentLayer.addTo(map);
    if (currentLayer.pm) currentLayer.pm.disable();
    currentLayer.on('pm:edit', () => {
      setAreaAndGeometry();
      if (isPredioForm) {
        if (perimeterAcceptedInput) perimeterAcceptedInput.value = '';
        setRegistrationState('editing');
        setPerimeterControls(true, false);
      }
    });
    currentLayer.on('pm:dragend', () => {
      setAreaAndGeometry();
      if (isPredioForm) {
        if (perimeterAcceptedInput) perimeterAcceptedInput.value = '';
        setRegistrationState('editing');
        setPerimeterControls(true, false);
      }
    });
    viewBounds.extend(currentLayer.getBounds());
    setAreaAndGeometry();
    renderBoundaryVertices();
    if (isPredioForm) setPerimeterControls(true, false);
    else document.getElementById('toggle-edit').hidden = false;
    return true;
  }

  function beginDrawing(mode) {
    if (drawing) return;
    if (mode === 'polygon' && currentLayer && !window.confirm('¿Reemplazar el perímetro existente?')) return;
    if (mode === 'line' && savedMeasurement && !window.confirm('¿Reemplazar la línea medida?')) return;
    if (mode === 'line' && savedMeasurement) {
      map.removeLayer(savedMeasurement);
      savedMeasurement = null;
      document.getElementById('clear-measurement').hidden = true;
    }
    drawing = true;
    drawingMode = mode;
    drawPoints = [];
    document.getElementById(mode === 'polygon' ? 'draw-perimeter' : 'measure-line').classList.add('is-active');
    document.getElementById('undo-point').disabled = true;
    document.getElementById('cancel-drawing').hidden = false;
    document.getElementById('finish-perimeter').hidden = mode !== 'polygon';
    document.getElementById('finish-line').hidden = mode !== 'line';
    setStatus(
      mode === 'polygon'
        ? isPredioForm
          ? 'Toca cada esquina de tu predio en el mapa. Cuando termines, toca «Terminar».'
          : 'Toca cada esquina de tu potrero en el mapa. Cuando termines, toca «Terminar».'
        : isPredioForm
          ? 'Toca los dos extremos de la línea que quieres medir.'
          : 'Medir línea: marca los puntos del lindero o tramo; necesitas al menos dos.',
    );
  }

  function addDrawingPoint(event) {
    if (!drawing) {
      if (latitudeInput && longitudeInput && !currentLayer) {
        latitudeInput.value = event.latlng.lat.toFixed(6);
        longitudeInput.value = event.latlng.lng.toFixed(6);
        if (marker) map.removeLayer(marker);
        marker = L.marker(event.latlng, { draggable: true }).addTo(map);
        marker.on('dragend', (dragEvent) => {
          latitudeInput.value = dragEvent.target.getLatLng().lat.toFixed(6);
          longitudeInput.value = dragEvent.target.getLatLng().lng.toFixed(6);
          updateFormMapSummary([]);
        });
        updateFormMapSummary([]);
        setStatus('Ubicación marcada. Puedes arrastrar el marcador o dibujar el perímetro.');
      }
      return;
    }
    drawPoints.push([event.latlng.lng, event.latlng.lat]);
    const latLngs = drawPoints.map((point) => [point[1], point[0]]);
    if (previewLine) map.removeLayer(previewLine);
    previewLine = L.polyline(latLngs, { color: '#255b37', dashArray: '6 5' }).addTo(map);
    const vertex = L.circleMarker(event.latlng, {
      radius: 5,
      color: '#1e3d28',
      fillColor: '#fff',
      fillOpacity: 1,
      weight: 2,
    }).addTo(map);
    drawMarkers.push(vertex);
    document.getElementById('undo-point').disabled = drawPoints.length === 0;
    const polygonMode = drawingMode === 'polygon';
    document.getElementById('finish-perimeter').disabled = polygonMode && drawPoints.length < 3;
    document.getElementById('finish-line').disabled = !polygonMode && drawPoints.length < 2;
    updateMeasurements(drawPoints, polygonMode && drawPoints.length >= 3);
  }

  function finishDrawing() {
    if (!drawing || drawingMode !== 'polygon' || drawPoints.length < 3) {
      setStatus('Marca al menos tres esquinas para terminar tu campo.');
      return;
    }
    if (hasSelfIntersection(drawPoints)) {
      setStatus('Las líneas se cruzan. Mueve las esquinas antes de guardar.');
      return;
    }
    if (isPlotForm && !polygonInsideParent(drawPoints)) {
      setStatus('Tu potrero debe quedar dentro del límite verde. Mueve las esquinas o toca «Ver el predio».');
      return;
    }
    const drawnAreaHa = polygonAreaSquareMetres(drawPoints) / 10000;
    const availableHectares = Number(areaInput?.max);
    if (isPlotForm && Number.isFinite(availableHectares) && Number(drawnAreaHa.toFixed(4)) > availableHectares) {
      setStatus(
        'Dibujaste más de las ' +
          availableHectares.toLocaleString('es-CL', { maximumFractionDigits: 2 }) +
          ' ha que quedan en este predio. Hazlo más pequeño.',
      );
      return;
    }
    const feature = {
      type: 'Feature',
      properties: {},
      geometry: {
        type: 'Polygon',
        coordinates: [[...drawPoints.map((point) => point.slice()), drawPoints[0].slice()]],
      },
    };
    stopDrawing();
    addCurrentLayer(feature, false);
    if (isPredioForm) {
      // Encuadra el polígono completo con margen para ver el entorno, sin acercarse en exceso.
      if (currentLayer) map.fitBounds(layerBounds(currentLayer), { padding: [56, 56], maxZoom: 17 });
      if (perimeterAcceptedInput) perimeterAcceptedInput.value = '';
      setPerimeterControls(true, false);
      showPerimeterConfirmation();
    } else {
      setStatus('Listo. Revisa las hectáreas y guarda tu potrero.');
    }
  }

  function finishLineMeasurement() {
    if (!drawing || drawingMode !== 'line' || drawPoints.length < 2) {
      setStatus('Marca al menos dos puntos para medir una línea.');
      return;
    }
    if (previewLine) {
      savedMeasurement = previewLine;
      previewLine = null;
      document.getElementById('clear-measurement').hidden = false;
    }
    const length = perimeterMetres(drawPoints, false);
    stopDrawing();
    const areaMessage = document.getElementById('live-area');
    if (areaMessage) areaMessage.textContent = 'Medición lineal terminada. Esta línea no cambia el perímetro guardado.';
    document.getElementById('live-perimeter').textContent = 'Longitud medida: ' + formatLength(length);
    setStatus('Medición lista. La línea es temporal y no modifica tu predio guardado.');
  }

  function clearLineMeasurement() {
    if (savedMeasurement) map.removeLayer(savedMeasurement);
    savedMeasurement = null;
    document.getElementById('clear-measurement').hidden = true;
    if (currentLayer) setAreaAndGeometry();
    else {
      document.getElementById('live-perimeter').textContent = '';
      document.getElementById('live-area').textContent = '';
      updateFormMapSummary([]);
    }
    setStatus('Medición lineal eliminada. El perímetro guardado no cambió.');
  }

  function clearPerimeter() {
    if (currentLayer && !window.confirm('¿Quitar el perímetro guardado? Podrás indicar una superficie manual.')) return;
    if (drawing) stopDrawing();
    if (currentLayer) map.removeLayer(currentLayer);
    currentLayer = null;
    if (geojsonInput) geojsonInput.value = '';
    if (legacyPerimeterInput) legacyPerimeterInput.value = '[]';
    if (areaInput) areaInput.readOnly = false;
    if (latitudeInput) latitudeInput.value = '';
    if (longitudeInput) longitudeInput.value = '';
    if (areaInput) areaInput.value = '';
    if (marker) map.removeLayer(marker);
    marker = null;
    document.getElementById('live-area').textContent = '';
    const perimeterOutput = document.getElementById('live-perimeter');
    if (perimeterOutput) perimeterOutput.textContent = '';
    document.getElementById('toggle-edit').hidden = true;
    setStatus('Contorno borrado. Escribe las hectáreas o dibuja de nuevo.');
  }

  function cancelDrawing() {
    if (!drawing) return;
    stopDrawing();
    setStatus('Dibujo cancelado. El perímetro guardado permanece sin cambios.');
    if (currentLayer) setAreaAndGeometry();
    else {
      document.getElementById('live-area').textContent = '';
      document.getElementById('live-perimeter').textContent = '';
    }
  }

  function addContextLayers() {
    // Formulario de predio (nuevo o edición): no se dibujan otros predios ni potreros.
    // Al editar, la única geometría visible es la del propio predio, que llega por el campo geojson del formulario.
    if (!form || isPredioForm) return;
    const predioId = document.getElementById('id_predio');
    const activeFarmMatch = window.location.pathname.match(/predios\/(\d+)\//);
    const activeFarmId = activeFarmMatch ? activeFarmMatch[1] : '';
    const editingFarm = /predios\/\d+\/editar/.test(window.location.pathname);
    const activePlotId = window.location.pathname.match(/potreros\/(\d+)\/editar/);
    const activeId = activePlotId ? activePlotId[1] : '';
    const activePredioId = predioId ? predioId.value : isPlotForm ? activeFarmId : '';
    parentFarm = isPlotForm ? farmData.predios.find((farm) => String(farm.id) === activePredioId) || null : null;
    farmData.predios.forEach((farm) => {
      if (editingFarm && String(farm.id) === activeFarmId) return;
      if (farm.geojson && (!activePredioId || String(farm.id) === activePredioId)) {
        const layer = L.geoJSON(farm.geojson, {
          style:
            isPlotForm && String(farm.id) === activePredioId
              ? { color: '#18713c', weight: 5, fillColor: '#64a76d', fillOpacity: 0.1, dashArray: '8 5' }
              : { color: '#496a8a', weight: 2, fillColor: '#8daac4', fillOpacity: 0.08 },
          interactive: !form,
        }).addTo(map);
        if (isPlotForm && String(farm.id) === activePredioId) parentFarmLayer = layer;
        if (!form) layer.on('click', () => setSelectedTerrain(farm, null));
        viewBounds.extend(layer.getBounds());
      } else if (isPlotForm && String(farm.id) === activePredioId && farm.lat !== null && farm.lng !== null) {
        parentFarmLayer = L.marker([farm.lat, farm.lng]).addTo(map);
        viewBounds.extend(parentFarmLayer.getLatLng());
      }
    });
    farmData.plots.forEach((plot) => {
      if (activeId && String(plot.id) === activeId) return;
      if (activePredioId && String(plot.predio) !== activePredioId) return;
      if (plot.geojson) {
        const layer = L.geoJSON(plot.geojson, {
          style: { color: '#7d8c78', weight: 2, fillColor: '#aebda5', fillOpacity: 0.08 },
        }).addTo(map);
        viewBounds.extend(layer.getBounds());
      }
    });
  }

  const FARM_STYLE = { color: '#3d6e8c', weight: 2, dashArray: '6 4', fillColor: '#8daac4', fillOpacity: 0.06 };
  function plotStyle(plot) {
    return plot && plot.crop
      ? { color: '#2f6b3f', weight: 2, fillColor: '#7cb07a', fillOpacity: 0.32 }
      : { color: '#6f7d69', weight: 2, fillColor: '#c4cfbd', fillOpacity: 0.22 };
  }

  function resetTerrainStyles() {
    farmLayers.forEach((layer) => {
      if (layer.setStyle) layer.setStyle(FARM_STYLE);
      else if (layer.setOpacity) layer.setOpacity(0.8);
    });
    plotLayers.forEach((layer, id) => {
      const plot = farmData.plots.find((item) => String(item.id) === String(id));
      if (layer.setStyle) layer.setStyle(plotStyle(plot));
      else if (layer.setOpacity) layer.setOpacity(0.8);
    });
  }

  function layerBounds(layer) {
    if (layer.getBounds) return layer.getBounds();
    const bounds = L.latLngBounds([]);
    if (layer.getLatLng) bounds.extend(layer.getLatLng());
    return bounds;
  }

  function selectFarm(farm, includePlots) {
    if (!farm) return;
    selectedFarmId = String(farm.id);
    selectedPlotId = '';
    resetTerrainStyles();
    const farmLayer = farmLayers.get(selectedFarmId);
    if (farmLayer) {
      if (farmLayer.setStyle) {
        farmLayer.setStyle({ color: '#1e3d28', weight: 4, dashArray: null, fillColor: '#74a567', fillOpacity: 0.12 });
      } else if (farmLayer.setOpacity) farmLayer.setOpacity(1);
      map.fitBounds(layerBounds(farmLayer), { padding: [28, 28], maxZoom: 17 });
    } else if (map && farm.lat !== null && farm.lng !== null) {
      map.setView([farm.lat, farm.lng], 16);
    }
    const farmPlots = farmData.plots.filter((plot) => String(plot.predio) === selectedFarmId);
    if (includePlots) {
      const plotBounds = window.L ? L.latLngBounds([]) : null;
      farmPlots.forEach((plot) => {
        const layer = plotLayers.get(String(plot.id));
        if (layer) {
          if (layer.setStyle) {
            layer.setStyle({ color: '#255b37', weight: 3, fillColor: '#74a567', fillOpacity: 0.28 });
          } else if (layer.setOpacity) layer.setOpacity(1);
          if (plotBounds) plotBounds.extend(layerBounds(layer));
        }
      });
      if (map && plotBounds.isValid()) map.fitBounds(plotBounds, { padding: [28, 28], maxZoom: 17 });
      setStatus(
        farmPlots.length
          ? 'Mostrando los potreros registrados en «' + farm.name + '».'
          : 'Este predio todavía no tiene potreros registrados.',
      );
    } else {
      setStatus(
        farmPlots.length
          ? 'Predio seleccionado. Elige uno de sus potreros o consulta sus datos.'
          : 'Este predio todavía no tiene potreros registrados. Puedes registrar el primero desde el panel.',
      );
    }
    setSelectedTerrain(farm, null);
  }

  function selectPlot(plot) {
    const farm = farmData.predios.find((item) => String(item.id) === String(plot.predio));
    if (!farm) return;
    selectedFarmId = String(farm.id);
    selectedPlotId = String(plot.id);
    resetTerrainStyles();
    const layer = plotLayers.get(selectedPlotId);
    if (layer) {
      if (layer.setStyle) {
        layer.setStyle({ color: '#c9a227', weight: 5, fillColor: '#55a85c', fillOpacity: 0.5 });
        if (layer.bringToFront) layer.bringToFront();
      } else if (layer.setOpacity) layer.setOpacity(1);
      map.fitBounds(layerBounds(layer), { padding: [32, 32], maxZoom: 18 });
    } else if (map && plot.lat !== null && plot.lng !== null) {
      map.setView([plot.lat, plot.lng], 17);
    } else {
      const farmLayer = farmLayers.get(selectedFarmId);
      if (farmLayer) map.fitBounds(layerBounds(farmLayer), { padding: [28, 28], maxZoom: 17 });
    }
    setStatus('Potrero «' + plot.name + '» seleccionado en «' + farm.name + '».');
    setSelectedTerrain(farm, plot);
  }

  function showAllFarms() {
    selectedFarmId = '';
    selectedPlotId = '';
    resetTerrainStyles();
    document.querySelectorAll('[data-select-farm]').forEach((button) => {
      button.classList.remove('is-selected');
      button.setAttribute('aria-pressed', 'false');
    });
    document.querySelectorAll('.farm-card.is-selected').forEach((card) => card.classList.remove('is-selected'));
    document.querySelectorAll('[data-farm-plots]').forEach((group) => {
      group.hidden = true;
    });
    document.querySelectorAll('[data-plot-card]').forEach((card) => {
      card.classList.remove('is-selected');
    });
    document.querySelectorAll('[data-select-plot]').forEach((button) => {
      button.classList.remove('is-selected');
      button.setAttribute('aria-pressed', 'false');
    });
    const plotsPanel = document.getElementById('farm-plots-picker');
    if (plotsPanel) plotsPanel.hidden = true;
    const details = document.getElementById('selected-terrain-details');
    const empty = document.getElementById('selected-terrain-empty');
    if (details) details.hidden = true;
    if (farmData.predios.length) renderOverview();
    else if (empty) {
      empty.hidden = false;
      empty.textContent = farmData.predios.length
        ? farmData.plots.length
          ? 'Selecciona un predio o potrero en el listado o en el mapa.'
          : 'Selecciona un predio para registrar y administrar sus potreros.'
        : 'No tienes predios registrados.';
    }
    setStatus(
      farmData.predios.length
        ? farmData.plots.length
          ? 'Selecciona un potrero en el listado o directamente en el mapa.'
          : 'Selecciona un predio para registrar y administrar sus potreros.'
        : 'No tienes predios registrados.',
    );
    const showPlotsButton = document.getElementById('show-selected-farm-plots');
    if (showPlotsButton) showPlotsButton.disabled = true;
    if (map && viewBounds.isValid()) map.fitBounds(viewBounds, { padding: [24, 24], maxZoom: 16 });
    else if (map) map.setView(melipillaCenter, 11);
    const params = new URLSearchParams(window.location.search);
    params.delete('predio');
    params.delete('potrero');
    window.history.replaceState(
      null,
      '',
      window.location.pathname + (params.toString() ? '?' + params.toString() : ''),
    );
  }

  // Deja en blanco todo dato temporal de dibujo antes de empezar un predio nuevo
  // (también frente a valores que el navegador restaure al volver atrás o recargar).
  function resetNewPredioState() {
    form.reset();
    [areaInput, latitudeInput, longitudeInput, geojsonInput, perimeterAcceptedInput].forEach((input) => {
      if (input) input.value = '';
    });
    if (legacyPerimeterInput) legacyPerimeterInput.value = '[]';
    if (areaInput) areaInput.readOnly = true;
    originalFeature = null;
    currentLayer = null;
    marker = null;
    if (mapSummary) mapSummary.hidden = true;
  }

  function loadFormGeometry() {
    if (isPlotForm) {
      try {
        const points = JSON.parse(legacyPerimeterInput.value || '[]');
        if (Array.isArray(points) && points.length >= 3) {
          addCurrentLayer(
            {
              type: 'Feature',
              properties: {},
              geometry: {
                type: 'Polygon',
                coordinates: [[...points.map((point) => point.slice()), points[0].slice()]],
              },
            },
            false,
          );
        }
      } catch (error) {
        setStatus('No se pudo cargar el perímetro existente. Puedes corregirlo dibujando uno nuevo.');
        console.error('No se pudo interpretar el perímetro del potrero.', error);
      }
    } else if (geojsonInput && geojsonInput.value && geojsonInput.value !== 'null') {
      try {
        const geometry = JSON.parse(geojsonInput.value);
        originalFeature = JSON.parse(JSON.stringify(geometry));
        addCurrentLayer(geometry, false);
      } catch (error) {
        setStatus('No se pudo cargar el perímetro GeoJSON existente. Corrige el registro o vuelve a dibujarlo.');
        console.error('No se pudo interpretar el perímetro del predio.', error);
      }
    }
    if (!currentLayer && latitudeInput?.value && longitudeInput?.value) {
      marker = L.marker([Number(latitudeInput.value), Number(longitudeInput.value)], { draggable: true }).addTo(map);
      marker.on('dragend', (event) => {
        latitudeInput.value = event.target.getLatLng().lat.toFixed(6);
        longitudeInput.value = event.target.getLatLng().lng.toFixed(6);
        updateFormMapSummary([]);
      });
      updateFormMapSummary([]);
      viewBounds.extend(marker.getLatLng());
    }
  }

  function showMap() {
    viewBounds = L.latLngBounds([]);
    map = L.map(mapElement, { zoomControl: true }).setView(
      melipillaCenter,
      isCreatingPredio ? NEW_PREDIO_ZOOM : form ? 14 : 11,
    );
    configureTiles();
    addContextLayers();
    if (!form) {
      farmData.predios.forEach((farm) => {
        if (farm.geojson) {
          const layer = L.geoJSON(farm.geojson, { style: FARM_STYLE }).addTo(map);
          layer.on('click', () => selectFarm(farm, false));
          farmLayers.set(String(farm.id), layer);
          viewBounds.extend(layer.getBounds());
        } else if (farm.lat !== null && farm.lng !== null) {
          const pin = L.marker([farm.lat, farm.lng]).addTo(map);
          pin.on('click', () => selectFarm(farm, false));
          farmLayers.set(String(farm.id), pin);
          viewBounds.extend(pin.getLatLng());
        }
      });
      farmData.plots.forEach((plot) => {
        const predio = farmData.predios.find((item) => String(item.id) === String(plot.predio));
        const fallbackFeature =
          Array.isArray(plot.coordinates) && plot.coordinates.length >= 3
            ? {
                type: 'Feature',
                properties: {},
                geometry: { type: 'Polygon', coordinates: [[...plot.coordinates, plot.coordinates[0]]] },
              }
            : null;
        const feature = plot.geojson || fallbackFeature;
        if (feature) {
          const layer = L.geoJSON(feature, { style: plotStyle(plot) }).addTo(map);
          layer.on('click', (event) => {
            if (event && event.originalEvent) L.DomEvent.stopPropagation(event);
            selectPlot(plot);
          });
          layer.bindTooltip(plotLabel(plot), {
            permanent: true,
            direction: 'center',
            className: 'plot-label',
            opacity: 1,
          });
          plotLayers.set(String(plot.id), layer);
          viewBounds.extend(layer.getBounds());
        } else if (plot.lat !== null && plot.lng !== null) {
          const pin = L.marker([plot.lat, plot.lng]).addTo(map);
          pin.bindTooltip(plotLabel(plot), {
            permanent: true,
            direction: 'top',
            className: 'plot-label',
            offset: [0, -20],
          });
          pin.on('click', () => selectPlot(plot));
          plotLayers.set(String(plot.id), pin);
          viewBounds.extend(pin.getLatLng());
        }
      });
      if (!farmData.predios.length) setStatus('No tienes predios registrados.');
      else if (!farmData.plots.length) setStatus('Selecciona un predio para registrar y administrar sus potreros.');
      else setStatus('Selecciona un potrero en el listado o directamente en el mapa.');
      const syncLabels = () => mapElement.classList.toggle('hide-plot-labels', map.getZoom() < 15);
      map.on('zoomend', syncLabels);
      syncLabels();
      bindListControls();
    } else {
      if (isCreatingPredio && form.hidden) resetNewPredioState();
      loadFormGeometry();
      map.on('click', addDrawingPoint);
      document.getElementById('draw-perimeter').addEventListener('click', () => beginDrawing('polygon'));
      document.getElementById('measure-line').addEventListener('click', () => beginDrawing('line'));
      document.getElementById('cancel-drawing').addEventListener('click', cancelDrawing);
      document.getElementById('finish-line').addEventListener('click', finishLineMeasurement);
      document.getElementById('clear-measurement').addEventListener('click', clearLineMeasurement);
      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') cancelDrawing();
      });
      document.getElementById('undo-point').addEventListener('click', () => {
        if (!drawing || !drawPoints.length) return;
        drawPoints.pop();
        const markerToRemove = drawMarkers.pop();
        if (markerToRemove) map.removeLayer(markerToRemove);
        if (previewLine) map.removeLayer(previewLine);
        previewLine = drawPoints.length
          ? L.polyline(
              drawPoints.map((point) => [point[1], point[0]]),
              {
                color: '#255b37',
                dashArray: '6 5',
              },
            ).addTo(map)
          : null;
        document.getElementById('undo-point').disabled = drawPoints.length === 0;
        document.getElementById('finish-perimeter').disabled = drawPoints.length < 3;
        document.getElementById('finish-line').disabled = drawPoints.length < 2;
        if (drawPoints.length >= 2) {
          updateMeasurements(drawPoints, drawingMode === 'polygon' && drawPoints.length >= 3);
        } else {
          const perimeterOutput = document.getElementById('live-perimeter');
          if (perimeterOutput) perimeterOutput.textContent = '';
          document.getElementById('live-area').textContent = '';
        }
      });
      document.getElementById('finish-perimeter').addEventListener('click', finishDrawing);
      const clearButton = document.getElementById('clear-perimeter');
      if (clearButton) clearButton.addEventListener('click', clearPerimeter);
      const editButton = document.getElementById(isPredioForm ? 'edit-perimeter' : 'toggle-edit');
      if (editButton) editButton.addEventListener('click', enableEditing);
      const acceptButton = document.getElementById('accept-perimeter');
      if (acceptButton) acceptButton.addEventListener('click', showPerimeterConfirmation);
      const cancelPerimeterButton = document.getElementById('cancel-perimeter');
      if (cancelPerimeterButton) cancelPerimeterButton.addEventListener('click', cancelPerimeterChanges);
      setRegistrationState(form && !form.hidden ? 'data' : 'draw');
      if (isPredioForm && form) form.addEventListener('submit', () => setRegistrationState('save'));
      const continueButton = document.getElementById('confirm-and-continue');
      if (continueButton) continueButton.addEventListener('click', continueAfterConfirmation);
      const returnButton = document.getElementById('return-to-edit');
      if (returnButton) returnButton.addEventListener('click', editConfirmedPerimeter);
      document.getElementById('locate-me').addEventListener('click', () => {
        if (!navigator.geolocation) {
          setStatus('Este navegador no permite obtener la ubicación. Marca el punto manualmente en el mapa.');
          return;
        }
        navigator.geolocation.getCurrentPosition(
          (position) => {
            const point = L.latLng(position.coords.latitude, position.coords.longitude);
            if (marker) map.removeLayer(marker);
            marker = L.marker(point, { draggable: true }).addTo(map);
            latitudeInput.value = point.lat.toFixed(6);
            longitudeInput.value = point.lng.toFixed(6);
            marker.on('dragend', (event) => {
              latitudeInput.value = event.target.getLatLng().lat.toFixed(6);
              longitudeInput.value = event.target.getLatLng().lng.toFixed(6);
              updateFormMapSummary([]);
            });
            updateFormMapSummary([]);
            map.setView(point, MY_LOCATION_ZOOM);
            setStatus('Ubicación detectada. Puedes ajustar el marcador y guardar.');
          },
          () => {
            setStatus('No fue posible obtener tu ubicación. Marca el punto manualmente en el mapa.');
          },
          { timeout: 10000 },
        );
      });
      document.getElementById('center-map').addEventListener('click', () => {
        if (isPlotForm && parentFarmLayer) {
          map.fitBounds(layerBounds(parentFarmLayer), { padding: [42, 42], maxZoom: 18 });
        } else if (viewBounds.isValid()) map.fitBounds(viewBounds, { padding: [24, 24], maxZoom: 17 });
        else map.setView(melipillaCenter, 12);
      });
      form.addEventListener('submit', (event) => {
        if (drawing) {
          event.preventDefault();
          setStatus('Finaliza o cancela el dibujo antes de guardar.');
        }
      });
      if (window.L && L.PM) {
        L.PM.setOptIn(true);
      }
      if (isPlotForm && parentFarm?.geojson) {
        setStatus('Opcional: dibuja tu potrero dentro del límite verde, o escribe las hectáreas.');
      } else if (isPlotForm) {
        setStatus('Opcional: dibuja tu potrero en el mapa, o escribe las hectáreas.');
      } else {
        setStatus('Toca «Dibujar mi campo» y marca las esquinas de tu predio.');
      }
    }

    if (isPlotForm && parentFarmLayer && mapElement.dataset.focusParentOnly !== 'false') {
      map.fitBounds(layerBounds(parentFarmLayer), { padding: [42, 42], maxZoom: 18 });
    } else if (viewBounds.isValid()) map.fitBounds(viewBounds, { padding: [24, 24], maxZoom: 17 });
    window.setTimeout(() => map.invalidateSize(), 0);
    // Si el contenedor cambia de tamaño (barra de desplazamiento, giro del celular, otro paso), el mapa se reajusta solo.
    if (window.ResizeObserver) {
      let resizeFrame = null;
      new ResizeObserver(() => {
        if (resizeFrame) return;
        resizeFrame = window.requestAnimationFrame(() => {
          resizeFrame = null;
          map.invalidateSize({ animate: false, pan: false });
        });
      }).observe(mapElement);
    }
    if (!form) selectInitialTerrain();
  }

  function plotLabel(plot) {
    return plot.name + (plot.crop ? ' · ' + plot.crop : '');
  }

  function renderOverview() {
    const details = document.getElementById('selected-terrain-details');
    const emptyState = document.getElementById('selected-terrain-empty');
    if (!details || !emptyState || !farmData.predios.length) return;
    details.replaceChildren();
    const stats = farmData.stats || {};
    const head = el('div', 'terrain-panel-head');
    head.append(el('span', 'terrain-eyebrow', 'Resumen de tu campo'), el('h3', '', 'Todos los predios'));
    details.append(head);
    const kpis = el('div', 'overview-kpis');
    [
      ['Predios', stats.predios],
      ['Potreros', stats.potreros],
      ['Superficie', fmtHa(stats.superficie_predios, 2) + ' ha'],
      ['Con cultivo', farmData.plots.filter((plot) => plot.crop).length],
    ].forEach(([label, value]) => {
      const box = el('div', 'overview-kpi');
      box.append(el('span', '', label), el('strong', '', String(value ?? 0)));
      kpis.append(box);
    });
    details.append(kpis);
    if (farmData.plots.length) {
      details.append(el('p', 'overview-hint', 'Selecciona un potrero para ver su ficha y asignar cultivo:'));
      const list = el('ul', 'overview-plots');
      farmData.plots.forEach((plot) => {
        const item = el('li');
        const button = el('button', 'overview-plot');
        button.type = 'button';
        const swatch = el('span', 'overview-swatch' + (plot.crop ? ' has-crop' : ''));
        const text = el('span', 'overview-text');
        text.append(
          el('strong', '', plot.name),
          el('small', '', plot.farmName + ' · ' + fmtHa(plot.areaHa, 2) + ' ha · ' + (plot.crop || 'sin cultivo')),
        );
        button.append(swatch, text, el('span', 'overview-status', plot.status || ''));
        button.addEventListener('click', () => selectPlot(plot));
        item.append(button);
        list.append(item);
      });
      details.append(list);
    } else {
      details.append(el('p', 'terrain-no-crop', 'Aún no tienes potreros. Selecciona un predio y registra el primero.'));
    }
    emptyState.hidden = true;
    details.hidden = false;
  }

  function selectInitialTerrain() {
    const initialPlot = farmData.plots.find((item) => String(item.id) === mapElement.dataset.initialPlot);
    if (initialPlot) {
      selectPlot(initialPlot);
      return;
    }
    const initialFarm = farmData.predios.find((item) => String(item.id) === mapElement.dataset.initialFarm);
    if (initialFarm) selectFarm(initialFarm, false);
    else renderOverview();
  }

  function bindListControls() {
    document.querySelectorAll('[data-select-farm]').forEach((button) => {
      button.addEventListener('click', () => {
        const farm = farmData.predios.find((item) => String(item.id) === button.dataset.selectFarm);
        selectFarm(farm, false);
      });
    });
    document.querySelectorAll('[data-select-plot]').forEach((button) => {
      button.addEventListener('click', () => {
        const plot = farmData.plots.find((item) => String(item.id) === button.dataset.selectPlot);
        if (plot) selectPlot(plot);
      });
    });
    document.addEventListener('click', (event) => {
      if (!event.target.closest('[data-focus-map]')) return;
      const mapCard = document.querySelector('.map-card');
      if (mapCard) mapCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    document.getElementById('show-all-farms')?.addEventListener('click', showAllFarms);
    document.getElementById('show-selected-farm-plots')?.addEventListener('click', () => {
      const farm = farmData.predios.find((item) => String(item.id) === selectedFarmId);
      if (farm) selectFarm(farm, true);
    });
    document.addEventListener('click', (event) => {
      const trigger = event.target.closest('[data-assign-crop]');
      if (!trigger) return;
      const plot = farmData.plots.find((item) => String(item.id) === trigger.dataset.assignCrop);
      if (plot) openCropDialog(plot);
    });
    setupCropDialog();
  }

  // ---------- Asignar cultivo por temporada ----------
  const cropDialog = document.getElementById('crop-dialog');
  let dialogPlot = null;

  function seasonOptions(selected) {
    const today = new Date();
    const start = today.getMonth() >= 6 ? today.getFullYear() : today.getFullYear() - 1;
    const values = [start - 1, start, start + 1, start + 2].map((year) => year + '-' + (year + 1));
    if (selected && !values.includes(selected)) values.unshift(selected);
    return { values, current: start + '-' + (start + 1) };
  }

  function catalogCrops() {
    const node = document.getElementById('agro-catalog');
    try {
      return JSON.parse(node.textContent) || {};
    } catch (error) {
      return {};
    }
  }

  function setupCropDialog() {
    if (!cropDialog || cropDialog.dataset.ready) return;
    cropDialog.dataset.ready = '1';
    const catalog = catalogCrops();
    const crops = (catalog.cultivos || []).filter((crop) => crop.active !== false);
    const select = document.getElementById('crop-select');
    const datalist = document.getElementById('crop-variety-options');
    select.replaceChildren(new Option(crops.length ? 'Selecciona un cultivo' : 'Sin cultivos en el catálogo', ''));
    const groups = new Map();
    crops.forEach((crop) => {
      const label = crop.group || 'Otros cultivos';
      if (!groups.has(label)) groups.set(label, []);
      groups.get(label).push(crop);
    });
    groups.forEach((items, label) => {
      const group = document.createElement('optgroup');
      group.label = label;
      items.forEach((crop) => group.append(new Option(crop.name, String(crop.id))));
      select.append(group);
    });
    const refreshVarieties = () => {
      datalist.replaceChildren();
      ((catalog.varieties || {})[select.value] || []).forEach((variety) =>
        datalist.append(new Option(variety, variety)),
      );
    };
    select.addEventListener('change', refreshVarieties);
    document.getElementById('crop-empty-catalog').hidden = crops.length > 0;
    document.getElementById('crop-fields').hidden = crops.length === 0;
    document.getElementById('crop-save').disabled = crops.length === 0;
    cropDialog.querySelectorAll('[data-close-dialog]').forEach((button) => {
      button.addEventListener('click', () => cropDialog.close());
    });
    document.getElementById('crop-sowing').addEventListener('change', (event) => {
      const value = event.target.value;
      if (!value) return;
      const [year, month] = value.split('-').map(Number);
      const start = month >= 7 ? year : year - 1;
      const label = start + '-' + (start + 1);
      const seasonSelect = document.getElementById('crop-season');
      if (![...seasonSelect.options].some((option) => option.value === label))
        seasonSelect.add(new Option(label, label));
      seasonSelect.value = label;
    });
    document.getElementById('crop-form').addEventListener('submit', saveCropAssignment);
    cropDialog._refreshVarieties = refreshVarieties;
  }

  function openCropDialog(plot) {
    if (!cropDialog) return;
    setupCropDialog();
    dialogPlot = plot;
    const farm = farmData.predios.find((item) => String(item.id) === String(plot.predio));
    document.getElementById('crop-dialog-title').textContent = plot.crop ? 'Cambiar cultivo' : 'Asignar cultivo';
    document.getElementById('crop-farm').textContent = farm ? farm.name : '—';
    document.getElementById('crop-current').textContent = plot.crop
      ? plot.crop + (plot.variety ? ' · ' + plot.variety : '') + (plot.season ? ' · ' + plot.season : '')
      : 'Sin cultivo asignado';
    const select = document.getElementById('crop-select');
    select.value = '';
    document.getElementById('crop-variety').value = '';
    document.getElementById('crop-sowing').value = '';
    document.getElementById('crop-harvest').value = '';
    document.getElementById('crop-notes').value = '';
    document.getElementById('crop-status').value = 'planned';
    document.getElementById('crop-error').textContent = '';
    const seasons = seasonOptions();
    const seasonSelect = document.getElementById('crop-season');
    seasonSelect.replaceChildren(...seasons.values.map((value) => new Option(value, value)));
    seasonSelect.value = seasons.current;
    const closePrevious = document.getElementById('crop-close-previous');
    closePrevious.checked = true;
    closePrevious.parentElement.hidden = !(plot.seasons || []).length && !plot.crop;
    cropDialog._refreshVarieties();
    if (typeof cropDialog.showModal === 'function') cropDialog.showModal();
    else cropDialog.setAttribute('open', '');
    select.focus();
  }

  async function saveCropAssignment(event) {
    event.preventDefault();
    const errorBox = document.getElementById('crop-error');
    const cropId = document.getElementById('crop-select').value;
    const sowing = document.getElementById('crop-sowing').value;
    const harvest = document.getElementById('crop-harvest').value;
    const status = document.getElementById('crop-status').value;
    errorBox.textContent = '';
    if (!cropId) {
      errorBox.textContent = 'Selecciona un cultivo del catálogo.';
      return;
    }
    if (sowing && harvest && harvest < sowing) {
      errorBox.textContent = 'La fecha de cosecha no puede ser anterior a la siembra.';
      return;
    }
    if (status === 'sown' && !sowing) {
      errorBox.textContent = 'Indica la fecha de siembra para marcar el cultivo como sembrado.';
      return;
    }
    if (status === 'harvested' && !harvest) {
      errorBox.textContent = 'Indica la fecha de cosecha para marcarlo como cosechado.';
      return;
    }
    const button = document.getElementById('crop-save');
    button.disabled = true;
    try {
      const token = cropDialog.querySelector('[name=csrfmiddlewaretoken]').value;
      const response = await fetch(dialogPlot.assignUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token, Accept: 'application/json' },
        body: JSON.stringify({
          mode: 'new',
          crop_id: cropId,
          variety: document.getElementById('crop-variety').value,
          season: document.getElementById('crop-season').value,
          sowing_date: sowing || null,
          harvest_date: harvest || null,
          status,
          notes: document.getElementById('crop-notes').value,
          close_previous: document.getElementById('crop-close-previous').checked,
        }),
      });
      if (!(response.headers.get('Content-Type') || '').includes('application/json')) {
        throw new Error('Tu sesión expiró. Recarga la página e inicia sesión nuevamente.');
      }
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || 'No se pudo guardar el cultivo.');
      applyPlotUpdate(data.plot);
      cropDialog.close();
      setStatus(data.message);
    } catch (error) {
      errorBox.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  }

  function refreshPlotState(plot) {
    // Actualiza chips y barra de planificación de la tarjeta sin recargar la página.
    const steps = [Boolean(plot.crop), Boolean(plot.sowing), Boolean(plot.harvest)];
    plot.planDone = steps.filter(Boolean).length;
    plot.planTotal = 3;
    const complete = plot.planDone === 3;
    document.querySelectorAll('[data-plot-state="' + plot.id + '"]').forEach((row) => {
      row.replaceChildren(stateChip(plot.crop ? 'Con cultivo' : 'Sin cultivo', plot.crop ? 'is-green' : 'is-neutral'));
      if (complete) row.appendChild(stateChip('Planificación completa', 'is-dark'));
      else if (!plot.crop) row.appendChild(stateChip('Sin planificar', 'is-neutral'));
      if (plot.overdueTasks) row.appendChild(stateChip('Labores vencidas · ' + plot.overdueTasks, 'is-red'));
      else if (plot.pendingTasks) row.appendChild(stateChip('Labores pendientes · ' + plot.pendingTasks, 'is-amber'));
    });
    document.querySelectorAll('[data-plot-plan="' + plot.id + '"]').forEach((meter) => {
      meter.hidden = !plot.crop;
      meter.querySelectorAll('.plan-meter-bar i').forEach((bar, index) => bar.classList.toggle('on', steps[index]));
      const strong = meter.querySelector('strong');
      if (strong) strong.textContent = plot.planDone + ' de 3';
    });
  }

  function applyPlotUpdate(updated) {
    const index = farmData.plots.findIndex((item) => String(item.id) === String(updated.id));
    if (index === -1) return;
    farmData.plots[index] = { ...farmData.plots[index], ...updated };
    const plot = farmData.plots[index];
    const layer = plotLayers.get(String(plot.id));
    if (layer && layer.setTooltipContent) layer.setTooltipContent(plotLabel(plot));
    document.querySelectorAll('[data-plot-crop-label="' + plot.id + '"]').forEach((node) => {
      node.textContent = plot.crop + (plot.variety ? ' · ' + plot.variety : '');
    });
    document.querySelectorAll('[data-plot-status="' + plot.id + '"]').forEach((node) => {
      node.textContent = plot.status;
    });
    document.querySelectorAll('[data-assign-crop="' + plot.id + '"]').forEach((node) => {
      if (node.tagName !== 'BUTTON') return;
      // La acción rápida desaparece al tener cultivo; en el menú el rótulo pasa a «Cambiar cultivo».
      if (node.hasAttribute('data-quick')) {
        node.hidden = true;
        return;
      }
      const label = node.querySelector('[data-assign-label]');
      if (label) label.textContent = 'Cambiar cultivo';
      else node.textContent = 'Cambiar cultivo';
    });
    const farmOfPlot = farmData.predios.find((item) => String(item.id) === String(plot.predio));
    document.querySelectorAll('[data-plot-planning="' + plot.id + '"]').forEach((node) => {
      if (farmOfPlot) node.href = plotActionUrl('calendar', plot, farmOfPlot);
      node.hidden = !plot.crop;
    });
    refreshPlotState(plot);
    selectPlot(plot);
  }

  if (!window.L) {
    if (!form) {
      // Sin cartografía el listado, el panel y la asignación de cultivos siguen funcionando.
      setStatus(
        'No se pudo cargar el mapa (revisa la conexión). Puedes seleccionar predios y potreros desde el listado.',
      );
      bindListControls();
      selectInitialTerrain();
      return;
    }
    setStatus(
      'No se pudo cargar Leaflet. Comprueba la conexión y recarga el mapa; también puedes ingresar la superficie y coordenadas manualmente.',
    );
    [
      'draw-perimeter',
      'undo-point',
      'finish-perimeter',
      'toggle-edit',
      'clear-perimeter',
      'locate-me',
      'center-map',
    ].forEach((id) => {
      const button = document.getElementById(id);
      if (button) button.disabled = true;
    });
    return;
  }
  showMap();
  // Al volver con «atrás/adelante» el navegador puede restaurar la página desde memoria: se recarga para empezar limpio.
  if (isCreatingPredio) {
    window.addEventListener('pageshow', (event) => {
      if (event.persisted) window.location.reload();
    });
  }
})();

// Editar/registrar potrero: «Más herramientas» es un menú flotante sobre el mapa.
// Se cierra al tocar fuera, con Escape o al elegir una herramienta; no toca la lógica del mapa.
(() => {
  const menu = document.querySelector('.potrero-form .more-tools');
  if (!menu) return;
  const summary = menu.querySelector('summary');
  document.addEventListener('click', (event) => {
    if (!menu.open || (summary && summary.contains(event.target))) return;
    const choseTool = menu.contains(event.target) && Boolean(event.target.closest('button'));
    if (!menu.contains(event.target) || choseTool) menu.open = false;
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !menu.open) return;
    menu.open = false;
    if (summary) summary.focus();
  });
})();
