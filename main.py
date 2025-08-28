import os, time
import json
import hmac
import hashlib
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

APP_PASSPHRASE = os.getenv("PASS_PHRASE", "")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
FUTURES_BASE_URL = os.getenv("FUTURES_BASE_URL", "https://testnet.binancefuture.com")
ORDER_USDT = float(os.getenv("ORDER_USDT", "50"))
LEVERAGE = int(os.getenv("LEVERAGE", "1"))


app = FastAPI()
_ids_processados = set()  # evita executar alerta duplicado

@app.get("/health")
def health():
    return {"ok": True, "epoch": int(time.time())}

def _tv_to_binance_symbol(tv_symbol: str) -> str:
    ...

def _sign(query: str) -> str:
    ...

async def _binance_futures_set_leverage(...):
    ...

async def _binance_futures_market_order(...):
    ...

def _calc_qty_from_usdt(...):
    ...


@app.post("/webhook")
async def webhook(req: Request):
    # 1) Lê o corpo cru (funciona com text/plain ou application/json)
    raw = await req.body()
    txt = raw.decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(txt)
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")

    # 2) Valida passphrase
    passphrase = payload.get("passphrase")
    if APP_PASSPHRASE == "" or passphrase != APP_PASSPHRASE:
        raise HTTPException(status_code=401, detail="Passphrase incorreta")

    # 3) Evita processar duplicados
    alert_id = str(payload.get("id"))
    if alert_id in _ids_processados:
        return JSONResponse({"status": "duplicado_ignorado"})
    _ids_processados.add(alert_id)

    # 4) Extrai campos
    action_raw = (payload.get("action") or "").lower().strip()
    action_map = {
        "buy": "long",
        "sell": "short",
        "close": "close",
        "long": "long",
        "short": "short",
        "exit_long": "close",
        "exit_short": "close",
        "hb": "ignore",
        "test": "ignore",
    }
    action = action_map.get(action_raw, None)

    symbol = str(payload.get("symbol", ""))
    price  = payload.get("price")
    tf     = str(payload.get("timeframe", ""))
    t_val  = payload.get("time")

    try:
        price_f = float(price)
    except Exception:
        price_f = None

    try:
        t_ms = int(t_val)
        time_iso = datetime.fromtimestamp(t_ms/1000, tz=timezone.utc).isoformat()
    except Exception:
        time_iso = str(t_val) if t_val is not None else None

    # 5) Log bonito
    print(f"[ALERTA] action={action_raw.upper()} -> {action.upper() if action else '???'} | "
          f"symbol={symbol} | price={price_f if price_f is not None else price} | "
          f"tf={tf} | time={time_iso}")

    # 6) Execução real (Binance Futures Testnet)
    if action == "ignore":
        return {"status": "ok", "note": "ignorado", "action": action_raw}

    symbol_b = _tv_to_binance_symbol(symbol)
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        print("[BINANCE] Faltam credenciais. Configure BINANCE_API_KEY/SECRET.")
    elif not symbol_b:
        print("[BINANCE] Símbolo inválido no payload.")
    else:
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                # garante alavancagem configurada
                await _binance_futures_set_leverage(client, symbol_b, LEVERAGE)
            except Exception as e:
                print(f"[BINANCE] Aviso: set leverage falhou: {e}")

            qty = _calc_qty_from_usdt(price_f or 0.0, ORDER_USDT, LEVERAGE)

            if action == "long":
                print(f"[EXECUTAR] COMPRA {symbol} ~{price} no {tf} (q={qty})")
                try:
                    resp = await _binance_futures_market_order(client, symbol_b, "BUY", qty, reduce_only=False)
                    print("[BINANCE][BUY] Resp:", resp)
                except Exception as e:
                    print("[BINANCE][BUY] ERRO:", e)

            elif action == "short":
                print(f"[EXECUTAR] VENDA {symbol} ~{price} no {tf} (q={qty})")
                try:
                    resp = await _binance_futures_market_order(client, symbol_b, "SELL", qty, reduce_only=False)
                    print("[BINANCE][SELL] Resp:", resp)
                except Exception as e:
                    print("[BINANCE][SELL] ERRO:", e)

            elif action == "close":
                print(f"[EXECUTAR] FECHAR {symbol} (reduceOnly)")
                try:
                    resp1 = await _binance_futures_market_order(client, symbol_b, "SELL", qty, reduce_only=True)
                    print("[BINANCE][CLOSE->SELL reduceOnly] Resp:", resp1)
                except Exception as e:
                    print("[BINANCE][CLOSE->SELL] ERRO:", e)
                try:
                    resp2 = await _binance_futures_market_order(client, symbol_b, "BUY", qty, reduce_only=True)
                    print("[BINANCE][CLOSE->BUY reduceOnly] Resp:", resp2)
                except Exception as e:
                    print("[BINANCE][CLOSE->BUY] ERRO:", e)

            else:
                raise HTTPException(status_code=400, detail=f"Ação desconhecida: {action_raw}")


    # 7) Resposta mais detalhada
    return {
        "status": "ok",
        "action": action,
        "action_raw": action_raw,
        "symbol": symbol,
        "price": price_f if price_f is not None else price,
        "time": t_val,
        "time_iso": time_iso,
        "timeframe": tf,
        "id": alert_id,
    }

