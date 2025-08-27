import os, time
import json
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

APP_PASSPHRASE = os.getenv("PASS_PHRASE", "")

app = FastAPI()
_ids_processados = set()  # evita executar alerta duplicado

@app.get("/health")
def health():
    return {"ok": True, "epoch": int(time.time())}

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

    # 6) Roteamento simples (só printa por enquanto)
    if action == "ignore":
        return {"status": "ok", "note": "ignorado", "action": action_raw}
    if action == "long":
        print(f"[EXECUTAR] COMPRA {symbol} ~{price} no {tf}")
    elif action == "short":
        print(f"[EXECUTAR] VENDA {symbol} ~{price} no {tf}")
    elif action == "close":
        print(f"[EXECUTAR] FECHAR {symbol}")
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

