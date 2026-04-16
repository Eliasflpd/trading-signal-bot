import os
import time
import math
import requests
import threading
from datetime import datetime

# -- Config ----------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

KUCOIN_BASE  = "https://api.kucoin.com/api/v1/market/candles"
YAHOO_BASE   = "https://query1.finance.yahoo.com/v8/finance/chart/"

# KuCoin crypto pairs
CRYPTO_SYMBOLS = {
    "BTC-USDT": "BTC/USD OTC",
    "ETH-USDT": "ETH/USD OTC",
    "SOL-USDT": "SOL/USD OTC",
    "XRP-USDT": "XRP/USD OTC",
}

# Yahoo Finance forex pairs (IQ Option OTC)
FOREX_SYMBOLS = {
    "EURUSD=X": "EURUSD OTC",
    "GBPUSD=X": "GBPUSD OTC",
    "EURGBP=X": "EURGBP OTC",
}

SYMBOLS = {**CRYPTO_SYMBOLS, **FOREX_SYMBOLS}

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


# -- Indicadores -----------------------------------------------------------

def calculate_rsi(closes, period=RSI_PERIOD):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-period - 1 + i] - closes[-period - 1 + i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(-diff)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        g = diff if diff > 0 else 0
        l = -diff if diff < 0 else 0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def calculate_ema(closes, period):
    if len(closes) < period:
        return closes[-1]
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 6)


def calculate_bollinger(closes, period=BB_PERIOD, std_mult=BB_STD):
    if len(closes) < period:
        return closes[-1], closes[-1], closes[-1]
    window = closes[-period:]
    mean   = sum(window) / period
    std    = math.sqrt(sum((x - mean) ** 2 for x in window) / period)
    return round(mean + std_mult * std, 6), round(mean, 6), round(mean - std_mult * std, 6)


def calculate_macd(closes, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    if len(closes) < slow + signal:
        return 0, 0, "Neutro"
    ema_fast   = calculate_ema(closes, fast)
    ema_slow   = calculate_ema(closes, slow)
    macd_line  = ema_fast - ema_slow
    macd_vals  = [calculate_ema(closes[:i], fast) - calculate_ema(closes[:i], slow)
                  for i in range(slow, len(closes) + 1)]
    signal_val = calculate_ema(macd_vals, signal) if len(macd_vals) >= signal else macd_line
    trend      = "Alta" if macd_line > signal_val else "Baixa"
    return round(macd_line, 6), round(signal_val, 6), trend


def calculate_stochastic(highs, lows, closes, k=STOCH_K, smooth=STOCH_SMOOTH):
    if len(closes) < k:
        return 50.0, 50.0
    window_h = highs[-k:]
    window_l = lows[-k:]
    highest  = max(window_h)
    lowest   = min(window_l)
    if highest == lowest:
        raw_k = 50.0
    else:
        raw_k = 100 * (closes[-1] - lowest) / (highest - lowest)
    stoch_k_vals = []
    for i in range(k, len(closes) + 1):
        h = max(highs[i - k:i])
        l = min(lows[i - k:i])
        if h == l:
            stoch_k_vals.append(50.0)
        else:
            stoch_k_vals.append(100 * (closes[i - 1] - l) / (h - l))
    stoch_d = sum(stoch_k_vals[-smooth:]) / smooth if len(stoch_k_vals) >= smooth else raw_k
    return round(raw_k, 2), round(stoch_d, 2)


def detect_candle_pattern(opens, highs, lows, closes):
    if len(closes) < 3:
        return "Nenhum"
    o1, h1, l1, c1 = opens[-2], highs[-2], lows[-2], closes[-2]
    o2, h2, l2, c2 = opens[-1], highs[-1], lows[-1], closes[-1]
    body1  = abs(c1 - o1)
    body2  = abs(c2 - o2)
    range1 = h1 - l1 if h1 != l1 else 0.0001
    range2 = h2 - l2 if h2 != l2 else 0.0001
    # Engolfo de Alta
    if c2 > o2 and c1 < o1 and c2 > o1 and o2 < c1:
        return "Engolfo de Alta"
    # Engolfo de Baixa
    if c2 < o2 and c1 > o1 and c2 < o1 and o2 > c1:
        return "Engolfo de Baixa"
    # Martelo
    lower_shadow2 = (min(o2, c2) - l2)
    upper_shadow2 = (h2 - max(o2, c2))
    if (lower_shadow2 > 2 * body2 and upper_shadow2 < body2 * 0.5 and body2 / range2 < 0.4):
        return "Martelo"
    # Estrela Cadente
    if (upper_shadow2 > 2 * body2 and lower_shadow2 < body2 * 0.5 and body2 / range2 < 0.4):
        return "Estrela Cadente"
    return "Nenhum"


# -- Dados de mercado -------------------------------------------------------

def get_candles_kucoin(symbol, interval="1min", limit=120):
    params = {"symbol": symbol, "type": interval}
    resp   = requests.get(KUCOIN_BASE, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "200000":
        raise Exception("KuCoin: " + str(data.get("msg", "erro desconhecido")))
    candles = data["data"]
    candles.reverse()
    candles = candles[-limit:]
    opens  = [float(c[1]) for c in candles]
    closes = [float(c[2]) for c in candles]
    highs  = [float(c[3]) for c in candles]
    lows   = [float(c[4]) for c in candles]
    return opens, highs, lows, closes


def get_candles_yahoo(symbol, limit=120):
    url     = YAHOO_BASE + symbol
    params  = {"interval": "1m", "range": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    resp    = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    data    = resp.json()
    result  = data["chart"]["result"][0]
    ohlcv   = result["indicators"]["quote"][0]
    opens_raw  = ohlcv["open"]
    highs_raw  = ohlcv["high"]
    lows_raw   = ohlcv["low"]
    closes_raw = ohlcv["close"]
    opens, highs, lows, closes = [], [], [], []
    for i in range(len(closes_raw)):
        if (opens_raw[i] is not None and highs_raw[i] is not None
                and lows_raw[i] is not None and closes_raw[i] is not None):
            opens.append(float(opens_raw[i]))
            highs.append(float(highs_raw[i]))
            lows.append(float(lows_raw[i]))
            closes.append(float(closes_raw[i]))
    return opens[-limit:], highs[-limit:], lows[-limit:], closes[-limit:]


def get_candles_full(symbol, limit=120):
    if symbol in FOREX_SYMBOLS:
        return get_candles_yahoo(symbol, limit)
    return get_candles_kucoin(symbol, limit=limit)


def seconds_to_next_candle():
    now = datetime.utcnow()
    elapsed = now.second + now.microsecond / 1_000_000
    return 60.0 - elapsed


# -- Expiração recomendada --------------------------------------------------

def get_expiration(rsi, stoch_k, pattern):
    strong_patterns = ["Engolfo de Alta", "Engolfo de Baixa", "Estrela Cadente"]
    if stoch_k < 20 or stoch_k > 80:
        return "1 minuto"
    if pattern in strong_patterns:
        return "1 minuto"
    if rsi < 30 or rsi > 70:
        return "2 minutos"
    return "5 minutos"


# -- Telegram --------------------------------------------------------------

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
    except Exception:
        return []


# -- Mensagens -------------------------------------------------------------

SEP = "━━━━━━━━━━━━━━━"

def build_signal_message(sig, entry=10):
    d     = sig["direction"]
    label = sig["label"]
    rsi   = sig["rsi"]
    stoch = sig["stoch_k"]
    pat   = sig["pattern"]
    expir = get_expiration(rsi, stoch, pat)
    if d == "CALL":
        header = "U0001f7e2 COMPRE — " + label
        action = "➡️ IQ Option → clique no botão VERDE"
    else:
        header = "U0001f534 VENDA — " + label
        action = "➡️ IQ Option → clique no botão VERMELHO"
    return (
        header + "\n"
        + "⏱ Tempo: " + expir + "\n"
        + "U0001f4b0 Entrada: $" + str(entry) + "\n"
        + "⏰ Você tem 50 segundos para entrar!\n"
        + action
    )


def build_warning_message(sig):
    d     = sig["direction"]
    label = sig["label"]
    if d == "CALL":
        cor = "U0001f7e2 VERDE = COMPRE"
    else:
        cor = "U0001f534 VERMELHO = VENDA"
    return (
        "⚡ ÚLTIMO AVISO — " + label + "\n"
        + "⏰ 20 segundos! Clique logo!\n"
        + cor
    )


# -- Analise ---------------------------------------------------------------

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
        rsi                         = calculate_rsi(closes)
        ema9                        = calculate_ema(closes, EMA_SHORT)
        ema21                       = calculate_ema(closes, EMA_LONG)
        bb_upper, bb_mid, bb_lower  = calculate_bollinger(closes)
        macd_line, macd_sig, macd_t = calculate_macd(closes)
        stoch_k, stoch_d            = calculate_stochastic(highs, lows, closes)
        pattern                     = detect_candle_pattern(opens, highs, lows, closes)
        price                       = closes[-1]
        call_pts = 0
        put_pts  = 0
        pat_bonus = 0
        # RSI
        rsi_lbl = "Neutro"
        if rsi < 30:
            call_pts += 1
            rsi_lbl = "Sobrevendido"
        elif rsi > 70:
            put_pts += 1
            rsi_lbl = "Sobrecomprado"
        # EMA
        ema_trend = "Neutro"
        if ema9 > ema21:
            call_pts += 1
            ema_trend = "Alta"
        elif ema9 < ema21:
            put_pts += 1
            ema_trend = "Baixa"
        # Bollinger Bands
        bb_status = "Neutro"
        if closes[-1] <= bb_lower:
            call_pts += 1
            bb_status = "Abaixo da banda"
        elif closes[-1] >= bb_upper:
            put_pts += 1
            bb_status = "Acima da banda"
        # MACD
        if macd_t == "Alta":
            call_pts += 1
        elif macd_t == "Baixa":
            put_pts += 1
        # Stochastic
        if stoch_k < 20:
            call_pts += 1
        elif stoch_k > 80:
            put_pts += 1
        # Candle pattern
        if pattern in ("Engolfo de Alta", "Martelo"):
            call_pts += 1
            pat_bonus = 1
        elif pattern in ("Engolfo de Baixa", "Estrela Cadente"):
            put_pts += 1
            pat_bonus = 1
        print(
            "[" + ts + "] " + symbol +
            " P:" + str(round(price, 4)) +
            " RSI:" + str(rsi) + "(" + rsi_lbl + ")" +
            " EMA:" + ema_trend +
            " BB:" + bb_status +
            " MACD:" + macd_t +
            " Stoch:" + str(stoch_k) +
            " Pat:" + pattern +
            " C=" + str(call_pts) + " P=" + str(put_pts)
        )
        if forced and target_chat:
            dir_str = "Sem direcao"
            if call_pts >= 3:
                dir_str = "CALL forte (" + str(call_pts) + "/6)"
            elif put_pts >= 3:
                dir_str = "PUT forte (" + str(put_pts) + "/6)"
            elif call_pts > put_pts:
                dir_str = "Leve CALL (" + str(call_pts) + "/6)"
            elif put_pts > call_pts:
                dir_str = "Leve PUT (" + str(put_pts) + "/6)"
            msg = (
                "<b>Analise: " + label + "</b>\n"
                "Preco: " + str(round(price, 5)) + "\n"
                "RSI(14): " + str(rsi) + " - " + rsi_lbl + "\n"
                "EMA9/21: " + ema_trend + "\n"
                "MACD: " + macd_t + " | Stoch: " + str(stoch_k) + "\n"
                "BB: " + bb_status + "\n"
                "Padrao: " + pattern + "\n"
                "Direcao: " + dir_str + "\n"
                "Pontos: CALL=" + str(call_pts) + "/6 PUT=" + str(put_pts) + "/6"
            )
            send_telegram(target_chat, msg)
        if call_pts >= 3:
            conf = min(65 + call_pts * 3 + pat_bonus * 5 + (5 if rsi < 30 else 0), 95)
            return dict(direction="CALL", label=label, symbol=symbol,
                        rsi=rsi, macd_trend=macd_t, stoch_k=stoch_k,
                        pattern=pattern, confidence=conf)
        if put_pts >= 3:
            conf = min(65 + put_pts * 3 + pat_bonus * 5 + (5 if rsi > 70 else 0), 95)
            return dict(direction="PUT", label=label, symbol=symbol,
                        rsi=rsi, macd_trend=macd_t, stoch_k=stoch_k,
                        pattern=pattern, confidence=conf)
        print("[" + ts + "] " + symbol + " - sem confluencia")
        return None
    except Exception as e:
        err = "[" + ts + "] Erro " + symbol + ": " + str(e)
        print(err)
        if forced and target_chat:
            send_telegram(target_chat, "Erro ao buscar " + label + ": " + str(e))
        return None


# -- Comandos --------------------------------------------------------------

def handle_command(text, chat_id):
    ts   = datetime.utcnow().strftime("%H:%M:%S")
    text = text.strip().lower().split("@")[0]
    print("[" + ts + "] CMD: " + text + " chat=" + str(chat_id))
    if text == "/start":
        msg = (
            "U0001f44b Ola! Sou o <b>Bot de Sinais IQ Option</b>.\n\n"
            "<b>Comandos:</b>\n"
            "/start - Boas-vindas\n"
            "/status - Uptime e cooldowns\n"
            "/sinal - Analise imediata de todos os ativos\n\n"
            "U0001f4cc <b>Ativos (7):</b> BTC, ETH, SOL, XRP, EURUSD, GBPUSD, EURGBP\n"
            "U0001f4ca <b>Indicadores:</b> RSI - EMA - BB - MACD - Stochastic\n"
            "U0001f56f <b>Padroes:</b> Martelo, Engolfo, Estrela Cadente\n"
            "⏰ Sinal enviado 50s antes do fechamento da vela!\n"
            "✅ Confluencia minima: 3 de 6 indicadores.\n"
            "⏱ Expiracao dinamica: 1, 2 ou 5 minutos conforme forca do sinal."
        )
        send_telegram(chat_id, msg)
    elif text == "/status":
        uptime = datetime.utcnow() - start_time
        h = int(uptime.total_seconds() // 3600)
        m = int((uptime.total_seconds() % 3600) // 60)
        now = time.time()
        lines = ["<b>Status do Bot</b>", "Uptime: " + str(h) + "h " + str(m) + "m", ""]
        for symbol, label in SYMBOLS.items():
            last = last_signal_time.get(symbol, 0)
            if last == 0:
                lines.append(label + ": aguardando sinal")
            else:
                remaining = int(COOLDOWN - (now - last))
                if remaining > 0:
                    lines.append(label + ": cooldown " + str(remaining) + "s")
                else:
                    lines.append(label + ": pronto")
        send_telegram(chat_id, "\n".join(lines))
    elif text == "/sinal":
        send_telegram(chat_id, "U0001f50d Analisando 7 ativos...")
        for symbol, label in SYMBOLS.items():
            run_analysis(symbol, label, forced=True, target_chat=chat_id)
    else:
        send_telegram(chat_id, "Comando desconhecido. Use /start, /status ou /sinal.")


# -- Polling ---------------------------------------------------------------

def polling_loop():
    global last_update_id
    print("Polling Telegram iniciado.")
    while True:
        try:
            updates = get_updates(offset=last_update_id + 1)
            for upd in updates:
                last_update_id = upd.get("update_id", last_update_id)
                msg  = upd.get("message", {})
                text = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id")
                if text and chat_id and text.startswith("/"):
                    handle_command(text, chat_id)
        except Exception as e:
            print("Erro polling: " + str(e))
        time.sleep(2)


# -- Signal loop -----------------------------------------------------------

def signal_loop():
    print("Loop de sinais iniciado.")
    while True:
        ts  = datetime.utcnow().strftime("%H:%M:%S")
        now = time.time()
        print("[" + ts + "] === Ciclo de analise ===")
        for symbol, label in SYMBOLS.items():
            last = last_signal_time.get(symbol, 0)
            if now - last < COOLDOWN:
                secs_cd = int(COOLDOWN - (now - last))
                print("[" + ts + "] " + symbol + " cooldown " + str(secs_cd) + "s")
                continue
            sig = run_analysis(symbol, label)
            if not sig:
                continue
            secs_left = seconds_to_next_candle()
            if secs_left > 58:
                wait_time = secs_left - 58
                ts2 = datetime.utcnow().strftime("%H:%M:%S")
                print("[" + ts2 + "] " + symbol + " aguardando " + str(round(wait_time, 1)) + "s para janela 50s")
                time.sleep(wait_time)
                secs_left = seconds_to_next_candle()
            if secs_left < 5:
                ts2 = datetime.utcnow().strftime("%H:%M:%S")
                print("[" + ts2 + "] " + symbol + " janela perdida, pulando")
                continue
            ts2   = datetime.utcnow().strftime("%H:%M:%S")
            msg_s = build_signal_message(sig)
            ok    = send_telegram(TELEGRAM_CHAT_ID, msg_s)
            if ok:
                last_signal_time[symbol] = time.time()
                print("[" + ts2 + "] Sinal " + sig["direction"] + " -> " + label + " (50s para fechar)")
            else:
                print("[" + ts2 + "] Falha Telegram no sinal principal")
                continue
            time.sleep(30)
            ts3 = datetime.utcnow().strftime("%H:%M:%S")
            msg_aviso = build_warning_message(sig)
            ok2 = send_telegram(TELEGRAM_CHAT_ID, msg_aviso)
            if ok2:
                print("[" + ts3 + "] Segundo aviso enviado -> " + label)
            else:
                print("[" + ts3 + "] Falha Telegram no segundo aviso")
        try:
            time.sleep(CHECK_INTERVAL)
        except Exception:
            pass


# -- Main ------------------------------------------------------------------

def main():
    print("Bot IQ Option v5 iniciado!")
    print("API: KuCoin (crypto) + Yahoo Finance (forex) | Ativos: " + str(list(SYMBOLS.keys())))
    print("Indicadores: RSI + EMA + BB + MACD + Stoch + Padroes")
    print("Ciclo: " + str(CHECK_INTERVAL) + "s | Cooldown: " + str(COOLDOWN) + "s")
    print("Timing: sinal 50s antes do fechamento da vela")
    print("Expiracao: dinamica (1, 2 ou 5 minutos)")
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
