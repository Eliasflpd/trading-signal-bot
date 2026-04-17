import os
import time
import math
import json
import requests
import threading
import websocket
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

LABEL = "GBP/USD OTC"

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
CHECK_INTERVAL = 30    # intervalo de checagem (segundos)

# ---------------------------------------------------------------------------
# Estado global
# ---------------------------------------------------------------------------

# Estado de sessões
session_signals  = [0, 0, 0]
session_notified = [False, False, False]
session_ended    = [False, False, False]
last_signal_time = 0.0
bot_start_time   = time.time()
last_update_id   = 0

# Estado de perdas/ganhos
consecutive_losses = 0
session_wins  = 0
session_losses = 0
stop_until = 0.0   # timestamp até quando o bot está pausado por perdas

# Buffer de candles M1 e M5 (dados do WebSocket)
m1_candles = []   # lista de dicts: {open, high, low, close, volume, is_closed}
m5_candles = []   # lista de dicts: {open, high, low, close, volume, is_closed}

# Lock para thread-safe
data_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Utilitários de tempo
# ---------------------------------------------------------------------------

def now_brt():
    return datetime.now(timezone.utc) + BRT_OFFSET


def active_session():
    t = now_brt()
    for i, (sh, sm, eh, em, name) in enumerate(SESSIONS):
        start = t.replace(hour=sh, minute=sm, second=0,  microsecond=0)
        end   = t.replace(hour=eh, minute=em, second=59, microsecond=999999)
        if start <= t <= end:
            return i, SESSIONS[i]
    return None, None


# ---------------------------------------------------------------------------
# WebSocket Binance — M1
# ---------------------------------------------------------------------------

def on_m1_message(ws, message):
    global m1_candles
    try:
        data = json.loads(message)
        k = data.get("k", {})
        candle = {
            "open":       float(k.get("o", 0)),
            "high":       float(k.get("h", 0)),
            "low":        float(k.get("l", 0)),
            "close":      float(k.get("c", 0)),
            "volume":     float(k.get("v", 0)),
            "is_closed":  k.get("x", False),
        }
        with data_lock:
            # Mantém buffer de 20 candles fechados + 1 em formação
            if candle["is_closed"]:
                m1_candles.append(candle)
                if len(m1_candles) > 25:
                    m1_candles = m1_candles[-25:]
            else:
                # Atualiza ou adiciona candle em formação
                if m1_candles and not m1_candles[-1]["is_closed"]:
                    m1_candles[-1] = candle
                else:
                    m1_candles.append(candle)
    except Exception as e:
        print("[WS M1] Erro: " + str(e))


def on_m1_error(ws, error):
    print("[WS M1] Erro: " + str(error))


def on_m1_close(ws, close_status_code, close_msg):
    print("[WS M1] Conexão fechada. Reconectando...")
    time.sleep(3)
    start_m1_ws()


def on_m1_open(ws):
    print("[WS M1] Conectado ao stream GBPUSDT@kline_1m")


def start_m1_ws():
    url = "wss://stream.binance.com:9443/ws/gbpusdt@kline_1m"
    ws = websocket.WebSocketApp(
        url,
        on_message=on_m1_message,
        on_error=on_m1_error,
        on_close=on_m1_close,
        on_open=on_m1_open,
    )
    ws.run_forever(ping_interval=30, ping_timeout=10)


# ---------------------------------------------------------------------------
# WebSocket Binance — M5
# ---------------------------------------------------------------------------

def on_m5_message(ws, message):
    global m5_candles
    try:
        data = json.loads(message)
        k = data.get("k", {})
        candle = {
            "open":       float(k.get("o", 0)),
            "high":       float(k.get("h", 0)),
            "low":        float(k.get("l", 0)),
            "close":      float(k.get("c", 0)),
            "volume":     float(k.get("v", 0)),
            "is_closed":  k.get("x", False),
        }
        with data_lock:
            if candle["is_closed"]:
                m5_candles.append(candle)
                if len(m5_candles) > 30:
                    m5_candles = m5_candles[-30:]
            else:
                if m5_candles and not m5_candles[-1]["is_closed"]:
                    m5_candles[-1] = candle
                else:
                    m5_candles.append(candle)
    except Exception as e:
        print("[WS M5] Erro: " + str(e))


def on_m5_error(ws, error):
    print("[WS M5] Erro: " + str(error))


def on_m5_close(ws, close_status_code, close_msg):
    print("[WS M5] Conexão fechada. Reconectando...")
    time.sleep(3)
    start_m5_ws()


def on_m5_open(ws):
    print("[WS M5] Conectado ao stream GBPUSDT@kline_5m")


def start_m5_ws():
    url = "wss://stream.binance.com:9443/ws/gbpusdt@kline_5m"
    ws = websocket.WebSocketApp(
        url,
        on_message=on_m5_message,
        on_error=on_m5_error,
        on_close=on_m5_close,
        on_open=on_m5_open,
    )
    ws.run_forever(ping_interval=30, ping_timeout=10)


# ---------------------------------------------------------------------------
# Análise de Velas — Price Action
# ---------------------------------------------------------------------------

def body_size(o, c):
    return abs(c - o)

def lower_shadow(o, c, l):
    return min(o, c) - l

def upper_shadow(o, c, h):
    return h - max(o, c)

def candle_range(h, l):
    return h - l if h != l else 0.00001

def is_bullish(o, c):
    return c > o

def is_bearish(o, c):
    return c < o

def is_doji(o, c, h, l):
    body = body_size(o, c)
    rng  = candle_range(h, l)
    return body < 0.1 * rng


def detect_pattern(candles):
    """Analisa os últimos 5 candles e retorna (direção, padrão) ou (None, None)."""
    if len(candles) < 5:
        return None, None

    last5 = candles[-5:]
    o = [c["open"]  for c in last5]
    h = [c["high"]  for c in last5]
    l = [c["low"]   for c in last5]
    c = [c["close"] for c in last5]

    o4, h4, l4, c4 = o[4], h[4], l[4], c[4]
    body4    = body_size(o4, c4)
    lshadow4 = lower_shadow(o4, c4, l4)
    ushadow4 = upper_shadow(o4, c4, h4)
    rng4     = candle_range(h4, l4)

    two_bearish = is_bearish(o[2], c[2]) and is_bearish(o[3], c[3])
    two_bullish = is_bullish(o[2], c[2]) and is_bullish(o[3], c[3])
    doji_mid    = is_doji(o[3], c[3], h[3], l[3])

    # --- COMPRA ---
    if (body4 > 0
            and lshadow4 >= 2 * body4
            and ushadow4 <= 0.3 * rng4
            and two_bearish):
        return "CALL", "Martelo"

    if (is_bullish(o[4], c[4])
            and is_bearish(o[3], c[3])
            and c[4] > o[3]
            and o[4] < c[3]):
        return "CALL", "Engolfo de Alta"

    big_bear_2 = is_bearish(o[2], c[2]) and body_size(o[2], c[2]) > 0.5 * candle_range(h[2], l[2])
    if (big_bear_2
            and doji_mid
            and is_bullish(o[4], c[4])
            and c[4] > (o[3] + c[3]) / 2):
        return "CALL", "Estrela da Manhã"

    if (two_bearish
            and is_bullish(o[4], c[4])
            and body4 > body_size(o[3], c[3])):
        return "CALL", "Harami de Alta"

    # --- VENDA ---
    if (body4 > 0
            and ushadow4 >= 2 * body4
            and lshadow4 <= 0.3 * rng4
            and two_bullish):
        return "PUT", "Estrela Cadente"

    if (is_bearish(o[4], c[4])
            and is_bullish(o[3], c[3])
            and c[4] < o[3]
            and o[4] > c[3]):
        return "PUT", "Engolfo de Baixa"

    big_bull_2 = is_bullish(o[2], c[2]) and body_size(o[2], c[2]) > 0.5 * candle_range(h[2], l[2])
    if (big_bull_2
            and doji_mid
            and is_bearish(o[4], c[4])
            and c[4] < (o[3] + c[3]) / 2):
        return "PUT", "Estrela da Tarde"

    if (two_bullish
            and is_bearish(o[4], c[4])
            and body4 > body_size(o[3], c[3])):
        return "PUT", "Harami de Baixa"

    return None, None


# ---------------------------------------------------------------------------
# Mudança 2 — Filtro de Volume
# ---------------------------------------------------------------------------

def volume_is_strong(candles):
    """Retorna True se o volume da última vela > média dos 10 anteriores."""
    if len(candles) < 11:
        return False  # dados insuficientes, aceita por padrão
    vols = [c["volume"] for c in candles[-11:-1]]  # últimas 10 fechadas
    if not vols:
        return False
    avg_vol = sum(vols) / len(vols)
    current_vol = candles[-1]["volume"]
    return current_vol > avg_vol


# ---------------------------------------------------------------------------
# Mudança 3 — Confirmação Multi-Timeframe M5
# ---------------------------------------------------------------------------

def calc_ema(values, period):
    """Calcula EMA de uma lista de valores."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def m5_trend():
    """Retorna 'UP', 'DOWN' ou None baseado em EMA9 vs EMA21 no M5."""
    with data_lock:
        closed = [c for c in m5_candles if c["is_closed"]]
    if len(closed) < 21:
        return None  # dados insuficientes — não bloqueia
    closes = [c["close"] for c in closed]
    ema9  = calc_ema(closes[-30:], 9)
    ema21 = calc_ema(closes[-30:], 21)
    if ema9 is None or ema21 is None:
        return None
    if ema9 > ema21:
        return "UP"
    elif ema9 < ema21:
        return "DOWN"
    return None


# ---------------------------------------------------------------------------
# Mudança 4 — Filtro de Notícias Econômicas
# ---------------------------------------------------------------------------

NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_news_cache = None
_news_cache_time = 0

def get_news():
    global _news_cache, _news_cache_time
    now = time.time()
    if _news_cache is not None and now - _news_cache_time < 1800:
        return _news_cache
    try:
        resp = requests.get(NEWS_URL, timeout=10)
        resp.raise_for_status()
        _news_cache = resp.json()
        _news_cache_time = now
        return _news_cache
    except Exception as e:
        print("[News] Falha ao buscar notícias: " + str(e))
        return _news_cache or []


def check_news_block():
    """Retorna (bloqueado, minutos_para_noticia) se houver notícia de alto impacto em 30 min."""
    try:
        news = get_news()
        now_utc = datetime.now(timezone.utc)
        for item in news:
            impact = str(item.get("impact", "")).lower()
            currency = str(item.get("currency", ""))
            if impact not in ("high", "red"):
                continue
            if currency not in ("USD", "GBP"):
                continue
            # Parseia data/hora da notícia (formato: "01-16-2025 08:30am")
            try:
                dt_str = item.get("date", "") + " " + item.get("time", "")
                dt_str = dt_str.strip()
                news_dt = datetime.strptime(dt_str, "%m-%d-%Y %I:%M%p")
                news_dt = news_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            diff = (news_dt - now_utc).total_seconds()
            if 0 <= diff <= 1800:  # próximos 30 minutos
                mins = int(diff // 60)
                return_time = (now_utc + timedelta(seconds=diff + 1800)).strftime("%H:%M")
                return True, mins, return_time
    except Exception as e:
        print("[News] Erro ao verificar: " + str(e))
    return False, 0, ""


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text, chat_id=None):
    if not TELEGRAM_BOT_TOKEN:
        return False
    cid = chat_id or TELEGRAM_CHAT_ID
    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
    payload = {"chat_id": cid, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception:
        return False

def send_to(chat_id, text):
    send_telegram(text, chat_id)


def get_updates(offset=0):
    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/getUpdates"
    params = {"timeout": 5, "offset": offset, "allowed_updates": ["message"]}
    try:
        resp = requests.get(url, params=params, timeout=10)
        return resp.json().get("result", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Mensagens
# ---------------------------------------------------------------------------

def msg_signal(direction, vol_strong, trend):
    vol_icon  = "Alto ✅" if vol_strong else "Baixo ⚠️"
    if direction == "CALL":
        trend_icon = "Alta ✅" if trend == "UP" else ("Baixa ⚠️" if trend == "DOWN" else "—")
        return (
            "\U0001f7e2 <b>COMPRE — GBP/USD OTC</b>\n"
            "⏱ Tempo: 1 minuto\n"
            "📊 Volume: " + vol_icon + " | M5: " + trend_icon + "\n"
            "➡️ Clique no botão VERDE"
        )
    else:
        trend_icon = "Baixa ✅" if trend == "DOWN" else ("Alta ⚠️" if trend == "UP" else "—")
        return (
            "\U0001f534 <b>VENDA — GBP/USD OTC</b>\n"
            "⏱ Tempo: 1 minuto\n"
            "📊 Volume: " + vol_icon + " | M5: " + trend_icon + "\n"
            "➡️ Clique no botão VERMELHO"
        )


def msg_warning():
    return (
        "⚡ 20 segundos! Clique logo!\n"
        "\U0001f7e2 VERDE = COMPRE\n"
        "\U0001f534 VERMELHO = VENDA"
    )


def msg_session_start(name, sh, sm, eh, em):
    return (
        "\U0001f7e2 <b>Sessão " + name + " iniciada!</b>\n"
        + "Monitorando GBP/USD OTC\n"
        + "Até " + str(eh).zfill(2) + ":" + str(em).zfill(2) + " • Máx. " + str(MAX_SIGNALS) + " sinais"
    )


def msg_session_end(name):
    return (
        "\U0001f534 <b>Sessão " + name + " encerrada.</b>\n"
        + "Até a próxima sessão!"
    )


# ---------------------------------------------------------------------------
# Mudança 5 — Stop após 3 perdas
# ---------------------------------------------------------------------------

def record_loss():
    global consecutive_losses, session_losses, stop_until
    session_losses += 1
    consecutive_losses += 1
    if consecutive_losses >= 3:
        stop_until = time.time() + 3600  # pausa por 1 hora
        resume_dt = datetime.fromtimestamp(stop_until, tz=timezone.utc) + BRT_OFFSET
        return True, resume_dt.strftime("%H:%M")
    return False, ""


def record_win():
    global consecutive_losses, session_wins
    session_wins += 1
    consecutive_losses = 0


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

def handle_command(text, chat_id):
    global consecutive_losses, session_wins, session_losses, stop_until
    ts   = now_brt().strftime("%H:%M:%S BRT")
    text = text.strip().lower().split("@")[0]
    print("[" + ts + "] CMD: " + text)

    if text == "/start":
        msg = (
            "\U0001f44b Olá! Sou o <b>Bot GBP/USD OTC BOT-N2</b>.\n\n"
            "<b>Estratégia:</b> Price Action + Volume + MTF\n"
            "<b>Ativo:</b> GBP/USD OTC\n\n"
            "<b>Janelas diárias (BRT):</b>\n"
            "\U0001f55b 09:00 – 11:00 — Londres\n"
            "\U0001f55d 14:00 – 16:00 — Londres + NY\n"
            "\U0001f315 21:00 – 23:59 — Noturna\n\n"
            "<b>Máx.:</b> 6 sinais por sessão\n"
            "/status — estado atual\n"
            "/perdi — registrar perda\n"
            "/ganhei — registrar vitória\n"
            "/placar — ver resultado da sessão"
        )
        send_to(chat_id, msg)

    elif text == "/status":
        brt_now   = now_brt()
        idx, sess = active_session()
        if idx is not None:
            sh, sm, eh, em, name = sess
            sessao = "Ativa ✅ (" + name + ") • " + str(session_signals[idx]) + "/" + str(MAX_SIGNALS) + " sinais"
        else:
            sessao = "Inativa ⏸"
        uptime = timedelta(seconds=int(time.time() - bot_start_time))
        h = int(uptime.total_seconds() // 3600)
        m = int((uptime.total_seconds() % 3600) // 60)
        remaining_cd = max(0, int(COOLDOWN_SECS - (time.time() - last_signal_time)))
        paused = ""
        if time.time() < stop_until:
            resume = datetime.fromtimestamp(stop_until, tz=timezone.utc) + BRT_OFFSET
            paused = "\n🛑 Pausado até " + resume.strftime("%H:%M") + " (3 perdas)"
        msg = (
            "<b>Status BOT-N2</b>\n"
            "Hora BRT: " + brt_now.strftime("%H:%M:%S") + "\n"
            "Sessão: " + sessao + "\n"
            "Cooldown: " + (str(remaining_cd) + "s" if remaining_cd > 0 else "pronto") + "\n"
            "Perdas seguidas: " + str(consecutive_losses) + "\n"
            "Uptime: " + str(h) + "h " + str(m) + "m"
            + paused
        )
        send_to(chat_id, msg)

    elif text == "/perdi":
        triggered, resume_time = record_loss()
        if triggered:
            msg = (
                "🛑 3 perdas seguidas detectadas.\n"
                "Pausando por 60 minutos.\n"
                "Próxima sessão: " + resume_time
            )
        else:
            msg = "📉 Perda registrada. Perdas seguidas: " + str(consecutive_losses) + "/3"
        send_to(chat_id, msg)

    elif text == "/ganhei":
        record_win()
        msg = "📈 Vitória registrada! Perdas seguidas zeradas. ✅"
        send_to(chat_id, msg)

    elif text == "/placar":
        total = session_wins + session_losses
        taxa  = int(session_wins / total * 100) if total > 0 else 0
        msg = (
            "📊 <b>Placar da sessão</b>\n"
            "✅ Vitórias: " + str(session_wins) + "\n"
            "❌ Derrotas: " + str(session_losses) + "\n"
            "📈 Taxa: " + str(taxa) + "%"
        )
        send_to(chat_id, msg)


# ---------------------------------------------------------------------------
# Polling (comandos Telegram)
# ---------------------------------------------------------------------------

def polling_loop():
    global last_update_id
    print("Polling iniciado.")
    while True:
        try:
            updates = get_updates(offset=last_update_id + 1)
            for upd in updates:
                last_update_id = upd["update_id"]
                msg = upd.get("message", {})
                txt = msg.get("text", "")
                cid = str(msg.get("chat", {}).get("id", ""))
                if txt.startswith("/"):
                    handle_command(txt, cid)
        except Exception as e:
            print("[Polling] Erro: " + str(e))
        time.sleep(1)


# ---------------------------------------------------------------------------
# Loop principal de sinais
# ---------------------------------------------------------------------------

def signal_loop():
    global last_signal_time
    global session_signals, session_notified, session_ended

    # Espera o WebSocket popular dados (mínimo 5 candles M1 e 21 candles M5)
    print("Aguardando dados do WebSocket...")
    for _ in range(60):
        with data_lock:
            m1_ok = len([c for c in m1_candles if c["is_closed"]]) >= 5
            m5_ok = len([c for c in m5_candles if c["is_closed"]]) >= 5
        if m1_ok and m5_ok:
            print("Dados prontos. Iniciando loop de sinais.")
            break
        time.sleep(2)
    else:
        print("Timeout aguardando dados WS. Prosseguindo mesmo assim.")

    print("Loop de sinais iniciado (BOT-N2).")

    while True:
        try:
            ts  = now_brt().strftime("%H:%M:%S BRT")
            now = time.time()

            # ---- Verifica pausa por 3 perdas --------------------------------
            if now < stop_until:
                resume = datetime.fromtimestamp(stop_until, tz=timezone.utc) + BRT_OFFSET
                print("[" + ts + "] Bot pausado até " + resume.strftime("%H:%M") + " (3 perdas).")
                time.sleep(CHECK_INTERVAL)
                continue

            idx, sess = active_session()

            # ---- Gerencia avisos de início / fim ----------------------------
            for i, (sh, sm, eh, em, name) in enumerate(SESSIONS):
                t     = now_brt()
                start = t.replace(hour=sh, minute=sm, second=0,  microsecond=0)
                end   = t.replace(hour=eh, minute=em, second=59, microsecond=999999)
                is_on = (start <= t <= end)

                if is_on and not session_notified[i]:
                    session_notified[i] = True
                    session_ended[i]    = False
                    session_signals[i]  = 0
                    send_telegram(msg_session_start(name, sh, sm, eh, em))
                    print("[" + ts + "] Sessão " + name + " aberta.")

                if not is_on and session_notified[i] and not session_ended[i]:
                    session_ended[i] = True
                    send_telegram(msg_session_end(name))
                    print("[" + ts + "] Sessão " + name + " encerrada.")

            if idx is None:
                time.sleep(CHECK_INTERVAL)
                continue

            sh, sm, eh, em, name = sess

            # ---- Limite de sinais ------------------------------------------
            if session_signals[idx] >= MAX_SIGNALS:
                print("[" + ts + "] Sessão " + name + ": limite atingido.")
                time.sleep(CHECK_INTERVAL)
                continue

            # ---- Cooldown --------------------------------------------------
            if now - last_signal_time < COOLDOWN_SECS:
                remaining = int(COOLDOWN_SECS - (now - last_signal_time))
                print("[" + ts + "] Cooldown: " + str(remaining) + "s restantes.")
                time.sleep(CHECK_INTERVAL)
                continue

            # ---- Mudança 4: Filtro de Notícias -----------------------------
            blocked, mins, return_time = check_news_block()
            if blocked:
                msg_news = (
                    "⚠️ Notícia importante em " + str(mins) + " minutos!\n"
                    "Pausando sinais por segurança.\n"
                    "Retorno em: " + return_time
                )
                send_telegram(msg_news)
                print("[" + ts + "] Notícia em " + str(mins) + "min. Pausando.")
                time.sleep(max(60, mins * 60))
                continue

            # ---- Busca candles do WebSocket --------------------------------
            with data_lock:
                m1_snap = list(m1_candles)

            if len(m1_snap) < 5:
                print("[" + ts + "] Aguardando candles M1...")
                time.sleep(CHECK_INTERVAL)
                continue

            # ---- Detecta padrão de Price Action ----------------------------
            # Usa apenas candles fechados + vela em formação (última)
            closed_m1 = [c for c in m1_snap if c["is_closed"]]
            all_m1    = closed_m1 + ([m1_snap[-1]] if not m1_snap[-1]["is_closed"] else [])

            direction, pattern = detect_pattern(all_m1)
            if direction is None:
                print("[" + ts + "] Nenhum padrão detectado.")
                time.sleep(CHECK_INTERVAL)
                continue

            # ---- Mudança 2: Filtro de Volume -------------------------------
            vol_strong = volume_is_strong(all_m1)
            if not vol_strong:
                print("[" + ts + "] Volume fraco. Ignorando padrão: " + str(pattern))
                time.sleep(CHECK_INTERVAL)
                continue

            # ---- Mudança 3: Confirmação M5 ---------------------------------
            trend = m5_trend()
            if trend is not None:
                if direction == "CALL" and trend != "UP":
                    print("[" + ts + "] M5 em baixa. Ignorando sinal CALL (" + str(pattern) + ").")
                    time.sleep(CHECK_INTERVAL)
                    continue
                if direction == "PUT" and trend != "DOWN":
                    print("[" + ts + "] M5 em alta. Ignorando sinal PUT (" + str(pattern) + ").")
                    time.sleep(CHECK_INTERVAL)
                    continue

            # ---- Envia sinal -----------------------------------------------
            ts2 = now_brt().strftime("%H:%M:%S BRT")
            ok  = send_telegram(msg_signal(direction, vol_strong, trend))
            if ok:
                last_signal_time     = time.time()
                session_signals[idx] += 1
                print("[" + ts2 + "] [" + name + "] " + direction + " (" + str(pattern) + ") #" + str(session_signals[idx]))
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
    print("Bot GBP/USD OTC Price Action BOT-N2 iniciado!")
    print("Janelas BRT: 09-11 (Londres) | 14-16 (Londres+NY) | 21-23:59 (Noturna)")
    print("Features: WebSocket + Volume + MTF M5 + Notícias + Stop 3 Perdas")

    # Inicia WebSocket M1 em thread separada
    t_m1 = threading.Thread(target=start_m1_ws, daemon=True)
    t_m1.start()

    # Inicia WebSocket M5 em thread separada
    t_m5 = threading.Thread(target=start_m5_ws, daemon=True)
    t_m5.start()

    # Inicia polling de comandos em thread separada
    t_poll = threading.Thread(target=polling_loop, daemon=True)
    t_poll.start()

    # Loop principal
    signal_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Bot encerrado.")
    except Exception as e:
        print("Erro critico: " + str(e))
        time.sleep(10)
