import os
import time
import math
import json
import requests
import threading
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

COOLDOWN_SECS  = 300
MAX_SIGNALS    = 12  # ALTERADO: 12 sinais/dia (4 por janela x 3 janelas)
CHECK_INTERVAL = 30

# Anti-Martingale
BASE_BET_DEMO  = 1.0
BASE_BET_REAL  = 10.0
MAX_LOSSES_AM  = 6

# ---------------------------------------------------------------------------
# JANELAS DE OPERACAO (Horario de Brasilia)
# 3 janelas com 4 sinais cada = 12 sinais/dia
# ---------------------------------------------------------------------------
TRADING_WINDOWS = [
    {
        "name": "ABERTURA_LONDRES",
        "emoji": "🌅",
        "start_hour": 5,   # 05:00 BRT
        "start_min": 0,
        "end_hour": 6,     # 06:00 BRT
        "end_min": 0,
        "max_signals": 4,
        "label": "Abertura de Londres",
    },
    {
        "name": "OVERLAP_LONDRES_NY",
        "emoji": "⭐",
        "start_hour": 10,  # 10:00 BRT
        "start_min": 0,
        "end_hour": 11,    # 11:00 BRT
        "end_min": 0,
        "max_signals": 4,
        "label": "PICO Londres+NY",
    },
    {
        "name": "POS_ALMOCO_NY",
        "emoji": "🌆",
        "start_hour": 15,  # 15:00 BRT
        "start_min": 0,
        "end_hour": 16,    # 16:00 BRT
        "end_min": 0,
        "max_signals": 4,
        "label": "Pos-almoco NY",
    },
]

# Contador de sinais por janela (resetado diariamente)
window_signal_count = {w["name"]: 0 for w in TRADING_WINDOWS}


def get_active_window():
    """Retorna a janela ativa agora ou None se estiver fora de qualquer janela."""
    now = now_brt()
    cur_min = now.hour * 60 + now.minute
    for w in TRADING_WINDOWS:
        start = w["start_hour"] * 60 + w["start_min"]
        end = w["end_hour"] * 60 + w["end_min"]
        if start <= cur_min < end:
            return w
    return None


def time_until_next_window():
    """Retorna (segundos_ate_proxima_janela, nome_da_proxima) para sleep inteligente."""
    now = now_brt()
    cur_min = now.hour * 60 + now.minute
    candidates = []
    for w in TRADING_WINDOWS:
        start = w["start_hour"] * 60 + w["start_min"]
        if start > cur_min:
            candidates.append((start - cur_min, w))
    if candidates:
        diff_min, w = min(candidates, key=lambda x: x[0])
        return diff_min * 60 - now.second, w
    # Nenhuma janela mais hoje — proxima e a primeira de amanha
    first = TRADING_WINDOWS[0]
    start = first["start_hour"] * 60 + first["start_min"]
    minutes_until_midnight = (24 * 60) - cur_min
    return (minutes_until_midnight + start) * 60 - now.second, first


# ---------------------------------------------------------------------------
# Ativos
# ---------------------------------------------------------------------------
ASSETS = {
    "GBP": {"label": "GBP/USD OTC", "source": "yahoo", "symbol": "GBPUSD=X"},
    # "EUR": {"label": "EUR/USD OTC", "source": "yahoo", "symbol": "EURUSD=X"},
    # "AUD": {"label": "AUD/USD OTC", "source": "yahoo", "symbol": "AUDUSD=X"},
}

# ---------------------------------------------------------------------------
# Estado global
# ---------------------------------------------------------------------------

daily_signals    = 0
daily_reset_date = ""
daily_notified   = False
last_signal_time = {"GBP": 0.0}
bot_start_time   = time.time()
last_update_id   = 0

consecutive_losses = 0
session_wins       = 0
session_losses     = 0
stop_until         = 0.0
current_bet        = BASE_BET_DEMO
last_signal_id     = None

asset_m1 = {"GBP": []}
asset_m5 = {"GBP": []}
data_lock = threading.Lock()

asset_vwap = {
    "GBP": {"cum_tp_vol": 0.0, "cum_vol": 0.0, "value": None, "reset_hour": -1},
}

supa = None

NEWS_URL            = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_news_cache         = None
_news_cache_time    = 0
_daily_report_sent_date = ""
_window_open_notified = {w["name"]: "" for w in TRADING_WINDOWS}

# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------

def init_supabase():
    global supa
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[Supabase] URL/KEY nao configurados. Journaling desativado.")
        return
    try:
        client = create_client(SUPABASE_URL, SUPABASE_KEY)
        client.table("trading_signals").select("id").limit(1).execute()
        supa = client
        print("[Supabase] Conexao validada com sucesso.")
    except Exception as e:
        err = str(e)
        if "Invalid API key" in err or "invalid" in err.lower() or "401" in err or "403" in err:
            print("[Supabase] ERRO: Chave de API invalida. Journaling desativado. Bot continua normalmente.")
        else:
            print("[Supabase] ERRO ao conectar: " + err + ". Journaling desativado. Bot continua normalmente.")
        supa = None


def _supa_call(fn):
    if supa is None:
        return None
    try:
        return fn()
    except Exception as e:
        print("[Supabase] Erro na operacao: " + str(e))
        return None


def log_signal(ativo, direcao, padrao, confianca, volume_confirmado, m5_confirmado, sessao,
               validado_ia=True, wick_signal=None, momentum_signal=None,
               vwap_signal=None, vwap_distance=None):
    global last_signal_id
    data = {
        "ativo": ativo, "direcao": direcao, "padrao": padrao,
        "confianca": confianca, "volume_confirmado": volume_confirmado,
        "m5_confirmado": m5_confirmado, "sessao": sessao,
        "validado_ia": validado_ia, "wick_signal": wick_signal,
        "momentum_signal": momentum_signal, "vwap_signal": vwap_signal,
        "vwap_distance": vwap_distance, "resultado": "pendente",
        "registrado_em": datetime.utcnow().isoformat(),
    }
    resp = _supa_call(lambda: supa.table("trading_signals").insert(data).execute())
    if resp and resp.data:
        last_signal_id = resp.data[0]["id"]
        print("[Supabase] Sinal registrado: " + last_signal_id)


def update_last_result(resultado):
    if last_signal_id is None:
        return
    _supa_call(lambda: supa.table("trading_signals").update({"resultado": resultado}).eq("id", last_signal_id).execute())


def get_weekly_stats():
    week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
    resp = _supa_call(lambda: supa.table("trading_signals").select("*").gte("created_at", week_ago).neq("resultado", "pendente").execute())
    if resp is None:
        return None
    rows = resp.data or []
    total = len(rows)
    if total == 0:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0, "best_pattern": "-", "best_session": "-"}
    wins = sum(1 for r in rows if r["resultado"] == "WIN")
    losses = total - wins
    win_rate = int(wins / total * 100)
    pstat = {}
    for r in rows:
        p = r.get("padrao") or "N/A"
        if p not in pstat: pstat[p] = {"wins": 0, "total": 0}
        pstat[p]["total"] += 1
        if r["resultado"] == "WIN": pstat[p]["wins"] += 1
    best_p = max(pstat, key=lambda k: pstat[k]["wins"] / pstat[k]["total"] if pstat[k]["total"] > 0 else 0)
    best_pr = int(pstat[best_p]["wins"] / pstat[best_p]["total"] * 100) if pstat[best_p]["total"] > 0 else 0
    sstat = {}
    for r in rows:
        s = r.get("sessao") or "N/A"
        if s not in sstat: sstat[s] = {"wins": 0, "total": 0}
        sstat[s]["total"] += 1
        if r["resultado"] == "WIN": sstat[s]["wins"] += 1
    best_s = max(sstat, key=lambda k: sstat[k]["wins"] / sstat[k]["total"] if sstat[k]["total"] > 0 else 0)
    return {"total": total, "wins": wins, "losses": losses, "win_rate": win_rate,
            "best_pattern": best_p + " (" + str(best_pr) + "%)", "best_session": best_s}


def get_daily_stats():
    today = datetime.utcnow().date().isoformat()
    resp = _supa_call(lambda: supa.table("trading_signals").select("*").gte("created_at", today).neq("resultado", "pendente").execute())
    if resp is None:
        return None
    rows = resp.data or []
    total = len(rows)
    if total == 0: return None
    wins = sum(1 for r in rows if r["resultado"] == "WIN")
    pstat = {}
    for r in rows:
        p = r.get("padrao") or "N/A"
        if p not in pstat: pstat[p] = {"wins": 0, "total": 0}
        pstat[p]["total"] += 1
        if r["resultado"] == "WIN": pstat[p]["wins"] += 1
    best_p = max(pstat, key=lambda k: pstat[k]["wins"] / pstat[k]["total"] if pstat[k]["total"] > 0 else 0)
    return {"total": total, "wins": wins, "losses": total - wins,
            "win_rate": int(wins / total * 100), "best_pattern": best_p}


# ---------------------------------------------------------------------------
# Tempo
# ---------------------------------------------------------------------------

def now_brt():
    return datetime.now(timezone.utc) + BRT_OFFSET


YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "application/json",
}

def fetch_yahoo_chart(symbol, interval, range_val, max_len, tag):
    base_url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol
    params = "interval=" + interval + "&range=" + range_val
    url = base_url + "?" + params
    try:
        print("[Yahoo] " + tag + " Buscando: interval=" + interval + " range=" + range_val)
        r = requests.get(url, headers=YAHOO_HEADERS, timeout=15)
        print("[Yahoo] " + tag + " HTTP " + str(r.status_code))
        r.raise_for_status()
        j = r.json()
        result_data = j.get("chart", {}).get("result", [])
        if not result_data:
            err = j.get("chart", {}).get("error", "sem resultado")
            print("[Yahoo] " + tag + " ERRO: " + str(err))
            return []
        res = result_data[0]
        timestamps = res.get("timestamp", [])
        q = res.get("indicators", {}).get("quote", [{}])[0]
        opens  = q.get("open",  [])
        highs  = q.get("high",  [])
        lows   = q.get("low",   [])
        closes = q.get("close", [])
        vols   = q.get("volume",[])
        candles = []
        for i in range(len(timestamps)):
            try:
                o = opens[i] if i < len(opens) else None
                h = highs[i] if i < len(highs) else None
                l = lows[i]  if i < len(lows)  else None
                c = closes[i]if i < len(closes) else None
                v = vols[i]  if vols and i < len(vols) else 1.0
                if o is None or c is None or o == 0 or c == 0:
                    continue
                candles.append({
                    "open": float(o), "high": float(h), "low": float(l),
                    "close": float(c), "volume": float(v or 1.0), "is_closed": True
                })
            except Exception:
                continue
        result = candles[-max_len:] if len(candles) > max_len else candles
        print("[Yahoo] " + tag + " " + str(len(result)) + " candles obtidos")
        return result
    except Exception as e:
        print("[Yahoo] " + tag + " EXCECAO: " + str(type(e).__name__) + ": " + str(e))
        return []


def fetch_yahoo_candles_m1(symbol):
    return fetch_yahoo_chart(symbol, "1m", "1d", 25, symbol + " M1")


def fetch_yahoo_candles_m5(symbol):
    return fetch_yahoo_chart(symbol, "5m", "5d", 30, symbol + " M5")


def yahoo_update_loop(asset_key):
    symbol = ASSETS[asset_key]["symbol"]
    tag = "[Yahoo " + asset_key + "]"
    print(tag + " Loop iniciado para " + symbol)
    while True:
        try:
            # Otimizacao: so atualiza dados se estamos em janela ou perto dela (15 min antes)
            now = now_brt()
            cur_min = now.hour * 60 + now.minute
            should_fetch = False
            for w in TRADING_WINDOWS:
                start = w["start_hour"] * 60 + w["start_min"]
                end = w["end_hour"] * 60 + w["end_min"]
                # Atualiza 15 min antes da janela ate o fim dela
                if (start - 15) <= cur_min < end:
                    should_fetch = True
                    break

            if should_fetch:
                m1 = fetch_yahoo_candles_m1(symbol)
                m5 = fetch_yahoo_candles_m5(symbol)
                with data_lock:
                    if m1:
                        asset_m1[asset_key] = m1
                        print(tag + " M1 atualizado: " + str(len(m1)) + " candles")
                    else:
                        print(tag + " M1 VAZIO")
                    if m5:
                        asset_m5[asset_key] = m5
                        print(tag + " M5 atualizado: " + str(len(m5)) + " candles")
                    else:
                        print(tag + " M5 VAZIO")
                time.sleep(15)
            else:
                # Fora de janela: sleep longo para economizar recursos
                time.sleep(60)
        except Exception as e:
            print(tag + " Erro loop: " + str(e))
            time.sleep(15)


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


# ---------------------------------------------------------------------------
# HIERARQUIA DE FORCA DOS PADROES (1-10)
# Padrao mais forte = mais confiavel = exige menos confluencia
# Padrao mais fraco = exige mais confluencia para disparar sinal
# ---------------------------------------------------------------------------
PATTERN_STRENGTH = {
    "Engolfo de Alta":              10,
    "Engolfo de Baixa":             10,
    "Pin Bar Bullish":               9,
    "Pin Bar Bearish":               9,
    "Estrela da Manha":              9,
    "Estrela da Tarde":              9,
    "Tweezer Bottom":                8,
    "Tweezer Top":                   8,
    "Martelo":                       8,
    "Estrela Cadente":               8,
    "Doji em Extremo Bullish":       7,
    "Doji em Extremo Bearish":       7,
    "Harami Bullish":                6,
    "Harami Bearish":                6,
    "3 Velas Bullish":               6,
    "3 Velas Bearish":               6,
    "Momentum Bullish":              5,
    "Momentum Bearish":              5,
    "Inside Bar Bullish":            4,
    "Inside Bar Bearish":            4,
    "Direcional Alta":               3,
    "Direcional Baixa":              3,
    "Fechamento Consecutivo Alta":   3,
    "Fechamento Consecutivo Baixa":  3,
}


def get_pattern_strength(pattern):
    """Retorna a forca do padrao (1-10). Default 5 para padroes nao listados."""
    if pattern is None:
        return 0
    return PATTERN_STRENGTH.get(pattern, 5)


def get_required_confluence(pattern):
    """
    Retorna o numero minimo de filtros (de 6) que o padrao precisa para disparar.
    Padrao FORTE precisa de menos confluencia.
    Padrao FRACO precisa de mais confluencia (protecao).
    """
    strength = get_pattern_strength(pattern)
    if strength >= 8:   return 2  # padrao forte: 2/6 (atual)
    if strength >= 5:   return 3  # padrao medio: 3/6
    return 4                       # padrao fraco: 4/6


def get_required_ia_confidence(pattern):
    """Padrao fraco precisa de IA com maior confianca para nao furar protecao."""
    strength = get_pattern_strength(pattern)
    if strength >= 8:   return 55  # forte: aceita 55% (atual)
    if strength >= 5:   return 60  # medio: 60%
    return 65                       # fraco: 65%


def detect_pattern(candles):
    """
    Detecta padroes de vela em ordem de forca (do mais forte ao mais fraco).
    Retorna a primeira correspondencia encontrada — sempre prioriza o mais forte.
    """
    if len(candles) < 5:
        return None, None
    last5 = candles[-5:]
    o = [c["open"]  for c in last5]
    h = [c["high"]  for c in last5]
    l = [c["low"]   for c in last5]
    c = [c["close"] for c in last5]
    o4, h4, l4, c4 = o[4], h[4], l[4], c[4]
    o3, h3, l3, c3 = o[3], h[3], l[3], c[3]
    body4    = body_size(o4, c4)
    body3    = body_size(o3, c3)
    lshadow4 = lower_shadow(o4, c4, l4)
    ushadow4 = upper_shadow(o4, c4, h4)
    rng4     = candle_range(h4, l4)
    rng3     = candle_range(h3, l3)
    two_bearish = is_bearish(o[2], c[2]) and is_bearish(o[3], c[3])
    two_bullish = is_bullish(o[2], c[2]) and is_bullish(o[3], c[3])
    doji_mid    = is_doji(o[3], c[3], h[3], l[3])

    # Contexto para Doji em Extremo (max/min das ultimas 5 velas disponiveis)
    max_last5 = max(h)
    min_last5 = min(l)
    is_at_high = h4 >= max_last5 - (rng4 * 0.1)  # vela atual no topo do range
    is_at_low  = l4 <= min_last5 + (rng4 * 0.1)  # vela atual no fundo do range

    # ============================================================
    # FORCA 10 — ENGOLFO (mais confiavel)
    # ============================================================
    if is_bullish(o[4],c[4]) and is_bearish(o[3],c[3]) and c[4]>o[3] and o[4]<c[3]:
        return "CALL", "Engolfo de Alta"
    if is_bearish(o[4],c[4]) and is_bullish(o[3],c[3]) and c[4]<o[3] and o[4]>c[3]:
        return "PUT", "Engolfo de Baixa"

    # ============================================================
    # FORCA 9 — PIN BAR e ESTRELA DA MANHA/TARDE
    # ============================================================
    # Pin Bar Bullish: sombra inferior >= 2x corpo + sombra superior <= 25% do range
    if rng4 > 0 and body4 > 0:
        if lshadow4 >= 2 * body4 and ushadow4 <= 0.25 * rng4 and is_bullish(o4, c4):
            return "CALL", "Pin Bar Bullish"
        if ushadow4 >= 2 * body4 and lshadow4 <= 0.25 * rng4 and is_bearish(o4, c4):
            return "PUT", "Pin Bar Bearish"

    # Estrela da Manha
    big_bear_2 = is_bearish(o[2],c[2]) and body_size(o[2],c[2])>0.5*candle_range(h[2],l[2])
    if big_bear_2 and doji_mid and is_bullish(o[4],c[4]) and c[4]>((o[2]+c[2])/2):
        return "CALL", "Estrela da Manha"
    # Estrela da Tarde
    big_bull_2 = is_bullish(o[2],c[2]) and body_size(o[2],c[2])>0.5*candle_range(h[2],l[2])
    if big_bull_2 and doji_mid and is_bearish(o[4],c[4]) and c[4]<((o[2]+c[2])/2):
        return "PUT", "Estrela da Tarde"

    # ============================================================
    # FORCA 8 — TWEEZER, MARTELO, ESTRELA CADENTE
    # ============================================================
    # Tweezer Bottom: 2 velas com mesma minima (tolerancia 0.05% do preco)
    if rng4 > 0 and rng3 > 0:
        tolerancia = c4 * 0.0005  # 0.05% do preco atual
        if abs(l4 - l3) <= tolerancia and is_bearish(o3, c3) and is_bullish(o4, c4):
            return "CALL", "Tweezer Bottom"
        if abs(h4 - h3) <= tolerancia and is_bullish(o3, c3) and is_bearish(o4, c4):
            return "PUT", "Tweezer Top"

    # Martelo (versao tradicional - precisa de 2 velas baixistas antes)
    if body4 > 0 and lshadow4 >= 2*body4 and ushadow4 <= 0.3*rng4 and two_bearish:
        return "CALL", "Martelo"
    # Estrela Cadente
    if body4 > 0 and ushadow4 >= 2*body4 and lshadow4 <= 0.3*rng4 and two_bullish:
        return "PUT", "Estrela Cadente"

    # ============================================================
    # FORCA 7 — DOJI EM EXTREMO
    # ============================================================
    is_doji_atual = is_doji(o4, c4, h4, l4)
    if is_doji_atual and is_at_low and two_bearish:
        return "CALL", "Doji em Extremo Bullish"
    if is_doji_atual and is_at_high and two_bullish:
        return "PUT", "Doji em Extremo Bearish"

    # ============================================================
    # FORCA 6 — HARAMI e 3 VELAS
    # ============================================================
    # Harami Bullish: vela 3 grande baixista + vela 4 pequena bullish DENTRO do corpo
    if body3 > 0 and body4 > 0 and body3 > 2 * body4:
        # Harami Bullish: vela 3 baixista forte + vela 4 alta dentro do corpo
        if is_bearish(o3, c3) and is_bullish(o4, c4):
            if c4 < o3 and o4 > c3:  # corpo da 4 dentro do corpo da 3
                return "CALL", "Harami Bullish"
        # Harami Bearish: vela 3 altista forte + vela 4 baixa dentro do corpo
        if is_bullish(o3, c3) and is_bearish(o4, c4):
            if c4 > o3 and o4 < c3:  # corpo da 4 dentro do corpo da 3
                return "PUT", "Harami Bearish"

    # 3 Velas
    three_bullish = is_bullish(o[2],c[2]) and is_bullish(o[3],c[3]) and is_bullish(o[4],c[4])
    three_bearish = is_bearish(o[2],c[2]) and is_bearish(o[3],c[3]) and is_bearish(o[4],c[4])
    if three_bullish:
        return "CALL", "3 Velas Bullish"
    if three_bearish:
        return "PUT", "3 Velas Bearish"

    # ============================================================
    # FORCA 5 — MOMENTUM
    # ============================================================
    if rng4 > 0 and body4 / rng4 >= 0.50:
        if is_bullish(o4, c4) and is_bullish(o[3], c[3]):
            return "CALL", "Momentum Bullish"
        if is_bearish(o4, c4) and is_bearish(o[3], c[3]):
            return "PUT", "Momentum Bearish"

    # ============================================================
    # FORCA 4 — INSIDE BAR (continuacao)
    # ============================================================
    # Inside Bar: vela 3 grande + vela 4 contida (high4 < high3 E low4 > low3)
    # E vela 4 confirma direcao
    if h4 < h3 and l4 > l3 and rng3 > 0 and body3 / rng3 >= 0.4:
        # Continuacao bullish: vela 3 alta + inside bar + vela 4 fecha em alta
        if is_bullish(o3, c3) and is_bullish(o4, c4):
            return "CALL", "Inside Bar Bullish"
        # Continuacao bearish: vela 3 baixa + inside bar + vela 4 fecha em baixa
        if is_bearish(o3, c3) and is_bearish(o4, c4):
            return "PUT", "Inside Bar Bearish"

    # ============================================================
    # FORCA 3 — FECHAMENTO CONSECUTIVO e DIRECIONAL (sinal mais fraco)
    # ============================================================
    if c[3] > o[2] and c[4] > o[3]:
        return "CALL", "Fechamento Consecutivo Alta"
    if c[3] < o[2] and c[4] < o[3]:
        return "PUT", "Fechamento Consecutivo Baixa"

    if rng4 > 0 and body4 / rng4 >= 0.30:
        if is_bullish(o4, c4) and is_bullish(o[3], c[3]):
            return "CALL", "Direcional Alta"
        if is_bearish(o4, c4) and is_bearish(o[3], c[3]):
            return "PUT", "Direcional Baixa"

    return None, None


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

def volume_is_strong(candles):
    if len(candles) < 11:
        return False
    vols = [c["volume"] for c in candles[-11:-1]]
    zeros = sum(1 for v in vols if v <= 0)
    if zeros >= 5:
        return True
    avg_vol = sum(v for v in vols if v > 0) / max(len(vols) - zeros, 1)
    if avg_vol <= 0:   return True
    last_vol = candles[-1]["volume"]
    if last_vol <= 0:  return True
    return last_vol > avg_vol * 0.5


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
    if ema9 is None or ema21 is None: return None
    if ema9 > ema21:   return "UP"
    elif ema9 < ema21: return "DOWN"
    return None


# ---------------------------------------------------------------------------
# Noticias
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
    currency_map = {"GBP": ("USD","GBP"), "EUR": ("USD","EUR"), "AUD": ("USD","AUD")}
    relevant = currency_map.get(asset_key, ("USD",))
    try:
        news = get_news()
        now_utc = datetime.now(timezone.utc)
        for item in news:
            impact   = str(item.get("impact","")).lower()
            currency = str(item.get("currency",""))
            if impact not in ("high","red"): continue
            if currency not in relevant:     continue
            try:
                dt_str  = item.get("date","") + " " + item.get("time","")
                news_dt = datetime.strptime(dt_str.strip(), "%m-%d-%Y %I:%M%p")
                news_dt = news_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            diff = (news_dt - now_utc).total_seconds()
            if 0 <= diff <= 1800:
                mins = int(diff // 60)
                return_time = (now_utc + timedelta(seconds=diff+1800)).strftime("%H:%M")
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
        r = requests.post("https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode},
                          timeout=10)
        return r.ok
    except Exception as e:
        print("[TG] Erro: " + str(e))
        return False


def send_to(chat_id, text, parse_mode="HTML"):
    if not TELEGRAM_BOT_TOKEN:
        return False
    try:
        r = requests.post("https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
                          timeout=10)
        return r.ok
    except Exception as e:
        print("[TG] send_to erro: " + str(e))
        return False


def get_updates(offset=None):
    try:
        params = {"timeout": 30}
        if offset: params["offset"] = offset
        r = requests.get("https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/getUpdates",
                         params=params, timeout=35)
        return r.json().get("result", [])
    except Exception as e:
        print("[TG] getUpdates erro: " + str(e))
        return []


# ---------------------------------------------------------------------------
# Mensagens
# ---------------------------------------------------------------------------

def msg_signal(asset_key, direction, vol_strong, trend, ia_confianca=None, ia_risco=None,
               bet=None, wick_label=None, mom_label=None, vwap_label=None, window_label=None):
    label     = ASSETS[asset_key]["label"]
    vol_icon  = "Alto \u2705" if vol_strong else "Baixo \u26a0\ufe0f"
    ia_linha  = ""
    if ia_confianca is not None:
        ia_linha = "\n\U0001f916 IA: Validado \u2705 | Confianca: " + str(ia_confianca) + "%"
        if ia_risco: ia_linha += " | Risco: " + ia_risco
    bet_linha  = ("\n\U0001f4b0 Entrada: $" + str(bet))  if bet  is not None else ""
    vwap_linha = ("\n\U0001f3e6 VWAP: " + vwap_label)    if vwap_label else ""
    wick_linha = ("\n\U0001f56f Pavio: " + wick_label + " \u2705") if wick_label else ""
    mom_linha  = ("\n\U0001f4c8 Momentum: " + mom_label + " \u2705") if mom_label else ""
    win_linha  = ("\n\U0001f4cd Janela: " + window_label) if window_label else ""
    if direction == "CALL":
        ti = "Alta \u2705" if trend=="UP" else ("Baixa \u26a0\ufe0f" if trend=="DOWN" else "\u2014")
        return ("\U0001f7e2 <b>COMPRE \u2014 " + label + "</b>\n"
                "\u23f1 Tempo: 1 minuto" + win_linha + vwap_linha + wick_linha + mom_linha + ia_linha + "\n"
                "\U0001f4ca Volume: " + vol_icon + " | M5: " + ti + "\n"
                "\u27a1\ufe0f Clique no botao VERDE\n"
                "\u26a1 \xdaLTIMO AVISO \u2014 20 segundos!" + bet_linha)
    else:
        ti = "Baixa \u2705" if trend=="DOWN" else ("Alta \u26a0\ufe0f" if trend=="UP" else "\u2014")
        return ("\U0001f534 <b>VENDA \u2014 " + label + "</b>\n"
                "\u23f1 Tempo: 1 minuto" + win_linha + vwap_linha + wick_linha + mom_linha + ia_linha + "\n"
                "\U0001f4ca Volume: " + vol_icon + " | M5: " + ti + "\n"
                "\u27a1\ufe0f Clique no botao VERMELHO\n"
                "\u26a1 \xdaLTIMO AVISO \u2014 20 segundos!" + bet_linha)


def msg_new_day(date_str):
    return ("\U0001f7e2 <b>Novo dia iniciado!</b>\n"
            "Monitorando: GBP/USD OTC\n"
            "EUR e AUD: pausados para calibracao\n\n"
            "\U0001f4cd <b>3 Janelas de Operacao (BRT):</b>\n"
            "\U0001f305 05h00-06h00 \u2014 Abertura Londres (4 sinais)\n"
            "\u2b50 10h00-11h00 \u2014 PICO Londres+NY (4 sinais)\n"
            "\U0001f306 15h00-16h00 \u2014 Pos-almoco NY (4 sinais)\n\n"
            "Total: " + str(MAX_SIGNALS) + " sinais maximos hoje.")


def msg_window_open(window):
    return ("\U0001f6a8 <b>Janela ABERTA:</b> " + window["emoji"] + " " + window["label"] + "\n"
            "\u23f1 Duracao: " + ("0" + str(window["start_hour"]))[-2:] + ":" + ("0" + str(window["start_min"]))[-2:]
            + " \u2014 " + ("0" + str(window["end_hour"]))[-2:] + ":" + ("0" + str(window["end_min"]))[-2:] + " BRT\n"
            "\U0001f3af Maximo nesta janela: " + str(window["max_signals"]) + " sinais\n"
            "\U0001f50d Procurando confluencia agora...")


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
    stop_until  = 0.0
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
            send_telegram("\U0001f4c5 <b>Resumo de hoje \u2014 " + today_str + "</b>\n"
                          "Sinais: " + str(stats["total"]) + " | WIN: " + str(stats["wins"]) + " | LOSS: " + str(stats["losses"]) + "\n"
                          "Win rate: " + str(stats["win_rate"]) + "%\n"
                          "Padrao mais certeiro: " + stats["best_pattern"])


# ---------------------------------------------------------------------------
# VIP
# ---------------------------------------------------------------------------

def get_vip_members():
    now_iso = datetime.utcnow().isoformat()
    resp = _supa_call(lambda: supa.table("vip_members").select("telegram_id,nome").eq("ativo",True).gt("expira_em",now_iso).execute())
    return (resp.data or []) if resp else []


def add_vip(telegram_id, dias, nome=None):
    expira = (datetime.utcnow() + timedelta(days=dias)).isoformat()
    plano  = "mensal" if dias<=31 else ("trimestral" if dias<=92 else "semestral")
    resp = _supa_call(lambda: supa.table("vip_members").upsert({
        "telegram_id": telegram_id, "nome": nome or telegram_id,
        "plano": plano, "ativo": True, "expira_em": expira,
    }).execute())
    return resp is not None


def remove_vip(telegram_id):
    resp = _supa_call(lambda: supa.table("vip_members").update({"ativo":False}).eq("telegram_id",telegram_id).execute())
    return resp is not None


def list_vip_active():
    now_iso = datetime.utcnow().isoformat()
    resp = _supa_call(lambda: supa.table("vip_members").select("*").eq("ativo",True).gt("expira_em",now_iso).execute())
    return (resp.data or []) if resp else []


def send_signal_to_vips(text):
    count = 0
    for m in get_vip_members():
        tid = m.get("telegram_id")
        if tid and send_to(tid, text):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Pavios e Momentum
# ---------------------------------------------------------------------------

def analyze_wicks(candles):
    closed = [c for c in candles if c["is_closed"]]
    if len(closed) < 3: return None, None, 0
    results = []
    for candle in closed[-3:]:
        o, h, l, cc = candle["open"], candle["high"], candle["low"], candle["close"]
        body = abs(cc - o); fr = h - l
        if fr == 0 or body == 0: results.append((None,None,0)); continue
        lw = min(o,cc) - l; uw = h - max(o,cc)
        if lw >= 2.5*body:                   results.append(("CALL","Rejeicao forte de baixa",15))
        elif uw >= 2.5*body:                  results.append(("PUT","Rejeicao forte de alta",15))
        elif uw >= 1.5*body and cc < o:       results.append(("PUT","Fakeout de alta",20))
        elif lw >= 1.5*body and cc > o:       results.append(("CALL","Fakeout de baixa",20))
        else:                                  results.append((None,None,0))
    cc = sum(1 for r in results if r[0]=="CALL"); pc = sum(1 for r in results if r[0]=="PUT")
    if cc >= 2: best=max([r for r in results if r[0]=="CALL"],key=lambda x:x[2]); return best
    if pc >= 2: best=max([r for r in results if r[0]=="PUT"], key=lambda x:x[2]); return best
    if results and results[-1][0]: return results[-1]
    return None, None, 0


def analyze_momentum(candles):
    closed = [c for c in candles if c["is_closed"]]
    if len(closed) < 5: return None, None, 0
    last5  = closed[-5:]
    bodies = [abs(c["close"]-c["open"]) for c in last5]
    closes = [c["close"] for c in last5]; opens_=[c["open"] for c in last5]
    last=last5[-1]; fr=last["high"]-last["low"]; lb=bodies[-1]
    is_doji_l = (fr>0) and (lb/fr<0.10)
    bc=sum(1 for i in range(3) if closes[i]>opens_[i]); be=sum(1 for i in range(3) if closes[i]<opens_[i])
    if is_doji_l and bc>=2: return "PUT","Exaustao de alta (doji)",15
    if is_doji_l and be>=2: return "CALL","Exaustao de baixa (doji)",15
    avg=sum(bodies[:-1])/max(len(bodies[:-1]),1)
    if avg>0:
        if bodies[-1]>1.5*avg and closes[-1]>opens_[-1]: return "CALL","Aceleracao bullish",10
        if bodies[-1]>1.5*avg and closes[-1]<opens_[-1]: return "PUT","Aceleracao bearish",10
    return None, None, 0


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------

def update_vwap_for(asset_key, candles):
    vwap = asset_vwap[asset_key]
    ch = now_brt().hour
    if ch in (9,14,21) and vwap["reset_hour"] != ch:
        vwap.update({"cum_tp_vol":0.0,"cum_vol":0.0,"value":None,"reset_hour":ch})
        print("[VWAP] Reset " + asset_key + " as " + str(ch) + "h.")
    closed = [c for c in candles if c["is_closed"]]
    if not closed: return
    wc = closed[-60:]
    tp_vol = sum(((c["high"]+c["low"]+c["close"])/3.0)*c["volume"] for c in wc)
    vol    = sum(c["volume"] for c in wc)
    vwap["value"] = (tp_vol/vol) if vol > 0 else None


def get_vwap_signal_for(asset_key, direction, candles):
    update_vwap_for(asset_key, candles)
    vv = asset_vwap[asset_key]["value"]
    if not vv: return None, 0.0, 0, False
    closed = [c for c in candles if c["is_closed"]]
    if not closed: return None, 0.0, 0, False
    pp = closed[-1]["close"]
    dist = (pp - vv) / vv * 100.0; ad = abs(dist)
    if ad <= 0.05: return "Neutro", round(dist,4), 0, False
    db = 10 if ad>0.2 else (5 if ad>0.1 else 0)
    if direction=="CALL" and pp<vv: return "Abaixo (zona COMPRA)", round(dist,4), 10+db, False
    if direction=="PUT"  and pp>vv: return "Acima (zona VENDA)",   round(dist,4), 10+db, False
    if direction=="CALL" and pp>vv:
        if ad>0.3: return "Acima (resistencia)", round(dist,4), 0, True
        return "Acima (resistencia)", round(dist,4), -5, False
    if direction=="PUT"  and pp<vv:
        if ad>0.3: return "Abaixo (suporte)", round(dist,4), 0, True
        return "Abaixo (suporte)", round(dist,4), -5, False
    return None, 0.0, 0, False


def get_current_vwap_for(asset_key):
    with data_lock:
        m1 = list(asset_m1[asset_key])
    if not m1: return None, None
    update_vwap_for(asset_key, m1)
    closed = [c for c in m1 if c["is_closed"]]
    vv = asset_vwap[asset_key]["value"]
    if closed and vv: return vv, closed[-1]["close"]
    if m1 and vv:     return vv, m1[-1]["close"]
    return None, None


# ---------------------------------------------------------------------------
# Validacao Claude
# ---------------------------------------------------------------------------

def validate_with_claude(direction, pattern, vol_strong, trend, candles_m1):
    if not ANTHROPIC_API_KEY:
        return True, 70, "IA desativada", "MEDIO"
    try:
        closes = [c["close"] for c in candles_m1[-10:] if c["is_closed"]]
        prompt = (
            "Voce e um analisador de sinais de trading binario de 1 minuto.\n"
            "Sinal: " + direction + " | Padrao: " + str(pattern) + "\n"
            "Volume forte: " + str(vol_strong) + " | Tendencia M5: " + str(trend) + "\n"
            "Closes M1: " + str(closes) + "\n\n"
            "Responda APENAS com JSON valido (sem markdown):\n"
            '{"validar": true/false, "confianca": 0-100, "motivo": "string", "risco": "BAIXO/MEDIO/ALTO"}'
        )
        c = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = c.messages.create(model="claude-3-haiku-20240307", max_tokens=256,
                                messages=[{"role":"user","content":prompt}])
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            lines = raw.split("\n"); raw = "\n".join(lines[1:]); raw = raw.rsplit("```",1)[0].strip()
        res = json.loads(raw)
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
        send_to(chat_id,
            "\U0001f44b Ola! Sou o <b>Bot Multi-Ativo BOT-N9</b>.\n\n"
            "<b>Ativo:</b> GBP/USD OTC (foco total)\n"
            "<b>Fonte:</b> Yahoo Finance\n"
            "<b>Operacao:</b> 3 Janelas Premium \U0001f3af\n\n"
            "\U0001f305 05h00-06h00 \u2014 Abertura Londres\n"
            "\u2b50 10h00-11h00 \u2014 PICO Londres+NY\n"
            "\U0001f306 15h00-16h00 \u2014 Pos-almoco NY\n\n"
            "<b>Max.:</b> 4 sinais por janela (12 totais/dia)\n\n"
            "/sinal | /status | /perdi | /ganhei | /placar | /relatorio\n"
            "<b>Admin:</b> /addvip | /removevip | /listvip")

    elif text == "/status":
        brt_now = now_brt()
        active = get_active_window()
        if active:
            sessao_lin = active["emoji"] + " <b>JANELA ATIVA:</b> " + active["label"] + " (" + str(window_signal_count[active["name"]]) + "/" + str(active["max_signals"]) + ")"
        else:
            secs, next_w = time_until_next_window()
            mins = secs // 60
            sessao_lin = "\U0001f4a4 Fora de janela. Proxima: " + next_w["emoji"] + " " + next_w["label"] + " em " + str(mins) + "min"

        up = timedelta(seconds=int(time.time()-bot_start_time))
        h_up = int(up.total_seconds()//3600); m_up = int((up.total_seconds()%3600)//60)
        paused = ""
        if time.time() < stop_until:
            resume = datetime.fromtimestamp(stop_until,tz=timezone.utc)+BRT_OFFSET
            paused = "\n\U0001f6d1 Pausado ate " + resume.strftime("%H:%M")
        with data_lock:
            gbp_m1 = len([c for c in asset_m1["GBP"] if c["is_closed"]])
        supabase_status = "Ativo \u2705" if supa is not None else "Desativado \u26a0\ufe0f"

        contagem_janelas = "\n\n\U0001f4ca <b>Contagem por janela hoje:</b>"
        for w in TRADING_WINDOWS:
            contagem_janelas += "\n" + w["emoji"] + " " + w["label"] + ": " + str(window_signal_count[w["name"]]) + "/" + str(w["max_signals"])

        msg = ("<b>Status BOT-N9 (3 Janelas)</b>\n"
               "Hora BRT: " + brt_now.strftime("%H:%M:%S") + "\n"
               + sessao_lin + "\n"
               "Total hoje: " + str(daily_signals) + "/" + str(MAX_SIGNALS) + " sinais\n"
               "Perdas seguidas: " + str(consecutive_losses) + "/" + str(MAX_LOSSES_AM) + "\n"
               "\U0001f4b0 Proxima entrada: $" + str(current_bet) + "\n"
               "Uptime: " + str(h_up) + "h " + str(m_up) + "m"
               + paused + "\n"
               "\n\U0001f4ca Candles M1: GBP=" + str(gbp_m1) + "\n"
               "\U0001f5c4 Supabase: " + supabase_status
               + contagem_janelas)
        vip_list = list_vip_active()
        vip_lines = "\n\n\U0001f451 <b>VIPs ativos:</b> " + str(len(vip_list))
        if vip_list:
            for v in sorted(vip_list, key=lambda x: x.get("expira_em",""))[:3]:
                exp = v.get("expira_em","?")[:10] if v.get("expira_em") else "?"
                vip_lines += "\n\u2022 " + (v.get("nome") or v["telegram_id"]) + " \u2014 " + exp
        send_to(chat_id, msg + vip_lines)

    elif text == "/perdi":
        triggered, resume_time = record_loss()
        if triggered == "6":
            msg = ("\U0001f6d1 <b>6 tentativas sem sucesso.</b>\nAte amanha!\n"
                   "\U0001f4b0 Valor resetado: $" + str(BASE_BET_DEMO))
        elif triggered == "3":
            msg = ("\U0001f6d1 3 perdas seguidas.\nPausando 60min.\n"
                   "Proxima sessao: " + resume_time + "\n"
                   "\U0001f4b0 Proxima entrada: $" + str(current_bet))
        else:
            msg = ("\U0001f4c9 Perda registrada. Seguidas: " + str(consecutive_losses) + "/" + str(MAX_LOSSES_AM) + "\n"
                   "\U0001f4b0 Proxima entrada: $" + str(current_bet))
        send_to(chat_id, msg)

    elif text == "/ganhei":
        record_win()
        msg_ganhei = (
            "\U0001f3c6 <b>Vitoria registrada!</b>\n"
            "Anti-Martingale: sessao encerrada com lucro.\n"
            "\U0001f4b0 Proxima entrada: $" + str(BASE_BET_DEMO) + "\n"
            "\U0001f4ca Placar hoje: " + str(session_wins) + " wins / " + str(session_losses) + " losses\n"
            "\u26a1 Aguarde o proximo sinal!"
        )
        send_to(chat_id, msg_ganhei)

    elif text == "/placar":
        total = session_wins + session_losses
        taxa  = int(session_wins/total*100) if total > 0 else 0
        send_to(chat_id,
            "\U0001f4ca <b>Placar da sessao</b>\n"
            "\u2705 Vitorias: " + str(session_wins) + "\n"
            "\u274c Derrotas: " + str(session_losses) + "\n"
            "\U0001f4c8 Taxa: " + str(taxa) + "%")

    elif text == "/relatorio":
        stats = get_weekly_stats()
        if stats is None or stats["total"] == 0:
            send_to(chat_id, "\U0001f4ca Sem dados para o relatorio desta semana.")
            return
        profit = round(stats["wins"]*0.8 - stats["losses"]*1.0, 2)
        send_to(chat_id,
            "\U0001f4ca <b>Relatorio da Semana</b>\n"
            "Sinais: " + str(stats["total"]) + "\n"
            "\u2705 WIN: " + str(stats["wins"]) + " (" + str(stats["win_rate"]) + "%)\n"
            "\u274c LOSS: " + str(stats["losses"]) + " (" + str(100-stats["win_rate"]) + "%)\n"
            "\U0001f3c6 Melhor padrao: " + stats["best_pattern"] + "\n"
            "\u23f0 Melhor sessao: " + stats["best_session"] + "\n"
            "\U0001f4b0 Se operado com $10: " + ("+" if profit>=0 else "") + str(profit))

    elif text.startswith("/addvip"):
        if chat_id != TELEGRAM_CHAT_ID:
            send_to(chat_id, "\u26d4 Acesso negado."); return
        parts = text.split()
        if len(parts) < 3:
            send_to(chat_id, "Uso: /addvip [telegram_id] [dias]"); return
        tid = parts[1]
        try: dias = int(parts[2])
        except ValueError:
            send_to(chat_id, "Dias deve ser numero."); return
        if add_vip(tid, dias):
            send_to(tid, "\U0001f389 Bem-vindo ao VIP!\nSinais de GBP/USD OTC nas 3 janelas premium.\n\u2705 Acesso por " + str(dias) + " dias.")
            send_to(chat_id, "\u2705 VIP ativado: " + tid + " por " + str(dias) + " dias.")
        else:
            send_to(chat_id, "\u274c Erro ao ativar VIP (Supabase indisponivel).")

    elif text.startswith("/removevip"):
        if chat_id != TELEGRAM_CHAT_ID:
            send_to(chat_id, "\u26d4 Acesso negado."); return
        parts = text.split()
        if len(parts) < 2:
            send_to(chat_id, "Uso: /removevip [telegram_id]"); return
        send_to(chat_id, "\u2705 VIP removido: " + parts[1] if remove_vip(parts[1]) else "\u274c Erro ao remover VIP.")

    elif text == "/listvip":
        if chat_id != TELEGRAM_CHAT_ID:
            send_to(chat_id, "\u26d4 Acesso negado."); return
        members = list_vip_active()
        if not members:
            send_to(chat_id, "\U0001f4cb Nenhum VIP ativo."); return
        lines = ["\U0001f451 <b>VIPs ativos:</b>"]
        for m in members:
            exp = m.get("expira_em","?")[:10] if m.get("expira_em") else "?"
            lines.append("\u2022 " + (m.get("nome") or m["telegram_id"]) + " | ID: " + m["telegram_id"] + " | Expira: " + exp)
        send_to(chat_id, "\n".join(lines))

    elif text == "/sinal":
        active = get_active_window()
        if not active:
            secs, next_w = time_until_next_window()
            mins = secs // 60
            send_to(chat_id, "\u23f3 Fora de janela. Proxima: " + next_w["emoji"] + " " + next_w["label"] + " em " + str(mins) + "min.")
            return
        send_to(chat_id, "\U0001f50d Analisando GBP/USD OTC na janela " + active["emoji"] + " " + active["label"] + "...")
        def _run_sinal():
            sent = check_asset_signal("GBP", forced=True)
            if not sent:
                send_to(chat_id, "\u26a0\ufe0f Nenhum sinal no momento.\nUse /status para mais detalhes.")
        threading.Thread(target=_run_sinal, daemon=True).start()


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
# Loop de sinais
# ---------------------------------------------------------------------------

def check_asset_signal(asset_key, forced=False):
    """Verifica e envia sinal. Retorna True se sinal enviado.
    forced=True ignora janela (usado pelo /sinal manual)."""
    global last_signal_time, current_bet, daily_signals
    ts  = now_brt().strftime("%H:%M:%S BRT")
    now = time.time()
    diag = "[DIAG " + asset_key + "]"

    # Verifica janela ativa (exceto se forced)
    active_window = get_active_window()
    if not forced:
        if active_window is None:
            return False
        if window_signal_count[active_window["name"]] >= active_window["max_signals"]:
            print("[" + ts + "] " + diag + " Janela " + active_window["name"] + " ja atingiu limite (" + str(active_window["max_signals"]) + ")")
            return False

    if now - last_signal_time[asset_key] < COOLDOWN_SECS:
        remaining = int(COOLDOWN_SECS - (now - last_signal_time[asset_key]))
        print("[" + ts + "] " + diag + " Cooldown: " + str(remaining) + "s")
        return False

    blocked, mins, return_time = check_news_block(asset_key)
    if blocked:
        print("[" + ts + "] " + diag + " BLOQUEADO: Noticia em " + str(mins) + "min")
        send_telegram("\u26a0\ufe0f Noticia (" + ASSETS[asset_key]["label"] + ") em "
                      + str(mins) + "min. Retorno: " + return_time)
        return False

    with data_lock:
        m1_snap = list(asset_m1[asset_key])

    closed_count = len([c for c in m1_snap if c["is_closed"]])
    if closed_count < 5:
        print("[" + ts + "] " + diag + " Aguardando candles M1 (" + str(closed_count) + "/5).")
        return False

    closed_m1 = [c for c in m1_snap if c["is_closed"]]
    all_m1    = closed_m1 + ([m1_snap[-1]] if m1_snap and not m1_snap[-1]["is_closed"] else [])

    direction, pattern = detect_pattern(all_m1)
    padrao_str = str(pattern) if direction else "Nenhum"
    if direction is None:
        print("[" + ts + "] " + diag + " Padrao: Nenhum | BLOQUEADO: sem padrao de vela")
        return False

    vol_ok = volume_is_strong(all_m1)
    if not vol_ok:
        print("[" + ts + "] " + diag + " Padrao: " + padrao_str + " (" + direction + ")"
              + " | Volume: FRACO | BLOQUEADO: volume insuficiente")
        return False

    filtros_ok = 0
    f_detalhes = []

    trend = m5_trend_for(asset_key)
    if trend is not None:
        if direction == "CALL" and trend == "UP":
            filtros_ok += 1
            f_detalhes.append("M5:Alta+")
        elif direction == "PUT" and trend == "DOWN":
            filtros_ok += 1
            f_detalhes.append("M5:Baixa+")
        else:
            f_detalhes.append("M5:Contra-")
    else:
        filtros_ok += 1
        f_detalhes.append("M5:SemDados+")

    wick_dir, wick_label, wick_bonus = analyze_wicks(all_m1)
    if wick_dir is None or wick_dir == direction:
        filtros_ok += 1
        f_detalhes.append("Wick:" + (wick_label if wick_label else "Neutro") + "+")
    else:
        f_detalhes.append("Wick:Contra-")

    mom_dir, mom_label, mom_bonus = analyze_momentum(all_m1)
    if mom_dir is None or mom_dir == direction:
        filtros_ok += 1
        f_detalhes.append("Mom:" + (mom_label if mom_label else "Neutro") + "+")
    else:
        f_detalhes.append("Mom:Contra-")

    vwap_label, vwap_dist, vwap_bonus, vwap_ignore = get_vwap_signal_for(asset_key, direction, m1_snap)
    filtros_ok += 1
    if vwap_ignore:
        f_detalhes.append("VWAP:Neutro+")
    else:
        f_detalhes.append("VWAP:" + (vwap_label if vwap_label else "Neutro") + "+")

    confianca = 50
    if vol_ok:              confianca += 25
    if trend is not None:   confianca += 15
    confianca += wick_bonus + mom_bonus
    confianca = min(confianca + vwap_bonus, 100)

    ts2 = now_brt().strftime("%H:%M:%S BRT")
    ia_valido, ia_confianca, ia_motivo, ia_risco = validate_with_claude(
        direction, pattern, vol_ok, trend, m1_snap)

    if ia_valido:
        filtros_ok += 1
        f_detalhes.append("IA:Valido+")
    else:
        f_detalhes.append("IA:Invalido(" + str(ia_motivo)[:20] + ")-")

    # Confianca minima da IA varia conforme forca do padrao
    ia_min_confianca = get_required_ia_confidence(pattern)
    if ia_confianca >= ia_min_confianca:
        filtros_ok += 1
        f_detalhes.append("IA%:" + str(ia_confianca) + ">=" + str(ia_min_confianca) + "+")
    else:
        f_detalhes.append("IA%:" + str(ia_confianca) + "<" + str(ia_min_confianca) + "-")

    # Score minimo dinamico baseado na forca do padrao
    forca_padrao = get_pattern_strength(pattern)
    confluencia_min = get_required_confluence(pattern)

    diag_linha = (diag + " Padrao: " + padrao_str + " [F" + str(forca_padrao) + "] (" + direction + ")"
                  + " | Vol: OK"
                  + " | " + " ".join(f_detalhes)
                  + " | Score: " + str(filtros_ok) + "/6 (min " + str(confluencia_min) + ")")

    if filtros_ok < confluencia_min:
        print("[" + ts + "] " + diag_linha + " | BLOQUEADO (padrao F" + str(forca_padrao)
              + " exige " + str(confluencia_min) + "/6, faltou "
              + str(confluencia_min - filtros_ok) + " filtro(s))")
        return False

    print("[" + ts2 + "] " + diag_linha + " | >>> SINAL DISPARADO <<<")

    window_label_text = (active_window["emoji"] + " " + active_window["label"]) if active_window else "Manual"

    signal_text = msg_signal(asset_key, direction, vol_ok, trend,
                             ia_confianca, ia_risco, bet=current_bet,
                             wick_label=wick_label, mom_label=mom_label, vwap_label=vwap_label,
                             window_label=window_label_text)

    if send_telegram(signal_text):
        last_signal_time[asset_key] = time.time()
        daily_signals += 1
        if active_window:
            window_signal_count[active_window["name"]] += 1
        sessao_log = active_window["name"] if active_window else "manual"
        print("[" + ts2 + "] [" + sessao_log + "] [" + asset_key + "] " + direction
              + " (" + str(pattern) + ") #" + str(daily_signals)
              + " IA=" + str(ia_confianca) + "%")
        vip_n = send_signal_to_vips(signal_text)
        if vip_n > 0: print("[VIP] " + str(vip_n) + " notificado(s).")
        log_signal(ativo=ASSETS[asset_key]["label"], direcao=direction, padrao=pattern,
                   confianca=ia_confianca, volume_confirmado=vol_ok,
                   m5_confirmado=(trend is not None), sessao=sessao_log, validado_ia=True,
                   wick_signal=wick_label, momentum_signal=mom_label,
                   vwap_signal=vwap_label, vwap_distance=vwap_dist)
        return True
    else:
        print("[" + ts2 + "] Falha envio " + asset_key + ".")
        return False


def signal_loop():
    global daily_signals, daily_reset_date, daily_notified

    print("Loop de sinais iniciado (BOT-N9 - 3 Janelas Premium).")

    while True:
        try:
            ts    = now_brt().strftime("%H:%M:%S BRT")
            now   = time.time()
            t_now = now_brt()
            today_str = t_now.strftime("%d/%m/%Y")

            check_daily_report()

            # Reset diario
            if daily_reset_date != today_str:
                daily_signals    = 0
                daily_notified   = False
                daily_reset_date = today_str
                # Reseta contagem de cada janela
                for w in TRADING_WINDOWS:
                    window_signal_count[w["name"]] = 0
                # Reseta flags de notificacao de janela
                for k in _window_open_notified:
                    _window_open_notified[k] = ""
                print("[" + ts + "] Novo dia: contagem de sinais e janelas resetadas.")

            # Aviso de novo dia
            if t_now.hour == 0 and t_now.minute == 0 and not daily_notified:
                daily_notified = True
                send_telegram(msg_new_day(today_str))
                print("[" + ts + "] Aviso de novo dia enviado.")

            if now < stop_until:
                resume = datetime.fromtimestamp(stop_until, tz=timezone.utc) + BRT_OFFSET
                print("[" + ts + "] Pausado ate " + resume.strftime("%H:%M") + ".")
                time.sleep(CHECK_INTERVAL); continue

            if daily_signals >= MAX_SIGNALS:
                # Atingiu maximo do dia, sleep ate amanha
                secs_to_midnight = ((24 - t_now.hour) * 3600) - (t_now.minute * 60) - t_now.second
                print("[" + ts + "] Max diario atingido. Sleep " + str(secs_to_midnight // 60) + "min ate reset.")
                time.sleep(min(secs_to_midnight + 5, 1800))
                continue

            # Verifica janela ativa
            active = get_active_window()

            if active is None:
                # Fora de janela: sleep ate proxima
                secs, next_w = time_until_next_window()
                mins = secs // 60
                print("[" + ts + "] Fora de janela. Proxima: " + next_w["emoji"] + " " + next_w["label"] + " em " + str(mins) + "min. Dormindo...")
                # Sleep no maximo 5 min para reagir bem ao comando /status
                time.sleep(min(secs, 300))
                continue

            # Janela ativa! Notifica abertura uma vez
            if _window_open_notified[active["name"]] != today_str:
                _window_open_notified[active["name"]] = today_str
                send_telegram(msg_window_open(active))
                print("[" + ts + "] Janela " + active["name"] + " ABERTA - aviso enviado.")

            # Verifica se ja atingiu limite da janela
            if window_signal_count[active["name"]] >= active["max_signals"]:
                print("[" + ts + "] Janela " + active["name"] + " esgotada (" + str(active["max_signals"]) + " sinais). Aguardando proxima.")
                time.sleep(CHECK_INTERVAL)
                continue

            # Cooldown geral
            all_in_cd = all(now - last_signal_time[ak] < COOLDOWN_SECS for ak in ASSETS)
            if all_in_cd:
                min_r = min(int(COOLDOWN_SECS-(now-last_signal_time[ak])) for ak in ASSETS)
                print("[" + ts + "] " + active["name"] + " - Cooldown: " + str(min_r) + "s")
                time.sleep(CHECK_INTERVAL); continue

            # Tenta gerar sinal
            signal_sent = False
            for ak in ASSETS:
                if window_signal_count[active["name"]] >= active["max_signals"]: break
                if check_asset_signal(ak):
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
    print("Bot BOT-N9 iniciado! (3 Janelas Premium)")
    print("GBP/USD OTC | Yahoo Finance")
    print("Janelas: 05h-06h | 10h-11h | 15h-16h BRT")
    print("Max: 4 sinais/janela = 12 sinais/dia")
    print("=" * 60)
    init_supabase()

    # Yahoo Finance apenas para GBP (EUR e AUD pausados)
    for ak in ["GBP"]:
        threading.Thread(target=yahoo_update_loop, args=(ak,), daemon=True).start()
        time.sleep(1)

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
