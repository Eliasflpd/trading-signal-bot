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

# Fuso horário BRT = UTC-3
BRT_OFFSET = timedelta(hours=-3)

# Janelas de operação (hora_início, minuto_início, hora_fim, minuto_fim, nome)
SESSIONS = [
    (9,  0, 11,  0, "Londres"),
    (14, 0, 16,  0, "Londres+NY"),
    (21, 0, 23, 59, "Noturna"),
]

COOLDOWN_SECS  = 300   # 5 min entre sinais
MAX_SIGNALS    = 6     # máximo por janela
CHECK_INTERVAL = 30    # segundos entre ciclos

# ---------------------------------------------------------------------------
# Estado global
# ---------------------------------------------------------------------------

last_signal_time = 0

# Por janela: controle de sinais e notificações
# Chave = índice da sessão (0, 1, 2)
session_signals   = {0: 0, 1: 0, 2: 0}   # sinais disparados nesta abertura
session_notified  = {0: False, 1: False, 2: False}  # aviso de início enviado?
session_ended     = {0: False, 1: False, 2: False}   # aviso de fim enviado?

last_update_id = 0
start_time     = datetime.utcnow()


# ---------------------------------------------------------------------------
# Horário
# ---------------------------------------------------------------------------

def now_brt():
    return datetime.now(timezone.utc).astimezone(timezone(BRT_OFFSET))


def active_session():
    """
    Retorna (index, session_tuple) se agora estiver dentro de alguma janela,
    ou (None, None) caso contrário.
    """
    t = now_brt()
    for i, (sh, sm, eh, em, name) in enumerate(SESSIONS):
        start = t.replace(hour=sh, minute=sm, second=0,  microsecond=0)
        end   = t.replace(hour=eh, minute=em, second=59, microsecond=999999)
        if start <= t <= end:
            return i, SESSIONS[i]
    return None, None


def seconds_to_next_candle():
    now = datetime.utcnow()
    elapsed = now.second + now.microsecond / 1_000_000
    return 60.0 - elapsed


# ---------------------------------------------------------------------------
# Dados de mercado
# ---------------------------------------------------------------------------

def get_candles(limit=10):
    params = {"symbol": SYMBOL, "type": "1min"}
    resp   = requests.get(KUCOIN_BASE, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "200000":
        raise Exception("KuCoin: " + str(data.get("msg", "erro")))
    candles = data["data"]
    candles.reverse()
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
    return body_size(o, c) <= 0.25 * candle_range(h, l)


def detect_pattern(opens, highs, lows, closes):
    if len(closes) < 5:
        return None, None

    o = opens[-5:]
    h = highs[-5:]
    l = lows[-5:]
    c = closes[-5:]

    o4, h4, l4, c4 = o[4], h[4], l[4], c[4]
    body4    = body_size(o4, c4)
    lshadow4 = lower_shadow(o4, c4, l4)
    ushadow4 = upper_shadow(o4, c4, h4)
    rng4     = candle_range(h4, l4)

    two_bearish = is_bearish(o[2], c[2]) and is_bearish(o[3], c[3])
    two_bullish = is_bullish(o[2], c[2]) and is_bullish(o[3], c[3])
    doji_mid    = is_doji(o[3], c[3], h[3], l[3])

    # --- COMPRA ---
    # Martelo
    if (body4 > 0
            and lshadow4 >= 2 * body4
            and ushadow4 <= 0.3 * rng4
            and two_bearish):
        return "CALL", "Martelo"

    # Engolfo de Alta
    if (is_bullish(o[4], c[4])
            and is_bearish(o[3], c[3])
            and c[4] > o[3]
            and o[4] < c[3]):
        return "CALL", "Engolfo de Alta"

    # Estrela da Manhã
    big_bear = is_bearish(o[2], c[2]) and body_size(o[2], c[2]) >= 0.5 * candle_range(h[2], l[2])
    big_bull = is_bullish(o[4], c[4]) and body_size(o[4], c[4]) >= 0.5 * candle_range(h[4], l[4])
    if big_bear and doji_mid and big_bull:
        return "CALL", "Estrela da Manhã"

    # --- VENDA ---
    # Estrela Cadente
    if (body4 > 0
            and ushadow4 >= 2 * body4
            and lshadow4 <= 0.3 * rng4
            and two_bullish):
        return "PUT", "Estrela Cadente"

    # Engolfo de Baixa
    if (is_bearish(o[4], c[4])
            and is_bullish(o[3], c[3])
            and o[4] > c[3]
            and c[4] < o[3]):
        return "PUT", "Engolfo de Baixa"

    # Estrela da Tarde
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


def send_to(chat_id, message):
    if not TELEGRAM_BOT_TOKEN:
        return False
    url  = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
    data = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
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


def msg_session_start(name, sh, sm, eh, em):
    return (
        "U0001f7e2 <b>Sessão " + name + " iniciada!</b>\n"
        + "Monitorando GBP/USD OTC\n"
        + "Até " + str(eh).zfill(2) + ":" + str(em).zfill(2) + " • Máx. " + str(MAX_SIGNALS) + " sinais"
    )


def msg_session_end(name):
    return (
        "U0001f534 <b>Sessão " + name + " encerrada.</b>\n"
        + "Até a próxima sessão!"
    )


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

def handle_command(text, chat_id):
    ts   = now_brt().strftime("%H:%M:%S BRT")
    text = text.strip().lower().split("@")[0]
    print("[" + ts + "] CMD: " + text)
    if text == "/start":
        msg = (
            "U0001f44b Olá! Sou o <b>Bot GBP/USD OTC</b>.\n\n"
            "<b>Estratégia:</b> Price Action (velas)\n"
            "<b>Ativo:</b> GBP/USD OTC\n\n"
            "<b>Janelas diárias (BRT):</b>\n"
            "U0001f55b 09:00 – 11:00 — Londres\n"
            "U0001f55d 14:00 – 16:00 — Londres + NY\n"
            "U0001f315 21:00 – 23:59 — Noturna\n\n"
            "<b>Máx.:</b> 6 sinais por sessão\n"
            "/status — ver estado atual"
        )
        send_to(chat_id, msg)
    elif text == "/status":
        brt_now   = now_brt()
        idx, sess = active_session()
        if idx is not None:
            sh, sm, eh, em, name = sess
            sessao = "Ativa ✅ (" + name + ") • " + str(session_signals[idx]) + "/" + str(MAX_SIGNALS) + " sinais"
        else:
            sessao = "Inativa (fora do horário)"
        uptime = datetime.utcnow() - start_time
        h = int(uptime.total_seconds() // 3600)
        m = int((uptime.total_seconds() % 3600) // 60)
        remaining_cd = max(0, int(COOLDOWN_SECS - (time.time() - last_signal_time)))
        msg = (
            "<b>Status</b>\n"
            "Hora BRT: " + brt_now.strftime("%H:%M:%S") + "\n"
            "Sessão: " + sessao + "\n"
            "Cooldown: " + (str(remaining_cd) + "s" if remaining_cd > 0 else "pronto") + "\n"
            "Uptime: " + str(h) + "h " + str(m) + "m"
        )
        send_to(chat_id, msg)


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def polling_loop():
    global last_update_id
    print("Polling iniciado.")
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


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------

def signal_loop():
    global last_signal_time
    global session_signals, session_notified, session_ended

    print("Loop de sinais iniciado.")

    while True:
        try:
            ts  = now_brt().strftime("%H:%M:%S BRT")
            now = time.time()

            idx, sess = active_session()

            # ---- Gerencia avisos de início / fim para cada sessão ----------
            for i, (sh, sm, eh, em, name) in enumerate(SESSIONS):
                t     = now_brt()
                start = t.replace(hour=sh, minute=sm, second=0,  microsecond=0)
                end   = t.replace(hour=eh, minute=em, second=59, microsecond=999999)
                is_on = (start <= t <= end)

                # Sessão acabou de abrir
                if is_on and not session_notified[i]:
                    session_notified[i] = True
                    session_ended[i]    = False
                    session_signals[i]  = 0
                    ok = send_telegram(msg_session_start(name, sh, sm, eh, em))
                    print("[" + ts + "] Sessao " + name + " iniciada. Telegram: " + str(ok))

                # Sessão acabou de fechar
                if not is_on and session_notified[i] and not session_ended[i]:
                    session_ended[i] = True
                    ok = send_telegram(msg_session_end(name))
                    print("[" + ts + "] Sessao " + name + " encerrada. Telegram: " + str(ok))

                # Reset do flag de notificação após encerramento
                # (permite reativar aviso no próximo dia)
                if not is_on and session_ended[i]:
                    # Verifica se já passou tempo suficiente para resetar
                    # (mais de 60 min desde o fim da sessão)
                    t_end = t.replace(hour=eh, minute=em, second=59, microsecond=0)
                    if t > t_end + timedelta(minutes=60):
                        session_notified[i] = False
                        session_ended[i]    = False

            # ---- Fora de qualquer janela: dorme e continua -----------------
            if idx is None:
                print("[" + ts + "] Fora de janela de operação.")
                time.sleep(CHECK_INTERVAL)
                continue

            sh, sm, eh, em, name = sess

            # ---- Limite de sinais da sessão --------------------------------
            if session_signals[idx] >= MAX_SIGNALS:
                print("[" + ts + "] Sessão " + name + ": limite de " + str(MAX_SIGNALS) + " sinais atingido.")
                time.sleep(CHECK_INTERVAL)
                continue

            # ---- Cooldown --------------------------------------------------
            if now - last_signal_time < COOLDOWN_SECS:
                remaining = int(COOLDOWN_SECS - (now - last_signal_time))
                print("[" + ts + "] Cooldown: " + str(remaining) + "s restantes.")
                time.sleep(CHECK_INTERVAL)
                continue

            # ---- Busca candles e detecta padrão ----------------------------
            try:
                opens, highs, lows, closes = get_candles(limit=10)
            except Exception as e:
                print("[" + ts + "] Erro candles: " + str(e))
                time.sleep(CHECK_INTERVAL)
                continue

            direction, pattern = detect_pattern(opens, highs, lows, closes)

            if not direction:
                print("[" + ts + "] [" + name + "] Nenhum padrão.")
                time.sleep(CHECK_INTERVAL)
                continue

            # ---- Aguarda janela de 50s antes do fechamento da vela ---------
            secs_left = seconds_to_next_candle()
            if secs_left > 58:
                wait = secs_left - 58
                print("[" + ts + "] Aguardando " + str(round(wait, 1)) + "s para janela 50s.")
                time.sleep(wait)
                secs_left = seconds_to_next_candle()
            if secs_left < 5:
                print("[" + ts + "] Janela perdida, pulando.")
                time.sleep(CHECK_INTERVAL)
                continue

            # ---- Envia sinal -----------------------------------------------
            ts2 = now_brt().strftime("%H:%M:%S BRT")
            ok  = send_telegram(msg_signal(direction))
            if ok:
                last_signal_time     = time.time()
                session_signals[idx] += 1
                print("[" + ts2 + "] [" + name + "] " + direction + " (" + pattern + ") #" + str(session_signals[idx]))
            else:
                print("[" + ts2 + "] Falha ao enviar sinal.")
                time.sleep(CHECK_INTERVAL)
                continue

            # ---- Segundo aviso 30s depois ----------------------------------
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
    print("Bot GBP/USD OTC Price Action v2 iniciado!")
    print("Ativo: " + SYMBOL)
    print("Janelas BRT: 09-11 (Londres) | 14-16 (Londres+NY) | 21-23:59 (Noturna)")
    print("Max sinais: " + str(MAX_SIGNALS) + " por sessao | Cooldown: " + str(COOLDOWN_SECS) + "s")
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
