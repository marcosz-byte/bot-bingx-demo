"""
Puente TradingView -> BingX (cuenta DEMO / VST)
================================================

Qué hace:
1. Expone un endpoint HTTPS que TradingView llama vía webhook cuando tu
   estrategia "Precision Sniper" dispara una señal buy/sell.
2. Valida un secreto propio (NO es la API key de BingX) para que nadie más
   pueda enviarte órdenes falsas a ese endpoint.
3. Traduce la señal a una orden de mercado en BingX, apuntando SIEMPRE al
   entorno demo (VST) mediante el modo sandbox de CCXT.
4. Coloca, además de la entrada, una orden de Stop Loss y una de Take Profit
   (TP1) como órdenes condicionales DENTRO de BingX — así el exchange
   gestiona la salida aunque tu servidor esté apagado en ese momento.

IMPORTANTE — antes de usarlo:
- Esto es una plantilla de partida, no un producto terminado. Los nombres
  exactos de parámetros de la API de BingX (positionSide, stopPrice,
  reduceOnly, tipos de orden) pueden variar según la versión de CCXT y el
  modo de posición (hedge vs one-way) de tu cuenta. Pruébalo primero con
  cantidades mínimas en DEMO y revisa cada respuesta antes de confiar en él.
- Ajusta ORDER_QUANTITY, el mapeo de símbolos y la gestión de TP2/TP3 a tu
  gusto — aquí solo se usa TP1 como salida automática por simplicidad.
"""

import os
import logging
from fastapi import FastAPI, Request, HTTPException
import ccxt

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tv-bingx-bridge")

app = FastAPI()

# ── Configuración vía variables de entorno (nunca hardcodees claves) ──
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]          # tu propio secreto, inventado por ti
BINGX_API_KEY = os.environ["BINGX_API_KEY"]
BINGX_API_SECRET = os.environ["BINGX_API_SECRET"]
ORDER_QUANTITY = float(os.environ.get("ORDER_QUANTITY", "0.001"))  # tamaño fijo por trade

exchange = ccxt.bingx({
    "apiKey": BINGX_API_KEY,
    "secret": BINGX_API_SECRET,
    "options": {"defaultType": "swap"},  # perpetuos USDT-M
    "enableRateLimit": True,
})
exchange.set_sandbox_mode(True)  # <- clave: apunta al entorno demo/VST de BingX


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


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="secreto inválido")

    payload = await request.json()
    log.info("Payload recibido: %s", payload)

    action = payload.get("action")
    if action not in ("buy", "sell"):
        # Aquí llegan también los eventos tp1_hit/tp2_hit/tp3_hit/sl_hit si
        # les configuras un webhook propio — de momento solo los registramos.
        log.info("Evento informativo (no es buy/sell): %s", payload.get("event"))
        return {"status": "ignored", "payload": payload}

    symbol = to_bingx_symbol(payload["ticker"])
    side = "buy" if action == "buy" else "sell"
    position_side = "LONG" if action == "buy" else "SHORT"
