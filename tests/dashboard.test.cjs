// Pruebas del JavaScript real sin dependencias de navegador.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function browser() {
  const elements = new Map();
  const images = [];
  const intervals = new Map();
  let intervalId = 0;
  class Element {
    constructor(tag = 'div') {
      this.tag = tag; this.style = {}; this.dataset = {}; this.children = []; this.textContent = '';
      const classes = new Set();
      this.classList = {add: name => classes.add(name), remove: name => classes.delete(name)};
      this.listeners = {};
    }
    appendChild(child) { this.children.push(child); return child; }
    replaceChildren(...children) { this.children = children; }
    querySelector(selector) { return selector === 'img' ? this.children.find(c => c.tag === 'img') : null; }
    removeAttribute(key) { delete this[key]; }
    addEventListener(event, listener) { this.listeners[event] = listener; }
    setAttribute(key, value) { this[key] = value; }
    getBoundingClientRect() { return {width:500}; }
    getContext() { return new Proxy({}, {get: (_, key) => key === 'createLinearGradient' ? () => ({addColorStop(){}}) : () => {}}); }
  }
  const context = vm.createContext({console, Date, Math, JSON, Object, Number, String, Array, Set, Promise,
    document:{getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
      createElement: tag => new Element(tag), body:new Element(), addEventListener(){}},
    localStorage:{getItem(){return 'off';}},
    window:{addEventListener(){}, devicePixelRatio:1},
    getComputedStyle(){return {fontFamily:'monospace'};},
    setInterval(callback, delay) {
      intervals.set(++intervalId, {callback, delay});
      return intervalId;
    },
    clearInterval(id) { intervals.delete(id); }, setTimeout(){},
    Image: class extends Element { constructor() { super('img'); images.push(this); } },
    location:{protocol:'http:', host:'localhost', reload(){}},
    WebSocket:class {constructor(){context.socket = this;}},
    fetch(){throw new Error('El guiado no debe lanzar peticiones HTTP');}
  });
  const source = fs.readFileSync('frontend/dashboard.js','utf8').split('init().catch')[0];
  vm.runInContext(source, context);
  return {context, images, elements, intervals, run:code => vm.runInContext(code,context)};
}

test('el botón de fin de secuencia pide confirmación y muestra la respuesta de NINA', async () => {
  const b = browser();
  const panel = b.run('buildPanel({id:"one", name:"Telescopio uno"})');
  const button = panel.children[0].children[1].children[1];
  const status = panel.children[1];
  b.elements.set(button.id, button);
  assert.equal(button.disabled, true);
  b.run('applyFullState("one", {connected:true});');
  assert.equal(button.disabled, true);
  b.run('applyFullState("one", {sequence_running:true});');
  assert.equal(button.disabled, false);
  b.context.window.confirm = () => false;
  await button.listeners.click();
  assert.equal(status.textContent, '');

  b.context.window.confirm = message => {
    assert.match(message, /Telescopio uno/);
    return true;
  };
  b.context.fetch = async (url, options) => {
    assert.equal(url, '/api/setups/one/sequence/to-end');
    assert.equal(options.method, 'POST');
    return {ok: false, json: async () => ({detail: 'NINA no tiene una secuencia avanzada en ejecución'})};
  };
  const pending = button.listeners.click();
  assert.equal(button.disabled, true);
  await pending;
  assert.match(status.textContent, /no tiene una secuencia/);
  assert.equal(button.disabled, false);

  b.context.fetch = async () => {
    b.run('applyFullState("one", {sequence_running:true});');
    assert.equal(button.disabled, true);
    b.run('applyFullState("one", {sequence_running:false});');
    return {ok: true, json: async () => ({ok: true})};
  };
  await button.listeners.click();
  assert.match(status.textContent, /ha aceptado el salto/);
  assert.equal(button.disabled, true);
  b.run('applyFullState("one", {sequence_running:true}); SETUPS = [{id:"one"}]; markDisconnected();');
  assert.equal(button.disabled, true);
});

test('cada captura se actualiza y una descarga antigua no reaparece tras cambiar objetivo', () => {
  const b = browser();
  b.run('updateImage("one", "/first", "1")');
  const first = b.images[0];
  first.onload();
  b.run('updateImage("one", "/second", "2")');
  assert.equal(b.elements.get('image-cell-one').children[0], first);
  b.images[1].onload();
  assert.equal(b.elements.get('image-cell-one').children[0], b.images[1]);
  b.run('updateImage("one", "/third", "3"); updateImage("one", null, null)');
  b.images[2].onload();
  assert.equal(b.elements.get('image-cell-one').querySelector('img'), undefined);
  b.run('updateImage("one", "/new-target", "4")');
  b.images[3].onload();
  assert.equal(b.elements.get('image-cell-one').children[0].src, '/new-target');
});

test('la cámara y la all-sky mantienen la imagen anterior hasta decodificar la siguiente', async () => {
  const b = browser();
  const decoding = [];
  b.context.Image.prototype.decode = function () {
    return new Promise((resolve, reject) => decoding.push({image: this, resolve, reject}));
  };
  const camera = b.run('cameraTile({id:"one", name:"Techo", refresh_seconds:10})');
  camera.setActive(true);
  const allSky = b.run('buildExtraCard({title:"All-sky", refresh_seconds:10}, 0)');
  b.run('updateImage("one", "/first", "1")');
  const frames = [camera.tile.children[0], allSky.children[1], b.elements.get('image-cell-one')];
  assert.ok(decoding.every(({image}) => image.src));
  decoding.slice(0, 3).forEach(load => load.resolve());
  await Promise.resolve();
  assert.deepEqual(frames.map(frame => frame.querySelector('img')), b.images.slice(0, 3));

  const refresh = () => [...b.intervals.values()].filter(timer => timer.delay === 10000)
    .forEach(timer => timer.callback());
  refresh();
  refresh();
  b.run('updateImage("one", "/second", "2")');
  assert.equal(decoding.length, 6);
  assert.deepEqual(frames.map(frame => frame.querySelector('img')), b.images.slice(0, 3));

  decoding[3].resolve();
  decoding[4].reject(new Error('Imagen no decodificable'));
  decoding[5].resolve();
  await Promise.resolve();
  assert.deepEqual(frames.map(frame => frame.querySelector('img')), [b.images[3], b.images[1], b.images[5]]);
  assert.equal(allSky.children[0].children[1].textContent, 'sin conexión');
});

test('la barra avanza entre sondeos aunque NINA solo indique la hora de fin', () => {
  const b = browser();
  let now = Date.parse('2026-11-20T01:00:00Z');
  b.context.Date = class extends Date { static now() { return now; } };
  const tick = [...b.intervals.values()].find(timer => timer.delay === 200).callback;
  const update = exp => b.run(`updateExposureState("one", ${JSON.stringify(exp)})`);
  const exposure = {is_exposing: true, end_time: '2026-11-20T01:01:00Z', observed_at_ms: now, duration: 0};
  update(exposure);
  const bar = b.elements.get('exposure-bar-one');
  const timer = b.elements.get('exposure-timer-one');
  assert.equal(bar.style.width, '0.0%');

  now += 30_000;
  tick();
  assert.equal(bar.style.width, '50.0%');
  assert.equal(timer.textContent, '30s restantes');
  update(exposure);
  assert.equal(bar.style.width, '50.0%');

  now += 31_000;
  tick();
  assert.equal(bar.style.width, '100.0%');
  update({camera_state: 'Downloading'});
  assert.equal(bar.className, 'exposure-bar-fill downloading');
  update({...exposure, end_time: '2026-11-20T01:01:41Z', observed_at_ms: now});
  assert.equal(bar.style.width, '0.0%');
  assert.equal(bar.className, 'exposure-bar-fill');
});

test('visor HD sigue la captura actual e ignora respuestas tardías', () => {
  const b = browser();
  b.run('updateImage("one", "/first", "1"); openImageModal("one")');
  const oldHD = b.images[1];
  b.run('updateImage("one", "/second", "2")');
  const newHD = b.images[3];
  oldHD.onload();
  assert.equal(b.elements.get('modal-img').src, '/second');
  newHD.onload();
  assert.match(b.elements.get('modal-img').src, /hires\?v=2$/);
  b.run('updateImage("one", null, null)');
  newHD.onload();
  assert.equal(b.elements.get('image-modal').style.display, 'none');
  assert.equal(b.elements.get('modal-img').src, undefined);
});

test('guiado directo, recuperación de estrella y ventana limitada sin fetch', () => {
  const b = browser();
  b.run('STATES.one = {guide_steps:[], guide_seq:0, guiding_status:"estrella perdida"}; SETUP_AXIS_UNIT.one = "arcsec"; connectWebSocket()');
  for (let seq=1; seq<=1000; seq++) {
    b.context.socket.onmessage({data:JSON.stringify({type:'guide_step', setup_id:'one', pixel_scale:null,
      point:{seq, ra_distance_px:0.5, dec_distance_px:-0.5, ra_distance_arcsec:null, dec_distance_arcsec:null}})});
  }
  assert.equal(b.run('STATES.one.guide_steps.length'), 300);
  assert.equal(b.run('STATES.one.guiding_status'), 'guiando');
  assert.equal(b.run('guideUnit("one")'), 'px');
  assert.match(b.elements.get('rms-one').children[0].innerHTML, /px/);
  b.context.socket.onmessage({data:JSON.stringify({type:'guiding_status', setup_id:'one', status:'desconectado', reset:true, pixel_scale:null})});
  assert.equal(b.run('STATES.one.guide_steps.length'), 0);
  assert.equal(b.elements.get('guiding-badge-one').textContent, 'desconectado');
});

test('la gráfica dibuja la curva roja y muestra hora y altura de culminación en móvil', () => {
  const b = browser();
  const canvas = b.run('document.getElementById("target-alt-canvas-one")');
  canvas.getBoundingClientRect = () => ({width: 265});
  const labels = [];
  const strokes = [];
  canvas.getContext = () => new Proxy({
    createLinearGradient: () => ({addColorStop() {}}),
    measureText: (label) => ({width: label.length * 5}),
    fillText(label, x) { labels.push({label, x}); },
    stroke() { strokes.push(this.strokeStyle); },
  }, {get: (obj, key) => obj[key] ?? (() => {})});

  b.run(`
    STATES.one = {
      site: {latitude: 40, longitude: 0},
      night: {start: "2000-01-01T00:00:00Z", end: "2000-01-02T00:00:00Z", initial_phase: "night", events: []},
    };
    renderTargetAltCanvas("one", {ra: 280.46061837 / 15, dec: -5.5});
  `);
  const localNoon = new Date("2000-01-01T12:00:00Z").toLocaleTimeString("es-ES", {hour: "2-digit", minute: "2-digit"});
  assert.ok(strokes.includes('#f43f5e'));
  assert.ok(labels.some(({label, x}) => label === `Culm. ${localNoon} · 44.5°` && Math.abs(x - 132.5) < 1));
});

test('las cámaras seleccionadas forman el mosaico y un fallo conserva el nombre y la última imagen', () => {
  const b = browser();
  b.intervals.clear();
  const card = b.run(`buildCameraCard([
    {id:"one", name:"Monturas", refresh_seconds:10},
    {id:"two", name:"Techo", refresh_seconds:15},
    {id:"three", name:"Entrada", refresh_seconds:20},
    {id:"four", name:"Vista general", refresh_seconds:30}
  ])`);
  const [header, frame] = card.children;
  const [summary, choices] = header.children[1].children;
  const [all, mounts, roof, entrance, overview] = choices.children.map(label => label.children[0]);
  const tileNames = () => frame.children.flatMap(row => row.children.map(tile => tile.children[1].children[0].textContent));
  const tick = delay => [...b.intervals.values()].find(timer => timer.delay === delay).callback();
  assert.deepEqual(frame.children.map(row => row.children.length), [2, 2]);
  assert.deepEqual(tileNames(), ['Monturas', 'Techo', 'Entrada', 'Vista general']);
  assert.equal(b.images.length, 4);
  tick(10000);
  assert.equal(b.images.length, 4, 'no inicia un refresco hasta terminar la captura anterior');

  roof.checked = false;
  roof.listeners.change();
  overview.checked = false;
  overview.listeners.change();
  assert.equal(summary.textContent, '2 seleccionadas');
  assert.deepEqual(tileNames(), ['Monturas', 'Entrada']);
  assert.deepEqual([...b.intervals.values()].map(timer => timer.delay), [10000, 20000]);
  assert.equal(all.indeterminate, true);

  const [mountTile, entranceTile] = frame.children[0].children;
  const staleImage = b.images[0];
  staleImage.onload();
  tick(10000);
  b.images[4].onerror();
  b.images[2].onerror();
  assert.equal(mountTile.children[0].children[0], staleImage);
  assert.match(mountTile.children[1].children[1].textContent, /Sin conexión · Última imagen/);
  assert.equal(entranceTile.children[1].children[0].textContent, 'Entrada');
  assert.equal(entranceTile.children[1].children[1].textContent, 'Imagen no disponible');

  mounts.checked = false;
  mounts.listeners.change();
  assert.equal(summary.textContent, 'Entrada');
  entrance.checked = false;
  entrance.listeners.change();
  assert.equal(frame.children[0].textContent, 'Selecciona al menos una cámara');
  assert.equal(b.intervals.size, 0);
  all.checked = true;
  all.listeners.change();
  assert.deepEqual(frame.children.map(row => row.children.length), [2, 2]);
  assert.equal(b.intervals.size, 4);
});
