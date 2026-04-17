import os
import time
import math
import json
import requests
import threading
import yfinance as yf
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
# Ativos monitorados
# ---------------------------------------------------------------------------
# NOTA: Binance bloqueia IPs da AWS (Railway) com HTTP 451.
# Todos os ativos usam Yahoo Finance (yfinance) como fonte de dados.
ASSETS = {
    "GBP": {"label": "GBP/USD OTC", "source": "yahoo", "symbol": "GBPUSD=X"},
    "EUR": {"label": "EUR/USD OTC", "source": "yahoo", "symbol": "EURUSD=X"},
    "AUD": {"label": "AUD/USD OTC", "source": "yahoo", "symbol": "AUDUSD=X"},
}

# ---------------------------------------------------------------------------
# Estado global
# ---------------------------------------------------------------------------

session_signals  = [0, 0, 0]
session_notified = [False, False, False]
session_ended    = [False, False, False]
last_signal_time = {"GBP": 0.0, "EUR": 0.0, "AUD": 0.0}
bot_start_time   = time.time()
last_update_id   = 0

consecutive_losses = 0
session_wins  = 0
session_losses = 0
stop_until = 0.0

current_bet = BASE_BET_DEMO
last_signal_id = None

asset_m1 = {"GBP": [], "EUR": [], "AUD": []}
asset_m5 = {"GBP": [], "EUR": [], "AUD": []}
data_lock = threading.Lock()

asset_vwap = {
    "GBP": {"cum_tp_vol": 0.0, "cum_vol": 0.0, "value": None, "reset_hour": -1},
    "EUR": {"cum_tp_vol": 0.0, "cum_vol": 0.0, "value": None, "reset_hour": -1},
    "AUD": {"cum_tp_vol": 0.0, "cum_vol": 0.0, "value": None, "reset_hour": -1},
}

supa = None

NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_news_cache = None
_news_cache_time = 0
_daily_report_sent_date = ""

# ---------------------------------------------------------------------------
# Supabase
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
        print("[Supabase] URL/KEY nao configurados.")


def log_signal(ativo, direcao, padrao, confianca, volume_confirmado, m5_confirmado, sessao,
               validado_ia=True, wick_signal=None, momentum_signal=None, vwap_signal=None, vwap_distance=None):
    global last_signal_id
    if supa is None:
        return
    try:
        data = {
            "ativo": ativo, "direcao": direcao, "padrao": padrao,
            "confianca": confianca, "volume_confirmado": volume_confirmado,
            "m5_confirmado": m5_confirmado, "sessao": sessao,
            "validado_ia": validado_ia, "wick_signal": wick_signal,
            "momentum_signal": momentum_signal, "vwap_signal": vwap_signal,
            "vwap_distance": vwap_distance, "resultado": "pendente",
            "registrado_em": datetime.utcnow().isoformat(),
        }
        resp = supa.table("trading_signals").insert(data).execute()
        if resp.data:
            last_signal_id = resp.data[0]["id"]
    except Exception as e:
        print("[Supabase] Erro log_signal: " + str(e))


def update_last_result(resultado):
    if supa is None or last_signal_id is None:
        return
    try:
        supa.table("trading_signals").update({"resultado": resultado}).eq("id", last_signal_id).execute()
    except Exception as e:
        print("[Supabase] Erro update_result: " + str(e))


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
        wins = sum(1 for r in rows if r["resultado"] == "WIN")
        losses = total - wins
        win_rate = int(wins / total * 100) if total > 0 else 0
        pattern_stats = {}
        for r in rows:
            p = r.get("padrao") or "N/A"
            if p not in pattern_stats:
                pattern_stats[p] = {"wins": 0, "total": 0}
            pattern_stats[p]["total"] += 1
            if r["resultado"] == "WIN":
                pattern_stats[p]["wins"] += 1
        best_pattern = max(pattern_stats, key=lambda k: pattern_stats[k]["wins"] / pattern_stats[k]["total"] if pattern_stats[k]["total"] > 0 else 0)
        best_pr = int(pattern_stats[best_pattern]["wins"] / pattern_stats[best_pattern]["total"] * 100) if pattern_stats[best_pattern]["total"] > 0 else 0
        session_stats = {}
        for r in rows:
            s = r.get("sessao") or "N/A"
            if s not in session_stats:
                session_stats[s] = {"wins": 0, "total": 0}
            session_stats[s]["total"] += 1
            if r["resultado"] == "WIN":
                session_stats[s]["wins"] += 1
        best_session = max(session_stats, key=lambda k: session_stats[k]["wins"] / session_stats[k]["total"] if session_stats[k]["total"] > 0 else 0)
        return {"total": total, "wins": wins, "losses": losses, "win_rate": win_rate,
                "best_pattern": best_pattern + " (" + str(best_pr) + "%)", "best_session": best_session}
    except Exception as e:
        print("[Supabase] Erro stats: " + str(e))
        return None


def get_daily_stats():
    if supa is None:
        return None
    try:
        today = datetime.utcnow().date().isoformat()
        resp = supa.table("trading_signals").select("*").gte("created_at", today).neq("resultado", "pendente").execute()
        rows = resp.data or []
        total = len(rows)
        if total == 0:
            return None
        wins = sum(1 for r in rows if r["resultado"] == "WIN")
        losses = total - wins
        win_rate = int(wins / total * 100)
        pattern_stats = {}
        for r in rows:
            p = r.get("padrao") or "N/A"
            if p not in pattern_stats:
                pattern_stats[p] = {"wins": 0, "total": 0}
            pattern_stats[p]["total"] += 1
            if r["resultado"] == "WIN":
                pattern_stats[p]["wins"] += 1
        best_pattern = max(pattern_stats, key=lambda k: pattern_stats[k]["wins"] / pattern_stats[k]["total"] if pattern_stats[k]["total"] > 0 else 0)
        return {"total": total, "wins": wins, "losses": losses, "win_rate": win_rate, "best_pattern": best_pattern}
    except Exception as e:
        return None


# ---------------------------------------------------------------------------
# Utilities de tempo
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
# Yahoo Finance — usa yfinance (confiavel, sem necessidade de crumb/cookie)
# ---------------------------------------------------------------------------

def yf_candles_to_list(df, max_len=30):
    """Converte DataFrame do yfinance para lista de dicts padrao."""
    if df is None or df.empty:
        return []
    candles = []
    for _, row in df.iterrows():
        try:
            o = float(row["Open"])
            h = float(row["High"])
            l = float(row["Low"])
            c = float(row["Close"])
            v = float(row.get("Volume", 1.0) or 1.0)
            if o == 0 or c == 0:
                continue
            candles.append({
                "open": o, "high": h, "low": l, "close": c,
                "volume": v, "is_closed": True,
            })
        except Exception:
            continue
    return candles[-max_len:] if len(candles) > max_len else candles


def fetch_yahoo_candles_m1(symbol):
    """Busca candles M1 do Yahoo Finance via yfinance."""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="1d", interval="1m")
        result = yf_candles_to_list(df, 25)
        print("[Yahoo] " + symbol + " M1: " + str(len(result)) + " candles")
        return result
    except Exception as e:
        print("[Yahoo] Erro M1 " + symbol + ": " + str(e))
        return []


def fetch_yahoo_candles_m5(symbol):
    """Busca candles M5 do Yahoo Finance via yfinance."""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="5d", interval="5m")
        result = yf_candles_to_list(df, 30)
        print("[Yahoo] " + symbol + " M5: " + str(len(result)) + " candles")
        return result
    except Exception as e:
        print("[Yahoo] Erro M5 " + symbol + ": " + str(e))
        return []


def yahoo_update_loop(asset_key):
    """Loop em background que atualiza candles M1 e M5 via yfinance a cada 60s."""
    symbol = ASSETS[asset_key]["symbol"]
    tag = "[Yahoo " + asset_key + "]"
    print(tag + " Loop iniciado para " + symbol)
    while True:
        try:
            m1 = fetch_yahoo_candles_m1(symbol)
            m5 = fetch_yahoo_candles_m5(symbol)
            with data_lock:
                if m1:
                    asset_m1[asset_key] = m1
                    print(tag + " M1 atualizado: " + str(len(m1)) + " candles")
                else:
                    print(tag + " M1 VAZIO - tentando novamente em 30s")
                if m5:
                    asset_m5[asset_key] = m5
                    print(tag + " M5 atualizado: " + str(len(m5)) + " candles")
                else:
                    print(tag + " M5 VAZIO - tentando novamente em 30s")
        except Exception as e:
            print(tag + " Erro loop: " + str(e))
        # Espera 60s se dados ok, 30s se vazio (retry mais rapido)
        with data_lock:
            has_data = len(asset_m1[asset_key]) > 0
        time.sleep(60 if has_data else 30)


# ---------------------------------------------------------------------------
# Price Action helpers
# ---------------------------------------------------------------------------

def body_size(o, c):        return abs(c - o)
def lower_shadow(o, c, l):  return min(o, c) - l
def upper_shadow(o, c, h):  return h - max(o, c)
def candle_range(h, l):     return h - l
def is_bullish(o, c):       return c > o
def is_bearish(o, c):       return c < o
def is_doji(o, c, h, l):
    rng = candle_range(h, l)
    return rng > 0 and body_size(o, c) / rng < 0.1


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
    if big_bear_2 and doji_mid and is_bullish(o[4],c[4]) and c[4]>((o[2]+c[2])/2):
        return "CALL", "Estrela da Manha"
    if body4 > 0 and ushadow4 >= 2*body4 and lshadow4 <= 0.3*rng4 and two_bullish:
        return "PUT", "Estrela Cadente"
    if is_bearish(o[4],c[4]) and is_bullish(o[3],c[3]) and c[4]<o[3] and o[4]>c[3]:
        return "PUT", "Engolfo de Baixa"
    big_bull_2 = is_bullish(o[2],c[2]) and body_size(o[2],c[2])>0.5*candle_range(h[2],l[2])
    if big_bull_2 and doji_mid and is_bearish(o[4],c[4]) and c[4]<((o[2]+c[2])/2):
        return "PUT", "Estrela da Tarde"
    return None, None


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

def volume_is_strong(candles):
    """Verifica se o volume atual e maior que a media das 10 velas anteriores.
    Para dados Yahoo Finance (forex), o volume e sempre 0 — retorna True como fallback.
    """
    if len(candles) < 11:
        return False
    vols = [c["volume"] for c in candles[-11:-1]]
    avg_vol = sum(vols) / len(vols)
    # Fallback: se volumes sao todos iguais (ex: Yahoo forex retorna 0),
    # considera volume como confirmado para nao bloquear sinais
    if avg_vol <= 0:
        return True
    last_vol = candles[-1]["volume"]
    if last_vol <= 0:
        return True
    return last_vol > avg_vol


# ---------------------------------------------------------------------------
# EMA / M5 Trend
# ---------------------------------------------------------------------------

def calc_ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def m5_trend_for(asset_key):
    with data_lock:
        closed = [c for c in asset_m5[asset_key] if c["is_closed"]]
    if len(closed) < 21:
        return None
    closes = [c["close"] for c in closed]
    ema9  = calc_ema(closes[-30:], 9)
    ema21 = calc_ema(closes[-30:], 21)
    if ema9 is None or ema21 is None:
        return None
    if ema9 > ema21:   return "UP"
    elif ema9 < ema21: return "DOWN"
    return None


# ---------------------------------------------------------------------------
# Noticias economicas
# ---------------------------------------------------------------------------

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


def check_news_block(asset_key):
    currency_map = {
        "GBP": ("USD", "GBP"),
        "EUR": ("USD", "EUR"),
        "AUD": ("USD", "AUD"),
    }
    relevant = currency_map.get(asset_key, ("USD",))
    try:
        news = get_news()
        now_utc = datetime.now(timezone.utc)
        for item in news:
            impact = str(item.get("impact", "")).lower()
            currency = str(item.get("currency", ""))
            if impact not in ("high", "red"): continue
            if currency not in relevant: continue
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

def send_telegram(text, parse_mode="HTML"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode}
        r = requests.post(url, json=payload, timeout=10)
        return r.ok
    except Exception as e:
        print("[TG] Erro: " + str(e))
        return False


def send_to(chat_id, text, parse_mode="HTML"):
    if not TELEGRAM_BOT_TOKEN:
        return False
    try:
        url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        r = requests.post(url, json=payload, timeout=10)
        return r.ok
    except Exception as e:
        print("[TG] send_to erro: " + str(e))
        return False


def get_updates(offset=None):
    try:
        url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/getUpdates"
        params = {"timeout": 30}
        if offset:
            params["offset"] = offset
        r = requests.get(url, params=params, timeout=35)
        return r.json().get("result", [])
    except Exception as e:
        print("[TG] getUpdates erro: " + str(e))
        return []


# ---------------------------------------------------------------------------
# Mensagens
# ---------------------------------------------------------------------------

def msg_signal(asset_key, direction, vol_strong, trend, ia_confianca=None, ia_risco=None,
               bet=None, wick_label=None, mom_label=None, vwap_label=None):
    label = ASSETS[asset_key]["label"]
    vol_icon = "Alto \u2705" if vol_strong else "Baixo \u26a0\ufe0f"
    ia_linha = ""
    if ia_confianca is not None:
        ia_linha = "\n\U0001f916 IA: Validado \u2705 | Confianca: " + str(ia_confianca) + "%"
        if ia_risco:
            ia_linha += " | Risco: " + ia_risco
    bet_linha  = ("\n\U0001f4b0 Entrada: $" + str(bet)) if bet is not None else ""
    vwap_linha = ("\n\U0001f3e6 VWAP: " + vwap_label) if vwap_label else ""
    wick_linha = ("\n\U0001f56f Pavio: " + wick_label + " \u2705") if wick_label else ""
    mom_linha  = ("\n\U0001f4c8 Momentum: " + mom_label + " \u2705") if mom_label else ""
    if direction == "CALL":
        trend_icon = "Alta \u2705" if trend == "UP" else ("Baixa \u26a0\ufe0f" if trend == "DOWN" else "\u2014")
        return ("\U0001f7e2 <b>COMPRE \u2014 " + label + "</b>\n"
                "\u23f1 Tempo: 1 minuto" + vwap_linha + wick_linha + mom_linha + ia_linha + "\n"
                "\U0001f4ca Volume: " + vol_icon + " | M5: " + trend_icon + "\n"
                "\u27a1\ufe0f Clique no botao VERDE\n"
                "\u26a1 \xdaLTIMO AVISO \u2014 20 segundos!" + bet_linha)
    else:
        trend_icon = "Baixa \u2705" if trend == "DOWN" else ("Alta \u26a0\ufe0f" if trend == "UP" else "\u2014")
        return ("\U0001f534 <b>VENDA \u2014 " + label + "</b>\n"
                "\u23f1 Tempo: 1 minuto" + vwap_linha + wick_linha + mom_linha + ia_linha + "\n"
                "\U0001f4ca Volume: " + vol_icon + " | M5: " + trend_icon + "\n"
                "\u27a1\ufe0f Clique no botao VERMELHO\n"
                "\u26a1 \xdaLTIMO AVISO \u2014 20 segundos!" + bet_linha)


def msg_session_start(name, sh, sm, eh, em):
    return ("\U0001f7e2 <b>Sessao " + name + " iniciada!</b>\n"
            + "Monitorando GBP/USD OTC | EUR/USD OTC | AUD/USD OTC\n"
            + "Ate " + str(eh).zfill(2) + ":" + str(em).zfill(2)
            + " \u2022 Max. " + str(MAX_SIGNALS) + " sinais totais")


def msg_session_end(name):
    return "\U0001f534 <b>Sessao " + name + " encerrada.</b>\nAte a proxima sessao!"


# ---------------------------------------------------------------------------
# Stop Loss / Record
# ---------------------------------------------------------------------------

def record_loss():
    global consecutive_losses, session_losses, stop_until, current_bet
    session_losses += 1
    consecutive_losses += 1
    update_last_result("LOSS")
    current_bet = current_bet * 2
    if consecutive_losses >= MAX_LOSSES_AM:
        stop_until = time.time() + 86400
        current_bet = BASE_BET_DEMO
        return "6", ""
    if consecutive_losses >= 3:
        stop_until = time.time() + 3600
        resume_dt = datetime.fromtimestamp(stop_until, tz=timezone.utc) + BRT_OFFSET
        return "3", resume_dt.strftime("%H:%M")
    return False, ""


def record_win():
    global consecutive_losses, session_wins, stop_until, current_bet
    session_wins += 1
    consecutive_losses = 0
    current_bet = BASE_BET_DEMO
    stop_until = 0.0
    update_last_result("WIN")


# ---------------------------------------------------------------------------
# Daily report
# ---------------------------------------------------------------------------

def check_daily_report():
    global _daily_report_sent_date
    now = now_brt()
    today_str = now.strftime("%d/%m/%Y")
    if now.hour == 23 and now.minute == 59 and _daily_report_sent_date != today_str:
        stats = get_daily_stats()
        if stats:
            _daily_report_sent_date = today_str
            msg = ("\U0001f4c5 <b>Resumo de hoje \u2014 " + today_str + "</b>\n"
                   "Sinais: " + str(stats["total"]) + " | WIN: " + str(stats["wins"]) + " | LOSS: " + str(stats["losses"]) + "\n"
                   "Win rate: " + str(stats["win_rate"]) + "%\n"
                   "Padrao mais certeiro: " + stats["best_pattern"])
            send_telegram(msg)


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
        print("[VIP] Erro get_vip: " + str(e))
        return []


def add_vip(telegram_id, dias, nome=None):
    if supa is None:
        return False
    try:
        expira = (datetime.utcnow() + timedelta(days=dias)).isoformat()
        supa.table("vip_members").upsert({
            "telegram_id": telegram_id, "nome": nome or telegram_id,
            "plano": "mensal" if dias <= 31 else ("trimestral" if dias <= 92 else "semestral"),
            "ativo": True, "expira_em": expira,
        }).execute()
        return True
    except Exception as e:
        print("[VIP] Erro add_vip: " + str(e))
        return False


def remove_vip(telegram_id):
    if supa is None:
        return False
    try:
        supa.table("vip_members").update({"ativo": False}).eq("telegram_id", telegram_id).execute()
        return True
    except Exception as e:
        print("[VIP] Erro remove_vip: " + str(e))
        return False


def list_vip_active():
    if supa is None:
        return []
    try:
        now_iso = datetime.utcnow().isoformat()
        resp = supa.table("vip_members").select("*").eq("ativo", True).gt("expira_em", now_iso).execute()
        return resp.data or []
    except Exception as e:
        print("[VIP] Erro list_vip: " + str(e))
        return []


def send_signal_to_vips(text):
    members = get_vip_members()
    count = 0
    for m in members:
        tid = m.get("telegram_id")
        if tid and send_to(tid, text):
            count += 1
    return count


# ---------------------------------------------------------------------------
# BOT-N7: Analise de Pavios e Momentum
# ---------------------------------------------------------------------------

def analyze_wicks(candles):
    closed = [c for c in candles if c["is_closed"]]
    if len(closed) < 3:
        return None, None, 0
    results = []
    for candle in closed[-3:]:
        o, h, l, cc = candle["open"], candle["high"], candle["low"], candle["close"]
        body = abs(cc - o)
        full_range = h - l
        if full_range == 0 or body == 0:
            results.append((None, None, 0))
            continue
        lower_wick = min(o, cc) - l
        upper_wick = h - max(o, cc)
        if lower_wick >= 2.5 * body:
            results.append(("CALL", "Rejeicao forte de baixa", 15))
        elif upper_wick >= 2.5 * body:
            results.append(("PUT", "Rejeicao forte de alta", 15))
        elif upper_wick >= 1.5 * body and cc < o:
            results.append(("PUT", "Fakeout de alta", 20))
        elif lower_wick >= 1.5 * body and cc > o:
            results.append(("CALL", "Fakeout de baixa", 20))
        else:
            results.append((None, None, 0))
    call_count = sum(1 for r in results if r[0] == "CALL")
    put_count  = sum(1 for r in results if r[0] == "PUT")
    if call_count >= 2:
        best = max([r for r in results if r[0] == "CALL"], key=lambda x: x[2])
        return "CALL", best[1], best[2]
    if put_count >= 2:
        best = max([r for r in results if r[0] == "PUT"], key=lambda x: x[2])
        return "PUT", best[1], best[2]
    if results and results[-1][0] is not None:
        return results[-1]
    return None, None, 0


def analyze_momentum(candles):
    closed = [c for c in candles if c["is_closed"]]
    if len(closed) < 5:
        return None, None, 0
    last5  = closed[-5:]
    bodies = [abs(c["close"] - c["open"]) for c in last5]
    closes = [c["close"] for c in last5]
    opens_ = [c["open"]  for c in last5]
    last = last5[-1]
    full_range = last["high"] - last["low"]
    last_body  = bodies[-1]
    is_doji_last = (full_range > 0) and (last_body / full_range < 0.10)
    bull_count = sum(1 for i in range(2) if closes[i] > opens_[i])
    bear_count = sum(1 for i in range(2) if closes[i] < opens_[i])
    if is_doji_last and bull_count >= 2:
        return "PUT", "Exaustao de alta (doji)", 15
    if is_doji_last and bear_count >= 2:
        return "CALL", "Exaustao de baixa (doji)", 15
    avg_body = sum(bodies[:-1]) / max(len(bodies[:-1]), 1)
    if avg_body > 0:
        if last_body > 1.5 * avg_body and closes[-1] > opens_[-1]:
            return "CALL", "Aceleracao bullish", 10
        if last_body > 1.5 * avg_body and closes[-1] < opens_[-1]:
            return "PUT", "Aceleracao bearish", 10
    return None, None, 0


# ---------------------------------------------------------------------------
# VWAP Institucional — por ativo
# ---------------------------------------------------------------------------

def update_vwap_for(asset_key, candles):
    vwap = asset_vwap[asset_key]
    brt_now = now_brt()
    current_hour = brt_now.hour
    session_hours = [9, 14, 21]
    if current_hour in session_hours and vwap["reset_hour"] != current_hour:
        vwap["cum_tp_vol"] = 0.0
        vwap["cum_vol"]    = 0.0
        vwap["value"]      = None
        vwap["reset_hour"] = current_hour
        print("[VWAP] Reset " + asset_key + " as " + str(current_hour) + "h.")
    closed = [c for c in candles if c["is_closed"]]
    if not closed:
        return
    window_c = closed[-60:]
    tp_vol_sum = sum(((c["high"] + c["low"] + c["close"]) / 3.0) * c["volume"] for c in window_c)
    vol_sum    = sum(c["volume"] for c in window_c)
    if vol_sum > 0:
        vwap["value"] = tp_vol_sum / vol_sum
    else:
        vwap["value"] = None


def get_vwap_signal_for(asset_key, direction, candles):
    update_vwap_for(asset_key, candles)
    vwap_value = asset_vwap[asset_key]["value"]
    if not vwap_value:
        return None, 0.0, 0, False
    closed = [c for c in candles if c["is_closed"]]
    if not closed:
        return None, 0.0, 0, False
    current_price = closed[-1]["close"]
    distance_pct  = (current_price - vwap_value) / vwap_value * 100.0
    abs_dist      = abs(distance_pct)
    if abs_dist <= 0.01:
        return "Neutro", round(distance_pct, 4), 0, False
    price_above = current_price > vwap_value
    price_below = current_price < vwap_value
    distance_bonus = 10 if abs_dist > 0.2 else (5 if abs_dist > 0.1 else 0)
    if direction == "CALL" and price_below:
        return "Abaixo (zona COMPRA)", round(distance_pct, 4), 10 + distance_bonus, False
    if direction == "PUT"  and price_above:
        return "Acima (zona VENDA)",   round(distance_pct, 4), 10 + distance_bonus, False
    if direction == "CALL" and price_above:
        if abs_dist > 0.3: return "Acima (resistencia)", round(distance_pct, 4), 0, True
        return "Acima (resistencia)", round(distance_pct, 4), -5, False
    if direction == "PUT"  and price_below:
        if abs_dist > 0.3: return "Abaixo (suporte)", round(distance_pct, 4), 0, True
        return "Abaixo (suporte)", round(distance_pct, 4), -5, False
    return None, 0.0, 0, False


def get_current_vwap_for(asset_key):
    """Retorna (vwap_value, current_price) para exibicao no /status."""
    with data_lock:
        m1 = list(asset_m1[asset_key])
    if not m1:
        return None, None
    update_vwap_for(asset_key, m1)
    closed = [c for c in m1 if c["is_closed"]]
    vwap_val = asset_vwap[asset_key]["value"]
    if closed and vwap_val:
        return vwap_val, closed[-1]["close"]
    # Fallback: usa o ultimo candle mesmo que nao fechado
    if m1 and vwap_val:
        return vwap_val, m1[-1]["close"]
    return None, None


# ---------------------------------------------------------------------------
# BOT-N4: Validacao via Claude API
# ---------------------------------------------------------------------------

def validate_with_claude(direction, pattern, vol_strong, trend, candles_m1):
    if not ANTHROPIC_API_KEY:
        return True, 70, "IA desativada", "MEDIO"
    try:
        closes = [c["close"] for c in candles_m1[-10:] if c["is_closed"]]
        prompt = (
            "Voce e um analisador de sinais de trading binario de 1 minuto.\n"
            "Ativo: Par de moedas OTC\n"
            "Sinal: " + direction + "\n"
            "Padrao: " + str(pattern) + "\n"
            "Volume forte: " + str(vol_strong) + "\n"
            "Tendencia M5: " + str(trend) + "\n"
            "Closes M1: " + str(closes) + "\n\n"
            "Responda APENAS com JSON valido (sem markdown):\n"
            '{"validar": true/false, "confianca": 0-100, "motivo": "string", "risco": "BAIXO/MEDIO/ALTO"}'
        )
        client_ia = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client_ia.messages.create(
            model="claude-3-haiku-20240307", max_tokens=256,
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
        risco     = str(res.get("risco", "MEDIO"))
        print("[Claude] validar=" + str(valido) + " confianca=" + str(confianca) + "%")
        return valido, confianca, motivo, risco
    except Exception as e:
        print("[Claude] Erro: " + str(e))
        return True, 65, "Erro na API", "MEDIO"


# ---------------------------------------------------------------------------
# Comandos Telegram
# ---------------------------------------------------------------------------

def handle_command(text, chat_id):
    global consecutive_losses, session_wins, session_losses, stop_until, current_bet
    ts   = now_brt().strftime("%H:%M:%S BRT")
    text = text.strip().lower().split("@")[0]
    print("[" + ts + "] CMD: " + text + " de " + str(chat_id))

    if text == "/start":
        msg = ("\U0001f44b Ola! Sou o <b>Bot Multi-Ativo BOT-N8</b>.\n\n"
               "<b>Ativos:</b> GBP/USD OTC | EUR/USD OTC | AUD/USD OTC\n"
               "<b>Janelas (BRT):</b>\n"
               "\U0001f55b 09:00\u201311:00 \u2014 Londres\n"
               "\U0001f55d 14:00\u201316:00 \u2014 Londres + NY\n"
               "\U0001f315 21:00\u201323:59 \u2014 Noturna\n\n"
               "<b>Max.:</b> 6 sinais/sessao (total 3 ativos)\n"
               "/status | /perdi | /ganhei | /placar | /relatorio\n"
               "<b>Admin:</b> /addvip | /removevip | /listvip")
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
        h_up = int(uptime.total_seconds() // 3600)
        m_up = int((uptime.total_seconds() % 3600) // 60)
        all_lsts = list(last_signal_time.values())
        min_cd = max(0, int(COOLDOWN_SECS - (time.time() - max(all_lsts)))) if all_lsts else 0
        paused = ""
        if time.time() < stop_until:
            resume = datetime.fromtimestamp(stop_until, tz=timezone.utc) + BRT_OFFSET
            paused = "\n\U0001f6d1 Pausado ate " + resume.strftime("%H:%M")

        # Candles status por ativo
        with data_lock:
            gbp_m1 = len([c for c in asset_m1["GBP"] if c["is_closed"]])
            eur_m1 = len([c for c in asset_m1["EUR"] if c["is_closed"]])
            aud_m1 = len([c for c in asset_m1["AUD"] if c["is_closed"]])
        candles_info = ("\n\U0001f4ca Candles M1: GBP=" + str(gbp_m1)
                        + " | EUR=" + str(eur_m1) + " | AUD=" + str(aud_m1))

        msg = ("<b>Status BOT-N8 Multi-Ativo</b>\n"
               "Hora BRT: " + brt_now.strftime("%H:%M:%S") + "\n"
               "Sessao: " + sessao + "\n"
               "Cooldown: " + ("Aguardando " + str(min_cd) + "s" if min_cd > 0 else "pronto") + "\n"
               "Perdas seguidas: " + str(consecutive_losses) + "/" + str(MAX_LOSSES_AM) + "\n"
               "\U0001f4b0 Proxima entrada: $" + str(current_bet) + "\n"
               "Uptime: " + str(h_up) + "h " + str(m_up) + "m"
               + paused + candles_info)

        # VIP info
        vip_members_list = list_vip_active()
        vip_count = len(vip_members_list)
        vip_lines = "\n\U0001f451 <b>VIPs ativos:</b> " + str(vip_count)
        if vip_members_list:
            sorted_vips = sorted(vip_members_list, key=lambda x: x.get("expira_em",""))
            for v in sorted_vips[:3]:
                nome_v = v.get("nome") or v["telegram_id"]
                exp_v  = v.get("expira_em","?")[:10] if v.get("expira_em") else "?"
                vip_lines += "\n\u2022 " + nome_v + " \u2014 " + exp_v

        # VWAP dos 3 ativos
        vwap_lines = "\n\n\U0001f4b9 <b>VWAP Institucional</b>"
        for ak in ASSETS:
            vv, pp = get_current_vwap_for(ak)
            lbl = ASSETS[ak]["label"]
            if vv and pp:
                zona = "zona COMPRA" if pp < vv else "zona VENDA"
                vwap_lines += ("\n\u2022 " + lbl + ": VWAP=" + str(round(vv, 5))
                               + " | Preco=" + str(round(pp, 5))
                               + " (" + zona + ")")
            else:
                with data_lock:
                    cnt = len(asset_m1[ak])
                vwap_lines += "\n\u2022 " + lbl + ": aguardando dados (" + str(cnt) + " candles)"
        send_to(chat_id, msg + vip_lines + vwap_lines)

    elif text == "/perdi":
        triggered, resume_time = record_loss()
        if triggered == "6":
            msg = ("\U0001f6d1 <b>6 tentativas sem sucesso.</b>\nAte amanha!\n"
                   "\U0001f4b0 Valor resetado para: $" + str(BASE_BET_DEMO))
        elif triggered == "3":
            msg = ("\U0001f6d1 3 perdas seguidas.\nPausando 60 minutos.\n"
                   "Proxima sessao: " + resume_time + "\n"
                   "\U0001f4b0 Proxima entrada: $" + str(current_bet))
        else:
            msg = ("\U0001f4c9 Perda registrada. Seguidas: " + str(consecutive_losses)
                   + "/" + str(MAX_LOSSES_AM) + "\n"
                   "\U0001f4b0 Proxima entrada: $" + str(current_bet))
        send_to(chat_id, msg)

    elif text == "/ganhei":
        record_win()
        msg = ("\U0001f4c8 <b>Vitoria registrada!</b> \u2705\n"
               "\U0001f3c6 Anti-Martingale: sessao encerrada com lucro.\n"
               "\U0001f4b0 Valor resetado: $" + str(BASE_BET_DEMO) + "\n"
               "\U0001f305 Retorno amanha!")
        send_to(chat_id, msg)

    elif text == "/placar":
        total = session_wins + session_losses
        taxa  = int(session_wins / total * 100) if total > 0 else 0
        msg = ("\U0001f4ca <b>Placar da sessao</b>\n"
               "\u2705 Vitorias: " + str(session_wins) + "\n"
               "\u274c Derrotas: " + str(session_losses) + "\n"
               "\U0001f4c8 Taxa: " + str(taxa) + "%")
        send_to(chat_id, msg)

    elif text == "/relatorio":
        stats = get_weekly_stats()
        if stats is None or stats["total"] == 0:
            send_to(chat_id, "\U0001f4ca Sem dados suficientes para o relatorio desta semana.")
            return
        profit = round(stats["wins"] * 0.8 - stats["losses"] * 1.0, 2)
        profit_str = ("+" if profit >= 0 else "") + str(profit)
        msg = ("\U0001f4ca <b>Relatorio da Semana</b>\n"
               "Sinais: " + str(stats["total"]) + "\n"
               "\u2705 WIN: " + str(stats["wins"]) + " (" + str(stats["win_rate"]) + "%)\n"
               "\u274c LOSS: " + str(stats["losses"]) + " (" + str(100 - stats["win_rate"]) + "%)\n"
               "\U0001f3c6 Melhor padrao: " + stats["best_pattern"] + "\n"
               "\u23f0 Melhor sessao: " + stats["best_session"] + "\n"
               "\U0001f4b0 Se operado com $10: " + profit_str)
        send_to(chat_id, msg)

    elif text.startswith("/addvip"):
        if chat_id != TELEGRAM_CHAT_ID:
            send_to(chat_id, "\u26d4 Acesso negado.")
            return
        parts = text.split()
        if len(parts) < 3:
            send_to(chat_id, "Uso: /addvip [telegram_id] [dias]")
            return
        tid = parts[1]
        try:
            dias = int(parts[2])
        except ValueError:
            send_to(chat_id, "Dias deve ser um numero.")
            return
        if add_vip(tid, dias):
            send_to(tid, ("\U0001f389 Bem-vindo ao VIP!\n\n"
                          "Voce recebera sinais de GBP/USD | EUR/USD | AUD/USD.\n"
                          "\u2705 Acesso por " + str(dias) + " dias."))
            send_to(chat_id, "\u2705 VIP ativado: " + tid + " por " + str(dias) + " dias.")
        else:
            send_to(chat_id, "\u274c Erro ao ativar VIP.")

    elif text.startswith("/removevip"):
        if chat_id != TELEGRAM_CHAT_ID:
            send_to(chat_id, "\u26d4 Acesso negado.")
            return
        parts = text.split()
        if len(parts) < 2:
            send_to(chat_id, "Uso: /removevip [telegram_id]")
            return
        tid = parts[1]
        send_to(chat_id, "\u2705 VIP removido: " + tid if remove_vip(tid) else "\u274c Erro ao remover VIP.")

    elif text == "/listvip":
        if chat_id != TELEGRAM_CHAT_ID:
            send_to(chat_id, "\u26d4 Acesso negado.")
            return
        members = list_vip_active()
        if not members:
            send_to(chat_id, "\U0001f4cb Nenhum VIP ativo.")
            return
        lines = ["\U0001f451 <b>VIPs ativos:</b>"]
        for m in members:
            exp = m.get("expira_em","?")[:10] if m.get("expira_em") else "?"
            nome = m.get("nome") or m["telegram_id"]
            lines.append("\u2022 " + nome + " | ID: " + m["telegram_id"] + " | Expira: " + exp)
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
# Loop principal de sinais — multi-ativo
# ---------------------------------------------------------------------------

def check_asset_signal(asset_key, idx, name):
    """Verifica e envia sinal para um ativo. Retorna True se sinal enviado."""
    global last_signal_time, current_bet, session_signals
    ts  = now_brt().strftime("%H:%M:%S BRT")
    now = time.time()

    # Cooldown por ativo
    if now - last_signal_time[asset_key] < COOLDOWN_SECS:
        remaining = int(COOLDOWN_SECS - (now - last_signal_time[asset_key]))
        print("[" + ts + "] [" + asset_key + "] Cooldown: " + str(remaining) + "s")
        return False

    # Verifica noticias
    blocked, mins, return_time = check_news_block(asset_key)
    if blocked:
        label = ASSETS[asset_key]["label"]
        send_telegram("\u26a0\ufe0f Noticia (" + label + ") em " + str(mins)
                      + "min. Pausando. Retorno: " + return_time)
        print("[" + ts + "] [" + asset_key + "] Noticia em " + str(mins) + "min.")
        return False

    # Candles
    with data_lock:
        m1_snap = list(asset_m1[asset_key])

    closed_count = len([c for c in m1_snap if c["is_closed"]])
    if closed_count < 5:
        print("[" + ts + "] [" + asset_key + "] Aguardando candles M1 (" + str(closed_count) + "/5)...")
        return False

    closed_m1 = [c for c in m1_snap if c["is_closed"]]
    all_m1    = closed_m1 + ([m1_snap[-1]] if m1_snap and not m1_snap[-1]["is_closed"] else [])

    direction, pattern = detect_pattern(all_m1)
    if direction is None:
        print("[" + ts + "] [" + asset_key + "] Nenhum padrao.")
        return False

    vol_strong = volume_is_strong(all_m1)
    if not vol_strong:
        print("[" + ts + "] [" + asset_key + "] Volume fraco: " + str(pattern))
        return False

    trend = m5_trend_for(asset_key)
    if trend is not None:
        if direction == "CALL" and trend != "UP":
            print("[" + ts + "] [" + asset_key + "] M5 discorda CALL.")
            return False
        if direction == "PUT" and trend != "DOWN":
            print("[" + ts + "] [" + asset_key + "] M5 discorda PUT.")
            return False

    wick_dir, wick_label, wick_bonus = analyze_wicks(all_m1)
    mom_dir,  mom_label,  mom_bonus  = analyze_momentum(all_m1)

    if wick_dir is not None and wick_dir != direction:
        print("[" + ts + "] [" + asset_key + "] Pavio discorda.")
        return False
    if mom_dir is not None and mom_dir != direction:
        print("[" + ts + "] [" + asset_key + "] Momentum discorda.")
        return False

    confianca = 50
    if vol_strong:          confianca += 25
    if trend is not None:   confianca += 25
    confianca += wick_bonus
    confianca += mom_bonus

    vwap_label, vwap_dist, vwap_bonus, vwap_ignore = get_vwap_signal_for(asset_key, direction, m1_snap)
    if vwap_ignore:
        return False
    confianca = min(confianca + vwap_bonus, 100)

    ts2 = now_brt().strftime("%H:%M:%S BRT")
    ia_valido, ia_confianca, ia_motivo, ia_risco = validate_with_claude(
        direction, pattern, vol_strong, trend, m1_snap)

    if not ia_valido:
        print("[IA] [" + asset_key + "] Invalidado: " + ia_motivo)
        return False
    if ia_confianca < 65:
        print("[IA] [" + asset_key + "] Confianca baixa: " + str(ia_confianca) + "%")
        return False

    signal_text = msg_signal(asset_key, direction, vol_strong, trend, ia_confianca, ia_risco,
                             bet=current_bet, wick_label=wick_label, mom_label=mom_label,
                             vwap_label=vwap_label)
    ok = send_telegram(signal_text)
    if ok:
        last_signal_time[asset_key] = time.time()
        session_signals[idx] += 1
        print("[" + ts2 + "] [" + name + "] [" + asset_key + "] " + direction
              + " (" + str(pattern) + ") #" + str(session_signals[idx])
              + " IA=" + str(ia_confianca) + "%")
        vip_count = send_signal_to_vips(signal_text)
        if vip_count > 0:
            print("[VIP] " + str(vip_count) + " membro(s) notificado(s).")
        log_signal(ativo=ASSETS[asset_key]["label"], direcao=direction, padrao=pattern,
                   confianca=ia_confianca, volume_confirmado=vol_strong,
                   m5_confirmado=(trend is not None), sessao=name, validado_ia=True,
                   wick_signal=wick_label, momentum_signal=mom_label,
                   vwap_signal=vwap_label, vwap_distance=vwap_dist)
        return True
    else:
        print("[" + ts2 + "] Falha envio " + asset_key + ".")
        return False


def signal_loop():
    global session_signals, session_notified, session_ended

    print("Aguardando dados iniciais via yfinance (max 120s por ativo)...")
    for ak in ASSETS:
        for _ in range(40):  # 40 * 3s = 120s
            with data_lock:
                ok = len([c for c in asset_m1[ak] if c["is_closed"]]) >= 5
            if ok:
                print("[Init] " + ak + " pronto.")
                break
            time.sleep(3)
        else:
            print("[Init] Timeout " + ak + " — prosseguindo sem dados.")

    print("Loop de sinais iniciado (BOT-N8 Multi-Ativo).")

    while True:
        try:
            ts  = now_brt().strftime("%H:%M:%S BRT")
            now = time.time()

            check_daily_report()

            if now < stop_until:
                resume = datetime.fromtimestamp(stop_until, tz=timezone.utc) + BRT_OFFSET
                print("[" + ts + "] Pausado ate " + resume.strftime("%H:%M") + ".")
                time.sleep(CHECK_INTERVAL)
                continue

            idx, sess = active_session()

            # Sessao start/end notifications
            brt_now = now_brt()
            t_now = brt_now
            for i, (sh, sm, eh, em, sname) in enumerate(SESSIONS):
                start = t_now.replace(hour=sh, minute=sm, second=0,  microsecond=0)
                end   = t_now.replace(hour=eh, minute=em, second=59, microsecond=999999)
                is_on = (start <= t_now <= end)
                if is_on and not session_notified[i]:
                    session_notified[i] = True
                    session_ended[i]    = False
                    session_signals[i]  = 0
                    send_telegram(msg_session_start(sname, sh, sm, eh, em))
                    print("[" + ts + "] Sessao " + sname + " iniciada.")
                if not is_on and session_notified[i] and not session_ended[i]:
                    session_ended[i] = True
                    send_telegram(msg_session_end(sname))
                    print("[" + ts + "] Sessao " + sname + " encerrada.")

            if idx is None:
                time.sleep(CHECK_INTERVAL)
                continue

            sh, sm, eh, em, name = sess

            if session_signals[idx] >= MAX_SIGNALS:
                print("[" + ts + "] Max sinais atingido: " + name + ".")
                time.sleep(CHECK_INTERVAL)
                continue

            # Verifica cooldown global — se TODOS em cooldown, aguarda
            all_in_cd = all(now - last_signal_time[ak] < COOLDOWN_SECS for ak in ASSETS)
            if all_in_cd:
                min_remaining = min(int(COOLDOWN_SECS - (now - last_signal_time[ak])) for ak in ASSETS)
                print("[" + ts + "] Todos ativos em cooldown: " + str(min_remaining) + "s")
                time.sleep(CHECK_INTERVAL)
                continue

            # Tenta sinal em cada ativo (rotacao)
            signal_sent = False
            for asset_key in ASSETS:
                if session_signals[idx] >= MAX_SIGNALS:
                    break
                if check_asset_signal(asset_key, idx, name):
                    signal_sent = True
                    time.sleep(30)
                    break

            if not signal_sent:
                time.sleep(CHECK_INTERVAL)

        except Exception as e:
            print("[SignalLoop] Erro: " + str(e))
            time.sleep(CHECK_INTERVAL)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Bot Multi-Ativo BOT-N8 iniciado!")
    print("GBP/USD OTC | EUR/USD OTC | AUD/USD OTC — todos via yfinance")
    print("NOTA: Binance WS bloqueado pela Railway (AWS HTTP 451) — usando Yahoo Finance")
    print("=" * 60)
    init_supabase()

    # Inicia loops Yahoo Finance para todos os 3 ativos
    for asset_key in ASSETS:
        threading.Thread(target=yahoo_update_loop, args=(asset_key,), daemon=True).start()
        time.sleep(1)  # stagger para evitar rate limit

    # Polling Telegram
    threading.Thread(target=polling_loop, daemon=True).start()

    signal_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Bot encerrado.")
    except Exception as e:
        print("Erro critico: " + str(e))
        time.sleep(10)
