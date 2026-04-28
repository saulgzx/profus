"""
gui_web.py — Servidor HTTP embebido para acceso mobile/remoto al dashboard.

Motivación:
  El dashboard tkinter corre en la GUI del bot (localhost, misma máquina).
  Para ver el estado desde el celular (ej: vía Tailscale cuando estamos
  fuera de casa), exponemos los mismos datos como HTTP + página responsive.

Arquitectura:
  * `WebDashboardServer` — envuelve `ThreadingHTTPServer` de stdlib en un
    thread daemon. No necesita deps extra (ni FastAPI/Flask).
  * Endpoints:
      GET /               → página HTML mobile-first
      GET /api/state      → JSON con snapshot de _collect_dashboard_state()
      GET /api/map/{id}   → JSON con celdas del mapa (cache server-side)
  * La página HTML polea /api/state cada 500ms via fetch(). No SSE ni WS
    para simplificar — polling 2Hz es suficiente para un dashboard.

Seguridad:
  * Por default bindea a 0.0.0.0 para que Tailscale (interfaz virtual
    tailscale0) pueda accederlo. Eso implica que cualquier device en tu
    LAN o en tu tailnet ve el dashboard. Es read-only (no hay endpoints
    POST/PUT/DELETE) — worst case es que alguien ve tu progreso.
  * Si querés aislar a localhost solo, seteá `web_server_host: 127.0.0.1`
    en la config.
  * NO abras el puerto en tu router al WAN público. Tailscale es la vía
    segura para acceso remoto.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from app_logger import get_logger

_log = get_logger("bot.gui.web")



# ───────────────────────────────────────────── PWA manifest + icon ──

_MANIFEST_JSON = """{
  "name": "Dofus Autofarm Dashboard",
  "short_name": "Autofarm",
  "description": "Estado live del bot en tiempo real",
  "start_url": "/",
  "display": "standalone",
  "orientation": "any",
  "background_color": "#0A0C10",
  "theme_color": "#0A0C10",
  "icons": [
    { "src": "/icon.svg?v=2", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable" }
  ]
}"""

# Icono: dofus de Sadida (huevo verde-amarillo con gradient y glossy) +
# engranaje central en verde oscuro (representa la automatización del bot).
# SVG escalable a cualquier tamaño (iOS 17+ y Chrome Android lo soportan
# en PWA manifest). A tamaños muy chicos (16-32px) los detalles del gear
# se pierden pero la silueta del dofus verde queda reconocible.
_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
  <defs>
    <radialGradient id="eggGrad" cx="38%" cy="30%" r="78%" fx="33%" fy="25%">
      <stop offset="0%" stop-color="#FFFF88"/>
      <stop offset="30%" stop-color="#E0FF45"/>
      <stop offset="60%" stop-color="#9ECB28"/>
      <stop offset="88%" stop-color="#4E7A12"/>
      <stop offset="100%" stop-color="#2E4A08"/>
    </radialGradient>
    <radialGradient id="highlight" cx="30%" cy="22%" r="28%">
      <stop offset="0%" stop-color="#FFFFFF" stop-opacity="0.85"/>
      <stop offset="60%" stop-color="#FFFFFF" stop-opacity="0.25"/>
      <stop offset="100%" stop-color="#FFFFFF" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="gearGrad" cx="35%" cy="30%" r="75%">
      <stop offset="0%" stop-color="#3E5818"/>
      <stop offset="60%" stop-color="#223608"/>
      <stop offset="100%" stop-color="#0E1A02"/>
    </radialGradient>
  </defs>

  <!-- Egg base shape (wider at bottom, pointed at top, Sadida style) -->
  <path d="M 100 12
           C 144 12, 180 64, 180 120
           C 180 170, 142 190, 100 190
           C 58 190, 20 170, 20 120
           C 20 64, 56 12, 100 12 Z"
        fill="url(#eggGrad)"
        stroke="#1E3206"
        stroke-width="3"/>

  <!-- Dofus texture spots (chispas oscuras que dan el look de huevo Sadida) -->
  <ellipse cx="60" cy="150" rx="7" ry="3.5" fill="#2E4A08" opacity="0.45"
           transform="rotate(22 60 150)"/>
  <ellipse cx="145" cy="90" rx="5.5" ry="2.8" fill="#2E4A08" opacity="0.4"
           transform="rotate(-18 145 90)"/>
  <ellipse cx="125" cy="170" rx="4" ry="2" fill="#2E4A08" opacity="0.35"/>
  <ellipse cx="40" cy="95" rx="3.5" ry="1.8" fill="#2E4A08" opacity="0.35"
           transform="rotate(40 40 95)"/>
  <ellipse cx="155" cy="145" rx="4" ry="2" fill="#2E4A08" opacity="0.3"
           transform="rotate(-30 155 145)"/>

  <!-- Central gear (engranaje) -->
  <g transform="translate(100, 112)">
    <!-- 8 teeth -->
    <g fill="url(#gearGrad)" stroke="#0A1500" stroke-width="1.2" stroke-linejoin="round">
      <rect x="-8" y="-42" width="16" height="20" rx="2"/>
      <rect x="-8" y="-42" width="16" height="20" rx="2" transform="rotate(45)"/>
      <rect x="-8" y="-42" width="16" height="20" rx="2" transform="rotate(90)"/>
      <rect x="-8" y="-42" width="16" height="20" rx="2" transform="rotate(135)"/>
      <rect x="-8" y="-42" width="16" height="20" rx="2" transform="rotate(180)"/>
      <rect x="-8" y="-42" width="16" height="20" rx="2" transform="rotate(225)"/>
      <rect x="-8" y="-42" width="16" height="20" rx="2" transform="rotate(270)"/>
      <rect x="-8" y="-42" width="16" height="20" rx="2" transform="rotate(315)"/>
    </g>
    <!-- Gear body (disco central) -->
    <circle r="30" fill="url(#gearGrad)" stroke="#0A1500" stroke-width="1.5"/>
    <!-- Inner hole: verde claro, muestra el "núcleo" del huevo -->
    <circle r="12" fill="#D8FF40" stroke="#0A1500" stroke-width="1.5"/>
    <circle r="9.5" fill="none" stroke="#4E7A12" stroke-width="0.8"/>
    <!-- Dot en el centro (tornillo) -->
    <circle r="3" fill="#0E1A02"/>
  </g>

  <!-- Highlight glossy (gota de luz en top-left) -->
  <ellipse cx="68" cy="55" rx="20" ry="36" fill="url(#highlight)"
           transform="rotate(-12 68 55)"/>
  <!-- Sparkle pequeño -->
  <circle cx="56" cy="40" r="2.8" fill="#FFFFFF" opacity="0.85"/>
  <circle cx="82" cy="30" r="1.4" fill="#FFFFFF" opacity="0.6"/>
</svg>"""


# ───────────────────────────────────────────────── HTML/CSS/JS inline ──

_HTML_PAGE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0A0C10">
<!-- PWA / mobile-app behavior -->
<link rel="manifest" href="/manifest.json">
<link rel="icon" type="image/svg+xml" href="/icon.svg?v=2">
<link rel="apple-touch-icon" href="/icon.svg?v=2">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Autofarm">
<meta name="mobile-web-app-capable" content="yes">
<title>Dofus Autofarm · Dashboard</title>
<style>
  :root {
    --bg: #0A0C10;
    --panel: #12151B;
    --elevated: #181C24;
    --overlay: #1F242E;
    --border: #2A3038;
    --text: #E6E8EC;
    --text2: #A0A6B0;
    --text3: #6B7280;
    --brand: #F5A524;
    --info: #38BDF8;
    --success: #4ADE80;
    --warning: #FBBF24;
    --danger: #F87171;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }

  /* ── Entry animations (staggered fade-in-up) ─────────────────────── */
  @keyframes fadeInUp {
    from { opacity: 0; transform: translateY(14px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  @keyframes logoIn {
    from { opacity: 0; transform: scale(0.6) rotate(-12deg); }
    to   { opacity: 1; transform: scale(1) rotate(0); }
  }
  @keyframes dotPulseOk {
    0%, 100% { box-shadow: 0 0 0 0 rgba(74, 222, 128, 0.55); }
    50%      { box-shadow: 0 0 0 7px rgba(74, 222, 128, 0); }
  }
  @keyframes slideFadeIn {
    from { opacity: 0; transform: translateY(24px) scale(0.985); }
    to   { opacity: 1; transform: translateY(0) scale(1); }
  }

  /* Accesibilidad: respetar preferencia del usuario */
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation-duration: 0.01ms !important;
      animation-iteration-count: 1 !important;
      transition-duration: 0.01ms !important;
    }
  }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 12px;
    padding-top: max(12px, env(safe-area-inset-top));
    padding-bottom: max(12px, env(safe-area-inset-bottom));
    -webkit-font-smoothing: antialiased;
  }
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 16px;
    gap: 12px;
    /* Entry: fade down + slight drop */
    animation: fadeInUp 480ms cubic-bezier(0.16, 1, 0.3, 1) 0ms both;
  }
  .logo {
    display: flex;
    align-items: center;
    gap: 10px;
    font-weight: 700;
    font-size: 14px;
  }
  .logo-svg {
    width: 32px;
    height: 32px;
    display: block;
    filter: drop-shadow(0 1px 2px rgba(0,0,0,0.5));
    flex-shrink: 0;
    /* Entry: scale + slight rotate (pop de dofus) */
    animation: logoIn 700ms cubic-bezier(0.34, 1.56, 0.64, 1) 100ms both;
    transform-origin: center;
  }
  .logo-svg svg { width: 100%; height: 100%; display: block; }
  .header-right {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    justify-content: flex-end;
  }
  .status-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 10px;
    border-radius: 20px;
    background: var(--elevated);
    border: 1px solid var(--border);
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
  }
  /* Botones de control — grandes para mobile (touch-friendly 44px) */
  .ctrl-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    min-width: 44px;
    min-height: 36px;
    padding: 6px 12px;
    border-radius: 8px;
    background: var(--elevated);
    border: 1px solid var(--border);
    color: var(--text);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    user-select: none;
    -webkit-tap-highlight-color: transparent;
    transition: transform 0.1s ease, background 0.15s ease, border-color 0.15s ease;
  }
  .ctrl-btn:active { transform: scale(0.96); }
  .ctrl-btn:hover { background: var(--overlay); border-color: rgba(255,255,255,0.12); }
  .ctrl-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .ctrl-btn.primary {
    background: var(--brand); border-color: #D18A10; color: #1a1a1a;
  }
  .ctrl-btn.primary:hover { background: #FFC04C; }
  .ctrl-btn.danger {
    background: #7f1d1d; border-color: #5a1111; color: #FFFFFF;
  }
  .ctrl-btn.danger:hover { background: #991b1b; }
  .ctrl-btn .ctrl-icon { font-size: 14px; line-height: 1; }
  @media (max-width: 520px) {
    .ctrl-btn { padding: 6px 10px; font-size: 12px; }
    .ctrl-btn-label { display: none; }  /* solo icono en pantallas chicas */
  }
  .dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--danger);
    flex-shrink: 0;
    transition: background 0.3s ease;
  }
  /* Pulse suave cuando está conectado (heartbeat del server) */
  .dot.ok {
    background: var(--success);
    animation: dotPulseOk 2.2s ease-out infinite;
  }
  .cards {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 8px;
    margin-bottom: 12px;
  }
  @media (max-width: 900px) {
    .cards { grid-template-columns: repeat(2, 1fr); }
  }
  @media (max-width: 460px) {
    .cards { grid-template-columns: 1fr; }
  }
  .card {
    background: var(--elevated);
    border: 1px solid var(--border);
    padding: 12px;
    border-radius: 6px;
    position: relative;
    overflow: hidden;
    /* Entry: stagger por cada card (nth-child delays abajo) */
    animation: fadeInUp 520ms cubic-bezier(0.16, 1, 0.3, 1) both;
    transition: transform 0.2s ease, border-color 0.2s ease;
  }
  .cards .card:nth-child(1) { animation-delay: 140ms; }
  .cards .card:nth-child(2) { animation-delay: 210ms; }
  .cards .card:nth-child(3) { animation-delay: 280ms; }
  .cards .card:nth-child(4) { animation-delay: 350ms; }
  /* Hover sutil en desktop */
  @media (hover: hover) {
    .card:hover {
      transform: translateY(-1px);
      border-color: rgba(255, 255, 255, 0.12);
    }
  }
  .card::before {
    content: "";
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: var(--brand);
  }
  .card.info::before { background: var(--info); }
  .card.success::before { background: var(--success); }
  .card-label {
    font-size: 10px;
    letter-spacing: 0.08em;
    color: var(--text3);
    font-weight: 700;
    text-transform: uppercase;
  }
  .card-value {
    font-size: 22px;
    font-weight: 700;
    margin-top: 4px;
    line-height: 1.2;
  }
  .card-sub {
    font-size: 12px;
    color: var(--text2);
    margin-top: 4px;
  }
  .map-wrap {
    background: var(--elevated);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px;
    position: relative;
    /* Entry: último en aparecer, slide + slight scale para dar punchy */
    animation: slideFadeIn 600ms cubic-bezier(0.16, 1, 0.3, 1) 450ms both;
  }
  /* HP card: fondo rojo + letras blancas (override de .card) */
  .card.hp-card {
    background: linear-gradient(180deg, #E0302E 0%, #B91C1C 60%, #8B1111 100%);
    border-color: #7A0E0E;
  }
  .card.hp-card::before { background: #FF6B6B; }
  .card.hp-card .card-label { color: rgba(255, 255, 255, 0.75); }
  .card.hp-card .card-value { color: #FFFFFF; text-shadow: 0 1px 2px rgba(0,0,0,0.35); }
  .card.hp-card .card-sub { color: rgba(255, 255, 255, 0.85); }
  .map-wrap::before {
    content: "";
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: var(--brand);
  }
  .map-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
    gap: 8px;
  }
  .map-label {
    font-size: 10px;
    letter-spacing: 0.08em;
    color: var(--text3);
    font-weight: 700;
    text-transform: uppercase;
  }
  .map-status {
    font-size: 11px;
    color: var(--text2);
    text-align: right;
  }
  .legend {
    display: flex;
    gap: 16px;
    margin-bottom: 8px;
    font-size: 11px;
    color: var(--text2);
  }
  .legend-item {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .legend-dot {
    width: 10px; height: 10px;
    border-radius: 2px;
  }
  .legend-dot.pj { background: var(--info); }
  .legend-dot.enemy { background: var(--danger); }
  #map-canvas {
    width: 100%;
    height: clamp(260px, 55vh, 520px);
    background: var(--bg);
    display: block;
    border-radius: 4px;
    touch-action: none;
  }
  .offline-banner {
    background: var(--danger);
    color: white;
    padding: 10px 12px;
    border-radius: 4px;
    margin-bottom: 12px;
    font-size: 13px;
    font-weight: 600;
    display: none;
  }
  .offline-banner.visible { display: block; }
  .footer {
    margin-top: 12px;
    text-align: center;
    font-size: 10px;
    color: var(--text3);
  }
</style>
</head>
<body>
  <div class="header">
    <div class="logo">
      <!-- SVG inlined at serve-time: evita cache issue del /icon.svg -->
      <div class="logo-svg">{{ICON_SVG}}</div>
      <span>Dofus Autofarm</span>
    </div>
    <div class="header-right">
      <!-- Botones de control — dinámicos según state.paused / state.running -->
      <button class="ctrl-btn" id="btn-pause" type="button" aria-label="Pausar/Reanudar">
        <span class="ctrl-icon" id="btn-pause-icon">⏸</span>
        <span class="ctrl-btn-label" id="btn-pause-label">Pausar</span>
      </button>
      <button class="ctrl-btn danger" id="btn-stop" type="button" aria-label="Detener">
        <span class="ctrl-icon">⏹</span>
        <span class="ctrl-btn-label">Detener</span>
      </button>
      <div class="status-pill">
        <span class="dot" id="conn-dot"></span>
        <span id="conn-label">Conectando…</span>
      </div>
    </div>
  </div>
  <div class="offline-banner" id="offline-banner">⚠ Sin conexión al bot · reintentando…</div>
  <div class="cards">
    <div class="card">
      <div class="card-label">Estado</div>
      <div class="card-value" id="state-value">—</div>
      <div class="card-sub" id="state-sub">—</div>
    </div>
    <div class="card info">
      <div class="card-label">Mapa actual</div>
      <div class="card-value" id="map-value">—</div>
      <div class="card-sub" id="map-sub">—</div>
    </div>
    <div class="card success">
      <div class="card-label">Combates</div>
      <div class="card-value" id="fights-value">0</div>
      <div class="card-sub" id="fights-sub">—</div>
    </div>
    <div class="card hp-card">
      <div class="card-label">PDV</div>
      <div class="card-value" id="hp-value">—</div>
      <div class="card-sub" id="hp-sub">—</div>
    </div>
  </div>
  <div class="map-wrap">
    <div class="map-header">
      <span class="map-label">Minimapa · Tiempo real</span>
      <span class="map-status" id="map-status"></span>
    </div>
    <div class="legend">
      <div class="legend-item"><span class="legend-dot pj"></span>PJ</div>
      <div class="legend-item"><span class="legend-dot enemy"></span>Enemigos</div>
    </div>
    <canvas id="map-canvas"></canvas>
  </div>
  <div class="footer" id="footer">—</div>

<script>
(() => {
  const $ = id => document.getElementById(id);
  const elState = $('state-value'), elStateSub = $('state-sub');
  const elMap = $('map-value'), elMapSub = $('map-sub');
  const elFights = $('fights-value'), elFightsSub = $('fights-sub');
  const elConnLabel = $('conn-label'), elConnDot = $('conn-dot');
  const elOffline = $('offline-banner');
  const elMapStatus = $('map-status');
  const elFooter = $('footer');
  const elHpValue = $('hp-value'), elHpSub = $('hp-sub');
  const elBtnPause = $('btn-pause'), elBtnPauseIcon = $('btn-pause-icon'),
        elBtnPauseLabel = $('btn-pause-label'), elBtnStop = $('btn-stop');
  const canvas = $('map-canvas');
  const ctx = canvas.getContext('2d');
  const mapCache = new Map();
  let currentMap = null;
  let lastOkAt = 0;

  async function fetchJSON(url) {
    try {
      const res = await fetch(url, { cache: 'no-store' });
      if (!res.ok) return null;
      return await res.json();
    } catch (e) { return null; }
  }

  async function fetchMap(mapId) {
    if (mapCache.has(mapId)) return mapCache.get(mapId);
    const data = await fetchJSON(`/api/map/${mapId}`);
    if (data) mapCache.set(mapId, data);
    return data;
  }

  function setConnected(ok) {
    elConnDot.classList.toggle('ok', ok);
    elConnLabel.textContent = ok ? 'Conectado' : 'Sin conexión';
    elOffline.classList.toggle('visible', !ok);
    if (ok) lastOkAt = Date.now();
  }

  // ── Control buttons (pause/resume/stop) ────────────────────────────
  async function sendControl(action) {
    try {
      const res = await fetch(`/api/control/${action}`, { method: 'POST' });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        console.warn('control error', action, body);
        return null;
      }
      return await res.json();
    } catch (e) { return null; }
  }

  function updateControlButtons(st) {
    const running = st.state && st.state !== 'idle' && st.state !== null;
    const paused = !!st.paused;
    // Pause/Resume toggle según estado actual
    if (paused) {
      elBtnPauseIcon.textContent = '▶';
      elBtnPauseLabel.textContent = 'Reanudar';
      elBtnPause.classList.add('primary');
      elBtnPause.dataset.action = 'resume';
    } else {
      elBtnPauseIcon.textContent = '⏸';
      elBtnPauseLabel.textContent = 'Pausar';
      elBtnPause.classList.remove('primary');
      elBtnPause.dataset.action = 'pause';
    }
    elBtnPause.disabled = !running;
    elBtnStop.disabled = !running;
  }

  elBtnPause.addEventListener('click', async () => {
    const action = elBtnPause.dataset.action || 'pause';
    elBtnPause.disabled = true;
    await sendControl(action);
    // Forzar refresh inmediato para ver nuevo estado (no esperar el poll)
    tick();
  });
  elBtnStop.addEventListener('click', async () => {
    if (!confirm('¿Detener el bot? Vas a tener que iniciarlo manualmente de nuevo desde la GUI del PC.')) return;
    elBtnStop.disabled = true;
    await sendControl('stop');
    tick();
  });

  function renderCards(st) {
    const m = st.metrics || {};
    const paused = !!st.paused;
    const bs = st.state;
    if (paused) {
      elState.textContent = 'Pausado';
      elState.style.color = 'var(--warning)';
    } else if (bs && bs !== 'idle') {
      elState.textContent = bs;
      elState.style.color = 'var(--success)';
    } else {
      elState.textContent = 'Detenido';
      elState.style.color = 'var(--danger)';
    }
    const sniffer = st.sniffer_active ? 'sniffer on' : 'sniffer off';
    const actor = st.actor_id || '?';
    const sess = (m.session_min || 0).toFixed(0);
    elStateSub.textContent = `${sniffer} · actor ${actor} · sesión ${sess} min`;

    elMap.textContent = st.map_id ?? '—';
    let sub = st.combat_cell != null ? `cell ${st.combat_cell}` : 'fuera de combate';
    if (st.pods_current != null && st.pods_max) sub += ` · pods ${st.pods_current}/${st.pods_max}`;
    elMapSub.textContent = sub;

    elFights.textContent = m.fights_total || 0;
    const rate = m.fights_hour_rate || 0;
    const spells = m.spells_total || 0;
    const fails = m.spells_fails || 0;
    const fr = spells ? (100 * fails / spells).toFixed(1) : '0.0';
    elFightsSub.textContent = `${rate}/h · ${spells} spells · ${fails} fails (${fr}%)`;

    updateControlButtons(st);

    // HP card (fondo rojo, letras blancas): HP actual + max_hp en subtítulo
    if (st.hp != null) {
      const hp = Math.max(0, Math.floor(Number(st.hp)));
      elHpValue.textContent = String(hp);
      const maxHp = Number(st.max_hp);
      if (maxHp > 0) {
        const pct = Math.round((hp / maxHp) * 100);
        elHpSub.textContent = `${hp} / ${maxHp} · ${pct}%`;
      } else {
        elHpSub.textContent = 'max desconocido';
      }
    } else {
      elHpValue.textContent = '—';
      elHpSub.textContent = 'fuera de combate';
    }
  }

  function resizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.floor(rect.width * dpr);
    canvas.height = Math.floor(rect.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function drawRhombus(cx, cy, w, h, fill, stroke, strokeW) {
    ctx.fillStyle = fill;
    ctx.beginPath();
    ctx.moveTo(cx, cy - h/2);
    ctx.lineTo(cx + w/2, cy);
    ctx.lineTo(cx, cy + h/2);
    ctx.lineTo(cx - w/2, cy);
    ctx.closePath();
    ctx.fill();
    if (stroke) {
      ctx.strokeStyle = stroke;
      ctx.lineWidth = strokeW || 1;
      ctx.stroke();
    }
  }

  function renderMap(st) {
    resizeCanvas();
    const rect = canvas.getBoundingClientRect();
    const cw = rect.width, ch = rect.height;
    ctx.fillStyle = '#0A0C10';
    ctx.fillRect(0, 0, cw, ch);

    if (!currentMap || !currentMap.cells) {
      elMapStatus.textContent = st.map_id ? `map ${st.map_id} · sin XML` : '(esperando map_id)';
      return;
    }

    let dmin=Infinity, dmax=-Infinity, smin=Infinity, smax=-Infinity;
    for (const c of currentMap.cells) {
      const dv = c.x - c.y, sv = c.x + c.y;
      if (dv < dmin) dmin = dv;
      if (dv > dmax) dmax = dv;
      if (sv < smin) smin = sv;
      if (sv > smax) smax = sv;
    }
    const pad = 12;
    const cellWFit = 2 * (cw - 2*pad) / ((dmax - dmin) + 2);
    const cellHFit = 2 * (ch - 2*pad) / ((smax - smin) + 2);
    let cellW, cellH;
    if (cellWFit / 2 < cellHFit) {
      cellH = cellWFit / 2; cellW = cellWFit;
    } else {
      cellH = cellHFit; cellW = cellH * 2;
    }
    cellW = Math.max(4, cellW);
    cellH = Math.max(2, cellH);
    const midX = (dmin + dmax) / 2, midY = (smin + smax) / 2;
    const ox = cw/2 - midX * cellW/2;
    const oy = ch/2 - midY * cellH/2;

    // bg cells
    for (const c of currentMap.cells) {
      const cx = ox + (c.x - c.y) * cellW/2;
      const cy = oy + (c.x + c.y) * cellH/2;
      drawRhombus(cx, cy, cellW, cellH, c.walkable ? '#2A303C' : '#15181F', '#2A3038', 1);
    }

    // index por cell_id para overlays
    const cellById = new Map();
    for (const c of currentMap.cells) cellById.set(c.id, c);

    const drawMarker = (id, color, label) => {
      const c = cellById.get(id);
      if (!c) return;
      const cx = ox + (c.x - c.y) * cellW/2;
      const cy = oy + (c.x + c.y) * cellH/2;
      drawRhombus(cx, cy, cellW*0.85, cellH*0.85, color, '#FFFFFF', 1);
      if (label) {
        ctx.fillStyle = '#FFFFFF';
        const fs = Math.max(8, Math.floor(cellH * 0.55));
        ctx.font = `bold ${fs}px -apple-system, sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(label, cx, cy);
      }
    };

    for (const ec of (st.enemy_cells || [])) drawMarker(ec, '#F87171', null);
    if (st.combat_cell != null) drawMarker(st.combat_cell, '#38BDF8', 'PJ');

    elMapStatus.textContent = `map ${currentMap.map_id} · ${currentMap.width}×${currentMap.height} · ${(st.enemy_cells || []).length} enemigos`;
  }

  async function tick() {
    const st = await fetchJSON('/api/state');
    if (!st) { setConnected(false); return; }
    setConnected(true);
    renderCards(st);
    if (st.map_id != null) {
      if (currentMap == null || currentMap.map_id !== st.map_id) {
        currentMap = await fetchMap(st.map_id);
      }
    } else {
      currentMap = null;
    }
    renderMap(st);
    elFooter.textContent = `actualizado ${new Date().toLocaleTimeString()}`;
  }

  window.addEventListener('resize', () => { /* redraw en el próximo tick */ });

  // Wake Lock API: mantener pantalla encendida mientras el usuario mira el
  // dashboard (Android Chrome, Samsung Internet, Safari iOS 16.4+). Falla
  // silenciosamente en browsers viejos. Re-adquiere al volver de background.
  let wakeLock = null;
  async function requestWakeLock() {
    if (!('wakeLock' in navigator)) return;
    try { wakeLock = await navigator.wakeLock.request('screen'); } catch (e) { /* ignore */ }
  }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') requestWakeLock();
  });
  // Primer intento (requiere user gesture en algunos browsers — si falla,
  // se re-intenta cuando el user toque la pantalla).
  requestWakeLock();
  document.addEventListener('click', requestWakeLock, { once: true });
  document.addEventListener('touchstart', requestWakeLock, { once: true, passive: true });

  resizeCanvas();
  tick();
  setInterval(tick, 500);
})();
</script>
</body>
</html>
"""


# ───────────────────────────────────────────────────────────── Server ──


class WebDashboardServer:
    """HTTP server embebido que sirve la página web + API del dashboard.

    Corre en un thread daemon; cuando el proceso del bot termina, el server
    muere con él.
    """

    def __init__(
        self,
        state_provider: Callable[[], dict],
        host: str = "0.0.0.0",
        port: int = 8000,
        control_handler: Callable[[str], dict] | None = None,
        game_state_provider: Callable[[], dict] | None = None,
    ) -> None:
        self._state_provider = state_provider
        self._control_handler = control_handler
        self._game_state_provider = game_state_provider
        self._host = host
        self._port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # Cache del mapa: reusamos _MapCache de gui_dashboard para no duplicar.
        try:
            from gui_dashboard import _MapCache
            xml_dir = os.path.join(os.path.dirname(__file__), "..", "mapas")
            self._map_cache = _MapCache(os.path.abspath(xml_dir))
        except Exception:
            self._map_cache = None

    def start(self) -> bool:
        if self._server is not None:
            return True
        try:
            handler_cls = _make_handler(
                self._state_provider, self._map_cache, self._control_handler,
                self._game_state_provider
            )
            self._server = ThreadingHTTPServer((self._host, self._port), handler_cls)
        except OSError as exc:
            _log.info(f"[WEB] No se pudo abrir puerto {self._port}: {exc}. "
                  f"Dashboard web desactivado (el tkinter dashboard sigue funcionando).")
            self._server = None
            return False
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="WebDashboard",
            daemon=True,
        )
        self._thread.start()
        # Log URLs utilizables
        msg_host = self._host if self._host != "0.0.0.0" else "localhost"
        _log.info(f"[WEB] Dashboard web activo en:")
        _log.info(f"[WEB]   local     http://{msg_host}:{self._port}")
        if self._host == "0.0.0.0":
            try:
                import socket
                hostname = socket.gethostname()
                lan_ip = socket.gethostbyname(hostname)
                _log.info(f"[WEB]   LAN       http://{lan_ip}:{self._port}")
                _log.info(f"[WEB]   hostname  http://{hostname}:{self._port}  (útil para Tailscale: "
                      f"añadile .ts.net si es tu tailnet, o usá el DNS de Tailscale)")
            except Exception:
                pass
        return True

    def stop(self) -> None:
        if self._server is None:
            return
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass
        self._server = None
        self._thread = None


def _make_handler(state_provider, map_cache, control_handler=None, game_state_provider=None):
    """Factory del handler: captura state_provider y map_cache en closure."""

    class _Handler(BaseHTTPRequestHandler):
        # Silenciar logs ruidosos por cada request (nos inundaría la consola)
        def log_message(self, fmt, *args):  # noqa: A003
            return

        def _send_json(self, obj: dict, status: int = 200, cache_max_age: int = 0) -> None:
            try:
                body = json.dumps(obj, default=str).encode("utf-8")
            except Exception as exc:
                body = json.dumps({"error": repr(exc)}).encode("utf-8")
                status = 500
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if cache_max_age > 0:
                self.send_header("Cache-Control", f"public, max-age={cache_max_age}")
            else:
                self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _send_text(self, body: str, content_type: str, status: int = 200,
                       no_cache: bool = True) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if no_cache:
                # HTML + manifest: no cache para que cambios en la UI se vean
                # al refrescar. El minimapa/JSON ya tenía no-cache; los
                # assets estáticos (icon.svg, cache de mapas) mantienen
                # max-age propio.
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):  # noqa: N802 — api de BaseHTTPRequestHandler
            path = self.path.split("?", 1)[0]
            try:
                if path in ("/", "/index.html"):
                    # Inyectar el SVG inline (evita fetch separado + cache issue).
                    # Placeholder {{ICON_SVG}} → contenido de _ICON_SVG.
                    html = _HTML_PAGE.replace("{{ICON_SVG}}", _ICON_SVG)
                    self._send_text(html, "text/html; charset=utf-8")
                    return
                if path == "/manifest.json":
                    # PWA manifest — browser lo detecta y ofrece "Install app"
                    self._send_text(
                        _MANIFEST_JSON, "application/manifest+json; charset=utf-8"
                    )
                    return
                if path == "/icon.svg":
                    # Icono para manifest + apple-touch-icon + favicon.
                    # Cache corto (1h) para que cambios de diseño se propaguen
                    # relativamente rápido. Cuando cambies el diseño SVG y
                    # quieras forzar fetch inmediato, bumpeá el `?v=N` en los
                    # href del HTML + src en el manifest.
                    self.send_response(200)
                    data = _ICON_SVG.encode("utf-8")
                    self.send_header("Content-Type", "image/svg+xml")
                    self.send_header("Cache-Control", "public, max-age=3600")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if path == "/api/state":
                    try:
                        state = state_provider() or {}
                    except Exception:
                        state = {}
                    # Asegurar json-serializable: convertimos sets a list
                    self._send_json(state)
                    return
                if path.startswith("/api/map/"):
                    try:
                        map_id = int(path.rsplit("/", 1)[1])
                    except (ValueError, IndexError):
                        self._send_json({"error": "invalid map_id"}, status=400)
                        return
                    if map_cache is None:
                        self._send_json({"error": "map_cache unavailable"}, status=503)
                        return
                    entry = map_cache.get(map_id)
                    if entry is None:
                        self._send_json({"error": "map not found", "map_id": map_id}, status=404)
                        return
                    cells_json = [
                        {"id": c.cell_id, "x": c.x, "y": c.y, "walkable": c.is_walkable}
                        for c in entry["cells"]
                    ]
                    self._send_json({
                        "map_id": map_id,
                        "width": entry["width"],
                        "height": entry["height"],
                        "cells": cells_json,
                    }, cache_max_age=3600)
                    return
                if path == "/healthz":
                    self._send_text("ok", "text/plain")
                    return
                if path == "/api/game_state":
                    # F7.C1 — JSON read-only del GameState (con timestamps).
                    if game_state_provider is None:
                        self._send_json({"error": "game_state_provider not configured"}, status=503)
                        return
                    try:
                        gs_dict = game_state_provider() or {}
                    except Exception as exc:
                        self._send_json({"error": "game_state error", "detail": str(exc)}, status=500)
                        return
                    self._send_json(gs_dict)
                    return
                self._send_text("Not Found", "text/plain", status=404)
            except Exception:
                traceback.print_exc()
                try:
                    self._send_text("Internal Error", "text/plain", status=500)
                except Exception:
                    pass

        def do_POST(self):  # noqa: N802
            """Control endpoints: pause, resume, stop del bot desde el web.

            Delega al control_handler provisto por la GUI (que llama a los
            métodos de bot_thread). Si no hay handler configurado, 503.
            """
            path = self.path.split("?", 1)[0]
            try:
                if not path.startswith("/api/control/"):
                    self._send_json({"error": "unknown endpoint"}, status=404)
                    return
                if control_handler is None:
                    self._send_json({"error": "control disabled on server"}, status=503)
                    return
                action = path.rsplit("/", 1)[1]
                if action not in ("pause", "resume", "stop"):
                    self._send_json(
                        {"error": f"unknown action {action!r}",
                         "allowed": ["pause", "resume", "stop"]},
                        status=400,
                    )
                    return
                try:
                    result = control_handler(action) or {}
                except Exception as exc:
                    result = {"ok": False, "error": repr(exc)}
                self._send_json(result)
            except Exception:
                traceback.print_exc()
                try:
                    self._send_json({"ok": False, "error": "internal"}, status=500)
                except Exception:
                    pass

    return _Handler
