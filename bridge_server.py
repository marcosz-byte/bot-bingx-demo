"""
Puente TradingView -> BingX (cuenta DEMO / VST)  —  v3
========================================================

Cambio de esta v3 respecto a v2: el endpoint del webhook ahora responde a
TradingView DE INMEDIATO ("queued") y recién después hace el trabajo real
con la API de BingX en segundo plano (FastAPI BackgroundTasks).

Por qué: TradingView le da al webhook una ventana de tiempo bastante corta
para responder. La v2 hacía hasta 5 llamadas SEGUIDAS a la API de BingX
(cerrar posición previa, setear apalancamiento, abrir la entrada, poner el
SL de emergencia, poner el TP1) ANTES de devolver la respuesta HTTP — si
esas llamadas tardaban un poco (latencia normal de red/exchange), superaba
el tiempo que tolera TradingView y la alerta se marcaba como
"request took too long and timed out", aunque el bridge probablemente
terminara el trabajo unos segundos después, ya demasiado tarde.

Con este cambio, el endpoint valida el secreto, parsea el JSON, hace
validaciones básicas (todo instantáneo, sin llamadas de red) y encola el
trabajo pesado — responde en milisegundos. Las órdenes en BingX se siguen
colocando exactamente igual, solo que después de que TradingView ya recibió
su "OK".

CONTRAPARTIDA a tener en cuenta: como la respuesta HTTP ya no espera el
resultado real de las órdenes, esa respuesta ya no te dice si la orden se
ejecutó bien o mal — vas a tener que mirar los logs del servidor para eso
(cada paso sigue logueado igual que antes), o el endpoint /health, que
ahora también expone la última operación procesada por símbolo.

Todo lo demás es igual a v2:
1. QTY y LEVERAGE se leen del payload ("qty", "leverage"), calculados por
   el Position Sizing del indicador. ORDER_QUANTITY/DEFAULT_LEVERAGE son
   solo fallback.
2. SL de emergencia (EMERGENCY_SL_MULT × la distancia del SL real) en vez
   del SL ajustado — el cierre real llega después, vía el evento "sl_hit"
   (que ya respeta la espera de N barras del Anti-Liquidation SL).
3. "sl_hit" y "max_bars_exit" cierran la posición a mercado y cancelan las
   órdenes condicionales colgando (antes solo se logueaban).
4. Antes de abrir una entrada nueva, cierra cualquier posición previa
   registrada en ese símbolo (fix del modo hedge: una señal opuesta no
   cierra sola la posición anterior).

LIMITACIONES (sin cambios respecto a v2):
- OPEN_POSITIONS vive en memoria — se pierde si el servidor reinicia con
  un trade abierto (la posición en BingX sigue abierta, solo se pierde la
  referencia a sus órdenes condicionales).
- Solo TP1 se refleja como salida automática real en el exchange — TP2/TP3
  y el trailing del indicador hoy son solo para las estadísticas del
  backtest.
- Parámetros de la API de BingX (positionSide, stopPrice, set_leverage)
  pueden variar según versión de CCXT y el modo de cuenta. Probá primero
  en DEMO con cantidades chicas.
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

exchange = ccxt.bingx({
    "apiKey": BINGX_API_KEY,
    "secret": BINGX_API_SECRET,
    "options": {"defaultType": "swap"},  # perpetuos USDT-M
    "enableRateLimit": True,
})
exchange.set_sandbox_mode(True)  # <- clave: apunta al entorno demo/VST de BingX

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


def cancel_order_safe(symbol: str, order_id: Optional[str], label: str) -> None:
    """Cancela una orden condicional sin tirar abajo el flujo si ya no existe
    (por ejemplo, si ya se ejecutó — el TP1 puede haber hecho fill antes de
    que llegue un sl_hit, y viceversa)."""
    if not order_id:
        return
    try:
        exchange.cancel_order(order_id, symbol)
        log.info("Cancelada orden %s (%s) en %s", label, order_id, symbol)
    except Exception as e:
        log.warning("No se pudo cancelar orden %s (%s) en %s: %s", label, order_id, symbol, e)


def close_position_market(symbol: str, reason: str) -> Optional[dict]:
    """Cierra a mercado lo que el bridge tenga registrado como abierto para
    ese símbolo, y cancela las órdenes condicionales colgando (SL de
    emergencia + TP1). Devuelve la orden de cierre, o None si no había
    nada registrado para ese símbolo."""
    pos = OPEN_POSITIONS.pop(symbol, None)
    if pos is None:
        log.info("close_position_market(%s): nada registrado para cerrar (razón: %s)", symbol, reason)
        return None

    exit_side = "sell" if pos["position_side"] == "LONG" else "buy"

    close_order = exchange.create_order(
        symbol=symbol,
        type="market",
        side=exit_side,
        amount=pos["qty"],
        params={"positionSide": pos["position_side"]},
    )
    log.info("Posición cerrada a mercado en %s por '%s' | orden: %s", symbol, reason, close_order.get("id"))

    cancel_order_safe(symbol, pos.get("sl_order_id"), "SL emergencia")
    cancel_order_safe(symbol, pos.get("tp1_order_id"), "TP1")

    return close_order


# ══════════════════════════════════════════════════════════
# TAREAS EN SEGUNDO PLANO — acá vive el trabajo "lento" (llamadas a BingX),
# que ahora corre DESPUÉS de que el endpoint ya le respondió a TradingView.
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

        take_profit = exchange.create_order(
            symbol=symbol, type="take_profit_market", side=exit_side, amount=qty,
            params={"positionSide": position_side, "stopPrice": tp1},
        )

        OPEN_POSITIONS[symbol] = {
            "position_side": position_side,
            "qty": qty,
            "sl_order_id": stop_loss.get("id"),
            "tp1_order_id": take_profit.get("id"),
        }

        log.info(
            "Entrada: %s | qty: %s | lev: %sx | SL emergencia: %s (real: %s) | TP1: %s",
            entry.get("id"), qty, leverage, emergency_sl, sl, take_profit.get("id"),
        )

        _record_result(symbol, "ok", {
            "entry_order_id": entry.get("id"),
            "qty": qty,
            "leverage": leverage,
            "emergency_sl_price": emergency_sl,
            "real_sl_price": sl,
            "sl_order_id": stop_loss.get("id"),
            "tp1_order_id": take_profit.get("id"),
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

    # Eventos informativos (tp1_hit, tp2_hit, tp3_hit, sl_pending): por
    # ahora no disparan ninguna acción, solo se registran.
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
        "open_positions": OPEN_POSITIONS,
        "last_results": LAST_RESULT,
    }
