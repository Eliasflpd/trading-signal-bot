import os
import time
import math
import json
import requests
import threading
import websocket
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
import anthropic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
SUPABASE_URL       = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY       = os.environ.get("SUPABASE_KEY", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

LABEL = "GBP/USD OTC"
BRT_OFFSET = timedelta(hours=-3)

SESSIONS = [
    (9,  0, 11,  0, "Londres"),
    (14, 0, 16,  0, "Londres+NY"),
    (21, 0, 23, 59, "Noturna"),
]

COOLDOWN_SECS  = 300
MAX_SIGNALS    = 6
CHECK_INTERVAL = 30

# Anti-Martingale
BASE_BET_DEMO  = 1.0   # $1 demo
BASE_BET_REAL  = 10.0  # $10 real
MAX_LOSSES_AM  = 6     # Para apos 6 perdas seguidas

# ---------------------------------------------------------------------------
# Estado global
# ---------------------------------------------------------------------------

session_signals  = [0, 0, 0]
session_notified = [False, False, False]
session_ended    = [False, False, False]
last_signal_time = 0.0
bot_start_time   = time.time()
last_update_id   = 0

consecutive_losses = 0
session_wins  = 0
session_losses = 0
stop_until = 0.0

# Anti-Martingale
current_bet = BASE_BET_DEMO  # Valor atual da entrada

last_signal_id = None   # UUID do ÃÂºltimo sinal inserido no Supabase

m1_candles = []
m5_candles = []
data_lock = threading.Lock()

# Supabase client (inicializado no main)
supa: Client = None

# ---------------------------------------------------------------------------
# Supabase Ã¢ÂÂ Journaling
# ---------------------------------------------------------------------------

def init_supabase():
    global supa
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            supa = create_client(SUPABASE_URL, SUPABASE_KEY)
            print("[Supabase] Cliente inicializado.")
        except Exception as e:
            print("[Supabase] Erro ao inicializar: " + str(e))
    else:
        print("[Supabase] URL/KEY nÃÂ£o configurados. Journaling desativado.")


def log_signal(ativo, direcao, padrao, confianca, volume_confirmado, m5_confirmado, sessao, validado_ia=True):
    global last_signal_id
    if supa is None:
        return
    try:
        data = {
            "ativo": ativo,
            "direcao": direcao,
            "padrao": padrao,
            "confianca": confianca,
            "volume_confirmado": volume_confirmado,
            "m5_confirmado": m5_confirmado,
            "sessao": sessao,
            "validado_ia": validado_ia,
            "resultado": "pendente",
            "registrado_em": datetime.utcnow().isoformat(),
        }
        resp = supa.table("trading_signals").insert(data).execute()
        if resp.data:
            last_signal_id = resp.data[0]["id"]
            print("[Supabase] Sinal registrado: " + last_signal_id)
    except Exception as e:
        print("[Supabase] Erro ao registrar sinal: " + str(e))


def update_last_result(resultado):
    if supa is None or last_signal_id is None:
        return
    try:
        supa.table("trading_signals").update({"resultado": resultado}).eq("id", last_signal_id).execute()
        print("[Supabase] Resultado atualizado: " + resultado)
    except Exception as e:
        print("[Supabase] Erro ao atualizar resultado: " + str(e))


def get_weekly_stats():
    if supa is None:
        return None
    try:
        week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
        resp = supa.table("trading_signals").select("*").gte("created_at", week_ago).neq("resultado", "pendente").execute()
        rows = resp.data or []
        total = len(rows)
        if total == 0:
            return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0, "best_pattern": "-", "best_session": "-"}
        wins   = sum(1 for r in rows if r["resultado"] == "WIN")
        losses = sum(1 for r in rows if r["resultado"] == "LOSS")
        win_rate = int(wins / total * 100) if total > 0 else 0

        # Melhor padrÃÂ£o
        pattern_stats = {}
        for r in rows:
            p = r.get("padrao") or "N/A"
            if p not in pattern_stats:
                pattern_stats[p] = {"wins": 0, "total": 0}
            pattern_stats[p]["total"] += 1
            if r["resultado"] == "WIN":
                pattern_stats[p]["wins"] += 1
        best_pattern = max(pattern_stats, key=lambda k: pattern_stats[k]["wins"] / pattern_stats[k]["total"] if pattern_stats[k]["total"] > 0 else 0)
        best_pattern_rate = int(pattern_stats[best_pattern]["wins"] / pattern_stats[best_pattern]["total"] * 100) if pattern_stats[best_pattern]["total"] > 0 else 0

        # Melhor sessÃÂ£o
        session_stats = {}
        for r in rows:
            s = r.get("sessao") or "N/A"
            if s not in session_stats:
                session_stats[s] = {"wins": 0, "total": 0}
            session_stats[s]["total"] += 1
            if r["resultado"] == "WIN":
                session_stats[s]["wins"] += 1
        best_session = max(session_stats, key=lambda k: session_stats[k]["wins"] / session_stats[k]["total"] if session_stats[k]["total"] > 0 else 0)
        best_session_rate = int(session_stats[best_session]["wins"] / session_stats[best_session]["total"] * 100) if session_stats[best_session]["total"] > 0 else 0

        return {
            "total": total, "wins": wins, "losses": losses, "win_rate": win_rate,
            "best_pattern": best_pattern + " (" + str(best_pattern_rate) + "%)",
            "best_session": best_session + " (" + str(best_session_rate) + "%)",
        }
    except Exception as e:
        print("[Supabase] Erro ao buscar stats: " + str(e))
        return None


def get_daily_stats():
    if supa is None:
        return None
    try:
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        resp = supa.table("trading_signals").select("*").gte("created_at", today).execute()
        rows = resp.data or []
        total  = len(rows)
        wins   = sum(1 for r in rows if r["resultado"] == "WIN")
        losses = sum(1 for r in rows if r["resultado"] == "LOSS")
        win_rate = int(wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

        pattern_stats = {}
        for r in rows:
            if r["resultado"] not in ("WIN", "LOSS"):
                continue
            p = r.get("padrao") or "N/A"
            if p not in pattern_stats:
                pattern_stats[p] = {"wins": 0, "total": 0}
            pattern_stats[p]["total"] += 1
            if r["resultado"] == "WIN":
                pattern_stats[p]["wins"] += 1
        best_pattern = "-"
        if pattern_stats:
            best_pattern = max(pattern_stats, key=lambda k: pattern_stats[k]["wins"] / pattern_stats[k]["total"] if pattern_stats[k]["total"] > 0 else 0)

        return {"total": total, "wins": wins, "losses": losses, "win_rate": win_rate, "best_pattern": best_pattern}
    except Exception as e:
        print("[Supabase] Erro stats diÃÂ¡rias: " + str(e))
        return None

# ---------------------------------------------------------------------------
# UtilitÃÂ¡rios de tempo
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
# WebSocket Binance Ã¢ÂÂ M1
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
            if candle["is_closed"]:
                m1_candles.append(candle)
                if len(m1_candles) > 25:
                    m1_candles = m1_candles[-25:]
            else:
                if m1_candles and not m1_candles[-1]["is_closed"]:
                    m1_candles[-1] = candle
                else:
                    m1_candles.append(candle)
    except Exception as e:
        print("[WS M1] Erro: " + str(e))

def on_m1_error(ws, error): print("[WS M1] Erro: " + str(error))
def on_m1_close(ws, code, msg):
    print("[WS M1] Fechado. Reconectando...")
    time.sleep(3)
    start_m1_ws()
def on_m1_open(ws): print("[WS M1] Conectado.")

def start_m1_ws():
    ws = websocket.WebSocketApp(
        "wss://stream.binance.com:9443/ws/gbpusdt@kline_1m",
        on_message=on_m1_message, on_error=on_m1_error,
        on_close=on_m1_close, on_open=on_m1_open,
    )
    ws.run_forever(ping_interval=30, ping_timeout=10)


# ---------------------------------------------------------------------------
# WebSocket Binance Ã¢ÂÂ M5
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

def on_m5_error(ws, error): print("[WS M5] Erro: " + str(error))
def on_m5_close(ws, code, msg):
    print("[WS M5] Fechado. Reconectando...")
    time.sleep(3)
    start_m5_ws()
def on_m5_open(ws): print("[WS M5] Conectado.")

def start_m5_ws():
    ws = websocket.WebSocketApp(
        "wss://stream.binance.com:9443/ws/gbpusdt@kline_5m",
        on_message=on_m5_message, on_error=on_m5_error,
        on_close=on_m5_close, on_open=on_m5_open,
    )
    ws.run_forever(ping_interval=30, ping_timeout=10)


# ---------------------------------------------------------------------------
# Price Action
# ---------------------------------------------------------------------------

def body_size(o, c): return abs(c - o)
def lower_shadow(o, c, l): return min(o, c) - l
def upper_shadow(o, c, h): return h - max(o, c)
def candle_range(h, l): return h - l if h != l else 0.00001
def is_bullish(o, c): return c > o
def is_bearish(o, c): return c < o
def is_doji(o, c, h, l):
    return body_size(o, c) < 0.1 * candle_range(h, l)


def detect_pattern(candles):
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
    if body4 > 0 and lshadow4 >= 2*body4 and ushadow4 <= 0.3*rng4 and two_bearish:
        return "CALL", "Martelo"
    if is_bullish(o[4],c[4]) and is_bearish(o[3],c[3]) and c[4]>o[3] and o[4]<c[3]:
        return "CALL", "Engolfo de Alta"
    big_bear_2 = is_bearish(o[2],c[2]) and body_size(o[2],c[2])>0.5*candle_range(h[2],l[2])
    if big_bear_2 and doji_mid and is_bullish(o[4],c[4]) and c[4]>(o[3]+c[3])/2:
        return "CALL", "Estrela da Manha"
    if two_bearish and is_bullish(o[4],c[4]) and body4>body_size(o[3],c[3]):
        return "CALL", "Harami de Alta"
    if body4>0 and ushadow4>=2*body4 and lshadow4<=0.3*rng4 and two_bullish:
        return "PUT", "Estrela Cadente"
    if is_bearish(o[4],c[4]) and is_bullish(o[3],c[3]) and c[4]<o[3] and o[4]>c[3]:
        return "PUT", "Engolfo de Baixa"
    big_bull_2 = is_bullish(o[2],c[2]) and body_size(o[2],c[2])>0.5*candle_range(h[2],l[2])
    if big_bull_2 and doji_mid and is_bearish(o[4],c[4]) and c[4]<(o[3]+c[3])/2:
        return "PUT", "Estrela da Tarde"
    if two_bullish and is_bearish(o[4],c[4]) and body4>body_size(o[3],c[3]):
        return "PUT", "Harami de Baixa"
    return None, None


def volume_is_strong(candles):
    if len(candles) < 11:
        return False
    vols = [c["volume"] for c in candles[-11:-1]]
    avg_vol = sum(vols) / len(vols)
    return candles[-1]["volume"] > avg_vol


def calc_ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def m5_trend():
    with data_lock:
        closed = [c for c in m5_candles if c["is_closed"]]
    if len(closed) < 21:
        return None
    closes = [c["close"] for c in closed]
    ema9  = calc_ema(closes[-30:], 9)
    ema21 = calc_ema(closes[-30:], 21)
    if ema9 is None or ema21 is None:
        return None
    if ema9 > ema21: return "UP"
    elif ema9 < ema21: return "DOWN"
    return None


# ---------------------------------------------------------------------------
# NotÃÂ­cias econÃÂ´micas
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
        print("[News] Falha: " + str(e))
        return _news_cache or []

def check_news_block():
    try:
        news = get_news()
        now_utc = datetime.now(timezone.utc)
        for item in news:
            impact = str(item.get("impact", "")).lower()
            currency = str(item.get("currency", ""))
            if impact not in ("high", "red"): continue
            if currency not in ("USD", "GBP"): continue
            try:
                dt_str = item.get("date", "") + " " + item.get("time", "")
                news_dt = datetime.strptime(dt_str.strip(), "%m-%d-%Y %I:%M%p")
                news_dt = news_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            diff = (news_dt - now_utc).total_seconds()
            if 0 <= diff <= 1800:
                mins = int(diff // 60)
                return_time = (now_utc + timedelta(seconds=diff + 1800)).strftime("%H:%M")
                return True, mins, return_time
    except Exception as e:
        print("[News] Erro: " + str(e))
    return False, 0, ""

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text, chat_id=None):
    if not TELEGRAM_BOT_TOKEN: return False
    cid = chat_id or TELEGRAM_CHAT_ID
    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
    payload = {"chat_id": cid, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception:
        return False

def send_to(chat_id, text): send_telegram(text, chat_id)

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

def msg_signal(direction, vol_strong, trend, ia_confianca=None, ia_risco=None, bet=None):
    vol_icon = "Alto \u2705" if vol_strong else "Baixo \u26a0\ufe0f"
    ia_linha = ""
    if ia_confianca is not None:
        ia_linha = " | \U0001f916 IA: Validado \u2705 | ConfianÃ§a: " + str(ia_confianca) + "%"
        if ia_risco:
            ia_linha += " | Risco: " + ia_risco
    bet_linha = ""
    if bet is not None:
        bet_linha = "\n\U0001f4b0 Entrada: $" + str(bet)
    if direction == "CALL":
        trend_icon = "Alta \u2705" if trend == "UP" else ("Baixa \u26a0\ufe0f" if trend == "DOWN" else "\u2014")
        return (
            "\U0001f7e2 <b>COMPRE \u2014 GBP/USD OTC</b>\n"
            "\u23f1 Tempo: 1 minuto" + ia_linha + "\n"
            "\U0001f4ca Volume: " + vol_icon + " | M5: " + trend_icon + "\n"
            "\u27a1\ufe0f Clique no bot\u00e3o VERDE\n"
            "\u26a1 \xdaLTIMO AVISO \u2014 20 segundos!" + bet_linha
        )
    else:
        trend_icon = "Baixa \u2705" if trend == "DOWN" else ("Alta \u26a0\ufe0f" if trend == "UP" else "\u2014")
        return (
            "\U0001f534 <b>VENDA \u2014 GBP/USD OTC</b>\n"
            "\u23f1 Tempo: 1 minuto" + ia_linha + "\n"
            "\U0001f4ca Volume: " + vol_icon + " | M5: " + trend_icon + "\n"
            "\u27a1\ufe0f Clique no bot\u00e3o VERMELHO\n"
            "\u26a1 \xdaLTIMO AVISO \u2014 20 segundos!" + bet_linha
        )

def msg_warning():
    return "\u26a1 20 segundos! Clique logo!\n\U0001f7e2 VERDE = COMPRE\n\U0001f534 VERMELHO = VENDA"

def msg_session_start(name, sh, sm, eh, em):
    return ("\U0001f7e2 <b>Sess\u00e3o " + name + " iniciada!</b>\n"
            + "Monitorando GBP/USD OTC\n"
            + "At\u00e9 " + str(eh).zfill(2) + ":" + str(em).zfill(2) + " \u2022 M\u00e1x. " + str(MAX_SIGNALS) + " sinais")

def msg_session_end(name):
    return "\U0001f534 <b>Sess\u00e3o " + name + " encerrada.</b>\nAt\u00e9 a pr\u00f3xima sess\u00e3o!"


# ---------------------------------------------------------------------------
# Stop Loss / Record
# ---------------------------------------------------------------------------

def record_loss():
    global consecutive_losses, session_losses, stop_until, current_bet
    session_losses += 1
    consecutive_losses += 1
    update_last_result("LOSS")
    # Anti-Martingale: dobra o valor apos cada perda
    current_bet = current_bet * 2
    # Para apos 6 perdas seguidas (Anti-Martingale limit)
    if consecutive_losses >= MAX_LOSSES_AM:
        stop_until = time.time() + 86400  # Pausa ate amanha
        current_bet = BASE_BET_DEMO       # Reset do valor
        return "6", ""
    # Mantém pausa de 1h apos 3 perdas seguidas (BOT-N2 feature)
    if consecutive_losses >= 3:
        stop_until = time.time() + 3600
        resume_dt = datetime.fromtimestamp(stop_until, tz=timezone.utc) + BRT_OFFSET
        return "3", resume_dt.strftime("%H:%M")
    return False, ""

def record_win():
    global consecutive_losses, session_wins, stop_until, current_bet
    session_wins += 1
    consecutive_losses = 0
    current_bet = BASE_BET_DEMO  # Reset ao valor base
    stop_until = time.time() + 86400  # Para sessao apos vitoria (Anti-Martingale)
    update_last_result("WIN")


# ---------------------------------------------------------------------------
# RelatÃÂ³rio diÃÂ¡rio automÃÂ¡tico (23:59 BRT)
# ---------------------------------------------------------------------------

_daily_report_sent_date = None

def check_daily_report():
    global _daily_report_sent_date
    now = now_brt()
    today_str = now.strftime("%d/%m/%Y")
    if now.hour == 23 and now.minute == 59 and _daily_report_sent_date != today_str:
        stats = get_daily_stats()
        if stats:
            _daily_report_sent_date = today_str
            win_rate = stats["win_rate"]
            msg = (
                "\U0001f4c5 <b>Resumo de hoje \u2014 " + today_str + "</b>\n"
                "Sinais: " + str(stats["total"]) + " | WIN: " + str(stats["wins"]) + " | LOSS: " + str(stats["losses"]) + "\n"
                "Win rate: " + str(win_rate) + "%\n"
                "Padr\u00e3o mais certeiro: " + stats["best_pattern"]
            )
            send_telegram(msg)
            print("[RelatÃÂ³rio] Resumo diÃÂ¡rio enviado.")


# ---------------------------------------------------------------------------
# Comandos Telegram
# ---------------------------------------------------------------------------

def handle_command(text, chat_id):
    global consecutive_losses, session_wins, session_losses, stop_until, current_bet
    ts   = now_brt().strftime("%H:%M:%S BRT")
    text = text.strip().lower().split("@")[0]
    print("[" + ts + "] CMD: " + text)

    if text == "/start":
        msg = (
            "\U0001f44b Ol\u00e1! Sou o <b>Bot GBP/USD OTC BOT-N5</b>.\n\n"
            "<b>Estrat\u00e9gia:</b> Price Action + Volume + MTF + Journaling\n"
            "<b>Ativo:</b> GBP/USD OTC\n\n"
            "<b>Janelas di\u00e1rias (BRT):</b>\n"
            "\U0001f55b 09:00 \u2013 11:00 \u2014 Londres\n"
            "\U0001f55d 14:00 \u2013 16:00 \u2014 Londres + NY\n"
            "\U0001f315 21:00 \u2013 23:59 \u2014 Noturna\n\n"
            "<b>M\u00e1x.:</b> 6 sinais por sess\u00e3o\n"
            "/status | /perdi | /ganhei | /placar | /relatorio\n<b>Admin:</b> /addvip | /removevip | /listvip"
        )
        send_to(chat_id, msg)

    elif text == "/status":
        brt_now   = now_brt()
        idx, sess = active_session()
        if idx is not None:
            sh, sm, eh, em, name = sess
            sessao = "Ativa \u2705 (" + name + ") \u2022 " + str(session_signals[idx]) + "/" + str(MAX_SIGNALS) + " sinais"
        else:
            sessao = "Inativa \u23f8"
        uptime = timedelta(seconds=int(time.time() - bot_start_time))
        h = int(uptime.total_seconds() // 3600)
        m = int((uptime.total_seconds() % 3600) // 60)
        remaining_cd = max(0, int(COOLDOWN_SECS - (time.time() - last_signal_time)))
        paused = ""
        if time.time() < stop_until:
            resume = datetime.fromtimestamp(stop_until, tz=timezone.utc) + BRT_OFFSET
            paused = "\n\U0001f6d1 Pausado at\u00e9 " + resume.strftime("%H:%M") + " (3 perdas)"
        msg = (
            "<b>Status BOT-N6</b>\n"
            "Hora BRT: " + brt_now.strftime("%H:%M:%S") + "\n"
            "Sess\u00e3o: " + sessao + "\n"
            "Cooldown: " + (str(remaining_cd) + "s" if remaining_cd > 0 else "pronto") + "\n"
            "Perdas seguidas: " + str(consecutive_losses) + "/" + str(MAX_LOSSES_AM) + "\n"
            "\U0001f4b0 Pr\u00f3xima entrada: $" + str(current_bet) + "\n"
            "Uptime: " + str(h) + "h " + str(m) + "m"
            + paused
        )
        # BOT-N5: VIP info
        vip_members_list = list_vip_active()
        vip_count = len(vip_members_list)
        vip_lines = "\n\U0001f451 <b>Membros VIP ativos:</b> " + str(vip_count)
        if vip_members_list:
            # Próximas expirações (top 3)
            sorted_vips = sorted(vip_members_list, key=lambda x: x.get("expira_em",""))
            vip_lines += "\n<b>Próximas expirações:</b>"
            for v in sorted_vips[:3]:
                nome_v = v.get("nome") or v["telegram_id"]
                exp_v  = v.get("expira_em","?")[:10] if v.get("expira_em") else "?"
                vip_lines += "\n\u2022 " + nome_v + " — " + exp_v
        send_to(chat_id, msg + vip_lines)

    elif text == "/perdi":
        triggered, resume_time = record_loss()
        if triggered == "6":
            msg = (
                "\U0001f6d1 <b>6 tentativas sem sucesso.</b>\n"
                "Encerrando sess\u00e3o. At\u00e9 amanh\u00e3!\n"
                "\U0001f4b0 Valor resetado para: $" + str(BASE_BET_DEMO)
            )
        elif triggered == "3":
            msg = (
                "\U0001f6d1 3 perdas seguidas detectadas.\n"
                "Pausando por 60 minutos.\n"
                "Pr\u00f3xima sess\u00e3o: " + resume_time + "\n"
                "\U0001f4b0 Pr\u00f3xima entrada: $" + str(current_bet)
            )
        else:
            msg = (
                "\U0001f4c9 Perda registrada. Perdas seguidas: " + str(consecutive_losses) + "/" + str(MAX_LOSSES_AM) + "\n"
                "\U0001f4b0 Pr\u00f3xima entrada: $" + str(current_bet)
            )
        send_to(chat_id, msg)

    elif text == "/ganhei":
        record_win()
        msg = (
            "\U0001f4c8 <b>Vit\u00f3ria registrada!</b> Perdas seguidas zeradas. \u2705\n"
            "\U0001f3c6 Anti-Martingale: sess\u00e3o encerrada com lucro.\n"
            "\U0001f4b0 Valor resetado para: $" + str(BASE_BET_DEMO) + "\n"
            "\U0001f305 Retorno amanh\u00e3!"
        )
        send_to(chat_id, msg)

    elif text == "/placar":
        total = session_wins + session_losses
        taxa  = int(session_wins / total * 100) if total > 0 else 0
        msg = (
            "\U0001f4ca <b>Placar da sess\u00e3o</b>\n"
            "\u2705 Vit\u00f3rias: " + str(session_wins) + "\n"
            "\u274c Derrotas: " + str(session_losses) + "\n"
            "\U0001f4c8 Taxa: " + str(taxa) + "%"
        )
        send_to(chat_id, msg)

    elif text == "/relatorio":
        stats = get_weekly_stats()
        if stats is None or stats["total"] == 0:
            send_to(chat_id, "\U0001f4ca Sem dados suficientes para o relat\u00f3rio desta semana.")
            return
        profit = round(stats["wins"] * 0.8 - stats["losses"] * 1.0, 2)
        profit_str = ("+" if profit >= 0 else "") + str(profit)
        msg = (
            "\U0001f4ca <b>Relat\u00f3rio da Semana</b>\n"
            "Sinais disparados: " + str(stats["total"]) + "\n"
            "\u2705 WIN: " + str(stats["wins"]) + " (" + str(stats["win_rate"]) + "%)\n"
            "\u274c LOSS: " + str(stats["losses"]) + " (" + str(100 - stats["win_rate"]) + "%)\n"
            "\U0001f3c6 Melhor padr\u00e3o: " + stats["best_pattern"] + "\n"
            "\u23f0 Melhor sess\u00e3o: " + stats["best_session"] + "\n"
            "\U0001f4b0 Se operado com $10: " + profit_str
        )
        send_to(chat_id, msg)

    # ----- BOT-N5: Comandos Admin VIP -----
    elif text.startswith("/addvip"):
        if chat_id != TELEGRAM_CHAT_ID:
            send_to(chat_id, "\u26d4 Acesso negado.")
            return
        parts = text.split()
        if len(parts) < 3:
            send_to(chat_id, "Uso: /addvip [telegram_id] [dias]")
            return
        tid  = parts[1]
        try:
            dias = int(parts[2])
        except ValueError:
            send_to(chat_id, "Dias deve ser um numero.")
            return
        ok_vip = add_vip(tid, dias)
        if ok_vip:
            # Mensagem de boas-vindas para o VIP
            send_to(tid,
                "\U0001f389 Bem-vindo ao Sinais IQ Option VIP!\n\n"
                "Você receberá sinais automáticos de\n"
                "GBP/USD OTC diretamente aqui.\n\n"
                "\u2705 Acesso ativo por " + str(dias) + " dias\n"
                "\U0001f4f1 Qualquer dúvida fale com o admin."
            )
            send_to(chat_id, "\u2705 VIP ativado para " + tid + " por " + str(dias) + " dias")
        else:
            send_to(chat_id, "\u274c Erro ao ativar VIP. Verifique o Supabase.")

    elif text.startswith("/removevip"):
        if chat_id != TELEGRAM_CHAT_ID:
            send_to(chat_id, "\u26d4 Acesso negado.")
            return
        parts = text.split()
        if len(parts) < 2:
            send_to(chat_id, "Uso: /removevip [telegram_id]")
            return
        tid = parts[1]
        ok_vip = remove_vip(tid)
        if ok_vip:
            send_to(chat_id, "\u274c VIP removido para " + tid)
        else:
            send_to(chat_id, "Erro ao remover VIP.")

    elif text == "/listvip":
        if chat_id != TELEGRAM_CHAT_ID:
            send_to(chat_id, "\u26d4 Acesso negado.")
            return
        members = list_vip_active()
        if not members:
            send_to(chat_id, "\U0001f4cb Nenhum membro VIP ativo.")
            return
        lines = ["\U0001f4cb <b>VIPs Ativos (" + str(len(members)) + ")</b>"]
        for m in members:
            expira_str = m.get("expira_em", "?")[:10] if m.get("expira_em") else "?"
            nome_str   = m.get("nome") or m["telegram_id"]
            lines.append("\u2022 " + nome_str + " | ID: " + m["telegram_id"] + " | Expira: " + expira_str)
        send_to(chat_id, "\n".join(lines))


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


# ---------------------------------------------------------------------------
# BOT-N4: ValidaÃ§Ã£o via Claude API
# ---------------------------------------------------------------------------

def validate_with_claude(direction, pattern, vol_strong, trend, candles_m1):
    if not ANTHROPIC_API_KEY:
        print("[Claude] ANTHROPIC_API_KEY nÃ£o configurada. Pulando validaÃ§Ã£o IA.")
        return True, 70, "API key nÃ£o configurada", "MÃDIO"
    try:
        brt_now = now_brt().strftime("%H:%M BRT")
        sess_idx, sess_info = active_session()
        sessao_nome = sess_info[4] if sess_info else "Fora de sessÃ£o"
        vol_vs_media = "Alto" if vol_strong else "Baixo"
        trend_label = "Alta" if trend == "UP" else ("Baixa" if trend == "DOWN" else "Lateral")
        closed = [c for c in candles_m1 if c["is_closed"]]
        ultimas5 = closed[-5:] if len(closed) >= 5 else closed
        velas_str = ""
        for i, c in enumerate(ultimas5):
            velas_str += ("  Vela " + str(i+1) + ": O=" + str(round(c["open"],5))
                         + " H=" + str(round(c["high"],5))
                         + " L=" + str(round(c["low"],5))
                         + " C=" + str(round(c["close"],5)) + "\n")
        prompt = (
            "VocÃª Ã© um trader profissional especializado em opÃ§Ãµes binÃ¡rias OTC. "
            "Analise estes dados e decida se o sinal Ã© VÃLIDO ou INVÃLIDO.\n\n"
            "Dados:\n"
            "- Ativo: GBP/USD OTC\n"
            "- DireÃ§Ã£o detectada: " + direction + "\n"
            "- PadrÃ£o de vela: " + str(pattern) + "\n"
            "- Volume vs mÃ©dia: " + vol_vs_media + "\n"
            "- TendÃªncia M5: " + trend_label + "\n"
            "- HorÃ¡rio BRT: " + brt_now + "\n"
            "- SessÃ£o ativa: " + sessao_nome + "\n"
            "- Ãltimas 5 velas (open, high, low, close):\n" + velas_str + "\n"
            "Responda APENAS em JSON vÃ¡lido (sem markdown):\n"
            '{"validar": true, "confianca": 80, "motivo": "explicaÃ§Ã£o", "risco": "BAIXO"}'
        )
        client_ia = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client_ia.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:])
            raw = raw.rsplit("```", 1)[0].strip()
        res       = json.loads(raw)
        valido    = bool(res.get("validar", False))
        confianca = int(res.get("confianca", 0))
        motivo    = str(res.get("motivo", ""))
        risco     = str(res.get("risco", "MÃDIO"))
        print("[Claude] validar=" + str(valido) + " confianÃ§a=" + str(confianca) + "% risco=" + risco)
        return valido, confianca, motivo, risco
    except Exception as e:
        print("[Claude] Erro: " + str(e))
        return True, 65, "Erro na API, sinal liberado", "MÃDIO"



# ---------------------------------------------------------------------------
# BOT-N5: Membros VIP
# ---------------------------------------------------------------------------

def get_vip_members():
    if supa is None:
        return []
    try:
        now_iso = datetime.utcnow().isoformat()
        resp = supa.table("vip_members").select("telegram_id,nome").eq("ativo", True).gt("expira_em", now_iso).execute()
        return resp.data or []
    except Exception as e:
        print("[VIP] Erro ao buscar membros: " + str(e))
        return []


def add_vip(telegram_id, dias, nome=None):
    if supa is None:
        return False
    try:
        expira = (datetime.utcnow() + timedelta(days=dias)).isoformat()
        supa.table("vip_members").upsert({
            "telegram_id": telegram_id,
            "nome": nome or telegram_id,
            "plano": "mensal" if dias <= 31 else ("trimestral" if dias <= 92 else "semestral"),
            "ativo": True,
            "expira_em": expira,
        }).execute()
        return True
    except Exception as e:
        print("[VIP] Erro ao adicionar: " + str(e))
        return False


def remove_vip(telegram_id):
    if supa is None:
        return False
    try:
        supa.table("vip_members").update({"ativo": False}).eq("telegram_id", telegram_id).execute()
        return True
    except Exception as e:
        print("[VIP] Erro ao remover: " + str(e))
        return False


def list_vip_active():
    if supa is None:
        return []
    try:
        now_iso = datetime.utcnow().isoformat()
        resp = supa.table("vip_members").select("*").eq("ativo", True).gt("expira_em", now_iso).execute()
        return resp.data or []
    except Exception as e:
        print("[VIP] Erro ao listar: " + str(e))
        return []


def count_vip_active():
    return len(get_vip_members())


def check_vip_expirations():
    if supa is None:
        return
    try:
        now = datetime.utcnow()
        in3days = (now + timedelta(days=3)).isoformat()
        resp3 = supa.table("vip_members").select("*").eq("ativo", True).lt("expira_em", in3days).gt("expira_em", now.isoformat()).execute()
        for m in (resp3.data or []):
            tid = m["telegram_id"]
            send_to(tid,
                "\u26a0\ufe0f Seu acesso VIP expira em 3 dias!\n"
                "Para renovar fale com o admin."
            )
        resp_exp = supa.table("vip_members").select("*").eq("ativo", True).lte("expira_em", now.isoformat()).execute()
        for m in (resp_exp.data or []):
            tid = m["telegram_id"]
            supa.table("vip_members").update({"ativo": False}).eq("telegram_id", tid).execute()
            send_to(tid,
                "\U0001f534 Seu acesso VIP foi encerrado.\n"
                "Obrigado por usar nossos sinais!\n"
                "Para reativar fale com o admin."
            )
            print("[VIP] Acesso encerrado para " + tid)
    except Exception as e:
        print("[VIP] Erro ao checar expiracoes: " + str(e))


def send_signal_to_vips(signal_text):
    members = get_vip_members()
    sent = 0
    for m in members:
        tid = m["telegram_id"]
        if tid != TELEGRAM_CHAT_ID:
            try:
                send_to(tid, signal_text)
                sent += 1
            except Exception as e:
                print("[VIP] Erro ao enviar para " + tid + ": " + str(e))
    return sent


def signal_loop():
    global last_signal_time, current_bet
    global session_signals, session_notified, session_ended
    last_vip_check = 0.0  # BOT-N5: controle de verificação VIP

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
        print("Timeout WS. Prosseguindo mesmo assim.")

    print("Loop de sinais iniciado (BOT-N6).")

    while True:
        try:
            ts  = now_brt().strftime("%H:%M:%S BRT")
            now = time.time()

            # RelatÃÂ³rio diÃÂ¡rio
            check_daily_report()

            if now < stop_until:
                resume = datetime.fromtimestamp(stop_until, tz=timezone.utc) + BRT_OFFSET
                print("[" + ts + "] Pausado atÃÂ© " + resume.strftime("%H:%M"))
                time.sleep(CHECK_INTERVAL)
                continue

            idx, sess = active_session()

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
                    print("[" + ts + "] SessÃÂ£o " + name + " aberta.")
                if not is_on and session_notified[i] and not session_ended[i]:
                    session_ended[i] = True
                    send_telegram(msg_session_end(name))
                    print("[" + ts + "] SessÃÂ£o " + name + " encerrada.")

            if idx is None:
                time.sleep(CHECK_INTERVAL)
                continue

            sh, sm, eh, em, name = sess

            if session_signals[idx] >= MAX_SIGNALS:
                print("[" + ts + "] Limite atingido.")
                time.sleep(CHECK_INTERVAL)
                continue

            if now - last_signal_time < COOLDOWN_SECS:
                remaining = int(COOLDOWN_SECS - (now - last_signal_time))
                print("[" + ts + "] Cooldown: " + str(remaining) + "s")
                time.sleep(CHECK_INTERVAL)
                continue

            blocked, mins, return_time = check_news_block()
            if blocked:
                msg_news = ("\u26a0\ufe0f NotÃÂ­cia importante em " + str(mins) + " minutos!\nPausando sinais por seguranÃÂ§a.\nRetorno em: " + return_time)
                send_telegram(msg_news)
                print("[" + ts + "] NotÃÂ­cia em " + str(mins) + "min.")
                time.sleep(max(60, mins * 60))
                continue

            with data_lock:
                m1_snap = list(m1_candles)

            if len(m1_snap) < 5:
                print("[" + ts + "] Aguardando candles M1...")
                time.sleep(CHECK_INTERVAL)
                continue

            closed_m1 = [c for c in m1_snap if c["is_closed"]]
            all_m1    = closed_m1 + ([m1_snap[-1]] if not m1_snap[-1]["is_closed"] else [])

            direction, pattern = detect_pattern(all_m1)
            if direction is None:
                print("[" + ts + "] Nenhum padrÃÂ£o.")
                time.sleep(CHECK_INTERVAL)
                continue

            vol_strong = volume_is_strong(all_m1)
            if not vol_strong:
                print("[" + ts + "] Volume fraco. Ignorando: " + str(pattern))
                time.sleep(CHECK_INTERVAL)
                continue

            trend = m5_trend()
            if trend is not None:
                if direction == "CALL" and trend != "UP":
                    print("[" + ts + "] M5 baixa. Ignorando CALL.")
                    time.sleep(CHECK_INTERVAL)
                    continue
                if direction == "PUT" and trend != "DOWN":
                    print("[" + ts + "] M5 alta. Ignorando PUT.")
                    time.sleep(CHECK_INTERVAL)
                    continue

            # Calcula confianÃÂ§a (0-100)
            confianca = 50
            if vol_strong: confianca += 25
            if trend is not None: confianca += 25

            ts2 = now_brt().strftime("%H:%M:%S BRT")
            # BOT-N4: ValidaÃ§Ã£o via Claude API
            ia_valido, ia_confianca, ia_motivo, ia_risco = validate_with_claude(
                direction, pattern, vol_strong, trend, m1_snap
            )
            if not ia_valido:
                print("[IA] Sinal invalidado. Motivo: " + ia_motivo)
                time.sleep(CHECK_INTERVAL)
                continue
            if ia_confianca < 65:
                print("[IA] ConfianÃ§a insuficiente: " + str(ia_confianca) + "%. Ignorando.")
                time.sleep(CHECK_INTERVAL)
                continue

            signal_text = msg_signal(direction, vol_strong, trend, ia_confianca, ia_risco, bet=current_bet)
            ok  = send_telegram(signal_text)
            if ok:
                last_signal_time     = time.time()
                session_signals[idx] += 1
                print("[" + ts2 + "] [" + name + "] " + direction + " (" + str(pattern) + ") #" + str(session_signals[idx]) + " IA=" + str(ia_confianca) + "%")
                # BOT-N5: Envia para VIPs
                vip_count = send_signal_to_vips(signal_text)
                if vip_count > 0:
                    print("[VIP] Sinal enviado para " + str(vip_count) + " membro(s) VIP.")
                # Registra no Supabase
                log_signal(
                    ativo="GBP/USD OTC",
                    direcao=direction,
                    padrao=pattern,
                    confianca=ia_confianca,
                    volume_confirmado=vol_strong,
                    m5_confirmado=(trend is not None),
                    sessao=name,
                    validado_ia=True,
                )
            else:
                print("[" + ts2 + "] Falha ao enviar sinal.")
                time.sleep(CHECK_INTERVAL)
                continue

            time.sleep(30)
            ts3 = now_brt().strftime("%H:%M:%S BRT")
            ok2 = send_telegram(msg_warning())
            if ok2: print("[" + ts3 + "] Segundo aviso enviado.")

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            ts = now_brt().strftime("%H:%M:%S BRT")
            print("[" + ts + "] Erro no loop: " + str(e))
            time.sleep(CHECK_INTERVAL)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Bot GBP/USD OTC BOT-N6 iniciado!")
    print("Features: WebSocket + Volume + MTF + Noticias + Stop Loss + Supabase Journaling + Claude AI + VIP + Anti-Martingale")
    init_supabase()
    t_m1 = threading.Thread(target=start_m1_ws, daemon=True)
    t_m1.start()
    t_m5 = threading.Thread(target=start_m5_ws, daemon=True)
    t_m5.start()
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
