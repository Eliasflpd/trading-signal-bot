import os
import time
import math
import requests
from datetime import datetime

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOLS = {
    "GBPUSDT": "GBP/USD",
    "BTCUSDT": "BTC/USD",
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
    return round(middle + std_mult * std, 6), round(middle, 6), round(middle - std_mult * std, 6)


def get_candles(symbol, interval="1m", limit=100):
    url    = f"{BINANCE_BASE}/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp   = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return [float(c[4]) for c in resp.json()]


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Token ou Chat ID nao configurados.")
        return False
    url  = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    resp = requests.post(url, data=data, timeout=10)
    return resp.status_code == 200


def build_message(direction, label, rsi, trend, bb_status, confidence):
    if direction == "CALL":
        icon   = "CALL"
        action = "-> Abra IQ Option -> clique ACIMA ^"
    else:
        icon   = "PUT"
        action = "-> Abra IQ Option -> clique ABAIXO v"
    line1 = icon + " <b>" + direction + " - " + label + "</b>"
    line2 = "Expiracao: 1 minuto"
    line3 = "RSI: " + str(rsi) + " | Tendencia: " + trend + " | BB: " + bb_status
    line4 = "Confianca: " + str(confidence) + "%"
    return line1 + "\n" + line2 + "\n" + line3 + "\n" + line4 + "\n" + action


def analyze(symbol, closes):
    rsi                   = calculate_rsi(closes)
    ema9                  = calculate_ema(closes, EMA_SHORT)
    ema21                 = calculate_ema(closes, EMA_LONG)
    bb_upper, _, bb_lower = calculate_bollinger(closes)
    price                 = closes[-1]
    call_signals = 0
    put_signals  = 0
    bb_status    = "Neutro"
    if rsi < 35:
        call_signals += 1
    elif rsi > 65:
        put_signals  += 1
    if ema9 > ema21:
        call_signals += 1
    elif ema9 < ema21:
        put_signals  += 1
    if price < bb_lower:
        call_signals += 1
        bb_status = "Sobrevendido"
    elif price > bb_upper:
        put_signals  += 1
        bb_status = "Sobrecomprado"
    if call_signals >= 3:
        confidence = min(60 + call_signals * 5 + (5 if rsi < 30 else 0), 95)
        return dict(direction="CALL", rsi=rsi, trend="Alta", bb_status=bb_status, confidence=confidence)
    if put_signals >= 3:
        confidence = min(60 + put_signals * 5 + (5 if rsi > 70 else 0), 95)
        return dict(direction="PUT", rsi=rsi, trend="Baixa", bb_status=bb_status, confidence=confidence)
    return None


def main():
    print("Bot de sinais iniciado.")
    while True:
        now = time.time()
        ts  = datetime.utcnow().strftime("%H:%M:%S UTC")
        for symbol, label in SYMBOLS.items():
            try:
                last = last_signal_time.get(symbol, 0)
                if now - last < COOLDOWN:
                    remaining = int(COOLDOWN - (now - last))
                    print("[" + ts + "] " + symbol + " em cooldown (" + str(remaining) + "s)")
                    continue
                closes = get_candles(symbol)
                if not closes:
                    continue
                print("[" + ts + "] " + symbol + " preco: " + str(closes[-1]))
                signal = analyze(symbol, closes)
                if signal:
                    msg = build_message(
                        direction=signal["direction"], label=label,
                        rsi=signal["rsi"], trend=signal["trend"],
                        bb_status=signal["bb_status"], confidence=signal["confidence"]
                    )
                    ok = send_telegram(msg)
                    if ok:
                        last_signal_time[symbol] = now
                        print("[" + ts + "] Sinal " + signal["direction"] + " enviado para " + label)
                    else:
                        print("[" + ts + "] Falha ao enviar no Telegram")
                else:
                    print("[" + ts + "] " + symbol + " sem confluencia")
            except requests.exceptions.RequestException as e:
                print("[" + ts + "] Erro de rede (" + symbol + "): " + str(e))
            except Exception as e:
                print("[" + ts + "] Erro inesperado (" + symbol + "): " + str(e))
        try:
            time.sleep(CHECK_INTERVAL)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Bot encerrado.")
    except Exception as e:
        print("Erro critico: " + str(e))
        time.sleep(10)
