#!/usr/bin/env python3
"""
Bot de Telegram para monitorización de vuelos en tiempo real.

Usa la API de Airlabs (1 000 req/mes gratis).
https://airlabs.co

Comandos:
  /registrarvuelo <CÓDIGO>  — Registra un vuelo para seguimiento
  /eliminarvuelo  <CÓDIGO>  — Deja de seguir un vuelo
  /vuelos                   — Lista los vuelos activos en este grupo
  /estado         <CÓDIGO>  — Consulta el estado actual de un vuelo
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ─────────────────────────────────────────────────────────────────────────────
#  Configuración
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
AIRLABS_API_KEY = os.getenv("AIRLABS_API_KEY", "")
AIRLABS_BASE    = "https://airlabs.co/api/v9"
DB_PATH         = os.getenv("DB_PATH", "flights.db")

# Minutos de retraso mínimos para notificar un cambio de retraso
DELAY_THRESHOLD = int(os.getenv("DELAY_THRESHOLD", "5"))

# Ventana de monitorización: desde DEP - WINDOW_PRE hasta DEP + WINDOW_POST
WINDOW_PRE_H  = int(os.getenv("WINDOW_PRE_H",  "3"))   # horas antes de salida
WINDOW_POST_H = int(os.getenv("WINDOW_POST_H", "1"))   # horas después de salida

# Intervalo del job de monitorización en segundos (por defecto 15 min)
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "900"))

logging.basicConfig(
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Base de datos  (SQLite local)
# ─────────────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Crea la tabla de vuelos si no existe."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vuelos (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id       INTEGER NOT NULL,
                flight_code   TEXT    NOT NULL COLLATE NOCASE,
                dep_time_utc  TEXT,
                last_status   TEXT    DEFAULT 'unknown',
                last_delay    INTEGER DEFAULT 0,
                registered_at TEXT    NOT NULL,
                last_checked  TEXT,
                active        INTEGER DEFAULT 1,
                UNIQUE(chat_id, flight_code)
            )
        """)


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def db_upsert(chat_id: int, code: str, dep_utc: Optional[str], status: str) -> None:
    """Inserta o actualiza el vuelo en la BD."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO vuelos (chat_id, flight_code, dep_time_utc, last_status, registered_at, active)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(chat_id, flight_code) DO UPDATE SET
                dep_time_utc = COALESCE(excluded.dep_time_utc, dep_time_utc),
                last_status  = excluded.last_status,
                active       = 1
            """,
            (chat_id, code.upper(), dep_utc, status, _now_str()),
        )


def db_deactivate(chat_id: int, code: str) -> bool:
    """Desactiva el seguimiento de un vuelo. Devuelve True si existía."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "UPDATE vuelos SET active = 0 WHERE chat_id = ? AND flight_code = ? COLLATE NOCASE",
            (chat_id, code.upper()),
        )
        return cur.rowcount > 0


def db_all_active() -> list:
    """Devuelve todos los vuelos activos de todos los grupos."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM vuelos WHERE active = 1").fetchall()


def db_for_chat(chat_id: int) -> list:
    """Devuelve los vuelos activos de un grupo concreto."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM vuelos WHERE chat_id = ? AND active = 1 ORDER BY dep_time_utc",
            (chat_id,),
        ).fetchall()


def db_update_status(
    chat_id: int,
    code: str,
    status: str,
    delay: int,
    dep_utc: Optional[str] = None,
) -> None:
    """Actualiza estado, retraso, hora de salida y timestamp de última consulta."""
    with sqlite3.connect(DB_PATH) as conn:
        if dep_utc:
            conn.execute(
                "UPDATE vuelos SET last_status=?, last_delay=?, last_checked=?, dep_time_utc=? "
                "WHERE chat_id=? AND flight_code=? COLLATE NOCASE",
                (status, delay, _now_str(), dep_utc, chat_id, code.upper()),
            )
        else:
            conn.execute(
                "UPDATE vuelos SET last_status=?, last_delay=?, last_checked=? "
                "WHERE chat_id=? AND flight_code=? COLLATE NOCASE",
                (status, delay, _now_str(), chat_id, code.upper()),
            )


# ─────────────────────────────────────────────────────────────────────────────
#  API de Airlabs
# ─────────────────────────────────────────────────────────────────────────────

STATUS_LABELS: dict[str, str] = {
    "scheduled": "🕐 Programado",
    "en-route":  "✈️ En vuelo",
    "active":    "✈️ En vuelo",
    "landed":    "🛬 Aterrizado",
    "cancelled": "❌ Cancelado",
    "diverted":  "⚠️ Desviado",
    "incident":  "🚨 Incidente",
    "unknown":   "❓ Desconocido",
}


def _api_get(endpoint: str, params: dict) -> Optional[dict]:
    """Realiza una petición GET a la API de Airlabs."""
    params = {**params, "api_key": AIRLABS_API_KEY}
    try:
        resp = requests.get(
            f"{AIRLABS_BASE}/{endpoint}",
            params=params,
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning("Airlabs /%s → HTTP %d: %s", endpoint, resp.status_code, resp.text[:300])
    except requests.RequestException as exc:
        logger.error("Airlabs /%s error: %s", endpoint, exc)
    return None


def fetch_flight(code: str) -> Optional[dict]:
    """
    Consulta el estado en tiempo real de un vuelo por código IATA.
    Devuelve el dict 'response' de Airlabs, o None si no hay datos.
    """
    data = _api_get("flight", {"flight_iata": code.upper()})
    if data:
        return data.get("response")
    return None


def parse_dep_utc(f: dict) -> Optional[datetime]:
    """Extrae la hora de salida en UTC del dict de vuelo."""
    for key in ("dep_time_utc", "dep_actual_utc", "dep_estimated_utc", "dep_time"):
        val = f.get(key)
        if val:
            try:
                return datetime.strptime(str(val)[:16], "%Y-%m-%d %H:%M").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
    return None


def format_flight(f: dict, header: str) -> str:
    """Formatea los datos de un vuelo como mensaje de Telegram (Markdown)."""
    code    = f.get("flight_iata", "?")
    airline = f.get("airline_iata", "")
    dep_ap  = f.get("dep_iata", "?")
    arr_ap  = f.get("arr_iata", "?")
    dep_t   = f.get("dep_time", f.get("dep_time_utc", "—"))
    arr_t   = f.get("arr_time", f.get("arr_time_utc", "—"))
    status  = STATUS_LABELS.get(f.get("status", ""), f.get("status") or "Desconocido")
    dep_dly = int(f.get("dep_delayed") or 0)
    arr_dly = int(f.get("arr_delayed") or 0)

    lines = [
        f"*{header}*",
        f"✈️  `{code}`" + (f"  ·  {airline}" if airline else ""),
        f"🛫  {dep_ap}  →  🛬  {arr_ap}",
        f"📅  Salida:   `{dep_t}` UTC",
        f"📅  Llegada:  `{arr_t}` UTC",
        f"📊  Estado:   {status}",
    ]

    if dep_dly > 0:
        lines.append(f"⏱️  Retraso salida:   *{dep_dly} min*")
    if arr_dly > 0:
        lines.append(f"⏱️  Retraso llegada:  *{arr_dly} min*")

    for field, label in (
        ("dep_terminal",  "🏢  Terminal salida"),
        ("dep_gate",      "🚪  Puerta salida"),
        ("arr_terminal",  "🏢  Terminal llegada"),
        ("arr_gate",      "🚪  Puerta llegada"),
        ("arr_baggage",   "🧳  Cinta equipaje"),
        ("aircraft_icao", "🛩️  Aeronave"),
    ):
        if f.get(field):
            lines.append(f"{label}: {f[field]}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
#  Comandos del bot
# ─────────────────────────────────────────────────────────────────────────────

_HELP = (
    "👋  *Bot de seguimiento de vuelos*\n\n"
    "Comandos disponibles:\n"
    "• /registrarvuelo `CÓDIGO`  — Registrar vuelo para seguimiento\n"
    "• /eliminarvuelo  `CÓDIGO`  — Dejar de seguir un vuelo\n"
    "• /vuelos                   — Ver vuelos activos en este grupo\n"
    "• /estado         `CÓDIGO`  — Consultar estado actual de un vuelo\n\n"
    "Ejemplo: `/registrarvuelo EY104`\n\n"
    "📡  Las notificaciones se envían automáticamente cada 15 min *si hay cambios* "
    f"durante las {WINDOW_PRE_H} h previas y {WINDOW_POST_H} h posteriores a la salida."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP, parse_mode="Markdown")


async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP, parse_mode="Markdown")


async def cmd_registrar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "⚠️  Uso: /registrarvuelo `CÓDIGO`\nEjemplo: /registrarvuelo EY104",
            parse_mode="Markdown",
        )
        return

    code    = context.args[0].upper()
    chat_id = update.effective_chat.id

    wait_msg = await update.message.reply_text(
        f"🔍  Buscando vuelo *{code}*…", parse_mode="Markdown"
    )

    f = fetch_flight(code)
    if not f:
        await wait_msg.edit_text(
            f"❌  No se encontró información para *{code}*.\n"
            "Comprueba el código IATA e inténtalo de nuevo.\n"
            "_Nota: algunos vuelos no aparecen hasta el día de salida._",
            parse_mode="Markdown",
        )
        return

    dep     = parse_dep_utc(f)
    dep_str = dep.strftime("%Y-%m-%d %H:%M") if dep else None
    status  = f.get("status", "unknown")

    db_upsert(chat_id, code, dep_str, status)

    reply = format_flight(f, f"✅  Vuelo {code} registrado")

    if dep:
        now       = datetime.now(timezone.utc)
        win_start = dep - timedelta(hours=WINDOW_PRE_H)
        if win_start > now:
            hours_left = (win_start - now).total_seconds() / 3600
            reply += (
                f"\n\n📡  _Seguimiento activo a partir de las_ "
                f"`{win_start.strftime('%H:%M')}` _UTC "
                f"(en {hours_left:.0f} h aprox.)_"
            )
        else:
            reply += "\n\n📡  _Seguimiento activo. Recibiréis notificaciones si hay cambios._"
    else:
        reply += (
            "\n\n📡  _No se pudo determinar la hora de salida. "
            "Se seguirá intentando en los próximos ciclos._"
        )

    await wait_msg.edit_text(reply, parse_mode="Markdown")


async def cmd_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "⚠️  Uso: /eliminarvuelo `CÓDIGO`", parse_mode="Markdown"
        )
        return

    code = context.args[0].upper()
    if db_deactivate(update.effective_chat.id, code):
        await update.message.reply_text(
            f"✅  Vuelo *{code}* eliminado del seguimiento.", parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"⚠️  El vuelo *{code}* no estaba registrado en este grupo.",
            parse_mode="Markdown",
        )


async def cmd_vuelos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    vuelos = db_for_chat(update.effective_chat.id)
    if not vuelos:
        await update.message.reply_text(
            "📭  No hay vuelos registrados en este grupo.\n"
            "Usa /registrarvuelo `CÓDIGO` para añadir uno.",
            parse_mode="Markdown",
        )
        return

    lines = ["*Vuelos activos en este grupo:*\n"]
    for v in vuelos:
        st  = STATUS_LABELS.get(v["last_status"] or "", v["last_status"] or "?")
        dep = v["dep_time_utc"] or "hora desconocida"
        dly = int(v["last_delay"] or 0)
        line = f"• `{v['flight_code']}` — {st}  ·  📅 {dep} UTC"
        if dly > 0:
            line += f"  _(+{dly} min)_"
        lines.append(line)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "⚠️  Uso: /estado `CÓDIGO`", parse_mode="Markdown"
        )
        return

    code     = context.args[0].upper()
    wait_msg = await update.message.reply_text(
        f"🔍  Consultando *{code}*…", parse_mode="Markdown"
    )

    f = fetch_flight(code)
    if not f:
        await wait_msg.edit_text(
            f"❌  Sin datos en tiempo real para *{code}* en este momento.\n"
            "_El vuelo puede no haber entrado aún en el sistema o estar fuera del rango de la API._",
            parse_mode="Markdown",
        )
        return

    await wait_msg.edit_text(
        format_flight(f, f"Estado actual — {code}"), parse_mode="Markdown"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Lógica de polling por fases  (ahorro de requests)
# ─────────────────────────────────────────────────────────────────────────────

def _elapsed_min(last_chk_str: Optional[str], now: datetime) -> float:
    """Minutos transcurridos desde la última consulta. Infinito si nunca se consultó."""
    if not last_chk_str:
        return float("inf")
    try:
        last_dt = datetime.strptime(last_chk_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        return (now - last_dt).total_seconds() / 60
    except ValueError:
        return float("inf")


def should_poll(vuelo: sqlite3.Row, now: datetime) -> bool:
    """
    Decide si hay que llamar a la API ahora para este vuelo.

    Fases (consumo estimado para un vuelo diario recurrente):
    ┌──────────────────────────────┬──────────┬──────────────┐
    │ Fase                         │ Intervalo│ Calls/día    │
    ├──────────────────────────────┼──────────┼──────────────┤
    │ Sin hora de salida conocida  │  1 h     │ ~24          │
    │ Salida en más de 24 h        │  6 h     │   4          │
    │ Salida en 3–24 h             │  2 h     │  ~10         │
    │ Ventana activa (−3 h a +1 h) │ 15 min   │  16  ← pico  │
    │ Aterrizado/cancelado         │  dormido │   0          │
    │   └─ se reactiva en dep+21 h │  (=3 h antes del día siguiente)        │
    └──────────────────────────────┴──────────┴──────────────┘
    Con 3 vuelos: ~48 calls/día en pico → bien dentro de 1 000/mes.
    """
    dep_str     = vuelo["dep_time_utc"]
    last_status = vuelo["last_status"] or "unknown"
    last_chk    = vuelo["last_checked"]
    elapsed     = _elapsed_min(last_chk, now)

    # Sin hora de salida conocida → cada hora
    if not dep_str:
        return elapsed >= 60

    try:
        dep = datetime.strptime(dep_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return elapsed >= 60

    hours_to_dep = (dep - now).total_seconds() / 3600  # positivo = salida en el futuro

    # ── Más de 1 h después de la salida programada ──────────────────────────
    if hours_to_dep < -WINDOW_POST_H:
        if last_status in ("landed", "cancelled"):
            # Vuelo diario: misma hora mañana.
            # Dormir hasta dep+21 h (= 3 h antes de la salida del día siguiente).
            next_wake = dep + timedelta(hours=21)
            return now >= next_wake
        # Estado desconocido post-salida → revisar cada hora
        return elapsed >= 60

    # ── Ventana activa: –3 h … +1 h respecto a la salida ───────────────────
    if hours_to_dep <= WINDOW_PRE_H:
        return True  # el intervalo del job (15 min) ya controla la cadencia

    # ── Salida dentro de 3–24 h → cada 2 h ──────────────────────────────────
    if hours_to_dep <= 24:
        return elapsed >= 120

    # ── Salida en más de 24 h → cada 6 h ────────────────────────────────────
    return elapsed >= 360


# ─────────────────────────────────────────────────────────────────────────────
#  Job de monitorización  (ejecutado cada MONITOR_INTERVAL segundos)
# ─────────────────────────────────────────────────────────────────────────────

async def monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(timezone.utc)
    bot = context.bot

    for vuelo in db_all_active():
        chat_id    = vuelo["chat_id"]
        code       = vuelo["flight_code"]
        old_status = vuelo["last_status"] or "unknown"
        old_delay  = int(vuelo["last_delay"] or 0)

        # ── Decidir si es momento de consultar la API ────────────────────────
        if not should_poll(vuelo, now):
            continue

        # ── Consultar la API ────────────────────────────────────────────────
        f = fetch_flight(code)
        if not f:
            continue

        new_status  = f.get("status", "unknown")
        new_delay   = int(f.get("dep_delayed") or 0)
        new_dep     = parse_dep_utc(f)
        new_dep_str = new_dep.strftime("%Y-%m-%d %H:%M") if new_dep else None

        status_changed = new_status != old_status
        delay_changed  = abs(new_delay - old_delay) >= DELAY_THRESHOLD

        # Guardar siempre el estado actualizado y la hora de salida si la obtuvimos
        db_update_status(chat_id, code, new_status, new_delay, new_dep_str)

        if not status_changed and not delay_changed:
            continue

        # ── Enviar notificación de cambio ───────────────────────────────────
        if status_changed:
            old_lbl = STATUS_LABELS.get(old_status, old_status)
            new_lbl = STATUS_LABELS.get(new_status, new_status)
            header  = f"🔔  Cambio de estado — {code}\n{old_lbl}  →  {new_lbl}"
        else:
            delta  = new_delay - old_delay
            sign   = "+" if delta > 0 else ""
            header = f"🔔  Cambio de retraso — {code}  ({sign}{delta} min)"

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=format_flight(f, header),
                parse_mode="Markdown",
            )
            logger.info(
                "Notificación enviada | vuelo=%s | estado=%s | chat=%d",
                code, new_status, chat_id,
            )
        except Exception as exc:
            logger.error("Error enviando notificación a chat %d: %s", chat_id, exc)

        # ── 5. Desactivar si el vuelo ya terminó ────────────────────────────
        if new_status in ("landed", "cancelled"):
            db_deactivate(chat_id, code)
            logger.info("Vuelo %s desactivado (estado final: %s)", code, new_status)


# ─────────────────────────────────────────────────────────────────────────────
#  Punto de entrada
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise SystemExit(
            "❌  TELEGRAM_TOKEN no configurado.\n"
            "   Copia .env.example a .env y rellena los valores."
        )
    if not AIRLABS_API_KEY:
        raise SystemExit(
            "❌  AIRLABS_API_KEY no configurado.\n"
            "   Regístrate en https://airlabs.co y copia tu API key en .env"
        )

    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("ayuda",          cmd_ayuda))
    app.add_handler(CommandHandler("registrarvuelo", cmd_registrar))
    app.add_handler(CommandHandler("eliminarvuelo",  cmd_eliminar))
    app.add_handler(CommandHandler("vuelos",         cmd_vuelos))
    app.add_handler(CommandHandler("estado",         cmd_estado))

    # Job de monitorización: primer ciclo a los 60 s de arrancar, luego cada MONITOR_INTERVAL s
    app.job_queue.run_repeating(
        monitor_job,
        interval=MONITOR_INTERVAL,
        first=60,
        name="monitor_vuelos",
    )

    logger.info(
        "🤖  Bot arrancado. Intervalo de monitorización: %d s. Pulsa Ctrl+C para parar.",
        MONITOR_INTERVAL,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
