import os
import time
import math
import requests
import threading
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOLS = {
    "GBPUSDT":  "GBP/USD",
    "BTCUSDT":  "BTC/USD",
    "SPYUSDT":  "US 500 (SPY)",
    "AMZNUSDT": "Amazon OTC",
}

BINANCE_BASE   = "https://api.binance.com/api/v3"
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


# ── Indicadores ─────────────────────────────────────────────────

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


# ── Binance ──────────────────────────────────────────────────────

def get_candles(symbol, interval="1m", limit=100):
    url    = BINANCE_BASE + "/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp   = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return [float(c[4]) for c in resp.json()]


# ── Telegram ──────────────────────────────────────────────────────

def tg_post(method, payload):
    url  = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/" + method
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        print("Erro Telegram " + method + ": " + str(e))
        return {}


def send_telegram(chat_id, message):
    return tg_post("sendMessage", {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    })


def get_updates(offset=0):
    url    = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/getUpdates"
    params = {"timeout": 5, "offset": offset}
    try:
        resp = requests.get(url, params=params, timeout=10)
        return resp.json().get("result", [])
    except Exception as e:
        print("Erro getUpdates: " + str(e))
        return []


# ── Analise ──────────────────────────────────────────────────────

def analyze_symbol(symbol, label, forced=False, target_chat=None):
    ts = datetime.utcnow().strftime("%H:%M:%S")
    try:
        closes = get_candles(symbol)
        if not closes:
            print("[" + ts + "] " + symbol + " — sem dados da Binance")
            return None

        rsi                   = calculate_rsi(closes)
        ema9                  = calculate_ema(closes, EMA_SHORT)
        ema21                 = calculate_ema(closes, EMA_LONG)
        bb_upper, _, bb_lower = calculate_bollinger(closes)
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

        print("[" + ts + "] Analisando " + symbol +
              " | Preco: " + str(round(price, 5)) +
              " | RSI: " + str(rsi) + " (" + rsi_label + ")" +
              " | Tendencia: " + trend +
              " | BB: " + bb_status +
              " | CALL=" + str(call_signals) + " PUT=" + str(put_signals))

        if call_signals >= 3:
            direction  = "CALL"
            confidence = min(60 + call_signals * 5 + (5 if rsi < 30 else 0), 95)
            return dict(direction=direction, rsi=rsi, trend=trend,
                        bb_status=bb_status, confidence=confidence,
                        price=price, symbol=symbol, label=label)

        if put_signals >= 3:
            direction  = "PUT"
            confidence = min(60 + put_signals * 5 + (5 if rsi > 70 else 0), 95)
            return dict(direction=direction, rsi=rsi, trend=trend,
                        bb_status=bb_status, confidence=confidence,
                        price=price, symbol=symbol, label=label)

        if forced and target_chat:
            msg = (
                "Analise " + label + " agora:\n"
                "Preco: " + str(round(price, 5)) + "\n"
                "RSI: " + str(rsi) + " (" + rsi_label + ")\n"
                "Tendencia: " + trend + "\n"
                "BB: " + bb_status + "\n"
                "Sinal: Sem confluencia suficiente (CALL=" + str(call_signals) + " PUT=" + str(put_signals) + ")"
            )
            send_telegram(target_chat, msg)

        print("[" + ts + "] " + symbol + " — Sem confluencia (CALL=" + str(call_signals) + " PUT=" + str(put_signals) + ")")
        return None

    except Exception as e:
        print("[" + ts + "] Erro ao analisar " + symbol + ": " + str(e))
        return None


def build_signal_message(sig):
    if sig["direction"] == "CALL":
        icon   = "CALL"
        action = "-> Abra IQ Option -> clique ACIMA ^"
    else:
        icon   = "PUT"
        action = "-> Abra IQ Option -> clique ABAIXO v"
    return (
        icon + " <b>" + sig["direction"] + " - " + sig["label"] + "</b>\n"
        "Expiracao: 1 minuto\n"
        "RSI: " + str(sig["rsi"]) + " | Tendencia: " + sig["trend"] + " | BB: " + sig["bb_status"] + "\n"
        "Confianca: " + str(sig["confidence"]) + "%\n"
        + action
    )


# ── Comandos Telegram ─────────────────────────────────────────────

def handle_command(text, chat_id):
    global last_signal_time
    text = text.strip().lower().split("@")[0]
    ts   = datetime.utcnow().strftime("%H:%M:%S")

    if text == "/start":
        print("[" + ts + "] Comando /start recebido de chat_id=" + str(chat_id))
        msg = (
            "Ola! Sou o bot de sinais IQ Option.\n\n"
            "Comandos disponíveis:\n"
            "/start - Mensagem de boas-vindas\n"
            "/status - Ver se estou rodando\n"
            "/sinal - Forcei uma analise agora\n\n"
            "Monitoro GBP/USD e BTC/USD usando RSI, EMA e Bollinger Bands.\n"
            "Quando 3+ indicadores confluem, envio sinal automaticamente."
        )
        send_telegram(chat_id, msg)

    elif text == "/status":
        print("[" + ts + "] Comando /status recebido de chat_id=" + str(chat_id))
        uptime = datetime.utcnow() - start_time
        hours  = int(uptime.total_seconds() // 3600)
        mins   = int((uptime.total_seconds() % 3600) // 60)
        cooldowns = []
        now = time.time()
        for sym, lbl in SYMBOLS.items():
            last = last_signal_time.get(sym, 0)
            diff = now - last
            if diff < COOLDOWN:
                rem = int(COOLDOWN - diff)
                cooldowns.append(lbl + ": cooldown " + str(rem) + "s")
            else:
                cooldowns.append(lbl + ": pronto")
        status_lines = "\n".join(cooldowns)
        msg = (
            "Bot ATIVO\n"
            "Uptime: " + str(hours) + "h " + str(mins) + "m\n"
            "Intervalo: " + str(CHECK_INTERVAL) + "s\n\n"
            "Status dos ativos:\n" + status_lines
        )
        send_telegram(chat_id, msg)

    elif text == "/sinal":
        print("[" + ts + "] Comando /sinal recebido de chat_id=" + str(chat_id))
        send_telegram(chat_id, "Forcando analise agora... aguarde.")
        for symbol, label in SYMBOLS.items():
            sig = analyze_symbol(symbol, label, forced=True, target_chat=chat_id)
            if sig:
                msg = build_signal_message(sig)
                send_telegram(chat_id, msg)
                last_signal_time[symbol] = time.time()
                print("[" + ts + "] Sinal forcado " + sig["direction"] + " enviado para " + label)

    else:
        send_telegram(chat_id, "Comando nao reconhecido. Use /start, /status ou /sinal")


# ── Loop de polling de comandos ──────────────────────────────────

def polling_loop():
    global last_update_id
    print("Polling de comandos iniciado.")
    while True:
        try:
            updates = get_updates(offset=last_update_id + 1)
            for upd in updates:
                last_update_id = upd.get("update_id", last_update_id)
                msg = upd.get("message", {})
                text = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id")
                if text and chat_id and text.startswith("/"):
                    handle_command(text, chat_id)
        except Exception as e:
            print("Erro polling: " + str(e))
        time.sleep(2)


# ── Loop principal de sinais ─────────────────────────────────────

def signal_loop():
    print("Loop de sinais iniciado.")
    while True:
        ts  = datetime.utcnow().strftime("%H:%M:%S")
        now = time.time()
        print("[" + ts + "] === Iniciando ciclo de analise ===")
        found_any = False

        for symbol, label in SYMBOLS.items():
            last = last_signal_time.get(symbol, 0)
            if now - last < COOLDOWN:
                remaining = int(COOLDOWN - (now - last))
                print("[" + ts + "] " + symbol + " em cooldown (" + str(remaining) + "s restantes)")
                continue

            sig = analyze_symbol(symbol, label)
            if sig:
                found_any = True
                msg = build_signal_message(sig)
                ok  = send_telegram(TELEGRAM_CHAT_ID, msg)
                if ok:
                    last_signal_time[symbol] = now
                    print("[" + ts + "] Sinal " + sig["direction"] + " enviado para " + label)
                else:
                    print("[" + ts + "] Falha ao enviar sinal no Telegram")

        if not found_any:
            print("[" + ts + "] Sem confluencia em nenhum ativo — aguardando proximo ciclo em " + str(CHECK_INTERVAL) + "s...")

        try:
            time.sleep(CHECK_INTERVAL)
        except Exception:
            pass


# ── Main ─────────────────────────────────────────────────────────

def main():
    print("Bot de sinais IQ Option iniciado!")
    print("Ativos: " + str(list(SYMBOLS.keys())))
    print("Intervalo: " + str(CHECK_INTERVAL) + "s | Cooldown: " + str(COOLDOWN) + "s")

    t_polling = threading.Thread(target=polling_loop, daemon=True)
    t_polling.start()

    signal_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Bot encerrado.")
    except Exception as e:
        print("Erro critico: " + str(e))
        time.sleep(10)
