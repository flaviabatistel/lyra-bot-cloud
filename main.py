import os, time
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
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")

    passphrase = payload.get("passphrase")
    alert_id   = str(payload.get("id"))
    action     = (payload.get("action") or "").lower()

    # valida passphrase
    if APP_PASSPHRASE == "" or passphrase != APP_PASSPHRASE:
        raise HTTPException(status_code=401, detail="Passphrase incorreta")

    # evita executar 2x se o TradingView reenviar o alerta
    if alert_id in _ids_processados:
        return JSONResponse({"status":"duplicado_ignorado"})
    _ids_processados.add(alert_id)

    symbol = payload.get("symbol")
    price  = payload.get("price")
    tf     = payload.get("timeframe")

    # ---- AQUI entra a lógica de compra/venda na corretora ----
    if action == "long":
        print(f"[EXECUTAR] COMPRA {symbol} ~{price} no {tf}")
    elif action == "short":
        print(f"[EXECUTAR] VENDA {symbol} ~{price} no {tf}")
    elif action == "close":
        print(f"[EXECUTAR] FECHAR {symbol}")
    else:
        raise HTTPException(status_code=400, detail=f"Ação desconhecida: {action}")

    return {"status":"ok","recebido":payload}

