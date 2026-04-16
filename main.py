import os
import time
import math
import requests
import threading
from datetime import datetime

# ── Configuracoes ─────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

BYBIT_BASE = "https://api.bybit.com/v5/market/kline"

SYMBOLS = {
    "BTCUSDT": "BTC/USD OTC",
    "ETHUSDT": "ETH/USD OTC",
    "GBPUSDT": "GBP/USD OTC",
    "SOLUSDT": "SOL/USD OTC",
}

CHECK_INTERVAL = 60
COOLDOWN       = 300
RSI_PERIOD     = 14
EMA_SHORT      = 9
EMA_LONG       = 21
BB_PERIOD      = 20
BB_STD         = 2.0
MACD_FAST      = 12
MACD_SLOW      = 26
MACD_SIGNAL    = 9
STOCH_K        = 5
STOCH_D        = 3
STOCH_SMOOTH   = 3

last_signal_time = {}
last_update_id   = 0
start_time       = datetime.utcnow()


# ── Indicadores ───────────────────────────────────────────────────

def calculate_rsi(closes, period=RSI_PERIOD):
    if len(closes) < period + 1:
        return 50.0
    deltas   = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains    = [d if d > 0 else 0.0 for d in deltas]
    losses   = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)


def calculate_ema(closes, period):
    if len(closes) < period:
        return closes[-1]
    k   = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 8)


def calculate_bollinger(closes, period=BB_PERIOD, std_mult=BB_STD):
    if len(closes) < period:
        v = closes[-1]
        return v, v, v
    window   = closes[-period:]
    middle   = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std      = math.sqrt(variance)
    return round(middle + std_mult * std, 8), round(middle, 8), round(middle - std_mult * std, 8)


def calculate_macd(closes, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    if len(closes) < slow + signal:
        return 0.0, 0.0, "Neutro"
    ema_fast   = calculate_ema(closes, fast)
    ema_slow   = calculate_ema(closes, slow)
    macd_line  = ema_fast - ema_slow
    # Calcula linha de sinal como EMA do MACD sobre janela disponivel
    macd_values = []
    for i in range(slow, len(closes) + 1):
        ef = calculate_ema(closes[:i], fast)
        es = calculate_ema(closes[:i], slow)
        macd_values.append(ef - es)
    if len(macd_values) < signal:
        return round(macd_line, 8), 0.0, "Neutro"
    signal_line = calculate_ema(macd_values, signal)
    histogram   = macd_line - signal_line
    if histogram > 0:
        trend = "Alta"
    elif histogram < 0:
        trend = "Baixa"
    else:
        trend = "Neutro"
    return round(macd_line, 8), round(signal_line, 8), trend


def calculate_stochastic(highs, lows, closes, k_period=STOCH_K, d_period=STOCH_D):
    if len(closes) < k_period:
        return 50.0, 50.0
    k_values = []
    for i in range(k_period - 1, len(closes)):
        high_max = max(highs[i - k_period + 1: i + 1])
        low_min  = min(lows[i  - k_period + 1: i + 1])
        if high_max == low_min:
            k_values.append(50.0)
        else:
            k_values.append(100 * (closes[i] - low_min) / (high_max - low_min))
    # Suaviza K com media movel simples de STOCH_SMOOTH periodos
    if len(k_values) >= STOCH_SMOOTH:
        k_smooth = sum(k_values[-STOCH_SMOOTH:]) / STOCH_SMOOTH
    else:
        k_smooth = k_values[-1]
    # D e a media de K
    if len(k_values) >= d_period:
        d_line = sum(k_values[-d_period:]) / d_period
    else:
        d_line = k_smooth
    return round(k_smooth, 2), round(d_line, 2)


def detect_candle_pattern(opens, highs, lows, closes):
    if len(closes) < 2:
        return "Nenhum"
    # Vela atual e anterior
    o1, h1, l1, c1 = opens[-2], highs[-2], lows[-2], closes[-2]
    o2, h2, l2, c2 = opens[-1], highs[-1], lows[-1], closes[-1]
    body1  = abs(c1 - o1)
    body2  = abs(c2 - o2)
    range1 = h1 - l1 if h1 != l1 else 0.0001
    range2 = h2 - l2 if h2 != l2 else 0.0001

    # Martelo: corpo pequeno no topo, sombra inferior longa (bullish)
    lower_shadow2 = min(o2, c2) - l2
    upper_shadow2 = h2 - max(o2, c2)
    if (body2 / range2 < 0.35 and
            lower_shadow2 >= 2 * body2 and
            upper_shadow2 <= body2):
        return "Martelo"

    # Estrela cadente: corpo pequeno no fundo, sombra superior longa (bearish)
    if (body2 / range2 < 0.35 and
            upper_shadow2 >= 2 * body2 and
            lower_shadow2 <= body2):
        return "Estrela Cadente"

    # Engolfo de Alta: vela anterior bearish, atual bullish e engolfa
    if (c1 < o1 and c2 > o2 and
            c2 > o1 and o2 < c1):
        return "Engolfo de Alta"

    # Engolfo de Baixa: vela anterior bullish, atual bearish e engolfa
    if (c1 > o1 and c2 < o2 and
            c2 < o1 and o2 > c1):
        return "Engolfo de Baixa"

    return "Nenhum"


# ── Bybit API ─────────────────────────────────────────────────────

def get_candles_full(symbol, interval="1", limit=120):
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
        raise Exception("Bybit: " + str(data.get("retMsg")))
    candles = data["result"]["list"]
    candles.reverse()
    # [timestamp, open, high, low, close, volume, turnover]
    opens  = [float(c[1]) for c in candles]
    highs  = [float(c[2]) for c in candles]
    lows   = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    return opens, highs, lows, closes


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


# ── Mensagem de sinal ─────────────────────────────────────────────

def build_signal_message(direction, label, rsi, macd_trend, stoch_k, pattern, confidence, entry=10):
    if direction == "CALL":
        icon   = "CALL"
        arrow  = "ACIMA"
        tri    = "^"
    else:
        icon   = "PUT"
        arrow  = "ABAIXO"
        tri    = "v"
    pattern_line = ""
    if pattern != "Nenhum":
        pattern_line = "Padrao: " + pattern + "\n"
    return (
        icon + " <b>" + direction + " - " + label + "</b>\n"
        "Expiracao: 1 min\n"
        "RSI: " + str(rsi) + " | MACD: " + macd_trend + " | Stoch: " + str(stoch_k) + "\n"
        + pattern_line +
        "Confianca: " + str(confidence) + "%\n"
        "Entrada sugerida: $" + str(entry) + "\n"
        "IQ Option -> " + arrow + " " + tri
    )


# ── Analise completa ──────────────────────────────────────────────

def run_analysis(symbol, label, forced=False, target_chat=None):
    ts = datetime.utcnow().strftime("%H:%M:%S")
    try:
        opens, highs, lows, closes = get_candles_full(symbol)

        if len(closes) < 50:
            msg = "Dados insuficientes para " + label
            print("[" + ts + "] " + msg)
            if forced and target_chat:
                send_telegram(target_chat, msg)
            return None

        # Indicadores
        rsi                         = calculate_rsi(closes)
        ema9                        = calculate_ema(closes, EMA_SHORT)
        ema21                       = calculate_ema(closes, EMA_LONG)
        bb_upper, bb_mid, bb_lower  = calculate_bollinger(closes)
        macd_line, macd_sig, macd_t = calculate_macd(closes)
        stoch_k, stoch_d            = calculate_stochastic(highs, lows, closes)
        pattern                     = detect_candle_pattern(opens, highs, lows, closes)
        price                       = closes[-1]

        # Pontuacao CALL
        call_pts = 0
        put_pts  = 0

        # RSI
        if rsi < 35:
            call_pts += 1
        elif rsi > 65:
            put_pts  += 1

        # EMA cross
        ema_trend = "Neutro"
        if ema9 > ema21:
            call_pts += 1
            ema_trend = "Alta"
        elif ema9 < ema21:
            put_pts  += 1
            ema_trend = "Baixa"

        # Bollinger
        bb_status = "Neutro"
        if price < bb_lower:
            call_pts += 1
            bb_status = "Sobrevendido"
        elif price > bb_upper:
            put_pts  += 1
            bb_status = "Sobrecomprado"

        # MACD
        if macd_t == "Alta":
            call_pts += 1
        elif macd_t == "Baixa":
            put_pts  += 1

        # Stochastic
        if stoch_k < 20:
            call_pts += 1
        elif stoch_k > 80:
            put_pts  += 1

        # Padrao de vela
        pattern_bonus = 0
        if pattern in ("Martelo", "Engolfo de Alta"):
            call_pts     += 1
            pattern_bonus = 1
        elif pattern in ("Estrela Cadente", "Engolfo de Baixa"):
            put_pts      += 1
            pattern_bonus = 1

        # Log detalhado
        print(
            "[" + ts + "] " + symbol +
            " | P:" + str(round(price, 4)) +
            " | RSI:" + str(rsi) +
            " | EMA:" + ema_trend +
            " | BB:" + bb_status +
            " | MACD:" + macd_t +
            " | Stoch:" + str(stoch_k) +
            " | Padrao:" + pattern +
            " | CALL=" + str(call_pts) + " PUT=" + str(put_pts)
        )

        # Resposta ao /sinal forcado (sempre responde)
        if forced and target_chat:
            direction_str = "Sem direcao clara"
            if call_pts >= 4:
                direction_str = "CALL forte (" + str(call_pts) + "/6)"
            elif put_pts >= 4:
                direction_str = "PUT forte (" + str(put_pts) + "/6)"
            elif call_pts > put_pts:
                direction_str = "Leve CALL (" + str(call_pts) + "/6)"
            elif put_pts > call_pts:
                direction_str = "Leve PUT (" + str(put_pts) + "/6)"

            msg = (
                "<b>Analise: " + label + "</b>\n"
                "Preco: " + str(round(price, 5)) + "\n"
                "RSI(14): " + str(rsi) + " | EMA: " + ema_trend + "\n"
                "MACD: " + macd_t + " | Stoch: " + str(stoch_k) + "\n"
                "BB: " + bb_status + "\n"
                "Padrao: " + pattern + "\n"
                "Direcao: " + direction_str + "\n"
                "Pontos: CALL=" + str(call_pts) + "/6  PUT=" + str(put_pts) + "/6"
            )
            send_telegram(target_chat, msg)

        # Sinal automatico: 4+ pontos de 6 possiveis
        if call_pts >= 4:
            base       = 65 + call_pts * 3 + pattern_bonus * 5
            confidence = min(base + (5 if rsi < 30 else 0), 95)
            return dict(direction="CALL", label=label, symbol=symbol,
                        rsi=rsi, macd_trend=macd_t, stoch_k=stoch_k,
                        pattern=pattern, confidence=confidence)

        if put_pts >= 4:
            base       = 65 + put_pts * 3 + pattern_bonus * 5
            confidence = min(base + (5 if rsi > 70 else 0), 95)
            return dict(direction="PUT", label=label, symbol=symbol,
                        rsi=rsi, macd_trend=macd_t, stoch_k=stoch_k,
                        pattern=pattern, confidence=confidence)

        print("[" + ts + "] " + symbol + " — sem confluencia suficiente")
        return None

    except Exception as e:
        err = "[" + ts + "] Erro ao analisar " + symbol + ": " + str(e)
        print(err)
        if forced and target_chat:
            send_telegram(target_chat, "Erro ao buscar dados de " + label + ": " + str(e))
        return None


# ── Comandos do Telegram ──────────────────────────────────────────

def handle_command(text, chat_id):
    ts   = datetime.utcnow().strftime("%H:%M:%S")
    text = text.strip().lower().split("@")[0]
    print("[" + ts + "] Comando: " + text + " | chat=" + str(chat_id))

    if text == "/start":
        msg = (
            "Ola! Sou o bot de sinais IQ Option.\n\n"
            "Comandos:\n"
            "/start  — Boas-vindas\n"
            "/status — Status e uptime\n"
            "/sinal  — Analise imediata de todos os ativos\n\n"
            "Ativos: BTC, ETH, GBP, SOL\n"
            "Indicadores: RSI, EMA, BB, MACD, Stochastic\n"
            "Padroes: Martelo, Engolfo, Estrela Cadente\n"
            "Sinal automatico quando 4+ de 6 indicadores confluem."
        )
        send_telegram(chat_id, msg)

    elif text == "/status":
        uptime = datetime.utcnow() - start_time
        h = int(uptime.total_seconds() // 3600)
        m = int((uptime.total_seconds() % 3600) // 60)
        now = time.time()
        lines = []
        for sym, lbl in SYMBOLS.items():
            diff = now - last_signal_time.get(sym, 0)
            if diff < COOLDOWN:
                lines.append(lbl + ": cooldown " + str(int(COOLDOWN - diff)) + "s")
            else:
                lines.append(lbl + ": pronto")
        msg = (
            "Bot ATIVO\n"
            "Uptime: " + str(h) + "h " + str(m) + "m\n"
            "API: Bybit | Ciclo: " + str(CHECK_INTERVAL) + "s\n\n"
            "\n".join(lines)
        )
        send_telegram(chat_id, msg)

    elif text == "/sinal":
        send_telegram(chat_id, "Analisando " + str(len(SYMBOLS)) + " ativos agora... aguarde.")
        for symbol, label in SYMBOLS.items():
            run_analysis(symbol, label, forced=True, target_chat=chat_id)

    else:
        send_telegram(chat_id, "Comando nao reconhecido. Use /start, /status ou /sinal")


# ── Thread polling de comandos ────────────────────────────────────

def polling_loop():
    global last_update_id
    print("Polling iniciado (intervalo: 2s).")
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
            print("Erro polling: " + str(e))
        time.sleep(2)


# ── Thread loop de sinais automaticos ────────────────────────────

def signal_loop():
    print("Loop de sinais automaticos iniciado.")
    while True:
        ts  = datetime.utcnow().strftime("%H:%M:%S")
        now = time.time()
        print("[" + ts + "] === Ciclo de analise ===")
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
                msg = build_signal_message(
                    direction  = sig["direction"],
                    label      = sig["label"],
                    rsi        = sig["rsi"],
                    macd_trend = sig["macd_trend"],
                    stoch_k    = sig["stoch_k"],
                    pattern    = sig["pattern"],
                    confidence = sig["confidence"],
                )
                ok = send_telegram(TELEGRAM_CHAT_ID, msg)
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
    print("Bot IQ Option iniciado!")
    print("Ativos: " + str(list(SYMBOLS.keys())))
    print("API: Bybit | RSI + EMA + BB + MACD + Stoch + Padroes de vela")
    print("Intervalo: " + str(CHECK_INTERVAL) + "s | Cooldown: " + str(COOLDOWN) + "s")

    t_poll = threading.Thread(target=polling_loop, daemon=True)
    t_poll.start()

    signal_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Bot encerrado.")
    except Exception as e:
        print("Erro critico: " + str(e))
        time.sleep(10)
