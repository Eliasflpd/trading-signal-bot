import os
import time
import math
import requests
import threading
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# API Kucoin — publica, sem restricao geografica, suporta klines
KUCOIN_BASE = "https://api.kucoin.com/api/v1/market/candles"

SYMBOLS = {
        "BTC-USDT": "BTC/USD OTC",
        "ETH-USDT": "ETH/USD OTC",
        "SOL-USDT": "SOL/USD OTC",
        "XRP-USDT": "XRP/USD OTC",
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
    return (
                round(middle + std_mult * std, 8),
                round(middle, 8),
                round(middle - std_mult * std, 8),
    )


def calculate_macd(closes, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
        if len(closes) < slow + signal:
                    return 0.0, 0.0, "Neutro"
                macd_values = []
    for i in range(slow, len(closes) + 1):
                ef = calculate_ema(closes[:i], fast)
                es = calculate_ema(closes[:i], slow)
                macd_values.append(ef - es)
            if len(macd_values) < signal:
                        return 0.0, 0.0, "Neutro"
                    macd_line   = macd_values[-1]
    signal_line = calculate_ema(macd_values, signal)
    histogram   = macd_line - signal_line
    trend = "Alta" if histogram > 0 else ("Baixa" if histogram < 0 else "Neutro")
    return round(macd_line, 8), round(signal_line, 8), trend


def calculate_stochastic(highs, lows, closes, k_period=STOCH_K, d_period=STOCH_D):
        if len(closes) < k_period:
                    return 50.0, 50.0
                k_values = []
    for i in range(k_period - 1, len(closes)):
                h_max = max(highs[i - k_period + 1: i + 1])
                l_min = min(lows[i - k_period + 1: i + 1])
                if h_max == l_min:
                                k_values.append(50.0)
else:
            k_values.append(100 * (closes[i] - l_min) / (h_max - l_min))
        k_smooth = sum(k_values[-STOCH_SMOOTH:]) / STOCH_SMOOTH if len(k_values) >= STOCH_SMOOTH else k_values[-1]
    d_line   = sum(k_values[-d_period:]) / d_period if len(k_values) >= d_period else k_smooth
    return round(k_smooth, 2), round(d_line, 2)


def detect_candle_pattern(opens, highs, lows, closes):
        if len(closes) < 2:
                    return "Nenhum"
                o1, h1, l1, c1 = opens[-2], highs[-2], lows[-2], closes[-2]
    o2, h2, l2, c2 = opens[-1], highs[-1], lows[-1], closes[-1]
    body2         = abs(c2 - o2)
    range2        = (h2 - l2) if h2 != l2 else 0.0001
    lower_shadow2 = min(o2, c2) - l2
    upper_shadow2 = h2 - max(o2, c2)

    if body2 / range2 < 0.35 and lower_shadow2 >= 2 * body2 and upper_shadow2 <= body2:
                return "Martelo"
            if body2 / range2 < 0.35 and upper_shadow2 >= 2 * body2 and lower_shadow2 <= body2:
                        return "Estrela Cadente"
                    if c1 < o1 and c2 > o2 and c2 > o1 and o2 < c1:
                                return "Engolfo de Alta"
                            if c1 > o1 and c2 < o2 and c2 < o1 and o2 > c1:
                                        return "Engolfo de Baixa"
                                    return "Nenhum"


# ── KuCoin API ────────────────────────────────────────────────────

def get_candles_full(symbol, interval="1min", limit=120):
        # KuCoin retorna [timestamp, open, close, high, low, volume, turnover]
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


def seconds_to_next_candle():
        """Retorna quantos segundos faltam para a proxima vela de 1 min fechar."""
    now = datetime.utcnow()
    seconds_elapsed = now.second + now.microsecond / 1_000_000
    return 60.0 - seconds_elapsed


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


# ── Mensagens de sinal ────────────────────────────────────────────

def build_signal_message(sig, entry=10):
        d      = sig["direction"]
    label  = sig["label"]
    rsi    = sig["rsi"]
    macd_t = sig["macd_trend"]
    stoch  = sig["stoch_k"]
    pat    = sig["pattern"]
    conf   = sig["confidence"]

    SEP = "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"

    if d == "CALL":
                header = "\U0001f7e2 <b>CALL \u2014 " + label + "</b>"
                action = "\u27a1\ufe0f IQ Option \u2192 <b>ACIMA \u25b2</b>"
else:
        header = "\U0001f534 <b>PUT \u2014 " + label + "</b>"
        action = "\u27a1\ufe0f IQ Option \u2192 <b>ABAIXO \u25bc</b>"

    pat_line = ""
    if pat != "Nenhum":
                pat_line = "\U0001f56f <b>Padr\u00e3o:</b> <i>" + pat + "</i>\n"

    msg = (
                header + "\n"
                + SEP + "\n"
                + "\u23f1 <b>Expira\u00e7\u00e3o:</b> 1 minuto\n"
                + "\U0001f4ca <b>RSI:</b> " + str(rsi)
                  + " | <b>MACD:</b> " + macd_t
                  + " | <b>Stoch:</b> " + str(stoch) + "\n"
                + pat_line
                + "\U0001f4aa <b>Confian\u00e7a:</b> " + str(conf) + "%\n"
                + "\U0001f4b0 <b>Entrada:</b> $" + str(entry) + "\n"
                + SEP + "\n"
                + "\u23f0 <b>Voc\u00ea tem 50 segundos para entrar!</b>\n"
                + action
    )
    return msg


def build_warning_message(sig):
        d     = sig["direction"]
    label = sig["label"]

    if d == "CALL":
                action = "\u27a1\ufe0f IQ Option \u2192 <b>ACIMA \u25b2</b>"
else:
        action = "\u27a1\ufe0f IQ Option \u2192 <b>ABAIXO \u25bc</b>"

    msg = (
                "\u26a1 <b>\u00daLTIMO AVISO \u2014 " + label + " " + d + "</b>\n"
                + "\u23f0 <b>20 segundos! Entra agora ou perde!</b>\n"
                + action
    )
    return msg


# ── Analise ───────────────────────────────────────────────────────

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

        if rsi < 35:   call_pts += 1
elif rsi > 65: put_pts  += 1

        ema_trend = "Neutro"
        if ema9 > ema21:   call_pts += 1; ema_trend = "Alta"
elif ema9 < ema21: put_pts  += 1; ema_trend = "Baixa"

        bb_status = "Neutro"
        if price < bb_lower:   call_pts += 1; bb_status = "Sobrevendido"
elif price > bb_upper: put_pts  += 1; bb_status = "Sobrecomprado"

        if macd_t == "Alta":  call_pts += 1
elif macd_t == "Baixa": put_pts += 1

        if stoch_k < 20:   call_pts += 1
elif stoch_k > 80: put_pts  += 1

        pat_bonus = 0
        if pattern in ("Martelo", "Engolfo de Alta"):
                        call_pts += 1; pat_bonus = 1
elif pattern in ("Estrela Cadente", "Engolfo de Baixa"):
            put_pts += 1; pat_bonus = 1

        rsi_lbl = "sobrevendido" if rsi < 35 else ("sobrecomprado" if rsi > 65 else "neutro")

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
            if call_pts >= 4:        dir_str = "CALL forte (" + str(call_pts) + "/6)"
elif put_pts >= 4:       dir_str = "PUT forte ("  + str(put_pts)  + "/6)"
elif call_pts > put_pts: dir_str = "Leve CALL ("  + str(call_pts) + "/6)"
elif put_pts > call_pts: dir_str = "Leve PUT ("   + str(put_pts)  + "/6)"
            msg = (
                                "<b>Analise: " + label + "</b>\n"
                                "Preco: "     + str(round(price, 5)) + "\n"
                                "RSI(14): "   + str(rsi) + " - " + rsi_lbl + "\n"
                                "EMA9/21: "   + ema_trend + "\n"
                                "MACD: "      + macd_t + " | Stoch: " + str(stoch_k) + "\n"
                                "BB: "        + bb_status + "\n"
                                "Padrao: "    + pattern + "\n"
                                "Direcao: "   + dir_str + "\n"
                                "Pontos: CALL=" + str(call_pts) + "/6 PUT=" + str(put_pts) + "/6"
            )
            send_telegram(target_chat, msg)

        if call_pts >= 4:
                        conf = min(65 + call_pts * 3 + pat_bonus * 5 + (5 if rsi < 30 else 0), 95)
            return dict(direction="CALL", label=label, symbol=symbol,
                                                rsi=rsi, macd_trend=macd_t, stoch_k=stoch_k,
                                                pattern=pattern, confidence=conf)

        if put_pts >= 4:
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


# ── Comandos ──────────────────────────────────────────────────────

def handle_command(text, chat_id):
        ts   = datetime.utcnow().strftime("%H:%M:%S")
    text = text.strip().lower().split("@")[0]
    print("[" + ts + "] CMD: " + text + " chat=" + str(chat_id))

    if text == "/start":
                msg = (
                                "\U0001f44b Ola! Sou o <b>Bot de Sinais IQ Option</b>.\n\n"
                                "<b>Comandos:</b>\n"
                                "/start \u2014 Boas-vindas\n"
                                "/status \u2014 Uptime e cooldowns\n"
                                "/sinal \u2014 Analise imediata de todos os ativos\n\n"
                                "\U0001f4cc <b>Ativos:</b> BTC, ETH, SOL, XRP\n"
                                "\U0001f4ca <b>Indicadores:</b> RSI - EMA - BB - MACD - Stochastic\n"
                                "\U0001f56f <b>Padroes:</b> Martelo, Engolfo, Estrela Cadente\n"
                                "\u23f0 Sinal enviado 50s antes do fechamento da vela!\n"
                                "\u2705 Confluencia minima: 4 de 6 indicadores."
                )
        send_telegram(chat_id, msg)

elif text == "/status":
        uptime = datetime.utcnow() - start_time
        h = int(uptime.total_seconds() // 3600)
        m = int((uptime.total_seconds() % 3600) // 60)
        now   = time.time()
        secs  = seconds_to_next_candle()
        lines = []
        for sym, lbl in SYMBOLS.items():
                        diff   = now - last_signal_time.get(sym, 0)
            status = "cooldown " + str(int(COOLDOWN - diff)) + "s" if diff < COOLDOWN else "pronto"
            lines.append(lbl + ": " + status)
        msg = (
                        "\U0001f916 <b>Bot ATIVO</b>\n"
                        "\u23f1 Uptime: " + str(h) + "h " + str(m) + "m\n"
                        "\U0001f517 API: KuCoin | Ciclo: " + str(CHECK_INTERVAL) + "s\n"
                        "\U0001f55b Proxima vela em: " + str(int(secs)) + "s\n\n"
                        + "\n".join(lines)
        )
        send_telegram(chat_id, msg)

elif text == "/sinal":
        send_telegram(chat_id, "\U0001f50d Analisando " + str(len(SYMBOLS)) + " ativos... aguarde.")
        for symbol, label in SYMBOLS.items():
                        run_analysis(symbol, label, forced=True, target_chat=chat_id)

else:
        send_telegram(chat_id, "\u2753 Use /start, /status ou /sinal")


# ── Polling thread ────────────────────────────────────────────────

def polling_loop():
        global last_update_id
    print("Polling de comandos iniciado (2s).")
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


# ── Signal loop ───────────────────────────────────────────────────

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

            # Calcular quantos segundos faltam para a proxima vela fechar
            secs_left = seconds_to_next_candle()

            # So envia se tiver entre 48 e 58 segundos restantes
            # (janela ideal: exatamente 50s antes do fechamento)
            if secs_left > 58:
                                # Ainda cedo demais: aguarda ate ficar dentro da janela
                                wait_time = secs_left - 58
                                ts2 = datetime.utcnow().strftime("%H:%M:%S")
                                print("[" + ts2 + "] " + symbol + " aguardando " + str(round(wait_time, 1)) + "s para janela de 50s")
                                time.sleep(wait_time)
                                secs_left = seconds_to_next_candle()

            if secs_left < 5:
                                # Vela ja fechando, tarde demais para esta vela
                                ts2 = datetime.utcnow().strftime("%H:%M:%S")
                                print("[" + ts2 + "] " + symbol + " - vela ja fechando, aguardando proxima")
                                time.sleep(secs_left + 2)
                                continue

            # Envia o sinal principal com aviso de 50s
            ts2 = datetime.utcnow().strftime("%H:%M:%S")
            msg_principal = build_signal_message(sig)
            ok = send_telegram(TELEGRAM_CHAT_ID, msg_principal)
            if ok:
                                last_signal_time[symbol] = now
                                print("[" + ts2 + "] Sinal " + sig["direction"] + " -> " + label + " (50s para fechar)")
else:
                print("[" + ts2 + "] Falha Telegram no sinal principal")
                continue

            # Aguarda 30 segundos e manda o segundo aviso (20s restantes)
            time.sleep(30)
            ts3 = datetime.utcnow().strftime("%H:%M:%S")
            msg_aviso = build_warning_message(sig)
            ok2 = send_telegram(TELEGRAM_CHAT_ID, msg_aviso)
            if ok2:
                                print("[" + ts3 + "] Segundo aviso enviado -> " + label)
else:
                print("[" + ts3 + "] Falha Telegram no segundo aviso")

        # Aguarda o restante do ciclo
        try:
                        time.sleep(CHECK_INTERVAL)
except Exception:
            pass


# ── Main ──────────────────────────────────────────────────────────

def main():
        print("Bot IQ Option v4 iniciado!")
    print("API: KuCoin | Ativos: " + str(list(SYMBOLS.keys())))
    print("Indicadores: RSI + EMA + BB + MACD + Stoch + Padroes")
    print("Ciclo: " + str(CHECK_INTERVAL) + "s | Cooldown: " + str(COOLDOWN) + "s")
    print("Timing: sinal 50s antes do fechamento da vela")

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
