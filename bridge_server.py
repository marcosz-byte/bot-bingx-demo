"""
Puente TradingView -> BingX (cuenta DEMO / VST)  —  v2
========================================================

Basado en tu bridge original para "Precision Sniper". Cambios en esta v2,
pensados para "Bot Signals MZ" (que ya calcula tamaño de posición y
apalancamiento dentro del propio indicador, en vez de usar un tamaño fijo):

1. QTY y LEVERAGE ya NO son fijos por variable de entorno — se leen del
   payload JSON que manda el indicador ("qty" y "leverage"), calculados por
   el Position Sizing del indicador (cartera × riesgo% / distancia del SL).
   ORDER_QUANTITY y DEFAULT_LEVERAGE quedan solo como fallback si algún
   día llega una alerta vieja sin esos campos.

2. STOP LOSS DE EMERGENCIA en vez de SL ajustado: el indicador ahora tiene
   "Anti-Liquidation Stop Loss", que espera N barras antes de confirmar el
   cierre por SL (para filtrar mechas). Eso significa que el SL "real" no
   lo puede ejecutar el exchange al toque — lo decide el indicador y llega
   después, vía el evento "sl_hit". Por eso:
     - En BingX SOLO se deja puesto un stop MUY ALEJADO (EMERGENCY_SL_MULT
       veces la distancia del SL real), que actúa como red de seguridad
       pura ante un desastre (servidor caído, gap extremo) — no como el
       SL normal del sistema.
     - El cierre real, al nivel de SL que ves en el dashboard del
       indicador, lo hace este bridge con una orden de mercado apenas
       llega el evento "sl_hit" (que ya respeta la espera configurada).

3. Se agregó manejo real de los eventos "sl_hit" y "max_bars_exit": ambos
   ahora cierran la posición en BingX con una orden de mercado y cancelan
   las órdenes condicionales que quedaron colgando (el stop de emergencia
   y el TP1). Antes estos eventos solo se registraban en el log y no hacían
   nada — la posición se quedaba abierta en el exchange aunque el
   indicador ya la diera por cerrada.

4. FIX IMPORTANTE que encontré en tu código original: en modo hedge
   (positionSide LONG/SHORT). una señal opuesta no cierra la posición
   anterior — abre una posición independiente en el lado contrario, y te
   quedarías con LONG y SHORT abiertos a la vez (según cómo BingX trate
   ese neteo). Bot Signals MZ funciona "stop-and-reverse" (nunca más de un
   trade a la vez), así que ahora, antes de abrir una entrada nueva, el
   bridge cierra primero cualquier posición previa que tenga registrada
   para ese símbolo.

LIMITACIÓN A TENER EN CUENTA:
- El registro de qué posición/órdenes hay abiertas vive en memoria
  (OPEN_POSITIONS), no en un archivo ni base de datos. Si reiniciás el
  servidor mientras hay un trade abierto, el bridge "olvida" los IDs de
  las órdenes condicionales asociadas (la posición en sí sigue abierta en
  BingX, solo se pierde la referencia para poder cancelarlas prolijamente
  más adelante). Para una demo está bien: si esto pasa a needs de cuenta
  real, conviene persistir OPEN_POSITIONS en algo simple como un archivo
  JSON o SQLite.
- Sigue usando SOLO TP1 como salida automática en el exchange (igual que
  tu versión original) — el indicador además trackea TP2/TP3/trailing
  para sus propias estadísticas de backtest, pero eso NO se refleja en la
  posición real todavía. Si en algún momento querés que el exchange
  también haga salidas parciales en TP2/TP3, avisame y lo armamos aparte
  (implica partir la orden de entrada en tramos).
- Los nombres exactos de parámetros de la API de BingX (positionSide,
  stopPrice, reduceOnly, set_leverage) pueden variar según versión de
  CCXT y el modo de tu cuenta (hedge vs one-way). Probá primero en DEMO
  con cantidades chicas.
"""

import os
import logging
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
import ccxt

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tv-bingx-bridge")

app = FastAPI()

# ── Configuración vía variables de entorno (nunca hardcodees claves) ──
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]          # tu propio secreto, inventado por ti
BINGX_API_KEY = os.environ["BINGX_API_KEY"]
BINGX_API_SECRET = os.environ["BINGX_API_SECRET"]

# Fallbacks — solo se usan si una alerta llega sin "qty" / "leverage"
# (por ejemplo, una alerta vieja o mal configurada).
DEFAULT_QUANTITY = float(os.environ.get("ORDER_QUANTITY", "0.001"))
DEFAULT_LEVERAGE = float(os.environ.get("DEFAULT_LEVERAGE", "1"))

# Qué tan lejos, en múltiplos de la distancia del SL real, va el stop de
# emergencia puesto en el exchange. 3.0 = tres veces más lejos que el SL
# que ves en el indicador. Subilo si tu Anti-Liq Confirmation Bars es alto
# (más tiempo de espera = más margen para que el precio se mueva mientras
# tanto) y no querés que la emergencia se dispare antes de tiempo.
EMERGENCY_SL_MULT = float(os.environ.get("EMERGENCY_SL_MULT", "3.0"))

exchange = ccxt.bingx({
    "apiKey": BINGX_API_KEY,
    "secret": BINGX_API_SECRET,
    "options": {"defaultType": "swap"},  # perpetuos USDT-M
    "enableRateLimit": True,
})
exchange.set_sandbox_mode(True)  # <- clave: apunta al entorno demo/VST de BingX

# Estado en memoria: qué hay abierto ahora mismo por símbolo.
# {"BTC/USDT:USDT": {"position_side": "LONG", "qty": 0.01,
#                     "sl_order_id": "...", "tp1_order_id": "..."}}
OPEN_POSITIONS: dict[str, dict] = {}


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


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="secreto inválido")

    raw_body = await request.body()
    try:
        payload = await request.json()
    except Exception:
        # Causa más común: el indicador tiene "Webhook JSON Format" (grupo
        # Alerts) en OFF, así que TradingView está mandando el texto legible
        # para humanos en vez de JSON. Devolvemos un error claro en vez de
        # un 500 pelado, para que se vea el motivo directo en el log de la
        # alerta de TradingView.
        log.error("Body no es JSON válido: %r", raw_body[:300])
        raise HTTPException(
            status_code=400,
            detail="El body recibido no es JSON válido. Revisá que 'Webhook JSON Format' "
                   "esté activado en el grupo Alerts del indicador.",
        )
    log.info("Payload recibido: %s", payload)

    action = payload.get("action")
    event = payload.get("event")
    if "ticker" not in payload:
        raise HTTPException(status_code=400, detail="Falta 'ticker' en el payload")
    symbol = to_bingx_symbol(payload["ticker"])

    # ── Eventos de gestión de un trade YA abierto ──
    # sl_hit: el Anti-Liquidation SL del indicador confirmó el cierre.
    # max_bars_exit: se agotó el límite de "Max Bars in Trade".
    # Ambos son cierres reales que el exchange no puede anticipar por su
    # cuenta (uno depende de la espera de N barras, el otro es por tiempo),
    # así que acá es donde el bridge tiene que actuar.
    if event in ("sl_hit", "max_bars_exit"):
        try:
            close_position_market(symbol, reason=event)
            return {"status": "closed", "event": event, "symbol": symbol}
        except Exception as e:
            log.exception("Error cerrando posición por evento %s", event)
            raise HTTPException(status_code=500, detail=str(e))

    # El resto de los eventos informativos (tp1_hit, tp2_hit, tp3_hit,
    # sl_pending) todavía no disparan ninguna acción acá — el TP1 real ya
    # lo maneja la orden condicional puesta en el exchange al abrir el
    # trade, y tp2/tp3/sl_pending son solo para tu seguimiento visual /
    # estadísticas por ahora.
    if action not in ("buy", "sell"):
        log.info("Evento informativo (sin acción): %s", payload.get("event"))
        return {"status": "ignored", "payload": payload}

    # ── Señal de entrada (buy/sell) ──
    side = "buy" if action == "buy" else "sell"
    position_side = "LONG" if action == "buy" else "SHORT"
    exit_side = "sell" if action == "buy" else "buy"

    entry_ref = float(payload["price"])   # precio de referencia al momento de la señal
    sl = float(payload["sl"])
    tp1 = float(payload["tp1"])
    qty = float(payload.get("qty") or DEFAULT_QUANTITY)
    leverage = float(payload.get("leverage") or DEFAULT_LEVERAGE)

    if qty <= 0:
        raise HTTPException(status_code=400, detail=f"qty inválida en el payload: {payload.get('qty')}")

    # Si había una posición previa registrada en este símbolo (por ejemplo,
    # un stop-and-reverse: la señal opuesta llegó ANTES que el evento de
    # cierre correspondiente), cerrarla primero. En modo hedge, BingX no
    # cierra sola una posición LONG solo porque abrís una SHORT nueva.
    if symbol in OPEN_POSITIONS:
        log.info("Había una posición previa registrada en %s — cerrándola antes de abrir la nueva.", symbol)
        try:
            close_position_market(symbol, reason="reversal")
        except Exception as e:
            log.exception("Error cerrando la posición previa antes de revertir")
            raise HTTPException(status_code=500, detail=str(e))

    # Apalancamiento — best effort: algunos modos/cuentas usan otros
    # parámetros (por ejemplo side=LONG/SHORT en hedge mode). Si falla,
    # se loguea pero no se aborta la entrada (puede que ya esté seteado
    # manualmente en la cuenta).
    try:
        exchange.set_leverage(leverage, symbol, params={"side": position_side})
    except Exception as e:
        log.warning("No se pudo setear apalancamiento (%sx) en %s: %s", leverage, symbol, e)

    # Distancia del SL real (la que calcula el indicador) → stop de
    # emergencia EMERGENCY_SL_MULT veces más lejos, como red de seguridad.
    sl_distance = abs(entry_ref - sl)
    emergency_sl = entry_ref - sl_distance * EMERGENCY_SL_MULT if action == "buy" else entry_ref + sl_distance * EMERGENCY_SL_MULT

    try:
        entry = exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=qty,
            params={"positionSide": position_side},
        )

        stop_loss = exchange.create_order(
            symbol=symbol,
            type="stop_market",
            side=exit_side,
            amount=qty,
            params={
                "positionSide": position_side,
                "stopPrice": emergency_sl,
            },
        )

        take_profit = exchange.create_order(
            symbol=symbol,
            type="take_profit_market",
            side=exit_side,
            amount=qty,
            params={
                "positionSide": position_side,
                "stopPrice": tp1,
            },
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

        return {
            "status": "ok",
            "entry_order_id": entry.get("id"),
            "qty": qty,
            "leverage": leverage,
            "emergency_sl_price": emergency_sl,
            "real_sl_price": sl,
            "sl_order_id": stop_loss.get("id"),
            "tp1_order_id": take_profit.get("id"),
        }

    except Exception as e:
        log.exception("Error ejecutando la orden en BingX demo")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    return {"status": "up", "open_positions": list(OPEN_POSITIONS.keys())}
