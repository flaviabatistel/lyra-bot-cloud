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
ORDER_USDT = _get_env_float("ORDER_USDT", 100.0)
LEVERAGE   = _get_env_int("LEVERAGE", 1)

app = FastAPI()
_ids_processados = set()  # evita executar alerta duplicado

@app.get("/health")
def health():
    return {"ok": True, "epoch": int(time.time())}

def tv_to_binance_symbol(tv_symbol: str) -> str:
    if not tv_symbol:
        return ""
    sym = tv_symbol.split(":")[-1].upper().strip()
    # Corrige BTCUSD -> BTCUSDT (comum em TV)
    if sym.endswith("USD") and not sym.endswith("USDT"):
        sym = sym + "T"
    return sym

def log(msg: str):
    print(msg, flush=True)

def _sign(query: str) -> str:
    return hmac.new(
        BINANCE_API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

# =========================
# Binance Futures (helpers)
# =========================

async def _binance_futures_get_position_qty(client: httpx.AsyncClient, symbol: str) -> float:
    """
    Retorna a posição líquida do símbolo em modo one-way:
      > 0 = long, < 0 = short, 0 = flat
    """
    ts = int(time.time() * 1000)
    q = f"symbol={symbol}&timestamp={ts}&recvWindow=5000"
    sig = _sign(q)
    url = f"{FUTURES_BASE_URL}/fapi/v2/positionRisk?{q}&signature={sig}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    r = await client.get(url, headers=headers)
    data = r.json()
    if isinstance(data, dict):
        data = [data]
    for it in data:
        if it.get("symbol") == symbol:
            try:
                return float(it.get("positionAmt", "0"))
            except Exception:
                return 0.0
    return 0.0

async def _binance_futures_set_leverage(client: httpx.AsyncClient, symbol: str, leverage: int):
    ts = int(time.time() * 1000)
    params = f"symbol={symbol}&leverage={leverage}&timestamp={ts}&recvWindow=5000"
    sig = _sign(params)
    url = f"{FUTURES_BASE_URL}/fapi/v1/leverage?{params}&signature={sig}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    r = await client.post(url, headers=headers)
    return r.json()

async def _binance_futures_market_order(client: httpx.AsyncClient, symbol: str, side: str, qty: float, reduce_only: bool = False):
    """
    side: "BUY" ou "SELL"
    reduce_only=True fecha posição sem abrir outra (fail-safe contra inversão quando flat)
    """
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

# =========================
# Roteador de sinais
# =========================

async def handle_signal(
    client: httpx.AsyncClient,
    tv_symbol: str,
    action: str,
    qty: float,
    allow_short: bool = False
):
    """
    action: "BUY" | "SELL" | "SHORT" | "COVER"
      BUY   -> abre/aumenta LONG
      SELL  -> fecha LONG (reduceOnly). Se allow_short=True e estiver flat, pode abrir SHORT.
      SHORT -> abre SHORT explicitamente
      COVER -> fecha SHORT (reduceOnly)
    """
    symbol = tv_to_binance_symbol(tv_symbol)
    action = (action or "").strip().upper()

    # lê posição atual ( >0 long, <0 short, 0 flat )
    pos_qty = await _binance_futures_get_position_qty(client, symbol)

    if action == "BUY":
        resp = await _binance_futures_market_order(client, symbol, side="BUY", qty=qty, reduce_only=False)
        print(f"[ROUTER] BUY -> OPEN/LONG | pos_before={pos_qty} | resp={resp}", flush=True)
        return resp

    if action == "SELL":
        if pos_qty > 0:
            close_qty = min(qty, pos_qty)
            resp = await _binance_futures_market_order(client, symbol, side="SELL", qty=close_qty, reduce_only=True)
            print(f"[ROUTER] SELL -> CLOSE_LONG {close_qty} | pos_before={pos_qty} | resp={resp}", flush=True)
            return resp
        else:
            if allow_short:
                resp = await _binance_futures_market_order(client, symbol, side="SELL", qty=qty, reduce_only=False)
                print(f"[ROUTER] SELL -> OPEN/SHORT | pos_before={pos_qty} | resp={resp}", flush=True)
                return resp
            else:
                print(f"[ROUTER] SELL -> SKIP (no long to close; short disabled) | pos={pos_qty}", flush=True)
                return {"skipped": True, "reason": "no long to close; short disabled"}

    if action == "SHORT":
        resp = await _binance_futures_market_order(client, symbol, side="SELL", qty=qty, reduce_only=False)
        print(f"[ROUTER] SHORT -> OPEN/SHORT | pos_before={pos_qty} | resp={resp}", flush=True)
        return resp

    if action == "COVER":
        if pos_qty < 0:
            close_qty = min(qty, abs(pos_qty))
            resp = await _binance_futures_market_order(client, symbol, side="BUY", qty=close_qty, reduce_only=True)
            print(f"[ROUTER] COVER -> CLOSE_SHORT {close_qty} | pos_before={pos_qty} | resp={resp}", flush=True)
            return resp
        else:
            print(f"[ROUTER] COVER -> SKIP (no short) | pos={pos_qty}", flush=True)
            return {"skipped": True, "reason": "no short to close"}

    print(f"[ROUTER] IGNORE action={action}", flush=True)
    return {"skipped": True, "reason": f"unknown action {action}"}

# =========================
# Webhook
# =========================

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
        "buy": "buy",      # <- importante: não mapear para "long"
        "sell": "sell",    # <- importante: não mapear para "short"
        "close": "close",
        "long": "buy",
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

    # 6) Execução real (Binance Futures - Testnet/Prod conforme base URL)
    if action is None:
        raise HTTPException(status_code=400, detail=f"Ação desconhecida: {action_raw}")

    if action == "ignore":
        return {"status": "ok", "note": "ignorado", "action": action_raw}

    symbol_b = tv_to_binance_symbol(symbol)
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        print("[BINANCE] Faltam credenciais. Configure BINANCE_API_KEY/SECRET.")
    elif not symbol_b:
        print("[BINANCE] Símbolo inválido no payload.")
    else:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # (opcional) garantir alavancagem
            try:
                await _binance_futures_set_leverage(client, symbol_b, LEVERAGE)
            except Exception as e:
                print(f"[BINANCE] Aviso: set leverage falhou: {e}")

            qty = _calc_qty_from_usdt(price_f or 0.0, ORDER_USDT, LEVERAGE)

            # mapeia para os verbos do roteador
            if action == "buy":
                await handle_signal(client, symbol, "BUY", qty, allow_short=False)
            elif action == "sell":
                # SELL fecha long; só abre short se você decidir (allow_short=True)
                await handle_signal(client, symbol, "SELL", qty, allow_short=False)
            elif action == "short":
                await handle_signal(client, symbol, "SHORT", qty, allow_short=True)
            elif action == "close":
                # tenta fechar ambos os lados com reduceOnly
                await handle_signal(client, symbol, "SELL", qty, allow_short=False)   # fecha long
                await handle_signal(client, symbol, "COVER", qty, allow_short=False)  # fecha short

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
                # Não deve acontecer, pois já validamos action acima
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

