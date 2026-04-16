import os
import time
import math
import requests
import threading
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

KUCOIN_BASE = "https://api.kucoin.com/api/v1/market/candles"
SYMBOL      = "GBP-USDT"
LABEL       = "GBP/USD OTC"

# Horário de operação: 21:00 — 23:59 BRT (UTC-3)
BRT_OFFSET      = timedelta(hours=-3)
SESSION_START_H = 21
SESSION_START_M = 0
SESSION_END_H   = 23
SESSION_END_M   = 59

COOLDOWN_SECS   = 300    # 5 minutos entre sinais
MAX_SIGNALS     = 6      # máximo por noite
CHECK_INTERVAL  = 30     # segundos entre ciclos de análise

# ---------------------------------------------------------------------------
# Estado global
# ---------------------------------------------------------------------------

last_signal_time  = 0
signals_tonight   = 0
session_notified  = False   # aviso de início enviado hoje?
session_ended     = False   # aviso de fim enviado hoje?
last_update_id    = 0
start_time        = datetime.utcnow()


# ---------------------------------------------------------------------------
# Horário
# ---------------------------------------------------------------------------

def now_brt():
    return datetime.now(timezone.utc).astimezone(timezone(BRT_OFFSET))


def in_session():
    """Retorna True se estiver dentro da janela 21:00–23:59 BRT."""
    t = now_brt()
    start = t.replace(hour=SESSION_START_H, minute=SESSION_START_M, second=0, microsecond=0)
    end   = t.replace(hour=SESSION_END_H,   minute=SESSION_END_M,   second=59, microsecond=999999)
    return start <= t <= end


def seconds_to_next_candle():
    now = datetime.utcnow()
    elapsed = now.second + now.microsecond / 1_000_000
    return 60.0 - elapsed


# ---------------------------------------------------------------------------
# Dados de mercado
# ---------------------------------------------------------------------------

def get_candles(limit=10):
    """Busca as últimas `limit` velas de 1 min no KuCoin para GBP-USDT."""
    params = {"symbol": SYMBOL, "type": "1min"}
    resp   = requests.get(KUCOIN_BASE, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "200000":
        raise Exception("KuCoin: " + str(data.get("msg", "erro")))
    candles = data["data"]
    candles.reverse()   # ordem cronológica (mais antiga primeiro)
    candles = candles[-limit:]
    opens  = [float(c[1]) for c in candles]
    closes = [float(c[2]) for c in candles]
    highs  = [float(c[3]) for c in candles]
    lows   = [float(c[4]) for c in candles]
    return opens, highs, lows, closes


# ---------------------------------------------------------------------------
# Detecção de padrões Price Action
# ---------------------------------------------------------------------------

def is_bullish(o, c):
    return c > o

def is_bearish(o, c):
    return c < o

def body_size(o, c):
    return abs(c - o)

def candle_range(h, l):
    return h - l if h != l else 0.00001

def upper_shadow(o, c, h):
    return h - max(o, c)

def lower_shadow(o, c, l):
    return min(o, c) - l

def is_doji(o, c, h, l):
    """Doji: corpo muito pequeno (< 25% do range total)."""
    return body_size(o, c) <= 0.25 * candle_range(h, l)


def detect_pattern(opens, highs, lows, closes):
    """
    Analisa as últimas velas e retorna (direction, pattern_name) ou (None, None).
    direction: 'CALL' (compra) ou 'PUT' (venda).
    Requer pelo menos 5 velas.
    """
    if len(closes) < 5:
        return None, None

    # Índices das últimas 5 velas (0 = mais antiga, 4 = mais recente)
    o = opens[-5:]
    h = highs[-5:]
    l = lows[-5:]
    c = closes[-5:]

    # ---- PADRÕES DE COMPRA ------------------------------------------------

    # Martelo: vela atual (índice 4) com sombra inferior >= 2x corpo,
    #          precedida por pelo menos 2 velas de baixa (índices 2 e 3)
    o4, h4, l4, c4 = o[4], h[4], l[4], c[4]
    body4   = body_size(o4, c4)
    lshadow4 = lower_shadow(o4, c4, l4)
    ushadow4 = upper_shadow(o4, c4, h4)
    rng4     = candle_range(h4, l4)
    two_bearish_before = is_bearish(o[2], c[2]) and is_bearish(o[3], c[3])

    if (body4 > 0
            and lshadow4 >= 2 * body4
            and ushadow4 <= 0.3 * rng4
            and two_bearish_before):
        return "CALL", "Martelo"

    # Engolfo de Alta: vela atual (4) bullish que engole vela anterior (3) bearish
    if (is_bullish(o[4], c[4])
            and is_bearish(o[3], c[3])
            and c[4] > o[3]
            and o[4] < c[3]):
        return "CALL", "Engolfo de Alta"

    # Estrela da Manhã: vela 2 bearish forte + vela 3 doji + vela 4 bullish forte
    big_bear = is_bearish(o[2], c[2]) and body_size(o[2], c[2]) >= 0.5 * candle_range(h[2], l[2])
    doji_mid = is_doji(o[3], c[3], h[3], l[3])
    big_bull = is_bullish(o[4], c[4]) and body_size(o[4], c[4]) >= 0.5 * candle_range(h[4], l[4])
    if big_bear and doji_mid and big_bull:
        return "CALL", "Estrela da Manhã"

    # ---- PADRÕES DE VENDA ------------------------------------------------

    # Estrela Cadente: sombra superior >= 2x corpo, precedida por 2+ velas de alta
    two_bullish_before = is_bullish(o[2], c[2]) and is_bullish(o[3], c[3])

    if (body4 > 0
            and ushadow4 >= 2 * body4
            and lshadow4 <= 0.3 * rng4
            and two_bullish_before):
        return "PUT", "Estrela Cadente"

    # Engolfo de Baixa: vela atual (4) bearish que engole vela anterior (3) bullish
    if (is_bearish(o[4], c[4])
            and is_bullish(o[3], c[3])
            and o[4] > c[3]
            and c[4] < o[3]):
        return "PUT", "Engolfo de Baixa"

    # Estrela da Tarde: vela 2 bullish forte + vela 3 doji + vela 4 bearish forte
    big_bull2 = is_bullish(o[2], c[2]) and body_size(o[2], c[2]) >= 0.5 * candle_range(h[2], l[2])
    big_bear2 = is_bearish(o[4], c[4]) and body_size(o[4], c[4]) >= 0.5 * candle_range(h[4], l[4])
    if big_bull2 and doji_mid and big_bear2:
        return "PUT", "Estrela da Tarde"

    return None, None


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url  = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=data, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print("Erro Telegram: " + str(e))
        return False


def get_updates(offset=0):
    url    = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/getUpdates"
    params = {"timeout": 5, "offset": offset, "allowed_updates": ["message"]}
    try:
        resp = requests.get(url, params=params, timeout=10)
        return resp.json().get("result", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Mensagens
# ---------------------------------------------------------------------------

def msg_signal(direction):
    if direction == "CALL":
        return (
            "U0001f7e2 <b>COMPRE — GBP/USD OTC</b>\n"
            + "⏱ Tempo: 1 minuto\n"
            + "➡️ Clique no botão VERDE"
        )
    else:
        return (
            "U0001f534 <b>VENDA — GBP/USD OTC</b>\n"
            + "⏱ Tempo: 1 minuto\n"
            + "➡️ Clique no botão VERMELHO"
        )


def msg_warning():
    return (
        "⚡ 20 segundos! Clique logo!\n"
        + "U0001f7e2 VERDE = COMPRE\n"
        + "U0001f534 VERMELHO = VENDA"
    )


def msg_session_start():
    return (
        "U0001f7e2 <b>Horário de operação iniciado!</b>\n"
        + "Monitorando GBP/USD OTC..."
    )


def msg_session_end():
    return (
        "U0001f534 <b>Encerrando operações por hoje.</b>\n"
        + "Até amanhã às 21h!"
    )


# ---------------------------------------------------------------------------
# Comandos Telegram
# ---------------------------------------------------------------------------

def handle_command(text, chat_id):
    ts   = now_brt().strftime("%H:%M:%S BRT")
    text = text.strip().lower().split("@")[0]
    print("[" + ts + "] CMD: " + text)
    if text == "/start":
        msg = (
            "U0001f44b Olá! Sou o <b>Bot GBP/USD OTC</b>.\n\n"
            "<b>Estratégia:</b> Price Action (velas)\n"
            "<b>Ativo:</b> GBP/USD OTC\n"
            "<b>Horário:</b> 21h – 23h59 (Brasília)\n"
            "<b>Padrões:</b> Martelo, Engolfo, Estrela da Manhã/Tarde\n"
            "<b>Máx. sinais:</b> 6 por noite\n\n"
            "/status — ver estado atual"
        )
        url  = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}, timeout=10)
    elif text == "/status":
        brt_now = now_brt()
        sessao  = "Ativa ✅" if in_session() else "Inativa (fora do horário)"
        uptime  = datetime.utcnow() - start_time
        h = int(uptime.total_seconds() // 3600)
        m = int((uptime.total_seconds() % 3600) // 60)
        remaining_cd = max(0, int(COOLDOWN_SECS - (time.time() - last_signal_time)))
        msg = (
            "<b>Status</b>\n"
            "Hora BRT: " + brt_now.strftime("%H:%M:%S") + "\n"
            "Sessão: " + sessao + "\n"
            "Sinais hoje: " + str(signals_tonight) + "/" + str(MAX_SIGNALS) + "\n"
            "Cooldown: " + (str(remaining_cd) + "s" if remaining_cd > 0 else "pronto") + "\n"
            "Uptime: " + str(h) + "h " + str(m) + "m"
        )
        url  = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}, timeout=10)


# ---------------------------------------------------------------------------
# Polling Telegram
# ---------------------------------------------------------------------------

def polling_loop():
    global last_update_id
    print("Polling iniciado.")
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


# ---------------------------------------------------------------------------
# Loop principal de sinais
# ---------------------------------------------------------------------------

def signal_loop():
    global last_signal_time, signals_tonight, session_notified, session_ended

    print("Loop de sinais iniciado.")

    # Rastreia o dia atual para resetar contagem de sinais à meia-noite BRT
    last_day = now_brt().date()

    while True:
        try:
            brt_now  = now_brt()
            ts       = brt_now.strftime("%H:%M:%S BRT")
            today    = brt_now.date()

            # Reset diário à meia-noite
            if today != last_day:
                signals_tonight  = 0
                session_notified = False
                session_ended    = False
                last_day         = today
                print("[" + ts + "] Reset diario: sinais e flags zerados.")

            active = in_session()

            # --- Aviso de INÍCIO de sessão ---
            if active and not session_notified:
                session_notified = True
                session_ended    = False
                ok = send_telegram(msg_session_start())
                print("[" + ts + "] Sessao iniciada. Telegram: " + str(ok))

            # --- Aviso de FIM de sessão ---
            if not active and session_notified and not session_ended:
                session_ended = True
                ok = send_telegram(msg_session_end())
                print("[" + ts + "] Sessao encerrada. Telegram: " + str(ok))

            # --- Fora do horário: dorme e continua ---
            if not active:
                print("[" + ts + "] Fora do horario de operacao.")
                time.sleep(CHECK_INTERVAL)
                continue

            # --- Limite de sinais atingido ---
            if signals_tonight >= MAX_SIGNALS:
                print("[" + ts + "] Limite de " + str(MAX_SIGNALS) + " sinais atingido.")
                time.sleep(CHECK_INTERVAL)
                continue

            # --- Cooldown ---
            now = time.time()
            if now - last_signal_time < COOLDOWN_SECS:
                remaining = int(COOLDOWN_SECS - (now - last_signal_time))
                print("[" + ts + "] Cooldown: " + str(remaining) + "s restantes.")
                time.sleep(CHECK_INTERVAL)
                continue

            # --- Busca candles e detecta padrão ---
            try:
                opens, highs, lows, closes = get_candles(limit=10)
            except Exception as e:
                print("[" + ts + "] Erro ao buscar candles: " + str(e))
                time.sleep(CHECK_INTERVAL)
                continue

            direction, pattern = detect_pattern(opens, highs, lows, closes)

            if not direction:
                print("[" + ts + "] Nenhum padrao detectado.")
                time.sleep(CHECK_INTERVAL)
                continue

            # --- Aguarda janela ideal (50s antes do fechamento da vela) ---
            secs_left = seconds_to_next_candle()
            if secs_left > 58:
                wait = secs_left - 58
                print("[" + ts + "] Aguardando " + str(round(wait, 1)) + "s para janela de 50s.")
                time.sleep(wait)
                secs_left = seconds_to_next_candle()
            if secs_left < 5:
                print("[" + ts + "] Janela perdida, pulando.")
                time.sleep(CHECK_INTERVAL)
                continue

            # --- Envia sinal ---
            ts2 = now_brt().strftime("%H:%M:%S BRT")
            ok  = send_telegram(msg_signal(direction))
            if ok:
                last_signal_time  = time.time()
                signals_tonight  += 1
                print("[" + ts2 + "] Sinal " + direction + " (" + pattern + ") enviado. Total: " + str(signals_tonight) + "/" + str(MAX_SIGNALS))
            else:
                print("[" + ts2 + "] Falha ao enviar sinal.")
                time.sleep(CHECK_INTERVAL)
                continue

            # --- Segundo aviso após 30s ---
            time.sleep(30)
            ts3 = now_brt().strftime("%H:%M:%S BRT")
            ok2 = send_telegram(msg_warning())
            if ok2:
                print("[" + ts3 + "] Segundo aviso enviado.")
            else:
                print("[" + ts3 + "] Falha no segundo aviso.")

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            ts = now_brt().strftime("%H:%M:%S BRT")
            print("[" + ts + "] Erro no loop: " + str(e))
            time.sleep(CHECK_INTERVAL)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Bot GBP/USD OTC Price Action v1 iniciado!")
    print("Ativo: " + SYMBOL + " | Horario: 21:00-23:59 BRT")
    print("Padroes: Martelo, Engolfo de Alta/Baixa, Estrela da Manha/Tarde, Estrela Cadente")
    print("Max sinais: " + str(MAX_SIGNALS) + " | Cooldown: " + str(COOLDOWN_SECS) + "s")
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
