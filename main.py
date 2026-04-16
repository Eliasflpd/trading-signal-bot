import os
import time
import math
import requests
import threading
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# Bybit API — sem restricao geografica, funciona no Railway US
BYBIT_BASE = "https://api.bybit.com/v5/market/kline"

SYMBOLS = {
    "BTCUSDT": "BTC/USD",
    "ETHUSDT": "ETH/USD",
    "GBPUSDT": "GBP/USD",
    "SOLUSDT": "SOL/USD",
}

CHECK_INTERVAL = 60
COOLDOWN       = 300
RSI_PERIOD     = 14
EMA_SHORT      = 9
EMA_LONG       = 21
BB_PERIOD      = 20
BB_STD         = 2.0

last_signal_time = {}
last_update_id   = 0
start_time       = datetime.utcnow()


# ── Indicadores ───────────────────────────────────────────────────

def calculate_rsi(closes, period=RSI_PERIOD):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def calculate_ema(closes, period):
    if len(closes) < period:
        return closes[-1]
    k   = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 6)


def calculate_bollinger(closes, period=BB_PERIOD, std_mult=BB_STD):
    if len(closes) < period:
        last = closes[-1]
        return last, last, last
    window   = closes[-period:]
    middle   = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std      = math.sqrt(variance)
    return (
        round(middle + std_mult * std, 6),
        round(middle, 6),
        round(middle - std_mult * std, 6),
    )


# ── Bybit API ─────────────────────────────────────────────────────

def get_candles(symbol, interval="1", limit=100):
    params = {
        "category": "linear",
        "symbol":   symbol,
        "interval": interval,
        "limit":    limit,
    }
    resp = requests.get(BYBIT_BASE, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode") != 0:
        raise Exception("Bybit erro: " + str(data.get("retMsg")))
    # Bybit retorna [timestamp, open, high, low, close, volume, turnover]
    # mais recente primeiro — invertemos
    candles = data["result"]["list"]
    candles.reverse()
    return [float(c[4]) for c in candles]


# ── Telegram ──────────────────────────────────────────────────────

def send_telegram(chat_id, message):
    if not TELEGRAM_BOT_TOKEN:
        return False
    url  = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
    data = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=data, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print("Erro send_telegram: " + str(e))
        return False


def get_updates(offset=0):
    url    = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/getUpdates"
    params = {"timeout": 5, "offset": offset, "allowed_updates": ["message"]}
    try:
        resp = requests.get(url, params=params, timeout=10)
        return resp.json().get("result", [])
    except Exception as e:
        print("Erro getUpdates: " + str(e))
        return []


# ── Analise ───────────────────────────────────────────────────────

def run_analysis(symbol, label, forced=False, target_chat=None):
    ts = datetime.utcnow().strftime("%H:%M:%S")
    try:
        closes = get_candles(symbol)
        if not closes or len(closes) < 30:
            msg = "Dados insuficientes para " + label
            print("[" + ts + "] " + msg)
            if forced and target_chat:
                send_telegram(target_chat, msg)
            return None

        rsi                   = calculate_rsi(closes)
        ema9                  = calculate_ema(closes, EMA_SHORT)
        ema21                 = calculate_ema(closes, EMA_LONG)
        bb_upper, bb_mid, bb_lower = calculate_bollinger(closes)
        price                 = closes[-1]

        call_signals = 0
        put_signals  = 0
        bb_status    = "Neutro"
        trend        = "Neutro"

        if rsi < 35:
            call_signals += 1
        elif rsi > 65:
            put_signals  += 1

        if ema9 > ema21:
            call_signals += 1
            trend = "Alta"
        elif ema9 < ema21:
            put_signals  += 1
            trend = "Baixa"

        if price < bb_lower:
            call_signals += 1
            bb_status = "Sobrevendido"
        elif price > bb_upper:
            put_signals  += 1
            bb_status = "Sobrecomprado"

        rsi_label = "neutro"
        if rsi < 35:
            rsi_label = "sobrevendido"
        elif rsi > 65:
            rsi_label = "sobrecomprado"

        log = ("[" + ts + "] " + symbol +
               " | Preco: " + str(round(price, 5)) +
               " | RSI: " + str(rsi) + " (" + rsi_label + ")" +
               " | EMA9: " + str(round(ema9, 4)) +
               " | EMA21: " + str(round(ema21, 4)) +
               " | Tendencia: " + trend +
               " | BB: " + bb_status +
               " | CALL=" + str(call_signals) + " PUT=" + str(put_signals))
        print(log)

        # Se /sinal forcado, responde com os valores atuais independente de confluencia
        if forced and target_chat:
            dir_str = "Indefinida"
            if call_signals >= 3:
                dir_str = "CALL (COMPRA)"
            elif put_signals >= 3:
                dir_str = "PUT (VENDA)"
            elif call_signals > put_signals:
                dir_str = "Leve tendencia CALL (" + str(call_signals) + "/3)"
            elif put_signals > call_signals:
                dir_str = "Leve tendencia PUT (" + str(put_signals) + "/3)"

            msg = (
                "<b>Analise " + label + "</b>\n"
                "Preco: " + str(round(price, 5)) + "\n"
                "RSI(" + str(RSI_PERIOD) + "): " + str(rsi) + " — " + rsi_label + "\n"
                "EMA9: " + str(round(ema9, 4)) + " | EMA21: " + str(round(ema21, 4)) + "\n"
                "Bollinger: " + bb_status + "\n"
                "Tendencia: " + trend + "\n"
                "Direcao: " + dir_str + "\n"
                "Confluencia: CALL=" + str(call_signals) + "/3  PUT=" + str(put_signals) + "/3"
            )
            send_telegram(target_chat, msg)

        if call_signals >= 3:
            confidence = min(60 + call_signals * 5 + (5 if rsi < 30 else 0), 95)
            return dict(direction="CALL", rsi=rsi, trend=trend,
                        bb_status=bb_status, confidence=confidence,
                        label=label, symbol=symbol)

        if put_signals >= 3:
            confidence = min(60 + put_signals * 5 + (5 if rsi > 70 else 0), 95)
            return dict(direction="PUT", rsi=rsi, trend=trend,
                        bb_status=bb_status, confidence=confidence,
                        label=label, symbol=symbol)

        print("[" + ts + "] " + symbol + " — sem confluencia")
        return None

    except Exception as e:
        err = "[" + ts + "] Erro ao analisar " + symbol + ": " + str(e)
        print(err)
        if forced and target_chat:
            send_telegram(target_chat, "Erro ao buscar dados de " + label + ": " + str(e))
        return None


def build_signal_message(sig):
    icon   = "CALL" if sig["direction"] == "CALL" else "PUT"
    action = "-> Abra IQ Option -> clique ACIMA ^" if sig["direction"] == "CALL" else "-> Abra IQ Option -> clique ABAIXO v"
    return (
        icon + " <b>" + sig["direction"] + " - " + sig["label"] + "</b>\n"
        "Expiracao: 1 minuto\n"
        "RSI: " + str(sig["rsi"]) + " | Tendencia: " + sig["trend"] + " | BB: " + sig["bb_status"] + "\n"
        "Confianca: " + str(sig["confidence"]) + "%\n"
        + action
    )


# ── Comandos ─────────────────────────────────────────────────────

def handle_command(text, chat_id):
    ts   = datetime.utcnow().strftime("%H:%M:%S")
    text = text.strip().lower().split("@")[0]
    print("[" + ts + "] Comando recebido: " + text + " de chat_id=" + str(chat_id))

    if text == "/start":
        msg = (
            "Ola! Sou o bot de sinais IQ Option.\n\n"
            "Comandos:\n"
            "/start — Boas-vindas\n"
            "/status — Ver se estou rodando\n"
            "/sinal — Analise imediata de todos os ativos\n\n"
            "Monitoro: BTC/USD, ETH/USD, GBP/USD, SOL/USD\n"
            "Indicadores: RSI(14), EMA(9/21), Bollinger(20)\n"
            "Sinal automatico quando 3+ indicadores confluem."
        )
        send_telegram(chat_id, msg)

    elif text == "/status":
        uptime = datetime.utcnow() - start_time
        h = int(uptime.total_seconds() // 3600)
        m = int((uptime.total_seconds() % 3600) // 60)
        now = time.time()
        lines = []
        for sym, lbl in SYMBOLS.items():
            last = last_signal_time.get(sym, 0)
            diff = now - last
            if diff < COOLDOWN:
                lines.append(lbl + ": cooldown " + str(int(COOLDOWN - diff)) + "s")
            else:
                lines.append(lbl + ": pronto para sinal")
        msg = (
            "Bot ATIVO\n"
            "Uptime: " + str(h) + "h " + str(m) + "m\n"
            "Intervalo: " + str(CHECK_INTERVAL) + "s\n"
            "API: Bybit\n\n"
            "Ativos:\n" + "\n".join(lines)
        )
        send_telegram(chat_id, msg)

    elif text == "/sinal":
        send_telegram(chat_id, "Analisando todos os ativos agora... aguarde.")
        for symbol, label in SYMBOLS.items():
            run_analysis(symbol, label, forced=True, target_chat=chat_id)

    else:
        send_telegram(chat_id, "Comando nao reconhecido. Use /start, /status ou /sinal")


# ── Thread polling ────────────────────────────────────────────────

def polling_loop():
    global last_update_id
    print("Polling de comandos iniciado (a cada 2s).")
    while True:
        try:
            updates = get_updates(offset=last_update_id + 1)
            for upd in updates:
                last_update_id = upd.get("update_id", last_update_id)
                msg     = upd.get("message", {})
                text    = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id")
                if text and chat_id and text.startswith("/"):
                    handle_command(text, chat_id)
        except Exception as e:
            print("Erro no polling: " + str(e))
        time.sleep(2)


# ── Thread sinais automaticos ─────────────────────────────────────

def signal_loop():
    print("Loop de sinais iniciado.")
    while True:
        ts  = datetime.utcnow().strftime("%H:%M:%S")
        now = time.time()
        print("[" + ts + "] === Iniciando ciclo de analise ===")
        found = False

        for symbol, label in SYMBOLS.items():
            last = last_signal_time.get(symbol, 0)
            if now - last < COOLDOWN:
                rem = int(COOLDOWN - (now - last))
                print("[" + ts + "] " + symbol + " em cooldown (" + str(rem) + "s)")
                continue

            sig = run_analysis(symbol, label)
            if sig:
                found = True
                msg = build_signal_message(sig)
                ok  = send_telegram(TELEGRAM_CHAT_ID, msg)
                if ok:
                    last_signal_time[symbol] = now
                    print("[" + ts + "] Sinal " + sig["direction"] + " enviado para " + label)
                else:
                    print("[" + ts + "] Falha ao enviar Telegram")

        if not found:
            print("[" + ts + "] Sem confluencia — proximo ciclo em " + str(CHECK_INTERVAL) + "s")

        try:
            time.sleep(CHECK_INTERVAL)
        except Exception:
            pass


# ── Main ──────────────────────────────────────────────────────────

def main():
    print("Bot de sinais IQ Option iniciado!")
    print("Ativos: " + str(list(SYMBOLS.keys())))
    print("API: Bybit (sem restricao geografica)")
    print("Intervalo: " + str(CHECK_INTERVAL) + "s | Cooldown: " + str(COOLDOWN) + "s")

    t = threading.Thread(target=polling_loop, daemon=True)
    t.start()

    signal_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Bot encerrado.")
    except Exception as e:
        print("Erro critico: " + str(e))
        time.sleep(10)
