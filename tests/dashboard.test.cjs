// Pruebas del JavaScript real sin dependencias de navegador.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function browser() {
  const elements = new Map();
  const images = [];
  class Element {
    constructor(tag = 'div') { this.tag = tag; this.style = {}; this.dataset = {}; this.children = []; this.textContent = ''; }
    appendChild(child) { this.children.push(child); return child; }
    replaceChildren(...children) { this.children = children; }
    querySelector(selector) { return selector === 'img' ? this.children.find(c => c.tag === 'img') : null; }
    removeAttribute(key) { delete this[key]; }
    addEventListener() {}
    getBoundingClientRect() { return {width:500}; }
    getContext() { return new Proxy({}, {get: (_, key) => key === 'createLinearGradient' ? () => ({addColorStop(){}}) : () => {}}); }
  }
  const context = vm.createContext({console, Date, Math, JSON, Object, Number, String, Array, Set, Promise,
    document:{getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
      createElement: tag => new Element(tag), body:new Element()},
    localStorage:{getItem(){return 'off';}},
    window:{addEventListener(){}, devicePixelRatio:1},
    getComputedStyle(){return {fontFamily:'monospace'};},
    setInterval(){return 1;}, clearInterval(){}, setTimeout(){},
    Image: class extends Element { constructor() { super('img'); images.push(this); } },
    location:{protocol:'http:', host:'localhost', reload(){}},
    WebSocket:class {constructor(){context.socket = this;}},
    fetch(){throw new Error('El guiado no debe lanzar peticiones HTTP');}
  });
  const source = fs.readFileSync('frontend/dashboard.js','utf8').split('init().catch')[0];
  vm.runInContext(source, context);
  return {context, images, elements, run:code => vm.runInContext(code,context)};
}

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
