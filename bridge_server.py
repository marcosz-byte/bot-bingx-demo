"""
Puente TradingView -> BingX (cuenta DEMO / VST)  —  v4
========================================================

Cambio de esta v4 respecto a v3: reparto REAL de la posición entre TP1,
TP2 y TP3 (antes, el 100% de la posición salía en TP1 — TP2/TP3 eran solo
informativos, sin ninguna orden real detrás).

Cómo funciona ahora:
1. Al entrar, la cantidad total ("qty" del payload) se divide en 3 partes
   según TP1_PCT / TP2_PCT / TP3_PCT (por defecto 33% / 33% / 34%,
   configurable por variable de entorno, deben sumar 1.0).
2. Se colocan 3 órdenes take_profit_market en BingX, una en cada nivel
   (tp1, tp2, tp3 del payload), cada una por su parte correspondiente de
   la cantidad. Si el precio llega a TP1, se cierra esa porción; si sigue
   y llega a TP2, se cierra otra porción; etc. — reparto real en el
   exchange, no depende de que el bridge siga corriendo.
3. Ajuste de precisión: cada cantidad se redondea al step que exige el
   símbolo en BingX (exchange.amount_to_precision). El resto de redondeo
   se le asigna a la pierna de TP3 para que la suma de las 3 siga dando
   exactamente el total (no queda una micro-fracción de posición sin
   orden). Si con una qty muy chica alguna pierna redondea a 0, esa orden
   se omite (se loguea un warning) — quedaría como una porción sin TP
   propio; revisá esto si operás con cantidades muy pequeñas.

CAMBIO IMPORTANTE DE SEGURIDAD que viene de la mano de lo anterior:
Antes (v2/v3), el cierre por SL/max_bars_exit mandaba una orden a mercado
por el 100% de la "qty" que el bridge tenía registrada al momento de la
entrada. Eso ya no es seguro con TPs parciales: si TP1 ya cerró un 33% de
la posición y después llega un sl_hit, cerrar "el 100% original" intenta
cerrar MÁS de lo que en realidad sigue abierto en el exchange.
Para evitar esto, antes de mandar el cierre a mercado el bridge ahora le
pregunta a BingX cuánto hay realmente abierto en ese momento
(exchange.fetch_positions) y cierra esa cantidad real — no la cantidad
"de memoria". Si esa consulta falla por algún motivo, cae de nuevo al
comportamiento anterior (usar la cantidad registrada) como último
recurso, dejando un warning en el log para que se pueda revisar a mano.

Todo lo demás es igual a v3 (respuesta instantánea al webhook vía
BackgroundTasks, SL de emergencia ancho, manejo de sl_hit/max_bars_exit,
cierre de posición previa antes de abrir una nueva).

LIMITACIONES (heredadas, sin cambios):
- OPEN_POSITIONS vive en memoria — se pierde si el servidor reinicia con
  un trade abierto (la posición en BingX sigue abierta, solo se pierde la
  referencia a sus órdenes condicionales).
- El trailing del indicador (mover SL a breakeven, etc.) sigue siendo solo
  para las estadísticas del backtest — no se refleja como orden real en
  el exchange.
- Parámetros de la API de BingX (positionSide, stopPrice, fetch_positions,
  set_leverage) pueden variar según versión de CCXT y el modo de cuenta.
  Puntualmente, la lectura de "cuánto queda abierto" vía fetch_positions
  es la parte más nueva de este archivo — probala primero en DEMO y
  revisá los logs/​/health después de que un TP parcial se ejecute, antes
  de confiar en ella para plata real.
"""

import os
import time
import logging
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
import ccxt

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tv-bingx-bridge")

app = FastAPI()

# ── Configuración vía variables de entorno (nunca hardcodees claves) ──
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]          # tu propio secreto, inventado por ti
BINGX_API_KEY = os.environ["BINGX_API_KEY"]
BINGX_API_SECRET = os.environ["BINGX_API_SECRET"]

# Fallbacks — solo se usan si una alerta llega sin "qty" / "leverage".
DEFAULT_QUANTITY = float(os.environ.get("ORDER_QUANTITY", "0.001"))
DEFAULT_LEVERAGE = float(os.environ.get("DEFAULT_LEVERAGE", "1"))

# Qué tan lejos, en múltiplos de la distancia del SL real, va el stop de
# emergencia puesto en el exchange.
EMERGENCY_SL_MULT = float(os.environ.get("EMERGENCY_SL_MULT", "3.0"))

# Reparto de la posición entre TP1 / TP2 / TP3. Deben sumar 1.0.
TP1_PCT = float(os.environ.get("TP1_PCT", "0.33"))
TP2_PCT = float(os.environ.get("TP2_PCT", "0.33"))
TP3_PCT = float(os.environ.get("TP3_PCT", "0.34"))

if abs((TP1_PCT + TP2_PCT + TP3_PCT) - 1.0) > 1e-6:
    raise RuntimeError(
        f"TP1_PCT + TP2_PCT + TP3_PCT debe sumar 1.0 (hoy suman "
        f"{TP1_PCT + TP2_PCT + TP3_PCT}). Revisá las variables de entorno."
    )

exchange = ccxt.bingx({
    "apiKey": BINGX_API_KEY,
    "secret": BINGX_API_SECRET,
    "options": {"defaultType": "swap"},  # perpetuos USDT-M
    "enableRateLimit": True,
})
exchange.set_sandbox_mode(True)  # <- clave: apunta al entorno demo/VST de BingX

try:
    exchange.load_markets()
except Exception as e:
    # No es fatal: create_order igual carga los mercados si hace falta.
    # Sin esto, el redondeo de precisión (amount_to_precision) usa un
    # fallback más tosco hasta que los mercados se carguen solos.
    log.warning("No se pudieron precargar los mercados de BingX: %s", e)

# Estado en memoria: qué hay abierto ahora mismo por símbolo.
OPEN_POSITIONS: dict[str, dict] = {}

# Último resultado procesado por símbolo — para poder revisar qué pasó sin
# tener que ir a buscar en los logs. Se pisa en cada evento nuevo.
LAST_RESULT: dict[str, dict] = {}


def to_bingx_symbol(tv_ticker: str) -> str:
    """
    Convierte el ticker que manda TradingView (p.ej. "BINGX:BTCUSDT.P" o
    "BTCUSDT") al formato unificado que espera CCXT (p.ej. "BTC/USDT:USDT").
    Ajusta esta función si operas otros pares o formatos.
    """
    base = tv_ticker.split(":")[-1].replace(".P", "")
    if base.endswith("USDT"):
        coin = base[:-4]
        return f"{coin}/USDT:USDT"
    return base


def _record_result(symbol: str, status: str, detail: dict) -> None:
    LAST_RESULT[symbol] = {"status": status, "ts": time.time(), **detail}


def round_amount(symbol: str, amount: float) -> float:
    """Redondea una cantidad al step que exige el símbolo en BingX. Si por
    algún motivo no se puede (mercados no cargados, símbolo raro), cae a un
    redondeo genérico de 6 decimales en vez de romper el flujo."""
    try:
        return float(exchange.amount_to_precision(symbol, amount))
    except Exception as e:
        log.warning("No se pudo aplicar precisión de %s a %s (%s) — uso redondeo genérico.", symbol, amount, e)
        return round(amount, 6)


def split_qty_by_tp(symbol: str, qty: float) -> dict:
    """Divide la qty total en 3 porciones (TP1/TP2/TP3) según los % configu-
    rados, aplicando la precisión del símbolo. El resto de redondeo se lo
    lleva la porción de TP3, para que la suma de las 3 dé exactamente el
    total (y no quede una fracción de posición sin orden asociada)."""
    qty1 = round_amount(symbol, qty * TP1_PCT)
    qty2 = round_amount(symbol, qty * TP2_PCT)
    qty3 = round_amount(symbol, qty - qty1 - qty2)

    for label, q in (("TP1", qty1), ("TP2", qty2), ("TP3", qty3)):
        if q <= 0:
            log.warning(
                "La porción de %s en %s redondeó a 0 (qty total muy chica para el "
                "step del símbolo) — esa orden se omite y esa parte de la posición "
                "queda sin TP propio.",
                label, symbol,
            )

    return {"tp1": qty1, "tp2": qty2, "tp3": qty3}


def get_remaining_qty(symbol: str, position_side: str, fallback_qty: float) -> float:
    """Le pregunta a BingX cuánto queda realmente abierto en este símbolo/
    lado antes de mandar un cierre a mercado — necesario ahora que puede
    haber TPs parciales ya ejecutados, así que "la qty original" ya no es
    confiable. Si la consulta falla, usa fallback_qty (comportamiento viejo)
    y deja un warning para revisar a mano."""
    try:
        positions = exchange.fetch_positions([symbol])
        for p in positions:
            side = str(p.get("side") or "").upper()          # ccxt unificado: "long"/"short"
            info_side = str((p.get("info") or {}).get("positionSide") or "").upper()
            if side == position_side or info_side == position_side:
                contracts = p.get("contracts")
                if contracts is not None and float(contracts) > 0:
                    return float(contracts)
        log.warning(
            "fetch_positions no encontró posición abierta en %s (%s) — probablemente ya "
            "estaba cerrada (TP la cerró toda) o hedge mode no expone 'side'/'positionSide' "
            "como se esperaba. Uso fallback_qty=%s.",
            symbol, position_side, fallback_qty,
        )
        return fallback_qty
    except Exception as e:
        log.warning("No se pudo consultar la posición real en %s vía fetch_positions: %s — uso fallback_qty=%s.", symbol, e, fallback_qty)
        return fallback_qty


def cancel_order_safe(symbol: str, order_id: Optional[str], label: str) -> None:
    """Cancela una orden condicional sin tirar abajo el flujo si ya no existe
    (por ejemplo, si ya se ejecutó — un TP puede haber hecho fill antes de
    que llegue un sl_hit, y viceversa)."""
    if not order_id:
        return
    try:
        exchange.cancel_order(order_id, symbol)
        log.info("Cancelada orden %s (%s) en %s", label, order_id, symbol)
    except Exception as e:
        log.warning("No se pudo cancelar orden %s (%s) en %s: %s", label, order_id, symbol, e)


def close_position_market(symbol: str, reason: str) -> Optional[dict]:
    """Cierra a mercado lo que quede realmente abierto en ese símbolo (con-
    sultado en vivo a BingX, no lo que el bridge tenga en memoria) y cancela
    las órdenes condicionales colgando (SL de emergencia + TP1/TP2/TP3).
    Devuelve la orden de cierre, o None si no había nada registrado para
    ese símbolo."""
    pos = OPEN_POSITIONS.pop(symbol, None)
    if pos is None:
        log.info("close_position_market(%s): nada registrado para cerrar (razón: %s)", symbol, reason)
        return None

    exit_side = "sell" if pos["position_side"] == "LONG" else "buy"
    real_qty = get_remaining_qty(symbol, pos["position_side"], fallback_qty=pos["qty"])

    close_order = None
    if real_qty > 0:
        close_order = exchange.create_order(
            symbol=symbol,
            type="market",
            side=exit_side,
            amount=real_qty,
            params={"positionSide": pos["position_side"]},
        )
        log.info("Posición cerrada a mercado en %s por '%s' | qty real: %s | orden: %s", symbol, reason, real_qty, close_order.get("id"))
    else:
        log.info("close_position_market(%s): qty real era 0 (ya estaba cerrada, probablemente por TPs) — no se manda orden de cierre.", symbol)

    cancel_order_safe(symbol, pos.get("sl_order_id"), "SL emergencia")
    cancel_order_safe(symbol, pos.get("tp1_order_id"), "TP1")
    cancel_order_safe(symbol, pos.get("tp2_order_id"), "TP2")
    cancel_order_safe(symbol, pos.get("tp3_order_id"), "TP3")

    return close_order


# ══════════════════════════════════════════════════════════
# TAREAS EN SEGUNDO PLANO — acá vive el trabajo "lento" (llamadas a BingX),
# que corre DESPUÉS de que el endpoint ya le respondió a TradingView.
# ══════════════════════════════════════════════════════════

def process_close_event(symbol: str, event: str) -> None:
    try:
        close_order = close_position_market(symbol, reason=event)
        _record_result(symbol, "closed", {"event": event, "order_id": close_order.get("id") if close_order else None})
    except Exception as e:
        log.exception("Error cerrando posición por evento %s en %s", event, symbol)
        _record_result(symbol, "error", {"event": event, "error": str(e)})


def process_entry(payload: dict) -> None:
    symbol = to_bingx_symbol(payload["ticker"])
    action = payload["action"]
    side = "buy" if action == "buy" else "sell"
    position_side = "LONG" if action == "buy" else "SHORT"
    exit_side = "sell" if action == "buy" else "buy"

    try:
        entry_ref = float(payload["price"])
        sl = float(payload["sl"])
        tp1 = float(payload["tp1"])
        tp2 = float(payload["tp2"])
        tp3 = float(payload["tp3"])
        qty = float(payload.get("qty") or DEFAULT_QUANTITY)
        leverage = float(payload.get("leverage") or DEFAULT_LEVERAGE)

        if qty <= 0:
            log.error("qty inválida en el payload (%s) — se aborta la entrada en %s", payload.get("qty"), symbol)
            _record_result(symbol, "error", {"error": f"qty inválida: {payload.get('qty')}"})
            return

        # Si había una posición previa registrada en este símbolo (stop-and-
        # reverse), cerrarla primero — en modo hedge, BingX no cierra sola
        # una posición LONG solo porque abrís una SHORT nueva.
        if symbol in OPEN_POSITIONS:
            log.info("Había una posición previa registrada en %s — cerrándola antes de abrir la nueva.", symbol)
            close_position_market(symbol, reason="reversal")

        # Apalancamiento — best effort.
        try:
            exchange.set_leverage(leverage, symbol, params={"side": position_side})
        except Exception as e:
            log.warning("No se pudo setear apalancamiento (%sx) en %s: %s", leverage, symbol, e)

        sl_distance = abs(entry_ref - sl)
        emergency_sl = entry_ref - sl_distance * EMERGENCY_SL_MULT if action == "buy" else entry_ref + sl_distance * EMERGENCY_SL_MULT

        entry = exchange.create_order(
            symbol=symbol, type="market", side=side, amount=qty,
            params={"positionSide": position_side},
        )

        stop_loss = exchange.create_order(
            symbol=symbol, type="stop_market", side=exit_side, amount=qty,
            params={"positionSide": position_side, "stopPrice": emergency_sl},
        )

        # Reparto real entre TP1/TP2/TP3 — una orden take_profit_market por
        # cada nivel, cada una por su porción de la qty total.
        qty_split = split_qty_by_tp(symbol, qty)
        tp_levels = {"tp1": tp1, "tp2": tp2, "tp3": tp3}
        tp_orders: dict[str, Optional[dict]] = {"tp1": None, "tp2": None, "tp3": None}

        for label, level_price in tp_levels.items():
            leg_qty = qty_split[label]
            if leg_qty <= 0:
                continue  # ya se logueó el warning en split_qty_by_tp
            tp_orders[label] = exchange.create_order(
                symbol=symbol, type="take_profit_market", side=exit_side, amount=leg_qty,
                params={"positionSide": position_side, "stopPrice": level_price},
            )

        OPEN_POSITIONS[symbol] = {
            "position_side": position_side,
            "qty": qty,
            "sl_order_id": stop_loss.get("id"),
            "tp1_order_id": tp_orders["tp1"].get("id") if tp_orders["tp1"] else None,
            "tp2_order_id": tp_orders["tp2"].get("id") if tp_orders["tp2"] else None,
            "tp3_order_id": tp_orders["tp3"].get("id") if tp_orders["tp3"] else None,
        }

        log.info(
            "Entrada: %s | qty: %s | lev: %sx | SL emergencia: %s (real: %s) | "
            "TP1: %s (qty %s) | TP2: %s (qty %s) | TP3: %s (qty %s)",
            entry.get("id"), qty, leverage, emergency_sl, sl,
            tp_orders["tp1"].get("id") if tp_orders["tp1"] else None, qty_split["tp1"],
            tp_orders["tp2"].get("id") if tp_orders["tp2"] else None, qty_split["tp2"],
            tp_orders["tp3"].get("id") if tp_orders["tp3"] else None, qty_split["tp3"],
        )

        _record_result(symbol, "ok", {
            "entry_order_id": entry.get("id"),
            "qty": qty,
            "leverage": leverage,
            "emergency_sl_price": emergency_sl,
            "real_sl_price": sl,
            "sl_order_id": stop_loss.get("id"),
            "tp1_order_id": tp_orders["tp1"].get("id") if tp_orders["tp1"] else None,
            "tp2_order_id": tp_orders["tp2"].get("id") if tp_orders["tp2"] else None,
            "tp3_order_id": tp_orders["tp3"].get("id") if tp_orders["tp3"] else None,
            "tp_qty_split": qty_split,
        })

    except Exception as e:
        log.exception("Error ejecutando la orden en BingX demo (%s en %s)", action, symbol)
        _record_result(symbol, "error", {"action": action, "error": str(e)})


# ══════════════════════════════════════════════════════════
# ENDPOINT — rápido: valida, parsea, encola. No espera a BingX.
# ══════════════════════════════════════════════════════════

@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request, background_tasks: BackgroundTasks):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="secreto inválido")

    raw_body = await request.body()
    try:
        payload = await request.json()
    except Exception:
        # Causa más común: "Webhook JSON Format" (grupo Alerts del
        # indicador) está en OFF, así que llega texto plano en vez de JSON.
        log.error("Body no es JSON válido: %r", raw_body[:300])
        raise HTTPException(
            status_code=400,
            detail="El body recibido no es JSON válido. Revisá que 'Webhook JSON Format' "
                   "esté activado en el grupo Alerts del indicador.",
        )

    if "ticker" not in payload:
        raise HTTPException(status_code=400, detail="Falta 'ticker' en el payload")

    log.info("Payload recibido: %s", payload)
    action = payload.get("action")
    event = payload.get("event")
    symbol = to_bingx_symbol(payload["ticker"])

    # sl_hit / max_bars_exit: cierre real de un trade ya abierto.
    if event in ("sl_hit", "max_bars_exit"):
        background_tasks.add_task(process_close_event, symbol, event)
        return {"status": "queued", "event": event, "symbol": symbol}

    # Eventos informativos (tp1_hit, tp2_hit, tp3_hit, sl_pending): las tres
    # salidas parciales ya están como órdenes reales puestas al entrar, así
    # que estos eventos no necesitan disparar ninguna acción — solo se
    # registran para referencia.
    if action not in ("buy", "sell"):
        log.info("Evento informativo (sin acción): %s", event)
        return {"status": "ignored", "payload": payload}

    # Señal de entrada — se encola, la ejecución real pasa en segundo plano.
    background_tasks.add_task(process_entry, payload)
    return {"status": "queued", "action": action, "symbol": symbol}


@app.get("/health")
async def health():
    return {
        "status": "up",
        "tp_split": {"tp1": TP1_PCT, "tp2": TP2_PCT, "tp3": TP3_PCT},
        "open_positions": OPEN_POSITIONS,
        "last_results": LAST_RESULT,
    }
