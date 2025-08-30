import os, time
import json
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import hmac
import hashlib
import httpx

APP_PASSPHRASE = os.getenv("PASS_PHRASE", "")

def _get_env_float(name: str, default: float) -> float:
    val = os.getenv(name, "").strip()
    try:
        return float(val) if val else default
    except Exception:
        print(f"[WARN] {name} inválida: {val!r}. Usando {default}.")
        return default

def _get_env_int(name: str, default: int) -> int:
    val = os.getenv(name, "").strip()
    try:
        return int(val) if val else default
    except Exception:
        print(f"[WARN] {name} inválida: {val!r}. Usando {default}.")
        return default

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
FUTURES_BASE_URL = os.getenv("FUTURES_BASE_URL", "https://testnet.binancefuture.com")
ORDER_USDT = _get_env_float("ORDER_USDT", 50.0)
LEVERAGE   = _get_env_int("LEVERAGE", 1)



app = FastAPI()
_ids_processados = set()  # evita executar alerta duplicado

@app.get("/health")
def health():
    return {"ok": True, "epoch": int(time.time())}

def _tv_to_binance_symbol(tv_symbol: str) -> str:
    if not tv_symbol:
        return ""
    sym = tv_symbol.split(":")[-1].upper().strip()
    if sym.endswith("USD") and not sym.endswith("USDT"):
        sym = sym + "T"  # BTCUSD -> BTCUSDT
    return sym


def _sign(query: str) -> str:
    return hmac.new(
        BINANCE_API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

async def _binance_futures_set_leverage(client: httpx.AsyncClient, symbol: str, leverage: int):
    # Opcional: garantir a alavancagem
    ts = int(time.time() * 1000)
    params = f"symbol={symbol}&leverage={leverage}&timestamp={ts}&recvWindow=5000"
    sig = _sign(params)
    url = f"{FUTURES_BASE_URL}/fapi/v1/leverage?{params}&signature={sig}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    r = await client.post(url, headers=headers)
    return r.json()

async def _binance_futures_market_order(client: httpx.AsyncClient, symbol: str, side: str, qty: float, reduce_only: bool = False):
    # side: "BUY" ou "SELL"; reduce_only fecha posição sem abrir outra
    ts = int(time.time() * 1000)
    q_str = f"{qty:.6f}".rstrip("0").rstrip(".")  # limpa zeros à direita
    params = f"symbol={symbol}&side={side}&type=MARKET&quantity={q_str}&reduceOnly={'true' if reduce_only else 'false'}&timestamp={ts}&recvWindow=5000"
    sig = _sign(params)
    url = f"{FUTURES_BASE_URL}/fapi/v1/order?{params}&signature={sig}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    r = await client.post(url, headers=headers)
    return r.json()

async def _binance_futures_income(client: httpx.AsyncClient, symbol: str = "", start_ms: int | None = None, income_type: str = "REALIZED_PNL", limit: int = 20):
    ts = int(time.time() * 1000)
    params = []
    if symbol:
        params.append(f"symbol={symbol}")
    params.append(f"incomeType={income_type}")
    if start_ms:
        params.append(f"startTime={start_ms}")
    params.append(f"limit={limit}")  
    params.append("recvWindow=5000")
    params.append(f"timestamp={ts}")
    q = "&".join(params)
    sig = _sign(q)
    url = f"{FUTURES_BASE_URL}/fapi/v1/income?{q}&signature={sig}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    r = await client.get(url, headers=headers)
    return r.json()

def _sum_recent_realized_pnl(items, symbol: str):
    try:
        same = [it for it in items if it.get("symbol") == symbol]
        if not same:
            return None, 0
        pnl_sum = sum(float(it.get("income", 0.0)) for it in same)
        return pnl_sum, len(same)
    except Exception:
        return None, 0


def _calc_qty_from_usdt(price: float, usdt: float, leverage: int = 1, min_qty: float = 0.001) -> float:
    if not price or price <= 0:
        return min_qty
    qty = (usdt * leverage) / float(price)
    # arredonda para 3 casas decimais (ex.: BTCUSDT min 0.001)
    qty = max(min_qty, round(qty, 3))
    return qty



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

                # --- PNL REALIZADO (Income History) ---
                try:
                    # Busca ganhos/perdas realizados dos últimos 10 minutos para este símbolo
                    start_ms = int(time.time() * 1000) - 10 * 60 * 1000
                    income = await _binance_futures_income(client, symbol_b, start_ms, "REALIZED_PNL", 20)
                    if isinstance(income, list):
                        pnl_sum, n = _sum_recent_realized_pnl(income, symbol_b)
                        if pnl_sum is not None:
                            print(f"[BINANCE][PnL] Realizado recente {symbol_b}: {pnl_sum:.4f} USDT (entradas: {n})")
                        else:
                            print(f"[BINANCE][PnL] Nenhum realized PnL encontrado ainda para {symbol_b}.")
                    else:
                        print(f"[BINANCE][PnL] Resposta inesperada do income: {income}")
                except Exception as e:
                    print(f"[BINANCE][PnL] ERRO ao consultar income: {e}")


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

