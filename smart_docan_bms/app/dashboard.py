"""Read-only Home Assistant ingress dashboard (local HTTP on port 1895)."""
from __future__ import annotations

from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from pathlib import Path

_CSS = """
*{box-sizing:border-box}body{margin:0;background:#101923;color:#f1f5fa;font:15px system-ui,Arial;padding:22px}
h1{margin:0 0 4px;font-size:24px}.app-brand{display:flex;align-items:center;gap:12px}.app-brand img{width:52px;height:52px;object-fit:contain;border-radius:10px}.muted{color:#96a6b8}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;margin-top:16px}
.card{background:#192733;border:1px solid #304150;border-radius:14px;padding:18px;min-width:0}
.good{color:#5dd3ac}.bad{color:#ff8d92}.chips{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.chip{background:#253545;border-radius:8px;padding:12px;min-width:0}
.chip b{display:block;font-size:20px;overflow-wrap:anywhere}
small{color:#97abbd}h2{font-size:17px;margin:2px 0 14px}h3{font-size:14px;color:#bdccdb;margin:18px 0 8px}
.cells{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}.cell{background:#253747;border-radius:6px;padding:8px;text-align:center}
.cell span{display:block;color:#a8b6c4;font-size:11px}.cell b{font-size:13px}
.tag{font-size:12px}.warn{background:#4c302e}.footer{font-size:12px;margin-top:18px}
.warning{background:#442a2e;border:1px solid #9b515c;border-radius:10px;padding:12px;margin:12px 0}
details{background:#14212d;padding:10px;border-radius:10px;margin-top:12px}
summary{cursor:pointer;color:#c6d4e2}dl{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
dt{color:#91a9bb;font-size:12px}dd{margin:0;font-weight:600;overflow-wrap:anywhere}
@media(max-width:450px){body{padding:12px}.chips{grid-template-columns:repeat(2,1fr)}.cells{grid-template-columns:repeat(4,1fr)}}
"""

def render(snapshot: dict) -> str:
    """Render a complete page; escape all untrusted names and values."""
    records = snapshot.get("batteries", [])
    summary = snapshot.get("aggregate", {})
    def fmt(value, suffix=""):
        return ("—" if value is None else escape(str(value)) + suffix)
    status = "Connecté" if snapshot.get("mqtt_connected") else "Non connecté"
    poll = int(snapshot.get("poll_seconds", 30))
    refresh_seconds = int(snapshot.get("dashboard_refresh_seconds", 20))
    cycle_duration = snapshot.get("cycle_duration_seconds")
    cycle_period = snapshot.get("last_cycle_period_seconds")
    cards = []
    for item in records:
        d = item.get("data") or {}
        name = escape(item.get("name") or item.get("address") or "BMS")
        slot = item.get("slot")
        state = "En ligne" if item.get("online") else "Hors ligne"
        hue = "good" if item.get("online") else "bad"
        chips = "".join(
            f'<div class="chip"><small>{label}</small><b>{fmt(d.get(key), suffix)}</b></div>'
            for key, label, suffix in (
                ("battery_level", "SOC", " %"), ("voltage", "Tension", " V"),
                ("current", "Courant", " A"), ("power", "Puissance", " W"),
                ("delta_cell_voltage", "Delta cellules", " V"),
                ("battery_health", "SOH", " %"),
            )
        )
        cell_items = [(k, v) for k, v in sorted(d.items()) if k.startswith("cell_") and k[5:].isdigit()]
        cells = "".join(
            f'<div class="cell"><span>Cellule {int(key[5:])}</span><b>{fmt(value, " V")}</b></div>'
            for key, value in cell_items
        )
        temp_items = [(k, v) for k, v in sorted(d.items()) if k.startswith("temp_") and k[5:].isdigit()]
        temps = ", ".join(f'Sonde {int(key[5:])}: {fmt(value, " °C")}' for key, value in temp_items) or "Non disponible"
        alerts = escape(", ".join(d.get("problems", [])) or ("Alarme signalée" if d.get("problem") else "Aucune alarme signalée"))
        error = escape(item.get("error") or "")
        detail_fields = (
            ("design_capacity", "Capacité nominale", " Ah"),
            ("cycle_charge", "Capacité restante", " Ah"),
            ("stored_energy_wh", "Énergie stockée estimée", " Wh"),
            ("cell_min_index", "Cellule minimale n°", ""),
            ("cell_max_index", "Cellule maximale n°", ""),
            ("cell_min_voltage", "Tension minimale", " V"),
            ("cell_max_voltage", "Tension maximale", " V"),
            ("temp_count", "Nombre de sondes annoncé", ""),
            ("balancer", "Équilibrage", ""),
            ("chrg_mosfet", "MOSFET charge", ""),
            ("dischrg_mosfet", "MOSFET décharge", ""),
            ("problem_code", "Code diagnostic", ""),
        )
        details = "".join(
            f"<div><dt>{title}</dt><dd>{fmt(d[key], unit)}</dd></div>"
            for key, title, unit in detail_fields if key in d
        )
        if slot:
            details += (f"<div><dt>Emplacement fixe</dt><dd>{int(slot)}</dd></div>"
                        f"<div><dt>Adresse Bluetooth associée</dt>"
                        f"<dd>{escape(str(item.get('address') or '—'))}</dd></div>")
        temp_debug = escape(item.get("temperature_diagnostic") or "Données non renseignées")
        cards.append(
            f'<section class="card"><h2>{name} <span class="{hue} tag">• {state}</span></h2>'
            f'<div class="chips">{chips}</div><h3>Cellules ({len(cell_items)})</h3>'
            f'<div class="cells">{cells or "<small>En attente des données</small>"}</div>'
            f'<h3>Températures</h3><p>{temps}</p>'
            f'<p class="muted tag">{temp_debug if not temp_items else ""}</p>'
            f'<h3>Diagnostic</h3><p>{alerts}</p>'
            f'<details><summary>Autres informations disponibles</summary><dl>{details}</dl></details>'
            f'<p class="muted tag">Signal : {fmt(d.get("rssi"), " dBm")} | '
            f'Cycles : {fmt(d.get("cycles"))} | Dernière lecture : {fmt(item.get("last_seen"))}'
            f'<br>{error}</p></section>'
        )
    if not cards:
        cards.append('<section class="card">Recherche des BMS DWF… Vérifiez le Bluetooth et les journaux de l’add-on.</section>')
    count = summary.get("online_count", 0)
    expected = summary.get("expected_count", 0)
    head = (
        f'<p class="muted">{count}/{expected} batteries en ligne · MQTT : {escape(status)} · '
        f'Lecture seule · Lectures BMS / publications MQTT : cible {poll} s · '
        f'Actualisation de cette page : {refresh_seconds} s</p>'
        f'<p class="muted tag">Durée du dernier cycle complet : '
        f'{fmt(cycle_duration, " s")} (recherche Bluetooth comprise). '
        f'Intervalle réel entre les deux derniers cycles : {fmt(cycle_period, " s")}. '
        'Si la durée d’un cycle dépasse la cible, le Bluetooth limite la cadence.</p>'
    )
    if not snapshot.get("mqtt_connected"):
        head += (
            '<div class="warning"><strong>MQTT non connecté</strong>'
            f'<p>Broker : {escape(snapshot.get("mqtt_target") or "Non déterminé")}<br>'
            f'Diagnostic : {escape(snapshot.get("mqtt_error") or "En attente de connexion")}<br>'
            f'Échecs constatés : {fmt(snapshot.get("mqtt_failures", 0))}</p>'
            '<small>Le Bluetooth reste opérationnel. Consulter le journal de l’add-on et le broker Mosquitto.</small>'
            '</div>'
        )
    if summary.get("complete"):
        head += (
            '<div class="grid"><section class="card"><h2>Parc batteries (câblage parallèle)</h2>'
            f'<div class="chips"><div class="chip"><small>SOC global</small><b>{fmt(summary.get("battery_level"), " %")}</b></div>'
            f'<div class="chip"><small>Courant total</small><b>{fmt(summary.get("current"), " A")}</b></div>'
            f'<div class="chip"><small>Puissance totale</small><b>{fmt(summary.get("power"), " W")}</b></div></div>'
            '</section></div>'
        )
    else:
        head += '<p class="bad">Synthèse suspendue : données manquantes sur au moins une batterie.</p>'
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="refresh" content="{refresh_seconds}"><title>Smart Docan BMS</title>'
        f'<style>{_CSS}</style></head><body>'
        '<div class="app-brand"><img src="logo.png" alt="Smart Docan BMS">'
        f'<h1>Smart Docan BMS</h1></div>{head}'
        f'<main class="grid">{"".join(cards)}</main>'
        '<p class="muted footer">Données indicatives : ne pas utiliser pour une protection de batterie.</p></body></html>'
    )

def start_dashboard(snapshot_callable, port: int = 1895) -> ThreadingHTTPServer:
    """Start ingress-compatible status page in a daemon thread."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.rstrip("/").endswith("/health"):
                content = json.dumps({"status": "ok"}).encode()
                mime = "application/json"
            elif self.path.rstrip("/").endswith("/logo.png"):
                content = Path("/app/logo.png").read_bytes()
                mime = "image/png"
            else:
                content = render(snapshot_callable()).encode("utf-8")
                mime = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        def log_message(self, fmt, *args):
            return
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
