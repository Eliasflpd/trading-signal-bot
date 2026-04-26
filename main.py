"""
============================================================
 BOT-N9 — Sinais IQ Option (3 Janelas + 2 Moedas)
 Ativos: GBP/USD OTC + EUR/USD OTC
 Fonte: Twelve Data API
 Estratégia: Price Action + Confluência mínima 2/6
============================================================
"""

import os
import asyncio
import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from telegram import Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update
from supabase import create_client, Client

# ============================================================
# CONFIGURAÇÃO
# ============================================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")

BRT = ZoneInfo("America/Sao_Paulo")

# ATIVOS MONITORADOS — 2 MOEDAS
ATIVOS = {
    "GBP/USD OTC": "GBP/USD",
    "EUR/USD OTC": "EUR/USD",
}

# JANELAS DE OPERAÇÃO (BRT)
JANELAS = [
    {"nome": "🌅 Abertura de Londres", "inicio": "05:00", "fim": "06:00", "max_sinais": 4},
    {"nome": "⭐ PICO Londres+NY", "inicio": "10:00", "fim": "11:00", "max_sinais": 4},
    {"nome": "🌆 Pós-almoço NY", "inicio": "15:00", "fim": "16:00", "max_sinais": 4},
]

MIN_CONFLUENCIA = 2  # mínimo de filtros confirmando
COOLDOWN_SEGUNDOS = 180  # 3 min entre sinais por ativo
MAX_SINAIS_DIA = 24  # 4 sinais × 3 janelas × 2 ativos

# Estado runtime
ultimo_sinal_por_ativo = {ativo: None for ativo in ATIVOS}
sinais_por_janela = {j["nome"]: 0 for j in JANELAS}
sinais_hoje = 0
data_atual = None

# Supabase
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("✅ Supabase conectado")
    except Exception as e:
        logger.warning(f"⚠️ Supabase não conectou: {e}")

# ============================================================
# UTILS DE HORÁRIO E JANELAS
# ============================================================

def agora_brt():
    return datetime.now(BRT)

def janela_ativa():
    """Retorna a janela ativa agora ou None."""
    now = agora_brt()
    hh_mm = now.strftime("%H:%M")
    for j in JANELAS:
        if j["inicio"] <= hh_mm < j["fim"]:
            return j
    return None

def proxima_janela():
    """Retorna (nome_janela, minutos_ate_abrir)."""
    now = agora_brt()
    hh_mm = now.strftime("%H:%M")
    for j in JANELAS:
        if hh_mm < j["inicio"]:
            h, m = map(int, j["inicio"].split(":"))
            alvo = now.replace(hour=h, minute=m, second=0, microsecond=0)
            mins = int((alvo - now).total_seconds() / 60)
            return j["nome"], mins
    # passou de todas hoje → primeira janela de amanhã
    j = JANELAS[0]
    h, m = map(int, j["inicio"].split(":"))
    alvo = (now + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
    mins = int((alvo - now).total_seconds() / 60)
    return j["nome"] + " (amanhã)", mins

def reset_diario_se_necessario():
    global sinais_hoje, sinais_por_janela, data_atual
    hoje = agora_brt().date()
    if data_atual != hoje:
        sinais_hoje = 0
        sinais_por_janela = {j["nome"]: 0 for j in JANELAS}
        data_atual = hoje
        logger.info(f"🟢 Novo dia: {hoje}")
        return True
    return False

# ============================================================
# FONTE DE DADOS — TWELVE DATA API (com retry e validação)
# ============================================================

def buscar_velas(symbol, tentativas=3):
    """
    Busca velas do Twelve Data API com retry.
    Retorna DataFrame com colunas Open/High/Low/Close em ordem
    cronológica ascendente, ou None se falhar.
    """
    if not TWELVEDATA_API_KEY:
        logger.error(f"[{symbol}] TWELVEDATA_API_KEY não configurada!")
        return None

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": "5min",
        "outputsize": 100,
        "apikey": TWELVEDATA_API_KEY,
    }

    for tentativa in range(1, tentativas + 1):
        try:
            resp = requests.get(url, params=params, timeout=15)

            if resp.status_code != 200:
                logger.error(f"[{symbol}] Tentativa {tentativa}: HTTP {resp.status_code}")
                continue

            data = resp.json()

            if data.get("status") == "error":
                motivo = data.get("message", "erro desconhecido")
                logger.error(f"[{symbol}] Tentativa {tentativa}: Twelve Data falhou: {motivo}")
                continue

            values = data.get("values", [])

            if len(values) < 30:
                logger.error(f"[{symbol}] Tentativa {tentativa}: Twelve Data falhou: apenas {len(values)} velas (mínimo 30)")
                continue

            # Converter para DataFrame
            df = pd.DataFrame(values)
            df = df.rename(columns={
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
            })
            df["Open"] = pd.to_numeric(df["Open"])
            df["High"] = pd.to_numeric(df["High"])
            df["Low"] = pd.to_numeric(df["Low"])
            df["Close"] = pd.to_numeric(df["Close"])

            # Twelve Data retorna desc → inverter para ordem cronológica asc
            df = df.iloc[::-1].reset_index(drop=True)

            logger.info(f"[{symbol}] ✅ {len(df)} velas obtidas via Twelve Data")
            return df

        except Exception as e:
            logger.error(f"[{symbol}] Tentativa {tentativa} falhou: {e}")

    logger.error(f"[{symbol}] ❌ Todas as {tentativas} tentativas falharam")
    return None

# ============================================================
# INDICADORES TÉCNICOS
# ============================================================

def calc_rsi(close, periodo=14):
    delta = close.diff()
    ganho = delta.where(delta > 0, 0).rolling(periodo).mean()
    perda = -delta.where(delta < 0, 0).rolling(periodo).mean()
    rs = ganho / perda.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calc_ema(close, periodo):
    return close.ewm(span=periodo, adjust=False).mean()

def calc_macd(close):
    ema12 = calc_ema(close, 12)
    ema26 = calc_ema(close, 26)
    macd = ema12 - ema26
    sinal = calc_ema(macd, 9)
    hist = macd - sinal
    return macd, sinal, hist

def calc_bollinger(close, periodo=20, desvios=2):
    media = close.rolling(periodo).mean()
    std = close.rolling(periodo).std()
    sup = media + desvios * std
    inf = media - desvios * std
    return sup, media, inf

# ============================================================
# PADRÕES DE VELA
# ============================================================

def detectar_padrao_alta(df):
    """Retorna padrão de alta detectado na última vela ou None."""
    if len(df) < 3:
        return None
    v = df.iloc[-1]  # vela atual
    a = df.iloc[-2]  # vela anterior

    corpo_v = abs(v["Close"] - v["Open"])
    sombra_inf = min(v["Open"], v["Close"]) - v["Low"]
    sombra_sup = v["High"] - max(v["Open"], v["Close"])

    # MARTELO (corpo pequeno, sombra inferior longa)
    if corpo_v > 0 and sombra_inf >= 2 * corpo_v and sombra_sup < corpo_v:
        return "Martelo"

    # ENGOLFO DE ALTA
    if a["Close"] < a["Open"] and v["Close"] > v["Open"]:
        if v["Close"] > a["Open"] and v["Open"] < a["Close"]:
            return "Engolfo de Alta"

    # PIN BAR DE ALTA
    if corpo_v > 0 and sombra_inf >= 2 * corpo_v:
        return "Pin Bar Alta"

    return None

def detectar_padrao_baixa(df):
    if len(df) < 3:
        return None
    v = df.iloc[-1]
    a = df.iloc[-2]

    corpo_v = abs(v["Close"] - v["Open"])
    sombra_inf = min(v["Open"], v["Close"]) - v["Low"]
    sombra_sup = v["High"] - max(v["Open"], v["Close"])

    # SHOOTING STAR
    if corpo_v > 0 and sombra_sup >= 2 * corpo_v and sombra_inf < corpo_v:
        return "Shooting Star"

    # ENGOLFO DE BAIXA
    if a["Close"] > a["Open"] and v["Close"] < v["Open"]:
        if v["Open"] > a["Close"] and v["Close"] < a["Open"]:
            return "Engolfo de Baixa"

    # PIN BAR DE BAIXA
    if corpo_v > 0 and sombra_sup >= 2 * corpo_v:
        return "Pin Bar Baixa"

    return None

# ============================================================
# ANÁLISE DE CONFLUÊNCIA
# ============================================================

def analisar(df, ativo):
    """
    Analisa o DataFrame e retorna dict com sinal ou None.
    Usa confluência mínima de MIN_CONFLUENCIA filtros (de 6 possíveis).
    """
    if df is None or len(df) < 50:
        logger.info(f"[{ativo}] DF insuficiente para análise")
        return None

    close = df["Close"]
    rsi = calc_rsi(close).iloc[-1]
    ema9 = calc_ema(close, 9).iloc[-1]
    ema21 = calc_ema(close, 21).iloc[-1]
    ema50 = calc_ema(close, 50).iloc[-1]
    macd_line, macd_sig, _ = calc_macd(close)
    macd_atual = macd_line.iloc[-1]
    macd_sinal = macd_sig.iloc[-1]
    bol_sup, bol_med, bol_inf = calc_bollinger(close)
    preco_atual = close.iloc[-1]

    pad_alta = detectar_padrao_alta(df)
    pad_baixa = detectar_padrao_baixa(df)

    # ============ ANÁLISE DE COMPRA ============
    score_call = 0
    motivos_call = []

    if rsi < 35:
        score_call += 1
        motivos_call.append(f"RSI sobrevendido ({rsi:.1f})")

    if macd_atual > macd_sinal:
        score_call += 1
        motivos_call.append("MACD cruzando alta")

    if preco_atual <= bol_inf.iloc[-1]:
        score_call += 1
        motivos_call.append("Preço na banda inferior")

    if ema9 > ema21:
        score_call += 1
        motivos_call.append("EMA9 > EMA21")

    if ema21 > ema50:
        score_call += 1
        motivos_call.append("Tendência alta (EMA21>50)")

    if pad_alta:
        score_call += 1
        motivos_call.append(f"Padrão {pad_alta}")

    # ============ ANÁLISE DE VENDA ============
    score_put = 0
    motivos_put = []

    if rsi > 65:
        score_put += 1
        motivos_put.append(f"RSI sobrecomprado ({rsi:.1f})")

    if macd_atual < macd_sinal:
        score_put += 1
        motivos_put.append("MACD cruzando baixa")

    if preco_atual >= bol_sup.iloc[-1]:
        score_put += 1
        motivos_put.append("Preço na banda superior")

    if ema9 < ema21:
        score_put += 1
        motivos_put.append("EMA9 < EMA21")

    if ema21 < ema50:
        score_put += 1
        motivos_put.append("Tendência baixa (EMA21<50)")

    if pad_baixa:
        score_put += 1
        motivos_put.append(f"Padrão {pad_baixa}")

    logger.info(f"[{ativo}] 📊 CALL={score_call}/6 PUT={score_put}/6 RSI={rsi:.1f}")

    # Decidir sinal
    if score_call >= MIN_CONFLUENCIA and score_call > score_put:
        return {
            "ativo": ativo,
            "direcao": "🟢 COMPRA (CALL)",
            "score": score_call,
            "motivos": motivos_call,
            "preco": float(preco_atual),
            "rsi": float(rsi),
        }

    if score_put >= MIN_CONFLUENCIA and score_put > score_call:
        return {
            "ativo": ativo,
            "direcao": "🔴 VENDA (PUT)",
            "score": score_put,
            "motivos": motivos_put,
            "preco": float(preco_atual),
            "rsi": float(rsi),
        }

    return None

# ============================================================
# ENVIO DE SINAL
# ============================================================

async def enviar_sinal(bot: Bot, sinal: dict, janela: dict):
    motivos_txt = "\n".join(f"• {m}" for m in sinal["motivos"])
    msg = (
        f"⚡ *SINAL — {janela['nome']}*\n\n"
        f"💱 Ativo: *{sinal['ativo']}*\n"
        f"🎯 Direção: *{sinal['direcao']}*\n"
        f"⏱ Expiração: *5 minutos (M5)*\n\n"
        f"💰 Preço: `{sinal['preco']:.5f}`\n"
        f"📈 Confluência: *{sinal['score']}/6*\n\n"
        f"*Motivos:*\n{motivos_txt}\n\n"
        f"⚠️ Trading envolve risco. Use só capital que pode perder."
    )

    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=msg,
            parse_mode="Markdown",
        )
        logger.info(f"✅ Sinal enviado: {sinal['ativo']} {sinal['direcao']}")

        # Salvar no Supabase
        if supabase:
            try:
                supabase.table("sinais").insert({
                    "ativo": sinal["ativo"],
                    "direcao": sinal["direcao"],
                    "score": sinal["score"],
                    "preco": sinal["preco"],
                    "rsi": sinal["rsi"],
                    "janela": janela["nome"],
                    "criado_em": agora_brt().isoformat(),
                }).execute()
            except Exception as e:
                logger.warning(f"Supabase insert falhou: {e}")

    except Exception as e:
        logger.error(f"❌ Falha ao enviar sinal: {e}")

# ============================================================
# LOOP PRINCIPAL DE ANÁLISE
# ============================================================

async def loop_analise(bot: Bot):
    global sinais_hoje, ultimo_sinal_por_ativo

    logger.info("🔄 Loop de análise iniciado")
    janela_anterior = None

    while True:
        try:
            reset_diario_se_necessario()
            jan = janela_ativa()

            # Notificar abertura/fechamento de janela
            if jan and jan["nome"] != janela_anterior:
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=(
                        f"🚨 *Janela ABERTA: {jan['nome']}*\n"
                        f"⏰ Duração: {jan['inicio']} – {jan['fim']} BRT\n"
                        f"🎯 Máximo nesta janela: {jan['max_sinais']} sinais por ativo\n"
                        f"💱 Monitorando: GBP/USD OTC + EUR/USD OTC\n"
                        f"🔍 Procurando confluência agora..."
                    ),
                    parse_mode="Markdown",
                )
                janela_anterior = jan["nome"]
            elif not jan and janela_anterior:
                janela_anterior = None

            # Fora de janela → dormir
            if not jan:
                await asyncio.sleep(60)
                continue

            # Limite por janela
            if sinais_por_janela[jan["nome"]] >= jan["max_sinais"] * len(ATIVOS):
                logger.info(f"Limite atingido na janela {jan['nome']}")
                await asyncio.sleep(60)
                continue

            # Limite diário
            if sinais_hoje >= MAX_SINAIS_DIA:
                logger.info("Limite diário atingido")
                await asyncio.sleep(60)
                continue

            # Analisar cada ativo
            for nome_ativo, symbol in ATIVOS.items():
                # Cooldown por ativo
                ult = ultimo_sinal_por_ativo[nome_ativo]
                if ult and (agora_brt() - ult).total_seconds() < COOLDOWN_SEGUNDOS:
                    continue

                df = buscar_velas(symbol)
                if df is None:
                    continue

                sinal = analisar(df, nome_ativo)
                if sinal:
                    await enviar_sinal(bot, sinal, jan)
                    ultimo_sinal_por_ativo[nome_ativo] = agora_brt()
                    sinais_hoje += 1
                    sinais_por_janela[jan["nome"]] += 1

            await asyncio.sleep(30)  # 30s entre rounds

        except Exception as e:
            logger.error(f"Erro no loop: {e}", exc_info=True)
            await asyncio.sleep(60)

# ============================================================
# COMANDOS DO TELEGRAM
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Bot BOT-N9 ativo*\n\n"
        "Comandos:\n"
        "/status — situação atual\n"
        "/janelas — janelas de operação\n"
        "/sinal — força análise agora",
        parse_mode="Markdown",
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_diario_se_necessario()
    jan = janela_ativa()
    if jan:
        status_jan = f"✅ Janela ATIVA: {jan['nome']} ({jan['inicio']}–{jan['fim']})"
    else:
        nome, mins = proxima_janela()
        status_jan = f"⏸ Fora de janela. Próxima: {nome} em {mins} min"

    contagem = "\n".join(f" • {nome}: {qtd}" for nome, qtd in sinais_por_janela.items())

    msg = (
        f"📊 *STATUS BOT-N9*\n\n"
        f"🕐 Hora BRT: {agora_brt().strftime('%H:%M:%S')}\n"
        f"{status_jan}\n\n"
        f"💱 Ativos: GBP/USD OTC + EUR/USD OTC\n"
        f"📈 Total hoje: {sinais_hoje}/{MAX_SINAIS_DIA}\n\n"
        f"*Por janela:*\n{contagem}\n\n"
        f"💾 Supabase: {'✅' if supabase else '❌'}\n"
        f"🌐 Fonte: Twelve Data API"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_janelas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = "📅 *JANELAS DE OPERAÇÃO (BRT)*\n\n"
    for j in JANELAS:
        txt += f"{j['nome']}\n ⏰ {j['inicio']}–{j['fim']} | máx {j['max_sinais']} sinais/ativo\n\n"
    txt += f"💱 Ativos: GBP/USD OTC + EUR/USD OTC\n"
    txt += f"📊 Total máximo/dia: {MAX_SINAIS_DIA} sinais"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def cmd_sinal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Força análise manual em todos os ativos."""
    await update.message.reply_text("🔍 Forçando análise em todos os ativos...")
    bot = context.bot
    encontrou = False

    for nome_ativo, symbol in ATIVOS.items():
        df = buscar_velas(symbol)
        if df is None:
            await update.message.reply_text(f"❌ {nome_ativo}: sem dados")
            continue

        sinal = analisar(df, nome_ativo)
        if sinal:
            jan = janela_ativa() or {"nome": "🔧 Manual"}
            await enviar_sinal(bot, sinal, jan)
            encontrou = True
        else:
            await update.message.reply_text(
                f"⚪ {nome_ativo}: sem confluência suficiente agora"
            )

    if not encontrou:
        await update.message.reply_text(
            "Nenhum sinal disparou. Mercado sem confluência clara nos 2 ativos."
        )

# ============================================================
# MAIN
# ============================================================

async def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID faltando!")
        return

    if not TWELVEDATA_API_KEY:
        logger.error("❌ TWELVEDATA_API_KEY faltando! Adicione no Railway Variables.")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("janelas", cmd_janelas))
    app.add_handler(CommandHandler("sinal", cmd_sinal))

    logger.info("=" * 60)
    logger.info("🤖 BOT-N9 (3 Janelas + 2 Moedas) INICIADO")
    logger.info("Janelas: 05h-06h | 10h-11h | 15h-16h BRT")
    logger.info("Ativos: GBP/USD OTC + EUR/USD OTC")
    logger.info("Fonte: Twelve Data API")
    logger.info("=" * 60)

    bot = app.bot
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            "🟢 *Bot BOT-N9 atualizado!*\n\n"
            "💱 Ativos: GBP/USD OTC + EUR/USD OTC\n"
            "📅 3 janelas: 05h | 10h | 15h BRT\n"
            "🎯 4 sinais por ativo por janela\n"
            "📊 Total máx: 24 sinais/dia\n"
            "🌐 Fonte: Twelve Data API\n\n"
            "Use /status, /janelas ou /sinal"
        ),
        parse_mode="Markdown",
    )

    asyncio.create_task(loop_analise(bot))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Encerrando...")
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
"""
============================================================
  BOT-N9 — Sinais IQ Option (3 Janelas + 2 Moedas)
  Ativos: GBP/USD OTC + EUR/USD OTC
  Fonte: Yahoo Finance (GBP=X e EUR=X)
  Estratégia: Price Action + Confluência mínima 2/6
============================================================
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import yfinance as yf
import pandas as pd
import numpy as np
from telegram import Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update
from supabase import create_client, Client

# ============================================================
# CONFIGURAÇÃO
# ============================================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

BRT = ZoneInfo("America/Sao_Paulo")

# ATIVOS MONITORADOS — 2 MOEDAS
ATIVOS = {
    "GBP/USD OTC": "GBPUSD=X",
    "EUR/USD OTC": "EURUSD=X",
}

# JANELAS DE OPERAÇÃO (BRT)
JANELAS = [
    {"nome": "🌅 Abertura de Londres",       "inicio": "05:00", "fim": "06:00", "max_sinais": 4},
    {"nome": "⭐ PICO Londres+NY",            "inicio": "10:00", "fim": "11:00", "max_sinais": 4},
    {"nome": "🌆 Pós-almoço NY",              "inicio": "15:00", "fim": "16:00", "max_sinais": 4},
]

MIN_CONFLUENCIA = 2          # mínimo de filtros confirmando
COOLDOWN_SEGUNDOS = 180       # 3 min entre sinais por ativo
MAX_SINAIS_DIA = 24           # 4 sinais × 3 janelas × 2 ativos

# Estado runtime
ultimo_sinal_por_ativo = {ativo: None for ativo in ATIVOS}
sinais_por_janela = {j["nome"]: 0 for j in JANELAS}
sinais_hoje = 0
data_atual = None

# Supabase
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("✅ Supabase conectado")
    except Exception as e:
        logger.warning(f"⚠️ Supabase não conectou: {e}")

# ============================================================
# UTILS DE HORÁRIO E JANELAS
# ============================================================

def agora_brt():
    return datetime.now(BRT)

def janela_ativa():
    """Retorna a janela ativa agora ou None."""
    now = agora_brt()
    hh_mm = now.strftime("%H:%M")
    for j in JANELAS:
        if j["inicio"] <= hh_mm < j["fim"]:
            return j
    return None

def proxima_janela():
    """Retorna (nome_janela, minutos_ate_abrir)."""
    now = agora_brt()
    hh_mm = now.strftime("%H:%M")
    for j in JANELAS:
        if hh_mm < j["inicio"]:
            h, m = map(int, j["inicio"].split(":"))
            alvo = now.replace(hour=h, minute=m, second=0, microsecond=0)
            mins = int((alvo - now).total_seconds() / 60)
            return j["nome"], mins
    # passou de todas hoje → primeira janela de amanhã
    j = JANELAS[0]
    h, m = map(int, j["inicio"].split(":"))
    alvo = (now + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
    mins = int((alvo - now).total_seconds() / 60)
    return j["nome"] + " (amanhã)", mins

def reset_diario_se_necessario():
    global sinais_hoje, sinais_por_janela, data_atual
    hoje = agora_brt().date()
    if data_atual != hoje:
        sinais_hoje = 0
        sinais_por_janela = {j["nome"]: 0 for j in JANELAS}
        data_atual = hoje
        logger.info(f"🟢 Novo dia: {hoje}")
        return True
    return False

# ============================================================
# FONTE DE DADOS — YAHOO FINANCE (com retry e validação)
# ============================================================

def buscar_velas(ticker, periodo="1d", intervalo="5m", tentativas=3):
    """
    Busca velas do Yahoo Finance com retry.
    Retorna DataFrame ou None se falhar.
    """
    for tentativa in range(1, tentativas + 1):
        try:
            data = yf.download(
                ticker,
                period=periodo,
                interval=intervalo,
                progress=False,
                auto_adjust=False,
            )
            if data is None or data.empty:
                logger.warning(f"[{ticker}] Tentativa {tentativa}: dados vazios")
                continue

            # yfinance multi-index fix
            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.get_level_values(0)

            # Validar colunas mínimas
            colunas_necessarias = {"Open", "High", "Low", "Close"}
            if not colunas_necessarias.issubset(data.columns):
                logger.warning(f"[{ticker}] Colunas faltando: {data.columns.tolist()}")
                continue

            # Validar que tem velas suficientes
            if len(data) < 30:
                logger.warning(f"[{ticker}] Poucas velas: {len(data)}")
                continue

            logger.info(f"[{ticker}] ✅ {len(data)} velas obtidas")
            return data

        except Exception as e:
            logger.error(f"[{ticker}] Tentativa {tentativa} falhou: {e}")

    logger.error(f"[{ticker}] ❌ Todas as {tentativas} tentativas falharam")
    return None

# ============================================================
# INDICADORES TÉCNICOS
# ============================================================

def calc_rsi(close, periodo=14):
    delta = close.diff()
    ganho = delta.where(delta > 0, 0).rolling(periodo).mean()
    perda = -delta.where(delta < 0, 0).rolling(periodo).mean()
    rs = ganho / perda.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calc_ema(close, periodo):
    return close.ewm(span=periodo, adjust=False).mean()

def calc_macd(close):
    ema12 = calc_ema(close, 12)
    ema26 = calc_ema(close, 26)
    macd = ema12 - ema26
    sinal = calc_ema(macd, 9)
    hist = macd - sinal
    return macd, sinal, hist

def calc_bollinger(close, periodo=20, desvios=2):
    media = close.rolling(periodo).mean()
    std = close.rolling(periodo).std()
    sup = media + desvios * std
    inf = media - desvios * std
    return sup, media, inf

# ============================================================
# PADRÕES DE VELA
# ============================================================

def detectar_padrao_alta(df):
    """Retorna padrão de alta detectado na última vela ou None."""
    if len(df) < 3:
        return None
    v = df.iloc[-1]   # vela atual
    a = df.iloc[-2]   # vela anterior

    corpo_v = abs(v["Close"] - v["Open"])
    sombra_inf = min(v["Open"], v["Close"]) - v["Low"]
    sombra_sup = v["High"] - max(v["Open"], v["Close"])

    # MARTELO (corpo pequeno, sombra inferior longa)
    if corpo_v > 0 and sombra_inf >= 2 * corpo_v and sombra_sup < corpo_v:
        return "Martelo"

    # ENGOLFO DE ALTA
    if a["Close"] < a["Open"] and v["Close"] > v["Open"]:
        if v["Close"] > a["Open"] and v["Open"] < a["Close"]:
            return "Engolfo de Alta"

    # PIN BAR DE ALTA
    if corpo_v > 0 and sombra_inf >= 2 * corpo_v:
        return "Pin Bar Alta"

    return None

def detectar_padrao_baixa(df):
    if len(df) < 3:
        return None
    v = df.iloc[-1]
    a = df.iloc[-2]

    corpo_v = abs(v["Close"] - v["Open"])
    sombra_inf = min(v["Open"], v["Close"]) - v["Low"]
    sombra_sup = v["High"] - max(v["Open"], v["Close"])

    # SHOOTING STAR
    if corpo_v > 0 and sombra_sup >= 2 * corpo_v and sombra_inf < corpo_v:
        return "Shooting Star"

    # ENGOLFO DE BAIXA
    if a["Close"] > a["Open"] and v["Close"] < v["Open"]:
        if v["Open"] > a["Close"] and v["Close"] < a["Open"]:
            return "Engolfo de Baixa"

    # PIN BAR DE BAIXA
    if corpo_v > 0 and sombra_sup >= 2 * corpo_v:
        return "Pin Bar Baixa"

    return None

# ============================================================
# ANÁLISE DE CONFLUÊNCIA
# ============================================================

def analisar(df, ativo):
    """
    Analisa o DataFrame e retorna dict com sinal ou None.
    Usa confluência mínima de MIN_CONFLUENCIA filtros (de 6 possíveis).
    """
    if df is None or len(df) < 50:
        logger.info(f"[{ativo}] DF insuficiente para análise")
        return None

    close = df["Close"]
    rsi = calc_rsi(close).iloc[-1]
    ema9 = calc_ema(close, 9).iloc[-1]
    ema21 = calc_ema(close, 21).iloc[-1]
    ema50 = calc_ema(close, 50).iloc[-1]
    macd_line, macd_sig, _ = calc_macd(close)
    macd_atual = macd_line.iloc[-1]
    macd_sinal = macd_sig.iloc[-1]
    bol_sup, bol_med, bol_inf = calc_bollinger(close)
    preco_atual = close.iloc[-1]

    pad_alta = detectar_padrao_alta(df)
    pad_baixa = detectar_padrao_baixa(df)

    # ============ ANÁLISE DE COMPRA ============
    score_call = 0
    motivos_call = []

    if rsi < 35:
        score_call += 1
        motivos_call.append(f"RSI sobrevendido ({rsi:.1f})")

    if macd_atual > macd_sinal:
        score_call += 1
        motivos_call.append("MACD cruzando alta")

    if preco_atual <= bol_inf.iloc[-1]:
        score_call += 1
        motivos_call.append("Preço na banda inferior")

    if ema9 > ema21:
        score_call += 1
        motivos_call.append("EMA9 > EMA21")

    if ema21 > ema50:
        score_call += 1
        motivos_call.append("Tendência alta (EMA21>50)")

    if pad_alta:
        score_call += 1
        motivos_call.append(f"Padrão {pad_alta}")

    # ============ ANÁLISE DE VENDA ============
    score_put = 0
    motivos_put = []

    if rsi > 65:
        score_put += 1
        motivos_put.append(f"RSI sobrecomprado ({rsi:.1f})")

    if macd_atual < macd_sinal:
        score_put += 1
        motivos_put.append("MACD cruzando baixa")

    if preco_atual >= bol_sup.iloc[-1]:
        score_put += 1
        motivos_put.append("Preço na banda superior")

    if ema9 < ema21:
        score_put += 1
        motivos_put.append("EMA9 < EMA21")

    if ema21 < ema50:
        score_put += 1
        motivos_put.append("Tendência baixa (EMA21<50)")

    if pad_baixa:
        score_put += 1
        motivos_put.append(f"Padrão {pad_baixa}")

    logger.info(f"[{ativo}] 📊 CALL={score_call}/6 PUT={score_put}/6 RSI={rsi:.1f}")

    # Decidir sinal
    if score_call >= MIN_CONFLUENCIA and score_call > score_put:
        return {
            "ativo": ativo,
            "direcao": "🟢 COMPRA (CALL)",
            "score": score_call,
            "motivos": motivos_call,
            "preco": float(preco_atual),
            "rsi": float(rsi),
        }

    if score_put >= MIN_CONFLUENCIA and score_put > score_call:
        return {
            "ativo": ativo,
            "direcao": "🔴 VENDA (PUT)",
            "score": score_put,
            "motivos": motivos_put,
            "preco": float(preco_atual),
            "rsi": float(rsi),
        }

    return None

# ============================================================
# ENVIO DE SINAL
# ============================================================

async def enviar_sinal(bot: Bot, sinal: dict, janela: dict):
    motivos_txt = "\n".join(f"• {m}" for m in sinal["motivos"])
    msg = (
        f"⚡ *SINAL — {janela['nome']}*\n\n"
        f"💱 Ativo: *{sinal['ativo']}*\n"
        f"🎯 Direção: *{sinal['direcao']}*\n"
        f"⏱ Expiração: *5 minutos (M5)*\n\n"
        f"💰 Preço: `{sinal['preco']:.5f}`\n"
        f"📈 Confluência: *{sinal['score']}/6*\n\n"
        f"*Motivos:*\n{motivos_txt}\n\n"
        f"⚠️ Trading envolve risco. Use só capital que pode perder."
    )

    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=msg,
            parse_mode="Markdown",
        )
        logger.info(f"✅ Sinal enviado: {sinal['ativo']} {sinal['direcao']}")

        # Salvar no Supabase
        if supabase:
            try:
                supabase.table("sinais").insert({
                    "ativo": sinal["ativo"],
                    "direcao": sinal["direcao"],
                    "score": sinal["score"],
                    "preco": sinal["preco"],
                    "rsi": sinal["rsi"],
                    "janela": janela["nome"],
                    "criado_em": agora_brt().isoformat(),
                }).execute()
            except Exception as e:
                logger.warning(f"Supabase insert falhou: {e}")

    except Exception as e:
        logger.error(f"❌ Falha ao enviar sinal: {e}")

# ============================================================
# LOOP PRINCIPAL DE ANÁLISE
# ============================================================

async def loop_analise(bot: Bot):
    global sinais_hoje, ultimo_sinal_por_ativo

    logger.info("🔄 Loop de análise iniciado")
    janela_anterior = None

    while True:
        try:
            reset_diario_se_necessario()
            jan = janela_ativa()

            # Notificar abertura/fechamento de janela
            if jan and jan["nome"] != janela_anterior:
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=(
                        f"🚨 *Janela ABERTA: {jan['nome']}*\n"
                        f"⏰ Duração: {jan['inicio']} – {jan['fim']} BRT\n"
                        f"🎯 Máximo nesta janela: {jan['max_sinais']} sinais por ativo\n"
                        f"💱 Monitorando: GBP/USD OTC + EUR/USD OTC\n"
                        f"🔍 Procurando confluência agora..."
                    ),
                    parse_mode="Markdown",
                )
                janela_anterior = jan["nome"]
            elif not jan and janela_anterior:
                janela_anterior = None

            # Fora de janela → dormir
            if not jan:
                await asyncio.sleep(60)
                continue

            # Limite por janela
            if sinais_por_janela[jan["nome"]] >= jan["max_sinais"] * len(ATIVOS):
                logger.info(f"Limite atingido na janela {jan['nome']}")
                await asyncio.sleep(60)
                continue

            # Limite diário
            if sinais_hoje >= MAX_SINAIS_DIA:
                logger.info("Limite diário atingido")
                await asyncio.sleep(60)
                continue

            # Analisar cada ativo
            for nome_ativo, ticker in ATIVOS.items():
                # Cooldown por ativo
                ult = ultimo_sinal_por_ativo[nome_ativo]
                if ult and (agora_brt() - ult).total_seconds() < COOLDOWN_SEGUNDOS:
                    continue

                df = buscar_velas(ticker)
                if df is None:
                    continue

                sinal = analisar(df, nome_ativo)
                if sinal:
                    await enviar_sinal(bot, sinal, jan)
                    ultimo_sinal_por_ativo[nome_ativo] = agora_brt()
                    sinais_hoje += 1
                    sinais_por_janela[jan["nome"]] += 1

            await asyncio.sleep(30)  # 30s entre rounds

        except Exception as e:
            logger.error(f"Erro no loop: {e}", exc_info=True)
            await asyncio.sleep(60)

# ============================================================
# COMANDOS DO TELEGRAM
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Bot BOT-N9 ativo*\n\n"
        "Comandos:\n"
        "/status — situação atual\n"
        "/janelas — janelas de operação\n"
        "/sinal — força análise agora",
        parse_mode="Markdown",
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_diario_se_necessario()
    jan = janela_ativa()
    if jan:
        status_jan = f"✅ Janela ATIVA: {jan['nome']} ({jan['inicio']}–{jan['fim']})"
    else:
        nome, mins = proxima_janela()
        status_jan = f"⏸ Fora de janela. Próxima: {nome} em {mins} min"

    contagem = "\n".join(f"  • {nome}: {qtd}" for nome, qtd in sinais_por_janela.items())

    msg = (
        f"📊 *STATUS BOT-N9*\n\n"
        f"🕐 Hora BRT: {agora_brt().strftime('%H:%M:%S')}\n"
        f"{status_jan}\n\n"
        f"💱 Ativos: GBP/USD OTC + EUR/USD OTC\n"
        f"📈 Total hoje: {sinais_hoje}/{MAX_SINAIS_DIA}\n\n"
        f"*Por janela:*\n{contagem}\n\n"
        f"💾 Supabase: {'✅' if supabase else '❌'}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_janelas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = "📅 *JANELAS DE OPERAÇÃO (BRT)*\n\n"
    for j in JANELAS:
        txt += f"{j['nome']}\n   ⏰ {j['inicio']}–{j['fim']} | máx {j['max_sinais']} sinais/ativo\n\n"
    txt += f"💱 Ativos: GBP/USD OTC + EUR/USD OTC\n"
    txt += f"📊 Total máximo/dia: {MAX_SINAIS_DIA} sinais"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def cmd_sinal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Força análise manual em todos os ativos."""
    await update.message.reply_text("🔍 Forçando análise em todos os ativos...")
    bot = context.bot
    encontrou = False

    for nome_ativo, ticker in ATIVOS.items():
        df = buscar_velas(ticker)
        if df is None:
            await update.message.reply_text(f"❌ {nome_ativo}: sem dados")
            continue

        sinal = analisar(df, nome_ativo)
        if sinal:
            jan = janela_ativa() or {"nome": "🔧 Manual"}
            await enviar_sinal(bot, sinal, jan)
            encontrou = True
        else:
            await update.message.reply_text(
                f"⚪ {nome_ativo}: sem confluência suficiente agora"
            )

    if not encontrou:
        await update.message.reply_text(
            "Nenhum sinal disparou. Mercado sem confluência clara nos 2 ativos."
        )

# ============================================================
# MAIN
# ============================================================

async def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID faltando!")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("janelas", cmd_janelas))
    app.add_handler(CommandHandler("sinal", cmd_sinal))

    logger.info("=" * 60)
    logger.info("🤖 BOT-N9 (3 Janelas + 2 Moedas) INICIADO")
    logger.info("Janelas: 05h-06h | 10h-11h | 15h-16h BRT")
    logger.info("Ativos: GBP/USD OTC + EUR/USD OTC")
    logger.info("=" * 60)

    bot = app.bot
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            "🟢 *Bot BOT-N9 atualizado!*\n\n"
            "💱 Ativos: GBP/USD OTC + *EUR/USD OTC* (NOVO)\n"
            "📅 3 janelas: 05h | 10h | 15h BRT\n"
            "🎯 4 sinais por ativo por janela\n"
            "📊 Total máx: 24 sinais/dia\n\n"
            "Use /status, /janelas ou /sinal"
        ),
        parse_mode="Markdown",
    )

    asyncio.create_task(loop_analise(bot))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Encerrando...")
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
    
