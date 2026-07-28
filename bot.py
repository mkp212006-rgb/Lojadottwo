import asyncio
import json
import os
import re
import secrets
import shutil
import logging
import threading
import time as time_module
import requests
try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None
try:
    from flask import Flask, request, jsonify
except Exception:
    Flask = request = jsonify = None
from io import BytesIO
from html import escape as html_escape
from datetime import datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown
from telegram.error import BadRequest
try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = ImageDraw = ImageFont = None

from database import BotDatabase
from validators import validar_destino_pedido

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    PicklePersistence,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

BASE_DIR = Path(__file__).resolve().parent
if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env")


def resolver_data_dir() -> Path:
    """Define onde os arquivos gerados pelo bot serão salvos.

    Por padrão fica em ./dados dentro da pasta do bot. Em hospedagens como
    Railway/Render, configure DATA_DIR para o caminho do volume persistente.
    """
    data_dir_env = os.getenv("DATA_DIR", "dados").strip() or "dados"
    data_dir = Path(data_dir_env).expanduser()
    if not data_dir.is_absolute():
        data_dir = BASE_DIR / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


DATA_DIR = resolver_data_dir()
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(DATA_DIR / "bot.sqlite3"))).expanduser()
if not DATABASE_PATH.is_absolute():
    DATABASE_PATH = BASE_DIR / DATABASE_PATH
DB = BotDatabase(DATABASE_PATH)

CATALOGO_PATH = BASE_DIR / "catalogo.json"
WELCOME_IMAGE_PATH = BASE_DIR / "tw_store_boas_vindas.png"
SUPORTE_IMAGE_PATH = BASE_DIR / "tw_store_suporte.png"
PAGAMENTO_INSTAGRAM_LAYOUT_PATH = BASE_DIR / "pagamento_instagram_layout.png"
PAGAMENTO_TIKTOK_LAYOUT_PATH = BASE_DIR / "pagamento_tiktok_layout.png"
ASSINATURA_IMAGE_PATHS = {
    "netflix": BASE_DIR / "netflix_premium.jpg",
    "prime_video": BASE_DIR / "prime_video_premium.jpg",
    "crunchyroll": BASE_DIR / "crunchyroll_premium.png",
    "spotify": BASE_DIR / "spotify_premium.png",
    "paramount": BASE_DIR / "paramount_premium.png",
}

with open(CATALOGO_PATH, "r", encoding="utf-8") as f:
    CATALOGO = json.load(f)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
PIX_CHAVE = os.getenv("PIX_CHAVE", "").strip()
PIX_COPIA_COLA = os.getenv("PIX_COPIA_COLA", "").strip()
PIX_RECEBEDOR = os.getenv("PIX_RECEBEDOR", "").strip()


# API da plataforma de pedidos.
# Preencha essas variáveis no .env antes de colocar o bot em produção.
PANEL_API_URL = os.getenv("PANEL_API_URL", "").strip()
PANEL_API_KEY = os.getenv("PANEL_API_KEY", "").strip()
try:
    PANEL_API_TIMEOUT = int(os.getenv("PANEL_API_TIMEOUT", "30"))
except ValueError:
    PANEL_API_TIMEOUT = 30

# Trava antes do pagamento: consulta a plataforma antes de gerar Pix.
# Se estiver sem saldo/sem serviço disponível, o cliente não recebe cobrança.
CHECK_ESTOQUE_ANTES_PAGAMENTO = os.getenv("CHECK_ESTOQUE_ANTES_PAGAMENTO", "true").strip().lower() not in (
    "0", "false", "nao", "não", "no", "off", "desativado"
)
try:
    MARGEM_SALDO_PLATAFORMA = float(os.getenv("MARGEM_SALDO_PLATAFORMA", "0").strip().replace(",", "."))
except ValueError:
    MARGEM_SALDO_PLATAFORMA = 0.0

try:
    PANEL_SERVICES_CACHE_TTL = int(os.getenv("PANEL_SERVICES_CACHE_TTL", "300"))
except ValueError:
    PANEL_SERVICES_CACHE_TTL = 300
PLATAFORMA_SERVICOS_CACHE = {"expira_em": 0.0, "dados": None}

# Mercado Pago — Pix automático.
# Configure essas variáveis no Railway, nunca direto no código.
MERCADO_PAGO_ACCESS_TOKEN = os.getenv("MERCADO_PAGO_ACCESS_TOKEN", "").strip()
MP_PAYER_EMAIL = os.getenv("MP_PAYER_EMAIL", "cliente@ttwostore.com").strip()
MP_WEBHOOK_URL = os.getenv("MP_WEBHOOK_URL", "").strip()
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "").strip()
try:
    MP_API_TIMEOUT = int(os.getenv("MP_API_TIMEOUT", "30"))
except ValueError:
    MP_API_TIMEOUT = 30
try:
    WEBHOOK_QUEUE_INTERVAL = int(os.getenv("WEBHOOK_QUEUE_INTERVAL", "45"))
except ValueError:
    WEBHOOK_QUEUE_INTERVAL = 45
try:
    WEBHOOK_QUEUE_MAX_ATTEMPTS = int(os.getenv("WEBHOOK_QUEUE_MAX_ATTEMPTS", "8"))
except ValueError:
    WEBHOOK_QUEUE_MAX_ATTEMPTS = 8

# Tempo para limpar automaticamente pedidos que ficaram aguardando pagamento.
# Use 0 para desativar. Padrão: 180 minutos / 3 horas.
try:
    PAGAMENTOS_PENDENTES_EXPIRAR_MINUTOS = int(os.getenv("PAGAMENTOS_PENDENTES_EXPIRAR_MINUTOS", "180"))
except ValueError:
    PAGAMENTOS_PENDENTES_EXPIRAR_MINUTOS = 180
try:
    PAGAMENTOS_PENDENTES_LIMPEZA_INTERVALO = int(os.getenv("PAGAMENTOS_PENDENTES_LIMPEZA_INTERVALO", "300"))
except ValueError:
    PAGAMENTOS_PENDENTES_LIMPEZA_INTERVALO = 300

# Limpa pedidos persistidos em pedidos_pendentes quando o bot inicia.
# Mantido ligado por padrão para impedir que webhooks/pendências antigas sejam
# reenviados para a plataforma após restart/deploy no Railway.
LIMPAR_PEDIDOS_PENDENTES_AO_INICIAR = os.getenv("LIMPAR_PEDIDOS_PENDENTES_AO_INICIAR", "true").strip().lower() not in (
    "0", "false", "nao", "não", "no", "off", "desativado"
)



TZ_BR = ZoneInfo("America/Sao_Paulo")
TOTAIS_SEMANAIS_PATH = DATA_DIR / "totais_semanais.json"
PEDIDOS_PENDENTES_PATH = DATA_DIR / "pedidos_pendentes.json"
COMPROVANTES_USADOS_PATH = DATA_DIR / "comprovantes_usados.json"
PAGAMENTOS_PROCESSADOS_PATH = DATA_DIR / "pagamentos_processados.json"
PEDIDOS_HISTORICO_PATH = DATA_DIR / "pedidos_historico.json"
BOT_PERSISTENCE_PATH = DATA_DIR / "bot_persistence.pkl"

ARQUIVOS_JSON_RUNTIME = {
    "totais_semanais.json": None,
    "pedidos_pendentes.json": {},
    "comprovantes_usados.json": {},
    "pagamentos_processados.json": {},
    "pedidos_historico.json": {},
}

# Evita processar o mesmo pagamento duas vezes quando o Mercado Pago reenvia
# notificações ou quando cliente toca em "verificar" ao mesmo tempo do webhook.
_MP_PAYMENTS_LOCK = threading.Lock()
_MP_PAYMENTS_EM_PROCESSAMENTO = set()
_FECHAMENTO_SEMANAL_LOCK = asyncio.Lock()


def agora_br() -> datetime:
    return datetime.now(TZ_BR)


# Momento em que esta instância do bot subiu. Usado para não reenviar
# automaticamente pagamentos aprovados antes do deploy/restart atual.
BOT_PROCESS_STARTED_AT = agora_br()


def formatar_data_expiracao_mercado_pago(data: datetime) -> str:
    """Formata a expiração do Pix no padrão aceito pelo Mercado Pago.

    O Mercado Pago rejeita datas em formato brasileiro/UTC textual, por exemplo:
    02-07-2026T07:31:01UTC.

    O formato correto precisa ficar assim:
    2026-07-02T04:31:01.000-03:00
    """
    if data.tzinfo is None:
        data = data.replace(tzinfo=TZ_BR)

    data = data.astimezone(TZ_BR)
    offset = data.strftime("%z")  # Exemplo: -0300
    offset_formatado = f"{offset[:3]}:{offset[3:]}" if offset else "-03:00"

    return f"{data:%Y-%m-%dT%H:%M:%S}.000{offset_formatado}"


def copiar_padrao_json(padrao):
    if isinstance(padrao, dict):
        return padrao.copy()
    if isinstance(padrao, list):
        return padrao.copy()
    return padrao


def carregar_json(caminho: Path, padrao):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    if not caminho.exists():
        return copiar_padrao_json(padrao)
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            dados = json.load(f)
    except Exception as exc:
        backup = caminho.with_suffix(caminho.suffix + f".corrompido-{agora_br():%Y%m%d%H%M%S}.bak")
        try:
            shutil.copy2(caminho, backup)
            logging.warning("JSON corrompido em %s. Backup criado em %s. Erro: %s", caminho, backup, exc)
        except Exception:
            logging.warning("JSON corrompido em %s. Não foi possível criar backup. Erro: %s", caminho, exc)
        return copiar_padrao_json(padrao)
    return dados if isinstance(dados, type(padrao)) else copiar_padrao_json(padrao)


def salvar_json(caminho: Path, dados):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_suffix(caminho.suffix + ".tmp")
    with open(temporario, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    os.replace(temporario, caminho)


def inicializar_arquivos_json_runtime():
    """Prepara a pasta de dados e mantém compatibilidade com versões antigas.

    A versão 1.6 usa SQLite. Os arquivos JSON antigos, quando existirem,
    são migrados para o banco na primeira inicialização.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for nome in ARQUIVOS_JSON_RUNTIME.keys():
        destino = DATA_DIR / nome
        origem_antiga = BASE_DIR / nome
        if origem_antiga.exists() and origem_antiga.resolve() != destino.resolve() and not destino.exists():
            try:
                shutil.copy2(origem_antiga, destino)
                logging.info("Arquivo legado migrado para a pasta dados: %s -> %s", origem_antiga, destino)
            except Exception as exc:
                logging.warning("Não foi possível copiar arquivo legado %s: %s", origem_antiga, exc)


inicializar_arquivos_json_runtime()
DB.migrar_jsons_se_vazio({
    "totais_semanais": TOTAIS_SEMANAIS_PATH,
    "pedidos_pendentes": PEDIDOS_PENDENTES_PATH,
    "comprovantes_usados": COMPROVANTES_USADOS_PATH,
    "pagamentos_processados": PAGAMENTOS_PROCESSADOS_PATH,
    "pedidos_historico": PEDIDOS_HISTORICO_PATH,
})


def gerar_pedido_id() -> str:
    return f"TW{agora_br():%Y%m%d%H%M%S}{secrets.token_hex(2).upper()}"


def preparar_pedido(pedido: dict) -> dict:
    pedido.setdefault("pedido_id", gerar_pedido_id())
    pedido.setdefault("criado_em", agora_br().strftime("%d/%m/%Y %H:%M:%S"))
    return pedido


def carregar_pedidos_pendentes() -> dict:
    return DB.carregar_pedidos_pendentes()


def salvar_pedidos_pendentes(dados: dict):
    DB.salvar_pedidos_pendentes(dados)


def salvar_pedido_pendente(pedido: dict):
    pedido_id = str(pedido.get("pedido_id") or gerar_pedido_id())
    pedido["pedido_id"] = pedido_id
    DB.salvar_pedido_pendente(pedido_id, pedido)


def obter_pedido_pendente(pedido_id: str) -> dict | None:
    return carregar_pedidos_pendentes().get(str(pedido_id))


def remover_pedido_pendente(pedido_id: str):
    DB.remover_pedido_pendente(str(pedido_id))


def pagamento_pendente_expiracao_ativa() -> bool:
    return int(PAGAMENTOS_PENDENTES_EXPIRAR_MINUTOS or 0) > 0


def data_base_expiracao_pagamento(pedido: dict) -> datetime | None:
    return parse_data_br(
        pedido.get("pagamento_criado_em")
        or pedido.get("criado_em")
        or pedido.get("atualizado_em")
    )


def calcular_expiracao_pagamento(pedido: dict) -> datetime | None:
    if not pagamento_pendente_expiracao_ativa():
        return None

    # Se o pagamento já nasceu com data de expiração gravada, respeita essa data.
    # Isso evita deixar o cliente usando um Pix/link que o Mercado Pago já expirou.
    expira_em_salvo = parse_data_br(pedido.get("pagamento_expira_em"))
    if expira_em_salvo:
        return expira_em_salvo

    base = data_base_expiracao_pagamento(pedido)
    if not base:
        return None
    return base + timedelta(minutes=int(PAGAMENTOS_PENDENTES_EXPIRAR_MINUTOS))


def pagamento_pendente_expirado(pedido: dict, agora: datetime | None = None) -> bool:
    if not pagamento_pendente_expiracao_ativa():
        return False
    if str(pedido.get("status") or "") != "aguardando_pagamento":
        return False
    expira_em = calcular_expiracao_pagamento(pedido)
    if not expira_em:
        return False
    return (agora or agora_br()) >= expira_em


def fechar_pagamento_expirado(pedido_id: str, pedido: dict, motivo: str = "Tempo limite para pagamento esgotado"):
    registro = dict(pedido or {})
    registro["pedido_id"] = str(pedido_id or registro.get("pedido_id") or "")
    registro["status"] = "pagamento_expirado"
    registro["expirado_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
    registro["motivo_expiracao"] = motivo
    salvar_pedido_historico(registro)
    remover_pedido_pendente(str(registro.get("pedido_id") or pedido_id))


def fechar_pagamentos_expirados_sync() -> list[dict]:
    """Fecha pedidos que passaram do prazo aguardando pagamento.

    Para Pix automático do Mercado Pago, consulta o status antes de fechar. Se o
    pagamento estiver aprovado, processa o pedido em vez de apagar a pendência.
    """
    if not pagamento_pendente_expiracao_ativa():
        return []

    expirados = []
    agora = agora_br()
    pendentes = carregar_pedidos_pendentes()

    for pedido_id, pedido in list(pendentes.items()):
        if not pagamento_pendente_expirado(pedido, agora):
            continue

        payment_id = str(pedido.get("mp_payment_id") or "").strip()
        motivo = "Pagamento pendente expirado automaticamente"

        if payment_id and mercado_pago_configurado():
            try:
                pagamento = consultar_pagamento_mercado_pago_sync(payment_id)
                status_mp = str(pagamento.get("status") or "").lower()
                if status_mp == "approved":
                    processar_pagamento_aprovado_sync(pedido, pagamento, origem="limpeza_expirados")
                    continue
                if status_mp:
                    motivo = f"Mercado Pago retornou status {status_mp} após o prazo"
                    pedido["mp_status"] = status_mp
                    pedido["mp_status_detail"] = str(pagamento.get("status_detail") or pedido.get("mp_status_detail") or "")
            except Exception as exc:
                logging.warning("Não foi possível verificar pagamento expirado %s no Mercado Pago: %s", payment_id, exc)
                # Para evitar fechar um pagamento que possa ter sido aprovado, tenta novamente no próximo ciclo.
                continue

        fechar_pagamento_expirado(str(pedido_id), pedido, motivo)
        expirados.append({"pedido_id": str(pedido_id), "user_id": pedido.get("user_id"), "motivo": motivo})

    return expirados


def botoes_pedido_expirado() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("🔄 Fazer novo pedido", "voltar:inicio")]])


async def avisar_cliente_pagamento_expirado(bot, pedido_id: str, user_id, motivo: str):
    if not user_id:
        return
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "⌛️ Seu link de pagamento expirou e o pedido foi fechado automaticamente.\n\n"
                f"ID do pedido: `{md(pedido_id)}`\n\n"
                "Para comprar, toque em *Fazer novo pedido* e comece do início."
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=botoes_pedido_expirado(),
        )
    except Exception as exc:
        logging.warning("Falha ao avisar cliente sobre pagamento expirado %s: %s", pedido_id, exc)


async def encerrar_interacao_se_pagamento_expirado(update: Update, context: ContextTypes.DEFAULT_TYPE, pedido: dict) -> bool:
    if not pedido or not pagamento_pendente_expirado(pedido):
        return False

    pedido_id = str(pedido.get("pedido_id") or "")
    await asyncio.to_thread(fechar_pagamentos_expirados_sync)

    historico = carregar_pedidos_historico().get(pedido_id) if pedido_id else None
    if historico and historico.get("status") == "pagamento_expirado":
        context.user_data.clear()
        await safe_edit_or_reply(
            update,
            (
                "⌛️ Esse link de pagamento expirou e o pedido foi fechado automaticamente.\n\n"
                f"ID do pedido: `{md(pedido_id)}`\n\n"
                "Para comprar, toque em *Fazer novo pedido* e comece do início."
            ),
            botoes_pedido_expirado(),
        )
        return True

    return False


def carregar_pedidos_historico() -> dict:
    return DB.carregar_pedidos_historico()


def salvar_pedido_historico(pedido: dict):
    if not pedido:
        return
    pedido_id = str(pedido.get("pedido_id") or gerar_pedido_id())
    registro = dict(pedido)
    registro["pedido_id"] = pedido_id
    registro["historico_atualizado_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
    DB.salvar_pedido_historico(pedido_id, registro)


def normalizar_id_consulta(texto: str) -> str:
    texto = str(texto or "").strip()
    texto = re.sub(r"[^A-Za-z0-9_-]+", "", texto)
    return texto[:80]


def buscar_pedido_local_por_id(consulta_id: str) -> tuple[dict | None, str | None]:
    consulta_id = normalizar_id_consulta(consulta_id)
    if not consulta_id:
        return None, None

    pendentes = carregar_pedidos_pendentes()
    if consulta_id in pendentes:
        return pendentes[consulta_id], "pendente"

    consulta_lower = consulta_id.lower()
    for pedido in pendentes.values():
        candidatos = [
            pedido.get("pedido_id"),
            pedido.get("plataforma_order_id"),
            pedido.get("mp_payment_id"),
        ]
        if any(str(item or "").lower() == consulta_lower for item in candidatos):
            return pedido, "pendente"

    historico = carregar_pedidos_historico()
    if consulta_id in historico:
        return historico[consulta_id], "historico"

    for pedido in historico.values():
        candidatos = [
            pedido.get("pedido_id"),
            pedido.get("plataforma_order_id"),
            pedido.get("mp_payment_id"),
        ]
        if any(str(item or "").lower() == consulta_lower for item in candidatos):
            return pedido, "historico"

    return None, None


def pedido_tem_id_plataforma(order_id) -> bool:
    texto = str(order_id or "").strip()
    if not texto:
        return False
    return texto.lower() not in ("não informado", "nao informado", "none", "null", "0")


def carregar_comprovantes_usados() -> dict:
    return DB.carregar_comprovantes_usados()


def comprovante_ja_usado(file_unique_id: str | None) -> bool:
    if not file_unique_id:
        return False
    return str(file_unique_id) in carregar_comprovantes_usados()


def marcar_comprovante_usado(file_unique_id: str | None, pedido: dict):
    if not file_unique_id:
        return
    usados = carregar_comprovantes_usados()
    usados[str(file_unique_id)] = {
        "pedido_id": pedido.get("pedido_id"),
        "user_id": pedido.get("user_id"),
        "valor": pedido.get("valor"),
        "registrado_em": agora_br().strftime("%d/%m/%Y %H:%M:%S"),
    }
    DB.salvar_comprovantes_usados(usados)


def ids_unicos(*valores: str) -> list[str]:
    ids = []
    for valor in valores:
        admin_id = str(valor or "").strip()
        if admin_id and admin_id not in ids:
            ids.append(admin_id)
    return ids








def ids_admin_relatorio_pedido(pedido: dict | None = None) -> list[str]:
    """Retorna o único destinatário de alertas operacionais: Admin 1."""
    return ids_unicos(ADMIN_CHAT_ID)


def ids_admin_relatorio_semanal() -> list[str]:
    """Compatibilidade: avisos administrativos continuam restritos ao Admin 1."""
    return ids_unicos(ADMIN_CHAT_ID)


def telegram_id_update(update: Update) -> str:
    if update.effective_user:
        return str(update.effective_user.id)
    if update.effective_chat:
        return str(update.effective_chat.id)
    return ""


def id_administrador_sistema(telegram_id) -> bool:
    return str(telegram_id or "").strip() == str(ADMIN_CHAT_ID or "").strip()


def eh_admin(update: Update) -> bool:
    """O único nível administrativo restante é o Admin 1 configurado."""
    return id_administrador_sistema(telegram_id_update(update))














def registro_aprovado(update: Update) -> bool:
    """Compatibilidade com integrações antigas; qualquer chat identificado tem acesso."""
    return bool(update.effective_user or update.effective_chat)


def pode_atender_suporte(update: Update) -> bool:
    return eh_admin(update)








async def bloquear_se_sem_acesso(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # Acesso livre: não existe mais bloqueio por cadastro/status/cargo.
    return False


def menu_painel_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("📊 Resumo dos pedidos", "admin_painel:resumo")],
        [btn("💳 Pedidos pendentes", "admin_painel:pagamentos_pendentes")],
        [btn("🧾 Últimos pedidos", "admin_painel:ultimos")],
    ])


def menu_voltar_painel_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("⬅️ Voltar ao painel", "admin_painel:inicio")]])


def texto_painel_admin() -> str:
    return (
        "🛠️ *Painel do Administrador*\n\n"
        "Acompanhe pedidos pendentes, histórico e alertas operacionais.\n\n"
        "Os relatórios de pedidos concluídos não são enviados automaticamente; "
        "o Admin 1 recebe apenas alertas de falta de estoque ou problemas de processamento."
    )




def texto_resumo_admin() -> str:
    pedidos_pendentes = carregar_pedidos_pendentes()
    historico = carregar_pedidos_historico()
    hoje = agora_br().date()
    pedidos_hoje = 0
    valor_hoje_centavos = 0
    for pedido in historico.values():
        dt = parse_data_br(pedido.get("aprovado_em") or pedido.get("historico_atualizado_em") or pedido.get("criado_em"))
        if dt and dt.date() == hoje:
            pedidos_hoje += 1
            valor_hoje_centavos += valor_para_centavos(pedido.get("valor"))
    webhooks_pendentes = len(DB.listar_webhooks_pendentes(limite=100, max_attempts=WEBHOOK_QUEUE_MAX_ATTEMPTS))
    return (
        "📊 *Resumo dos pedidos*\n\n"
        f"💳 Pedidos pendentes: *{len(pedidos_pendentes)}*\n"
        f"🧾 Pedidos finalizados hoje: *{pedidos_hoje}*\n"
        f"💰 Faturamento hoje: *R$ {centavos_para_moeda(valor_hoje_centavos)}*\n"
        f"🔁 Webhooks pendentes/retry: *{webhooks_pendentes}*\n\n"
        f"🗄️ Banco: `{md(DATABASE_PATH.name)}`"
    )


async def painel_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not eh_admin(update):
        await update.message.reply_text("Apenas o administrador configurado pode abrir este painel.")
        return
    context.user_data.clear()
    mensagem = await update.message.reply_text(
        texto_painel_admin(), parse_mode=ParseMode.MARKDOWN,
        reply_markup=menu_painel_admin(), disable_web_page_preview=True,
    )
    guardar_mensagem_bot(context, mensagem)


async def mostrar_painel_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not eh_admin(update):
        await update.callback_query.answer("Apenas o administrador pode usar este painel.", show_alert=True)
        return
    context.user_data.clear()
    await safe_edit_or_reply(update, texto_painel_admin(), menu_painel_admin())


async def mostrar_resumo_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not eh_admin(update):
        await update.callback_query.answer("Apenas o administrador pode consultar o resumo.", show_alert=True)
        return
    await safe_edit_or_reply(update, texto_resumo_admin(), menu_voltar_painel_admin())


async def mostrar_pagamentos_pendentes_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not eh_admin(update):
        await update.callback_query.answer("Apenas o administrador pode ver pedidos.", show_alert=True)
        return
    await safe_edit_or_reply(update, texto_pagamentos_pendentes_admin(), menu_voltar_painel_admin())


async def mostrar_ultimos_pedidos_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not eh_admin(update):
        await update.callback_query.answer("Apenas o administrador pode ver pedidos.", show_alert=True)
        return
    await safe_edit_or_reply(update, texto_ultimos_pedidos_admin(), menu_voltar_painel_admin())


def parse_data_br(texto: str | None) -> datetime | None:
    texto = str(texto or "").strip()
    if not texto:
        return None
    formatos = ["%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]
    for fmt in formatos:
        try:
            dt = datetime.strptime(texto[:19], fmt)
            return dt.replace(tzinfo=TZ_BR)
        except Exception:
            pass
    return None


def parse_data_mercado_pago(texto: str | None) -> datetime | None:
    """Lê datas ISO retornadas pelo Mercado Pago com segurança."""
    texto = str(texto or "").strip()
    if not texto:
        return None

    candidatos = [texto, texto.replace("Z", "+00:00")]
    for candidato in candidatos:
        try:
            dt = datetime.fromisoformat(candidato)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ_BR)
            return dt.astimezone(TZ_BR)
        except Exception:
            pass

    return parse_data_br(texto)


def data_aprovacao_mercado_pago(pagamento: dict) -> datetime | None:
    if not isinstance(pagamento, dict):
        return None
    for chave in ("date_approved", "money_release_date", "date_created", "last_modified"):
        dt = parse_data_mercado_pago(pagamento.get(chave))
        if dt:
            return dt
    return None


def pagamento_aprovado_antes_desta_instancia(pagamento: dict, margem_segundos: int = 0) -> bool:
    """Evita que webhook/pagamento antigo seja reenviado após restart/deploy.

    Se a aprovação aconteceu antes desta instância subir, não dá para saber se
    uma tentativa anterior chegou na plataforma. Por segurança, o pedido vai
    para revisão manual em vez de ser enviado automaticamente de novo.
    """
    aprovado_em = data_aprovacao_mercado_pago(pagamento)
    if not aprovado_em:
        return False
    limite_seguro = BOT_PROCESS_STARTED_AT - timedelta(seconds=max(0, int(margem_segundos)))
    return aprovado_em <= limite_seguro








def texto_pagamentos_pendentes_admin() -> str:
    pedidos = carregar_pedidos_pendentes()
    pendentes = []
    for pedido_id, pedido in pedidos.items():
        status = str(pedido.get("status") or "")
        if status in {"aguardando_pagamento", "aguardando_aprovacao_admin", "aguardando_link", "aguardando_email_iptv"}:
            pendentes.append((pedido_id, pedido))

    if not pendentes:
        return "💳 *Pagamentos/Pedidos Pendentes*\n\nNenhum pedido pendente no momento."

    linhas = [f"💳 *Pagamentos/Pedidos Pendentes*\n\nTotal: *{len(pendentes)}*\n"]
    for pedido_id, pedido in pendentes[:50]:
        linhas.append(
            f"• `{md(pedido_id)}` — *{md(traduzir_status_local(pedido.get('status')))}*\n"
            f"  {md(pedido.get('catalogo', ''))} | {md(pedido.get('servico', ''))}\n"
            f"  Valor: R$ {md(pedido.get('valor', ''))} | Cliente: {md(pedido.get('usuario', ''))}\n"
            f"  Telegram ID: `{md(pedido.get('user_id', ''))}`"
            + (f"\n  Expira em: {md(pedido.get('pagamento_expira_em'))}" if pedido.get('pagamento_expira_em') else "")
        )
    if len(pendentes) > 50:
        linhas.append(f"\nMostrando 50 de {len(pendentes)} pendências.")
    return "\n".join(linhas)


def texto_ultimos_pedidos_admin() -> str:
    historico = carregar_pedidos_historico()
    if not historico:
        return "🧾 *Últimos pedidos*\n\nAinda não há pedidos finalizados no histórico."

    def chave(item):
        pedido = item[1]
        dt = parse_data_br(pedido.get("historico_atualizado_em") or pedido.get("aprovado_em") or pedido.get("criado_em"))
        return dt or datetime.min.replace(tzinfo=TZ_BR)

    itens = sorted(historico.items(), key=chave, reverse=True)[:12]
    linhas = [
        "🧾 *Últimos pedidos finalizados*",
        "",
        f"Mostrando os *{len(itens)}* pedidos mais recentes.",
        "",
    ]

    for pedido_id, pedido in itens:
        data_pedido = (
            pedido.get("historico_atualizado_em")
            or pedido.get("aprovado_em")
            or pedido.get("criado_em")
            or "Não informado"
        )
        linhas.append(
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🧾 *Pedido:* `{md(pedido_id)}`\n"
            f"👤 *Cliente:* {md(pedido.get('usuario') or 'Não informado')}\n"
            f"🆔 *Telegram ID:* `{md(pedido.get('user_id') or 'Não informado')}`\n"
            f"📦 *Serviço:* {md(pedido.get('catalogo') or 'Não informado')} | {md(pedido.get('servico') or 'Não informado')}\n"
            f"💰 *Valor:* R$ {md(pedido.get('valor') or '0,00')}\n"
            f"📌 *Status:* {md(traduzir_status_local(pedido.get('status')))}\n"
            f"🌐 *Pedido plataforma:* `{md(pedido.get('plataforma_order_id') or 'Não informado')}`\n"
            f"🗓️ *Data:* {md(data_pedido)}"
        )

    if len(historico) > len(itens):
        linhas.append(f"\nMostrando 12 de {len(historico)} pedidos no histórico.")

    return "\n\n".join(linhas)




























































































def valor_para_centavos(valor) -> int:
    texto = str(valor).strip().replace("R$", "").replace(" ", "")
    texto = texto.replace(".", "").replace(",", ".")
    try:
        return int(round(float(texto) * 100))
    except ValueError:
        return 0


def centavos_para_moeda(centavos: int) -> str:
    reais = centavos / 100
    texto = f"{reais:,.2f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")






























async def rotina_limpeza_pagamentos_expirados(application: Application):
    while True:
        try:
            expirados = await asyncio.to_thread(fechar_pagamentos_expirados_sync)
            for item in expirados:
                await avisar_cliente_pagamento_expirado(
                    application.bot,
                    item.get("pedido_id"),
                    item.get("user_id"),
                    item.get("motivo", ""),
                )
        except Exception as exc:
            logging.warning("Falha na limpeza de pagamentos expirados: %s", exc)

        intervalo = max(60, int(PAGAMENTOS_PENDENTES_LIMPEZA_INTERVALO or 300))
        await asyncio.sleep(intervalo)





async def iniciar_rotinas(application: Application):
    # A rotina de fechamento/relatório semanal foi removida.
    if PAGAMENTOS_PENDENTES_LIMPEZA_INTERVALO > 0:
        application.create_task(rotina_limpeza_pagamentos_expirados(application))


def md(texto) -> str:
    return escape_markdown(str(texto), version=1)


def money(valor: str) -> str:
    return f"R$ {valor}"


CATALOGOS_COM_ENVIO_API = {
    "Instagram",
    "Instagram — Serviços Brasileiros",
    "Instagram_Brasileiros",
    "TikTok",
    "Kwai",
}

CATALOGOS_COM_EMAIL = {
    "IPTV XCIPTV",
    "IPTV Livestream 4K",
    "Internet Ilimitada",
    "Assinaturas",
}


def catalogo_exige_email(pedido_ou_catalogo) -> bool:
    if isinstance(pedido_ou_catalogo, dict):
        if str(pedido_ou_catalogo.get("tipo_destino") or "").strip().lower() == "email":
            return True
        catalogo = str(pedido_ou_catalogo.get("catalogo") or "").strip()
    else:
        catalogo = str(pedido_ou_catalogo or "").strip()

    return catalogo in CATALOGOS_COM_EMAIL or "assinatura" in catalogo.lower()


class PlataformaAPIConfigError(Exception):
    pass


class PlataformaAPIRequestError(Exception):
    pass


class PlataformaEstoqueIndisponivel(Exception):
    pass


def limpar_erro_api(erro) -> str:
    texto = str(erro or "").strip()
    if PANEL_API_KEY:
        texto = texto.replace(PANEL_API_KEY, "***")
    if MERCADO_PAGO_ACCESS_TOKEN:
        texto = texto.replace(MERCADO_PAGO_ACCESS_TOKEN, "***")

    # Nunca envia para o cliente dados financeiros retornados pelo painel.
    # Alguns painéis retornam campos como charge/currency até em mensagens de erro.
    texto = re.sub(r"(['\"]?charge['\"]?\s*[:=]\s*)['\"]?[^,}\n]+", r"\1***", texto, flags=re.IGNORECASE)
    texto = re.sub(r"(['\"]?currency['\"]?\s*[:=]\s*)['\"]?[^,}\n]+", r"\1***", texto, flags=re.IGNORECASE)
    texto = re.sub(r"valor\s+cobrado\s+no\s+painel\s*[:=]?\s*[^,}\n]+", "valor cobrado no painel: ***", texto, flags=re.IGNORECASE)
    texto = re.sub(r"moeda\s*[:=]\s*[^,}\n]+", "moeda: ***", texto, flags=re.IGNORECASE)

    return texto[:900]


class MercadoPagoConfigError(Exception):
    pass


class MercadoPagoRequestError(Exception):
    pass


def mercado_pago_configurado() -> bool:
    return bool(MERCADO_PAGO_ACCESS_TOKEN)


def valor_pedido_float(valor) -> float:
    centavos = valor_para_centavos(valor)
    if centavos <= 0:
        raise MercadoPagoConfigError("Valor do pedido inválido para gerar Pix.")
    return round(centavos / 100, 2)


def mp_headers(pedido_id: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {MERCADO_PAGO_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    if pedido_id:
        # Chave fixa por pedido: evita Pix duplicado em retries do mesmo pedido.
        headers["X-Idempotency-Key"] = f"tw-store-{pedido_id}"
    return headers


def criar_pagamento_mercado_pago_sync(pedido: dict) -> dict:
    if not MERCADO_PAGO_ACCESS_TOKEN:
        raise MercadoPagoConfigError("MERCADO_PAGO_ACCESS_TOKEN não configurado.")

    pedido_id = str(pedido.get("pedido_id") or gerar_pedido_id())
    pedido["pedido_id"] = pedido_id

    descricao = f"{pedido.get('catalogo', 'Pedido')} - {pedido.get('servico', '')} - {pedido.get('quantidade', '')}".strip()
    payload = {
        "transaction_amount": valor_pedido_float(pedido.get("valor")),
        "description": descricao[:250],
        "payment_method_id": "pix",
        "external_reference": pedido_id,
        "payer": {
            "email": MP_PAYER_EMAIL or "cliente@ttwostore.com",
        },
    }
    if pagamento_pendente_expiracao_ativa():
        expira_em = agora_br() + timedelta(minutes=int(PAGAMENTOS_PENDENTES_EXPIRAR_MINUTOS))
        payload["date_of_expiration"] = formatar_data_expiracao_mercado_pago(expira_em)
        pedido["pagamento_expira_em"] = expira_em.strftime("%d/%m/%Y %H:%M:%S")
    if MP_WEBHOOK_URL:
        payload["notification_url"] = MP_WEBHOOK_URL

    try:
        resposta = requests.post(
            "https://api.mercadopago.com/v1/payments",
            headers=mp_headers(pedido_id),
            json=payload,
            timeout=MP_API_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise MercadoPagoRequestError(f"Falha de conexão com Mercado Pago: {limpar_erro_api(exc)}") from exc

    try:
        dados = resposta.json()
    except ValueError:
        dados = {"raw": resposta.text[:500]}

    if resposta.status_code not in (200, 201):
        raise MercadoPagoRequestError(
            f"Mercado Pago respondeu HTTP {resposta.status_code}: {limpar_erro_api(dados)}"
        )

    transaction_data = (
        dados.get("point_of_interaction", {})
        .get("transaction_data", {})
    )
    qr_code = transaction_data.get("qr_code") or ""
    qr_code_base64 = transaction_data.get("qr_code_base64") or ""
    ticket_url = transaction_data.get("ticket_url") or ""

    if not qr_code:
        raise MercadoPagoRequestError("Mercado Pago criou o pagamento, mas não retornou Pix copia e cola.")

    return {
        "id": str(dados.get("id")),
        "status": dados.get("status"),
        "status_detail": dados.get("status_detail"),
        "external_reference": dados.get("external_reference"),
        "transaction_amount": dados.get("transaction_amount"),
        "qr_code": qr_code,
        "qr_code_base64": qr_code_base64,
        "ticket_url": ticket_url,
        "raw": dados,
    }


def consultar_pagamento_mercado_pago_sync(payment_id: str) -> dict:
    if not MERCADO_PAGO_ACCESS_TOKEN:
        raise MercadoPagoConfigError("MERCADO_PAGO_ACCESS_TOKEN não configurado.")

    try:
        resposta = requests.get(
            f"https://api.mercadopago.com/v1/payments/{payment_id}",
            headers={"Authorization": f"Bearer {MERCADO_PAGO_ACCESS_TOKEN}"},
            timeout=MP_API_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise MercadoPagoRequestError(f"Falha de conexão com Mercado Pago: {limpar_erro_api(exc)}") from exc

    try:
        dados = resposta.json()
    except ValueError:
        dados = {"raw": resposta.text[:500]}

    if not resposta.ok:
        raise MercadoPagoRequestError(
            f"Mercado Pago respondeu HTTP {resposta.status_code}: {limpar_erro_api(dados)}"
        )

    return dados


def aplicar_pagamento_mercado_pago_no_pedido(pedido: dict, pagamento: dict):
    pedido["mp_payment_id"] = str(pagamento.get("id") or "")
    pedido["mp_status"] = str(pagamento.get("status") or "")
    pedido["mp_status_detail"] = str(pagamento.get("status_detail") or "")
    pedido["mp_external_reference"] = str(pagamento.get("external_reference") or "")
    pedido["mp_qr_code"] = pagamento.get("qr_code") or pedido.get("mp_qr_code") or ""
    pedido["mp_ticket_url"] = pagamento.get("ticket_url") or pedido.get("mp_ticket_url") or ""
    pedido["status"] = "aguardando_pagamento"
    pedido["pagamento_criado_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
    if pagamento_pendente_expiracao_ativa() and not pedido.get("pagamento_expira_em"):
        pedido["pagamento_expira_em"] = (
            agora_br() + timedelta(minutes=int(PAGAMENTOS_PENDENTES_EXPIRAR_MINUTOS))
        ).strftime("%d/%m/%Y %H:%M:%S")


async def garantir_pagamento_mercado_pago(pedido: dict) -> tuple[bool, str]:
    if not mercado_pago_configurado():
        return False, "Mercado Pago não configurado."

    if pedido.get("mp_payment_id") and pedido.get("mp_qr_code"):
        salvar_pedido_pendente(pedido)
        return True, "Pagamento já criado."

    try:
        pagamento = await asyncio.to_thread(criar_pagamento_mercado_pago_sync, pedido)
    except Exception as exc:
        return False, limpar_erro_api(exc)

    aplicar_pagamento_mercado_pago_no_pedido(pedido, pagamento)
    salvar_pedido_pendente(pedido)
    return True, "Pagamento criado."


def obter_pedido_por_pagamento(payment_id: str | None = None, external_reference: str | None = None) -> dict | None:
    pendentes = carregar_pedidos_pendentes()
    if external_reference and str(external_reference) in pendentes:
        return pendentes[str(external_reference)]

    for pedido in pendentes.values():
        if payment_id and str(pedido.get("mp_payment_id")) == str(payment_id):
            return pedido
        if external_reference and str(pedido.get("pedido_id")) == str(external_reference):
            return pedido
    return None


def carregar_pagamentos_processados() -> dict:
    return DB.carregar_pagamentos_processados()


def pagamento_ja_processado(payment_id: str) -> bool:
    if not payment_id:
        return False
    return str(payment_id) in carregar_pagamentos_processados()


def iniciar_processamento_pagamento(payment_id: str) -> bool:
    """Reserva o pagamento para processamento nesta instância."""
    if not payment_id:
        return True
    payment_id = str(payment_id)
    with _MP_PAYMENTS_LOCK:
        if payment_id in _MP_PAYMENTS_EM_PROCESSAMENTO:
            return False
        if pagamento_ja_processado(payment_id):
            return False
        _MP_PAYMENTS_EM_PROCESSAMENTO.add(payment_id)
        return True


def finalizar_processamento_pagamento(payment_id: str):
    if not payment_id:
        return
    with _MP_PAYMENTS_LOCK:
        _MP_PAYMENTS_EM_PROCESSAMENTO.discard(str(payment_id))


def marcar_pagamento_processado(payment_id: str, pedido: dict):
    if not payment_id:
        return
    dados = carregar_pagamentos_processados()
    dados[str(payment_id)] = {
        "pedido_id": pedido.get("pedido_id"),
        "user_id": pedido.get("user_id"),
        "valor": pedido.get("valor"),
        "processado_em": agora_br().strftime("%d/%m/%Y %H:%M:%S"),
    }
    DB.salvar_pagamentos_processados(dados)


def obter_pedido_historico_por_pagamento(payment_id: str | None = None, external_reference: str | None = None) -> dict | None:
    """Localiza pagamentos já finalizados no histórico.

    Isso é uma trava importante para restart: se o Mercado Pago reenviar um
    webhook antigo, o bot reconhece que o pedido já saiu dos pendentes e não
    tenta criar outro pedido na plataforma.
    """
    historico = carregar_pedidos_historico()

    if external_reference and str(external_reference) in historico:
        return historico[str(external_reference)]

    for pedido in historico.values():
        if payment_id and str(pedido.get("mp_payment_id") or "") == str(payment_id):
            return pedido
        if external_reference and str(pedido.get("pedido_id") or "") == str(external_reference):
            return pedido
    return None


def reconstruir_pagamentos_processados_do_historico():
    """Recria a trava de pagamentos processados a partir do histórico.

    Em deploys/reinícios onde a tabela/JSON de pagamentos processados ficou
    vazio, os pedidos pagos ainda aparecem no histórico. Esta rotina evita que
    webhooks antigos voltem a acionar o envio automático na plataforma.
    """
    reconstruidos = 0
    for pedido in carregar_pedidos_historico().values():
        payment_id = str(pedido.get("mp_payment_id") or "").strip()
        if not payment_id or pagamento_ja_processado(payment_id):
            continue
        marcar_pagamento_processado(payment_id, pedido)
        reconstruidos += 1
    if reconstruidos:
        logging.info("Trava de pagamentos reconstruída pelo histórico: %s registro(s).", reconstruidos)


def pedido_ja_enviado_para_plataforma(pedido: dict) -> bool:
    if not pedido:
        return False
    if pedido.get("plataforma_api_status") == "enviado":
        return True
    return pedido_tem_id_plataforma(pedido.get("plataforma_order_id"))


def status_envio_plataforma(pedido: dict) -> str:
    return str((pedido or {}).get("plataforma_api_status") or "").strip().lower()


def envio_plataforma_bloqueado_para_auto(pedido: dict) -> bool:
    """Estados que nunca devem chamar a API automaticamente de novo."""
    status_api = status_envio_plataforma(pedido)
    return status_api in {"processando", "revisao_manual", "erro", "ignorado_manual", "resolvido_manual", "ignorado_restart"}


def envio_plataforma_estava_processando(pedido: dict) -> bool:
    """Detecta pedido salvo no meio do envio para a plataforma.

    Se o Railway reiniciar depois que o bot marcou o pedido como
    "processando", mas antes de gravar o ID retornado pela plataforma, não é
    seguro chamar a API novamente: a primeira chamada pode ter criado o pedido
    mesmo sem o bot ter conseguido salvar a resposta. Nessa situação o bot
    finaliza o pagamento e manda para revisão manual, evitando duplicidade.
    """
    if not pedido:
        return False
    return status_envio_plataforma(pedido) == "processando" and not pedido_ja_enviado_para_plataforma(pedido)


def marcar_envio_plataforma_para_revisao_manual(pedido: dict, origem: str = "restart", motivo: str | None = None):
    pedido["plataforma_api_status"] = "revisao_manual"
    pedido["plataforma_api_erro"] = motivo or (
        "Envio automático pausado por segurança: o bot/servidor reiniciou "
        "ou um webhook antigo foi recebido enquanto este pedido podia já ter sido enviado "
        "para a plataforma. Confira no painel da plataforma se o pedido já foi criado "
        "antes de reenviar manualmente."
    )
    pedido["plataforma_revisao_manual_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
    pedido["plataforma_revisao_manual_origem"] = origem


def callback_revisao_manual(acao: str, pedido_id: str) -> str:
    pedido_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(pedido_id or ""))[:36]
    return f"admin_revisao_{acao}:{pedido_id}"


def botoes_revisao_manual_admin_dict(pedido_id: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✅ Já foi feito", "callback_data": callback_revisao_manual("feito", pedido_id)}],
            [{"text": "🔁 Reenviar para plataforma", "callback_data": callback_revisao_manual("reenviar", pedido_id)}],
            [{"text": "❌ Ignorar pendência", "callback_data": callback_revisao_manual("ignorar", pedido_id)}],
        ]
    }


def botoes_revisao_manual_admin(pedido_id: str) -> InlineKeyboardMarkup:
    dados = botoes_revisao_manual_admin_dict(pedido_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(botao["text"], callback_data=botao["callback_data"]) for botao in linha]
        for linha in dados["inline_keyboard"]
    ])


def texto_alerta_revisao_manual_admin(pedido: dict, origem: str = "startup") -> str:
    return (
        "⚠️ *PEDIDO EM REVISÃO MANUAL*\n\n"
        "O pagamento foi confirmado, mas o envio automático foi bloqueado para evitar duplicidade.\n\n"
        f"🆔 *Pedido:* `{md(pedido.get('pedido_id', ''))}`\n"
        f"💳 *Mercado Pago ID:* `{md(pedido.get('mp_payment_id', ''))}`\n"
        f"🗂️ *Catálogo:* {md(pedido.get('catalogo', ''))}\n"
        f"📌 *Serviço:* {md(pedido.get('servico', ''))}\n"
        f"🔢 *Quantidade:* {md(pedido.get('quantidade', ''))}\n"
        f"🔗 *Link/@:* {md(pedido.get('link', ''))}\n"
        f"👤 *Cliente:* {md(pedido.get('usuario') or 'Cliente')} — `{md(pedido.get('user_id') or '')}`\n\n"
        f"🚫 *Motivo:* {md(pedido.get('plataforma_api_erro') or 'Envio automático bloqueado por segurança.')}\n\n"
        "Antes de reenviar, confira na plataforma se esse pedido já existe."
    )


def notificar_admin_revisao_manual_sync(pedido: dict, origem: str = "startup"):
    admin_id = str(ADMIN_CHAT_ID or "").strip()
    if not admin_id or not pedido:
        return
    if pedido.get("plataforma_revisao_admin_notificado"):
        return
    enviar_telegram_sync(
        admin_id,
        texto_alerta_revisao_manual_admin(pedido, origem),
        reply_markup=botoes_revisao_manual_admin_dict(str(pedido.get("pedido_id") or "")),
    )
    pedido["plataforma_revisao_admin_notificado"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")


def corrigir_pedidos_com_envio_interrompido():
    """Fecha pedidos que ficaram salvos como processando após queda/restart.

    O objetivo é não reenviar automaticamente um pedido para a plataforma se o
    bot caiu no intervalo entre a chamada da API e a gravação do ID retornado.
    """
    corrigidos = 0
    for pedido_id, pedido in list(carregar_pedidos_pendentes().items()):
        if pedido.get("catalogo") not in CATALOGOS_COM_ENVIO_API:
            continue
        if not envio_plataforma_bloqueado_para_auto(pedido):
            continue
        if pedido_ja_enviado_para_plataforma(pedido):
            continue

        if status_envio_plataforma(pedido) != "revisao_manual":
            marcar_envio_plataforma_para_revisao_manual(pedido, origem="startup")
        if str(pedido.get("status") or "").strip().lower() == "pagamento_aprovado":
            notificar_admin_revisao_manual_sync(pedido, origem="startup")
            salvar_pedido_historico(pedido)
            payment_id = str(pedido.get("mp_payment_id") or "").strip()
            if payment_id:
                marcar_pagamento_processado(payment_id, pedido)
            remover_pedido_pendente(str(pedido_id))
        else:
            salvar_pedido_pendente(pedido)
        corrigidos += 1

    if corrigidos:
        logging.warning(
            "Pedido(s) com envio interrompido movidos para revisão manual: %s",
            corrigidos,
        )



STATUS_PENDENTES_LIMPEZA_STARTUP = {
    "aguardando_link",
    "aguardando_email_iptv",
    "aguardando_pagamento",
    "aguardando_aprovacao_admin",
    "pendente",
}

STATUS_PAGOS_LIMPEZA_STARTUP = {
    "pagamento_aprovado",
    "pago",
    "paid",
    "approved",
}


def pedido_pago_confirmado_local(pedido: dict) -> bool:
    """Detecta pedido já pago usando apenas dados locais salvos."""
    if not pedido:
        return False
    status_local = str(pedido.get("status") or "").strip().lower()
    status_mp = str(pedido.get("mp_status") or "").strip().lower()
    return (
        status_local in STATUS_PAGOS_LIMPEZA_STARTUP
        or status_mp == "approved"
        or bool(pedido.get("aprovado_em"))
    )


def limpar_persistencia_transiente_no_startup():
    """Remove user_data antigo para botões de pagamento velhos não reprocessarem pedidos."""
    if not LIMPAR_PEDIDOS_PENDENTES_AO_INICIAR:
        return
    if not BOT_PERSISTENCE_PATH.exists():
        return
    backup = BOT_PERSISTENCE_PATH.with_suffix(BOT_PERSISTENCE_PATH.suffix + f".limpo-{agora_br():%Y%m%d%H%M%S}.bak")
    try:
        shutil.move(str(BOT_PERSISTENCE_PATH), str(backup))
        logging.warning("Persistência antiga do bot movida para %s para limpar pedidos antigos em user_data.", backup)
    except Exception as exc:
        logging.warning("Não foi possível limpar a persistência antiga %s: %s", BOT_PERSISTENCE_PATH, exc)


def limpar_pedidos_pendentes_salvos_no_startup():
    """Remove pendências antigas antes de processar webhooks no restart.

    Isso impede que o Railway, ao reiniciar o bot, reenvie para a plataforma
    pedidos que já tinham ficado salvos em pedidos_pendentes.
    Pedidos pagos são movidos para o histórico e o pagamento fica marcado como
    processado; pedidos não pagos são encerrados e removidos da fila de pendentes.
    """
    if not LIMPAR_PEDIDOS_PENDENTES_AO_INICIAR:
        logging.info("Limpeza de pedidos pendentes no startup desativada por configuração.")
        return

    pedidos = carregar_pedidos_pendentes()
    if not pedidos:
        return

    removidos_pendentes = 0
    pagos_bloqueados = 0
    outros_removidos = 0

    for pedido_id, pedido in list(pedidos.items()):
        pedido = dict(pedido or {})
        pedido_id = str(pedido_id or pedido.get("pedido_id") or "").strip()
        if not pedido_id:
            continue
        pedido["pedido_id"] = pedido_id

        status_local = str(pedido.get("status") or "").strip().lower()
        pago = pedido_pago_confirmado_local(pedido)

        if pago:
            pedido["status"] = "pagamento_aprovado"
            pedido.setdefault("aprovado_em", pedido.get("historico_atualizado_em") or agora_br().strftime("%d/%m/%Y %H:%M:%S"))
            if pedido.get("catalogo") in CATALOGOS_COM_ENVIO_API and not pedido_ja_enviado_para_plataforma(pedido):
                pedido["plataforma_api_status"] = "ignorado_restart"
                pedido["plataforma_api_erro"] = (
                    "Pedido pago removido da fila de pendentes ao iniciar o bot para impedir "
                    "reenvio automático após restart do Railway. Se precisar, reenvie manualmente."
                )
                pedido["plataforma_resolucao_manual"] = "ignorado_startup"
                pedido["plataforma_resolvido_manual_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
                pagos_bloqueados += 1
            else:
                outros_removidos += 1

            pedido["removido_de_pendentes_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
            pedido["removido_de_pendentes_motivo"] = "Limpeza automática no startup: pagamento já estava aprovado."
            salvar_pedido_historico(pedido)
            payment_id = str(pedido.get("mp_payment_id") or "").strip()
            if payment_id:
                marcar_pagamento_processado(payment_id, pedido)
            remover_pedido_pendente(pedido_id)
            continue

        if status_local in STATUS_PENDENTES_LIMPEZA_STARTUP or not status_local:
            pedido["status"] = "pendente_removido_restart"
            pedido["removido_de_pendentes_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
            pedido["removido_de_pendentes_motivo"] = (
                "Pedido pendente encerrado automaticamente ao iniciar o bot para evitar reprocessamento."
            )
            salvar_pedido_historico(pedido)
            remover_pedido_pendente(pedido_id)
            removidos_pendentes += 1
            continue

        # Qualquer outro registro dentro de pedidos_pendentes também sai da fila,
        # porque manter pendência antiga é o que causa reenvio no restart.
        pedido["removido_de_pendentes_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
        pedido["removido_de_pendentes_motivo"] = (
            f"Removido automaticamente do pending no startup. Status anterior: {status_local or 'sem status'}."
        )
        salvar_pedido_historico(pedido)
        remover_pedido_pendente(pedido_id)
        outros_removidos += 1

    total = removidos_pendentes + pagos_bloqueados + outros_removidos
    if total:
        try:
            salvar_json(PEDIDOS_PENDENTES_PATH, {})
        except Exception as exc:
            logging.warning("Não foi possível limpar JSON legado de pedidos pendentes: %s", exc)

        logging.warning(
            "Limpeza startup: %s pedido(s) removidos de pendentes; %s pendente(s) encerrado(s), %s pago(s) bloqueado(s) contra reenvio, %s outro(s).",
            total,
            removidos_pendentes,
            pagos_bloqueados,
            outros_removidos,
        )


def pagamento_antigo_sem_trava_deve_ir_para_revisao(pedido: dict, pagamento: dict) -> bool:
    if not pedido or pedido.get("catalogo") not in CATALOGOS_COM_ENVIO_API:
        return False
    if pedido_ja_enviado_para_plataforma(pedido) or envio_plataforma_bloqueado_para_auto(pedido):
        return False
    return pagamento_aprovado_antes_desta_instancia(pagamento)


def pagamento_aprovado_e_valido(pedido: dict, pagamento: dict) -> tuple[bool, str]:
    if str(pagamento.get("status")) != "approved":
        return False, f"Status ainda não aprovado: {pagamento.get('status')}"

    payment_id = str(pagamento.get("id") or "")
    if payment_id and pagamento_ja_processado(payment_id):
        return False, "Pagamento já processado anteriormente."

    external_reference = str(pagamento.get("external_reference") or "")
    pedido_id = str(pedido.get("pedido_id") or "")
    if external_reference and pedido_id and external_reference != pedido_id:
        return False, "Referência externa do pagamento não pertence a este pedido."

    esperado = valor_para_centavos(pedido.get("valor"))
    recebido = int(round(float(pagamento.get("transaction_amount") or 0) * 100))
    if esperado <= 0 or recebido != esperado:
        return False, f"Valor divergente. Esperado {esperado} centavos, recebido {recebido} centavos."

    return True, "OK"


def telegram_api_url(metodo: str) -> str:
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{metodo}"


def enviar_telegram_sync(chat_id, text: str, reply_markup: dict | None = None, parse_mode: str = "Markdown") -> bool:
    if not BOT_TOKEN or not chat_id:
        return False
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resposta = requests.post(telegram_api_url("sendMessage"), json=payload, timeout=20)
        if not resposta.ok:
            logging.warning("Falha ao enviar mensagem Telegram via API: %s", resposta.text[:300])
        return resposta.ok
    except Exception as exc:
        logging.warning("Falha ao enviar mensagem Telegram via API: %s", exc)
        return False










































def processar_pagamento_aprovado_sync(pedido: dict, pagamento: dict, origem: str = "webhook") -> bool:
    if not pedido:
        return False

    payment_id = str(pagamento.get("id") or pedido.get("mp_payment_id") or "")
    if payment_id and not iniciar_processamento_pagamento(payment_id):
        logging.info("Pagamento %s já está em processamento ou já foi processado.", payment_id)
        return False

    try:
        valido, motivo = pagamento_aprovado_e_valido(pedido, pagamento)
        if not valido:
            logging.warning("Pagamento não processado: %s", motivo)
            return False

        status_api_antes = str(pedido.get("plataforma_api_status") or "").strip().lower()

        pedido["status"] = "pagamento_aprovado"
        pedido["mp_payment_id"] = payment_id
        pedido["mp_status"] = str(pagamento.get("status") or "approved")
        pedido["aprovado_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
        pedido["aprovado_por"] = "Mercado Pago"
        pedido["processado_por"] = origem

        # Salva o estado aprovado antes de chamar a plataforma.
        # Se o bot cair durante o processamento, o restart não trata o pedido
        # como um pagamento novo sem histórico.
        salvar_pedido_pendente(pedido)

        if pedido.get("catalogo") in CATALOGOS_COM_ENVIO_API:
            if pedido_ja_enviado_para_plataforma(pedido):
                pedido["plataforma_api_status"] = "enviado"
            elif envio_plataforma_bloqueado_para_auto(pedido):
                if status_api_antes != "revisao_manual":
                    marcar_envio_plataforma_para_revisao_manual(
                        pedido,
                        origem=f"{origem}_restart_guard",
                        motivo=(
                            "Envio automático bloqueado: este pedido já tinha uma tentativa de envio "
                            f"registrada como '{status_api_antes or 'desconhecido'}'. Para evitar duplicidade após restart/webhook, "
                            "confira na plataforma antes de reenviar."
                        ),
                    )
                salvar_pedido_pendente(pedido)
            elif pagamento_antigo_sem_trava_deve_ir_para_revisao(pedido, pagamento):
                marcar_envio_plataforma_para_revisao_manual(
                    pedido,
                    origem=f"{origem}_pagamento_antigo",
                    motivo=(
                        "Pagamento aprovado antes desta instância do bot subir. O envio automático foi pausado "
                        "porque pode ser webhook/pedido antigo reprocessado após restart do Railway."
                    ),
                )
                salvar_pedido_pendente(pedido)
            else:
                pedido["plataforma_api_status"] = "processando"
                pedido["plataforma_processando_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
                pedido["plataforma_tentativa_envio_em"] = pedido["plataforma_processando_em"]
                salvar_pedido_pendente(pedido)
                try:
                    resultado = criar_pedido_plataforma_sync(pedido)
                    pedido["plataforma_api_status"] = "enviado"
                    pedido["plataforma_service_id"] = resultado.get("service_id")
                    pedido["plataforma_quantidade"] = resultado.get("quantity")
                    pedido["plataforma_order_id"] = resultado.get("order_id") or "Não informado"
                    pedido["plataforma_resposta"] = resultado.get("response")
                    salvar_pedido_pendente(pedido)
                except Exception as exc:
                    marcar_envio_plataforma_para_revisao_manual(
                        pedido,
                        origem=f"{origem}_erro_api",
                        motivo=(
                            "A tentativa de envio para a plataforma falhou ou não retornou com segurança. "
                            f"Erro: {limpar_erro_api(exc)}. Confira na plataforma antes de tentar novamente."
                        ),
                    )
                    salvar_pedido_pendente(pedido)

        salvar_pedido_historico(pedido)
        marcar_pagamento_processado(payment_id, pedido)
        remover_pedido_pendente(str(pedido.get("pedido_id") or ""))

        # Pedidos concluídos não geram relatório automático. O Admin 1 é avisado
        # somente quando há falha, estoque indisponível ou revisão manual.
        notificar_admin_problema_pedido_sync(pedido)

        teclado_menu = {"inline_keyboard": [[{"text": "🏠 Menu inicial", "callback_data": "voltar:inicio"}]]}
        enviar_telegram_sync(
            pedido.get("user_id"),
            texto_final_pedido(pedido),
            reply_markup=teclado_menu,
        )
        return True
    finally:
        finalizar_processamento_pagamento(payment_id)


def processar_notificacao_mercado_pago_sync(payment_id: str, origem: str = "webhook") -> bool:
    """Consulta o Mercado Pago e processa o pedido fora da resposta HTTP do webhook."""
    try:
        pagamento = consultar_pagamento_mercado_pago_sync(payment_id)
        if str(pagamento.get("status")) != "approved":
            logging.info("Pagamento %s recebido no webhook com status %s.", payment_id, pagamento.get("status"))
            return False

        external_reference = pagamento.get("external_reference")
        pedido = obter_pedido_por_pagamento(payment_id, external_reference)
        if not pedido:
            pedido_historico = obter_pedido_historico_por_pagamento(payment_id, external_reference)
            if pedido_historico:
                marcar_pagamento_processado(payment_id, pedido_historico)
                logging.info("Webhook antigo ignorado: pagamento %s já está no histórico.", payment_id)
                return True

            # Se o pagamento foi aprovado mas não há pedido pendente, reprocessar
            # esse mesmo webhook a cada restart só cria risco de duplicidade.
            logging.warning("Pagamento aprovado sem pedido pendente: %s. Webhook será encerrado para evitar repetição.", payment_id)
            return True

        return processar_pagamento_aprovado_sync(pedido, pagamento, origem=origem)
    except Exception as exc:
        logging.exception("Erro ao processar notificação Mercado Pago: %s", limpar_erro_api(exc))
        return False

def processar_eventos_webhook_pendentes_sync(limite: int = 20):
    """Processa eventos de webhook persistidos no SQLite com retry."""
    eventos = DB.listar_webhooks_pendentes(limite=limite, max_attempts=WEBHOOK_QUEUE_MAX_ATTEMPTS)
    for evento in eventos:
        event_id = int(evento["id"])
        payment_id = str(evento.get("payment_id") or "")
        if not payment_id:
            DB.marcar_webhook_erro(event_id, "payment_id vazio")
            continue
        if pagamento_ja_processado(payment_id):
            DB.marcar_webhook_processado(event_id)
            continue
        if not DB.marcar_webhook_processando(event_id):
            continue
        try:
            ok = processar_notificacao_mercado_pago_sync(payment_id, origem=evento.get("origem") or "webhook_queue")
            if ok or pagamento_ja_processado(payment_id):
                DB.marcar_webhook_processado(event_id)
            else:
                DB.marcar_webhook_erro(event_id, "Pagamento ainda não processado. Será tentado novamente.")
        except Exception as exc:
            DB.marcar_webhook_erro(event_id, limpar_erro_api(exc))


def iniciar_rotina_webhook_queue():
    """Inicia uma rotina leve para reprocessar webhooks pendentes após restart/falha."""
    def worker():
        while True:
            try:
                processar_eventos_webhook_pendentes_sync()
            except Exception as exc:
                logging.warning("Falha na rotina da fila de webhook: %s", exc)
            time_module.sleep(max(15, WEBHOOK_QUEUE_INTERVAL))

    thread = threading.Thread(target=worker, daemon=True, name="webhook-queue")
    thread.start()


def extrair_payment_id_webhook(dados: dict) -> str | None:
    candidatos = [
        dados.get("id"),
        dados.get("data", {}).get("id") if isinstance(dados.get("data"), dict) else None,
        dados.get("resource"),
        request.args.get("id") if request else None,
        request.args.get("data.id") if request else None,
    ]
    for item in candidatos:
        if item is None:
            continue
        texto = str(item).strip()
        match = re.search(r"(\d+)$", texto)
        if match:
            return match.group(1)
    return None


def criar_flask_app():
    if Flask is None:
        return None

    web_app = Flask(__name__)

    @web_app.get("/")
    def home():
        return "TW Store Bot online", 200

    @web_app.get("/health")
    def health():
        return jsonify({"ok": True})

    @web_app.route("/webhook/mercadopago", methods=["GET", "POST"])
    def webhook_mercado_pago():
        if request.method == "GET":
            return jsonify({"ok": True, "route": "/webhook/mercadopago"})

        if MP_WEBHOOK_SECRET:
            segredo_recebido = request.args.get("secret") or request.headers.get("X-Webhook-Secret")
            if segredo_recebido != MP_WEBHOOK_SECRET:
                return jsonify({"ok": False, "error": "unauthorized"}), 401

        dados = request.get_json(silent=True) or {}
        payment_id = extrair_payment_id_webhook(dados)
        if not payment_id:
            logging.info("Webhook Mercado Pago sem payment_id. Dados: %s Args: %s", dados, dict(request.args))
            return jsonify({"ok": True, "ignored": "payment_id_not_found"})

        if pagamento_ja_processado(payment_id):
            return jsonify({"ok": True, "ignored": "already_processed", "payment_id": payment_id})

        DB.enfileirar_webhook(payment_id, payload=dados, origem="webhook")
        thread = threading.Thread(
            target=processar_eventos_webhook_pendentes_sync,
            kwargs={"limite": 5},
            daemon=True,
        )
        thread.start()

        # O Mercado Pago espera HTTP 200/201 rapidamente. O evento fica persistido
        # no SQLite e será reprocessado mesmo se o bot reiniciar.
        return jsonify({"ok": True, "queued": True, "payment_id": payment_id})

    return web_app


def iniciar_servidor_web():
    web_app = criar_flask_app()
    if web_app is None:
        logging.warning("Flask não instalado. Webhook Mercado Pago indisponível.")
        return

    try:
        port = int(os.getenv("PORT", "8080"))
    except ValueError:
        port = 8080

    def run():
        web_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    logging.info("Servidor webhook iniciado na porta %s", port)


def chave_env_service_id(catalogo: str, servico_chave: str) -> str:
    bruto = f"PANEL_SERVICE_ID_{catalogo}_{servico_chave}".upper()
    return re.sub(r"[^A-Z0-9]+", "_", bruto).strip("_")


def quantidade_para_api(valor) -> int:
    texto = str(valor or "").strip()
    texto = texto.replace(".", "").replace(",", "")
    numeros = re.sub(r"[^0-9]", "", texto)
    if not numeros:
        raise PlataformaAPIConfigError("Quantidade do pedido não encontrada para envio à plataforma.")
    return int(numeros)


def obter_service_id_api(pedido: dict) -> str:
    service_id = str(pedido.get("api_service_id") or "").strip()
    if service_id and service_id.lower() not in ("none", "null", "0"):
        return service_id

    catalogo = str(pedido.get("catalogo_api") or pedido.get("catalogo") or "").strip()
    servico_chave = str(pedido.get("servico_chave") or "").strip()
    if catalogo and servico_chave:
        env_name = chave_env_service_id(catalogo, servico_chave)
        service_id = os.getenv(env_name, "").strip()
        if service_id:
            return service_id

    raise PlataformaAPIConfigError(
        "Service ID da plataforma não configurado. "
        "Preencha api_service_id no catalogo.json ou use a variável "
        f"{chave_env_service_id(catalogo, servico_chave)} no .env."
    )


def extrair_order_id(resultado) -> str:
    if isinstance(resultado, dict):
        for chave in ("order", "order_id", "id"):
            if resultado.get(chave) is not None:
                return str(resultado[chave])
    return ""


def numero_decimal_plataforma(valor) -> float | None:
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)

    texto = str(valor).strip()
    if not texto:
        return None

    texto = re.sub(r"[^0-9,.-]", "", texto)
    if not texto or texto in {"-", ",", "."}:
        return None

    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif "," in texto:
        texto = texto.replace(",", ".")

    try:
        return float(texto)
    except ValueError:
        return None


def requisicao_plataforma_sync(payload: dict):
    if not PANEL_API_URL:
        raise PlataformaAPIConfigError("PANEL_API_URL não configurada no .env.")
    if not PANEL_API_KEY:
        raise PlataformaAPIConfigError("PANEL_API_KEY não configurada no .env.")

    dados_envio = {"key": PANEL_API_KEY}
    dados_envio.update(payload or {})

    try:
        resposta = requests.post(PANEL_API_URL, data=dados_envio, timeout=PANEL_API_TIMEOUT)
    except requests.RequestException as exc:
        raise PlataformaAPIRequestError(f"Falha de conexão com a plataforma: {limpar_erro_api(exc)}") from exc

    try:
        resultado = resposta.json()
    except ValueError:
        resultado = {"raw": resposta.text[:500]}

    if not resposta.ok:
        raise PlataformaAPIRequestError(
            f"A plataforma respondeu HTTP {resposta.status_code}: {limpar_erro_api(resultado)}"
        )

    if isinstance(resultado, dict) and resultado.get("error"):
        raise PlataformaAPIRequestError(f"Erro retornado pela plataforma: {limpar_erro_api(resultado.get('error'))}")

    return resultado


def consultar_saldo_plataforma_sync() -> dict:
    resultado = requisicao_plataforma_sync({"action": "balance"})
    if not isinstance(resultado, dict):
        raise PlataformaAPIRequestError(f"Retorno inesperado ao consultar saldo: {limpar_erro_api(resultado)}")

    saldo_raw = (
        resultado.get("balance")
        or resultado.get("saldo")
        or resultado.get("amount")
        or resultado.get("funds")
    )
    saldo = numero_decimal_plataforma(saldo_raw)
    if saldo is None:
        raise PlataformaAPIRequestError(f"Não consegui interpretar o saldo da plataforma: {limpar_erro_api(resultado)}")

    return {
        "saldo": saldo,
        "saldo_raw": saldo_raw,
        "moeda": resultado.get("currency") or resultado.get("moeda") or "",
        "raw": resultado,
    }


def consultar_servicos_plataforma_sync() -> list:
    agora_cache = time_module.time()
    dados_cache = PLATAFORMA_SERVICOS_CACHE.get("dados")
    if (
        PANEL_SERVICES_CACHE_TTL > 0
        and isinstance(dados_cache, list)
        and agora_cache < float(PLATAFORMA_SERVICOS_CACHE.get("expira_em") or 0)
    ):
        return dados_cache

    resultado = requisicao_plataforma_sync({"action": "services"})
    if isinstance(resultado, list):
        servicos = resultado
    elif isinstance(resultado, dict):
        servicos = None
        for chave in ("services", "data", "result"):
            if isinstance(resultado.get(chave), list):
                servicos = resultado[chave]
                break
        if servicos is None:
            raise PlataformaAPIRequestError(f"Retorno inesperado ao consultar serviços: {limpar_erro_api(resultado)}")
    else:
        raise PlataformaAPIRequestError(f"Retorno inesperado ao consultar serviços: {limpar_erro_api(resultado)}")

    PLATAFORMA_SERVICOS_CACHE["dados"] = servicos
    PLATAFORMA_SERVICOS_CACHE["expira_em"] = agora_cache + max(0, PANEL_SERVICES_CACHE_TTL)
    return servicos


def buscar_servico_plataforma_sync(service_id: str) -> dict | None:
    service_id = str(service_id or "").strip()
    if not service_id:
        return None

    servicos = consultar_servicos_plataforma_sync()
    for servico in servicos:
        if not isinstance(servico, dict):
            continue
        sid = str(servico.get("service") or servico.get("id") or servico.get("service_id") or "").strip()
        if sid == service_id:
            return servico
    return None


def formatar_inteiro_br(valor) -> str:
    try:
        numero = int(float(valor))
    except (TypeError, ValueError):
        return str(valor or "").strip()
    return f"{numero:,}".replace(",", ".")


def calcular_limite_solicitacoes_plataforma_sync(
    catalogo: str,
    servico_chave: str,
    quantidade,
    api_service_id: str | None = None,
) -> dict | None:
    """Calcula quantas vezes o pacote selecionado cabe no limite máximo do serviço no painel."""
    if not PANEL_API_URL or not PANEL_API_KEY:
        return None

    pedido_base = {
        "catalogo": catalogo,
        "servico_chave": servico_chave,
        "quantidade": quantidade,
        "quantidade_api": quantidade,
        "api_service_id": api_service_id,
    }
    service_id = obter_service_id_api(pedido_base)
    servico = buscar_servico_plataforma_sync(service_id)
    if servico is None:
        return None

    quantidade_pacote = quantidade_para_api(quantidade)
    maximo = numero_decimal_plataforma(servico.get("max"))
    minimo = numero_decimal_plataforma(servico.get("min"))
    if maximo is None or quantidade_pacote <= 0:
        return None

    maximo_int = int(maximo)
    minimo_int = int(minimo) if minimo is not None else None
    solicitacoes_possiveis = maximo_int // quantidade_pacote

    return {
        "service_id": service_id,
        "quantidade_pacote": quantidade_pacote,
        "maximo": maximo_int,
        "minimo": minimo_int,
        "solicitacoes_possiveis": solicitacoes_possiveis,
        "maximo_texto": formatar_inteiro_br(maximo_int),
        "minimo_texto": formatar_inteiro_br(minimo_int) if minimo_int is not None else "",
        "solicitacoes_texto": formatar_inteiro_br(solicitacoes_possiveis),
    }


def aplicar_limite_solicitacoes_no_pedido(pedido: dict, info: dict | None):
    if not pedido or not info:
        return
    pedido["plataforma_estoque_max"] = info.get("maximo")
    pedido["plataforma_estoque_max_texto"] = info.get("maximo_texto")
    pedido["plataforma_solicitacoes_possiveis"] = info.get("solicitacoes_possiveis")
    pedido["plataforma_solicitacoes_possiveis_texto"] = info.get("solicitacoes_texto")


def linha_solicitacoes_possiveis_pagamento(pedido: dict) -> str:
    texto = (pedido or {}).get("plataforma_solicitacoes_possiveis_texto")
    if not texto:
        return ""

    try:
        numero = int(str((pedido or {}).get("plataforma_solicitacoes_possiveis") or texto).replace(".", ""))
    except (TypeError, ValueError):
        numero = None
    vezes = "vez" if numero == 1 else "vezes"
    return f"• Pode solicitar até: {texto} {vezes} este pacote\n"


def texto_limite_solicitacoes(info: dict | None) -> str:
    if not info:
        return ""

    linhas = [f"📊 Limite disponível: {info.get('maximo_texto', '')}"]
    solicitacoes = info.get("solicitacoes_possiveis")
    if solicitacoes is not None:
        vezes = "vez" if int(solicitacoes) == 1 else "vezes"
        linhas.append(f"Pode solicitar até: {info.get('solicitacoes_texto', solicitacoes)} {vezes} este pacote")
    return "\n".join(linhas).strip()


def aplicar_limite_solicitacoes_na_mensagem(mensagem: str, info: dict | None) -> str:
    texto_estoque = texto_limite_solicitacoes(info)
    if not mensagem or not texto_estoque:
        return mensagem

    mensagem = str(mensagem)
    padrao_estoque = re.compile(r"(?mi)^\s*(?:📊\s*)?(?:Estoque|Limite disponível)\s*:\s*.*$")
    if padrao_estoque.search(mensagem):
        mensagem = padrao_estoque.sub(texto_estoque, mensagem, count=1)
    else:
        padrao_plataforma = re.compile(r"(?mi)^(\s*(?:📲\s*)?Plataforma\s*:\s*.*)$")
        if padrao_plataforma.search(mensagem):
            mensagem = padrao_plataforma.sub(r"\1\n" + texto_estoque, mensagem, count=1)
        else:
            mensagem = texto_estoque + "\n\n" + mensagem

    # Evita duplicar a linha caso uma versão antiga do catálogo já tenha essa informação fixa.
    mensagem = re.sub(
        r"(?mi)^\s*(?:🔁\s*)?Pode solicitar até\s*:\s*.*$",
        "",
        mensagem,
    )
    mensagem = re.sub(r"\n{3,}", "\n\n", mensagem).strip()
    if "Pode solicitar até:" not in mensagem:
        linhas = mensagem.splitlines()
        for i, linha in enumerate(linhas):
            if re.match(r"\s*Estoque\s*:", linha, flags=re.IGNORECASE):
                linhas.insert(i + 1, texto_estoque.splitlines()[-1])
                mensagem = "\n".join(linhas)
                break
    return mensagem


async def obter_limite_solicitacoes_item(
    catalogo: str,
    servico_chave: str,
    item: dict,
    servico: dict,
) -> dict | None:
    if catalogo not in CATALOGOS_COM_ENVIO_API:
        return None

    api_service_id = item.get("api_service_id") or servico.get("api_service_id")
    quantidade = item.get("quantidade")
    try:
        return await asyncio.to_thread(
            calcular_limite_solicitacoes_plataforma_sync,
            catalogo,
            servico_chave,
            quantidade,
            api_service_id,
        )
    except (PlataformaAPIConfigError, PlataformaAPIRequestError, PlataformaEstoqueIndisponivel) as exc:
        logging.warning("Não foi possível consultar o estoque/limite da plataforma: %s", limpar_erro_api(exc))
    except Exception as exc:
        logging.warning("Erro inesperado ao consultar estoque/limite da plataforma: %s", limpar_erro_api(exc))
    return None


def estimar_custo_pedido_plataforma_sync(pedido: dict) -> dict:
    service_id = obter_service_id_api(pedido)
    quantidade = quantidade_para_api(pedido.get("quantidade_api") or pedido.get("quantidade"))

    servico = buscar_servico_plataforma_sync(service_id)
    if servico is None:
        raise PlataformaEstoqueIndisponivel(
            f"Service ID {service_id} não encontrado na lista de serviços da plataforma."
        )

    minimo = numero_decimal_plataforma(servico.get("min"))
    maximo = numero_decimal_plataforma(servico.get("max"))
    if minimo is not None and quantidade < int(minimo):
        raise PlataformaEstoqueIndisponivel(
            f"Quantidade {quantidade} abaixo do mínimo permitido pela plataforma ({int(minimo)})."
        )
    if maximo is not None and quantidade > int(maximo):
        raise PlataformaEstoqueIndisponivel(
            f"Quantidade {quantidade} acima do máximo permitido pela plataforma ({int(maximo)})."
        )

    rate = numero_decimal_plataforma(
        servico.get("rate")
        or servico.get("price")
        or servico.get("valor")
        or servico.get("custo")
    )
    custo = None
    if rate is not None:
        custo = round((rate * quantidade) / 1000, 6)

    return {
        "service_id": service_id,
        "quantidade": quantidade,
        "servico": servico,
        "rate": rate,
        "custo_estimado": custo,
    }


def verificar_reposicao_antes_pagamento_sync(pedido: dict) -> tuple[bool, str]:
    if not CHECK_ESTOQUE_ANTES_PAGAMENTO:
        return True, "Verificação antes do pagamento desativada."

    if pedido.get("catalogo") not in CATALOGOS_COM_ENVIO_API:
        return True, "Catálogo sem envio automático para plataforma."

    saldo_info = consultar_saldo_plataforma_sync()
    saldo = float(saldo_info["saldo"])
    moeda = str(saldo_info.get("moeda") or "").strip()

    estimativa = estimar_custo_pedido_plataforma_sync(pedido)
    custo = estimativa.get("custo_estimado")
    service_id = estimativa.get("service_id")
    quantidade = estimativa.get("quantidade")

    if custo is not None:
        necessario = float(custo) + float(MARGEM_SALDO_PLATAFORMA)
        if saldo + 0.000001 < necessario:
            detalhe = (
                "Saldo insuficiente na plataforma antes de gerar o Pix. "
                f"Saldo: {saldo:.6f} {moeda}; necessário estimado: {necessario:.6f} {moeda}; "
                f"service_id: {service_id}; quantidade: {quantidade}."
            )
            return False, detalhe

        detalhe = (
            "Saldo confirmado antes do pagamento. "
            f"Saldo: {saldo:.6f} {moeda}; custo estimado: {float(custo):.6f} {moeda}; "
            f"service_id: {service_id}; quantidade: {quantidade}."
        )
        return True, detalhe

    if saldo <= float(MARGEM_SALDO_PLATAFORMA):
        detalhe = (
            "Saldo zerado/insuficiente na plataforma antes de gerar o Pix. "
            f"Saldo: {saldo:.6f} {moeda}; service_id: {service_id}; quantidade: {quantidade}."
        )
        return False, detalhe

    detalhe = (
        "Saldo positivo confirmado antes do pagamento, mas não foi possível estimar o custo do serviço. "
        f"Saldo: {saldo:.6f} {moeda}; service_id: {service_id}; quantidade: {quantidade}."
    )
    return True, detalhe


def mensagem_cliente_sem_reposicao() -> str:
    return (
        "⚠️ *Serviço temporariamente sem reposição de estoque.*\n\n"
        "No momento não consigo liberar esse pedido automaticamente. "
        "Tente novamente mais tarde ou fale com o atendimento.\n\n"
        "✅ Nenhum Pix foi gerado e você não precisa pagar nada agora."
    )


def texto_admin_bloqueio_sem_reposicao(pedido: dict, detalhe: str) -> str:
    username = f"@{pedido.get('username')}" if pedido.get("username") else "Sem username"
    return (
        "🚫 *PEDIDO BLOQUEADO ANTES DO PAGAMENTO*\n\n"
        "O cliente tentou iniciar um pedido, mas o bot não gerou Pix porque detectou falta de saldo/reposição na plataforma.\n\n"
        f"🆔 *Pedido:* `{md(pedido.get('pedido_id', ''))}`\n"
        f"🗂️ *Catálogo:* {md(pedido.get('catalogo', ''))}\n"
        f"📌 *Serviço:* {md(pedido.get('servico', ''))}\n"
        f"🔢 *Quantidade:* {md(pedido.get('quantidade', ''))}\n"
        f"💰 *Valor que seria cobrado:* R$ {md(pedido.get('valor', ''))}\n"
        f"🔗 *Link/@:* {md(pedido.get('link', ''))}\n\n"
        f"👤 *Cliente:* {md(pedido.get('usuario', 'Cliente'))}\n"
        f"📱 *Telegram:* {md(username)}\n"
        f"🆔 *ID Telegram:* `{pedido.get('user_id', '')}`\n\n"
        f"⚠️ *Detalhe interno:* {md(limpar_erro_api(detalhe))}\n\n"
        "Reponha saldo na plataforma ou troque o Service ID do serviço no catálogo."
    )


async def avisar_admin_bloqueio_sem_reposicao(context: ContextTypes.DEFAULT_TYPE, pedido: dict, detalhe: str):
    if not ADMIN_CHAT_ID:
        return
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=texto_admin_bloqueio_sem_reposicao(pedido, detalhe),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logging.warning("Falha ao avisar admin sobre bloqueio sem reposição: %s", exc)


async def verificar_reposicao_antes_pagamento(update: Update, context: ContextTypes.DEFAULT_TYPE, pedido: dict) -> bool:
    if not pedido:
        return False

    try:
        ok, detalhe = await asyncio.to_thread(verificar_reposicao_antes_pagamento_sync, pedido)
    except (PlataformaAPIConfigError, PlataformaAPIRequestError, PlataformaEstoqueIndisponivel) as exc:
        ok = False
        detalhe = limpar_erro_api(exc)
    except Exception as exc:
        ok = False
        detalhe = f"Erro inesperado ao verificar saldo/reposição: {limpar_erro_api(exc)}"

    if ok:
        pedido["ultima_verificacao_reposicao"] = detalhe
        return True

    pedido["status"] = "bloqueado_sem_reposicao"
    pedido["bloqueado_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
    pedido["motivo_bloqueio"] = detalhe

    await avisar_admin_bloqueio_sem_reposicao(context, pedido, detalhe)
    await enviar_texto_sequencial(
        update,
        context,
        mensagem_cliente_sem_reposicao(),
        InlineKeyboardMarkup([[btn("🏠 Menu inicial", "voltar:inicio")]]),
    )
    return False



def criar_pedido_plataforma_sync(pedido: dict) -> dict:
    if pedido.get("catalogo") not in CATALOGOS_COM_ENVIO_API:
        return {"skipped": True}

    if not PANEL_API_URL:
        raise PlataformaAPIConfigError("PANEL_API_URL não configurada no .env.")
    if not PANEL_API_KEY:
        raise PlataformaAPIConfigError("PANEL_API_KEY não configurada no .env.")

    service_id = obter_service_id_api(pedido)
    quantidade = quantidade_para_api(pedido.get("quantidade_api") or pedido.get("quantidade"))
    link = str(pedido.get("link") or "").strip()
    if not link:
        raise PlataformaAPIConfigError("Link/@ não encontrado no pedido.")

    payload = {
        "key": PANEL_API_KEY,
        "action": "add",
        "service": service_id,
        "link": link,
        "quantity": quantidade,
    }

    try:
        resposta = requests.post(PANEL_API_URL, data=payload, timeout=PANEL_API_TIMEOUT)
    except requests.RequestException as exc:
        raise PlataformaAPIRequestError(f"Falha de conexão com a plataforma: {limpar_erro_api(exc)}") from exc

    try:
        resultado = resposta.json()
    except ValueError:
        resultado = {"raw": resposta.text[:500]}

    if not resposta.ok:
        raise PlataformaAPIRequestError(
            f"A plataforma respondeu HTTP {resposta.status_code}: {limpar_erro_api(resultado)}"
        )

    if isinstance(resultado, dict) and resultado.get("error"):
        raise PlataformaAPIRequestError(f"Erro retornado pela plataforma: {limpar_erro_api(resultado.get('error'))}")

    return {
        "service_id": service_id,
        "quantity": quantidade,
        "response": resultado,
        "order_id": extrair_order_id(resultado),
    }


def consultar_status_pedido_plataforma_sync(order_id: str) -> dict:
    order_id = normalizar_id_consulta(order_id)
    if not order_id:
        raise PlataformaAPIConfigError("ID do pedido não informado.")
    if not PANEL_API_URL:
        raise PlataformaAPIConfigError("PANEL_API_URL não configurada no .env.")
    if not PANEL_API_KEY:
        raise PlataformaAPIConfigError("PANEL_API_KEY não configurada no .env.")

    payload = {
        "key": PANEL_API_KEY,
        "action": "status",
        "order": order_id,
    }

    try:
        resposta = requests.post(PANEL_API_URL, data=payload, timeout=PANEL_API_TIMEOUT)
    except requests.RequestException as exc:
        raise PlataformaAPIRequestError(f"Falha de conexão com a plataforma: {limpar_erro_api(exc)}") from exc

    try:
        resultado = resposta.json()
    except ValueError:
        resultado = {"raw": resposta.text[:500]}

    if not resposta.ok:
        raise PlataformaAPIRequestError(
            f"A plataforma respondeu HTTP {resposta.status_code}: {limpar_erro_api(resultado)}"
        )

    if isinstance(resultado, dict) and resultado.get("error"):
        raise PlataformaAPIRequestError(f"Erro retornado pela plataforma: {limpar_erro_api(resultado.get('error'))}")

    return resultado if isinstance(resultado, dict) else {"raw": resultado}


def solicitar_refil_pedido_plataforma_sync(order_id: str) -> dict:
    order_id = normalizar_id_consulta(order_id)
    if not order_id:
        raise PlataformaAPIConfigError("ID do pedido não informado.")
    if not PANEL_API_URL:
        raise PlataformaAPIConfigError("PANEL_API_URL não configurada no .env.")
    if not PANEL_API_KEY:
        raise PlataformaAPIConfigError("PANEL_API_KEY não configurada no .env.")

    payload = {
        "key": PANEL_API_KEY,
        "action": "refill",
        "order": order_id,
    }

    try:
        resposta = requests.post(PANEL_API_URL, data=payload, timeout=PANEL_API_TIMEOUT)
    except requests.RequestException as exc:
        raise PlataformaAPIRequestError(f"Falha de conexão com a plataforma: {limpar_erro_api(exc)}") from exc

    try:
        resultado = resposta.json()
    except ValueError:
        resultado = {"raw": resposta.text[:500]}

    if not resposta.ok:
        raise PlataformaAPIRequestError(
            f"A plataforma respondeu HTTP {resposta.status_code}: {limpar_erro_api(resultado)}"
        )

    if isinstance(resultado, dict) and resultado.get("error"):
        raise PlataformaAPIRequestError(f"Reposição/refil indisponível: {limpar_erro_api(resultado.get('error'))}")

    return resultado if isinstance(resultado, dict) else {"raw": resultado}


STATUS_PLATAFORMA_PT = {
    "pending": "Pendente",
    "in progress": "Em andamento",
    "inprogress": "Em andamento",
    "processing": "Processando",
    "completed": "Concluído",
    "complete": "Concluído",
    "partial": "Parcial",
    "canceled": "Cancelado",
    "cancelled": "Cancelado",
}


def traduzir_status_plataforma(status) -> str:
    texto = str(status or "desconhecido").strip()
    return STATUS_PLATAFORMA_PT.get(texto.lower(), texto or "desconhecido")


def traduzir_status_local(status) -> str:
    mapa = {
        "aguardando_link": "Aguardando link/@ do cliente",
        "aguardando_email_iptv": "Aguardando e-mail do cliente",
        "aguardando_pagamento": "Aguardando pagamento",
        "aguardando_aprovacao_admin": "Comprovante em análise",
        "pagamento_aprovado": "Pagamento aprovado",
        "comprovante_reprovado": "Comprovante reprovado",
        "pagamento_expirado": "Pagamento expirado",
        "pendente_removido_restart": "Pendente removido no restart",
    }
    texto = str(status or "").strip()
    return mapa.get(texto, texto or "Não informado")


def texto_status_pedido_local(pedido: dict, origem: str | None = None) -> str:
    plataforma_id = pedido.get("plataforma_order_id")
    status_api = pedido.get("plataforma_api_status")
    linhas = [
        "📦 *Resumo do seu pedido*",
        "",
        f"🆔 *ID do pedido:* `{md(pedido.get('pedido_id', ''))}`",
        f"📌 *Status:* {md(traduzir_status_local(pedido.get('status')))}",
    ]

    if pedido.get("catalogo"):
        linhas.append(f"🗂️ *Catálogo:* {md(pedido.get('catalogo'))}")
    if pedido.get("servico"):
        linhas.append(f"🛒 *Serviço:* {md(pedido.get('servico'))}")
    if pedido.get("quantidade"):
        linhas.append(f"🔢 *Quantidade:* {md(pedido.get('quantidade'))}")
    if pedido_tem_id_plataforma(plataforma_id):
        linhas.append(f"🚀 *ID na plataforma:* `{md(plataforma_id)}`")
    if status_api:
        linhas.append(f"📡 *Envio para plataforma:* {md(status_api)}")
    if pedido.get("plataforma_api_erro"):
        linhas.append(f"⚠️ *Erro no envio:* {md(pedido.get('plataforma_api_erro'))}")

    if origem == "pendente":
        linhas.extend([
            "",
            "Esse pedido ainda está no fluxo interno do bot. Assim que ele for enviado para a plataforma, o status atualizado aparecerá aqui.",
        ])

    return "\n".join(linhas)


def texto_status_pedido_plataforma(order_id: str, resultado: dict, pedido_local: dict | None = None) -> str:
    status = resultado.get("status") or resultado.get("Status") or resultado.get("state") or resultado.get("raw") or "desconhecido"
    linhas = [
        "🔎 *Status do pedido*",
        "",
    ]

    linhas.extend([
        f"🚀 *ID na plataforma:* `{md(order_id)}`",
        f"📌 *Status:* {md(traduzir_status_plataforma(status))}",
    ])

    campos = [
        ("start_count", "📈 *Contagem inicial*"),
        ("remains", "⏳ *Restante*"),
    ]
    for chave, rotulo in campos:
        valor = resultado.get(chave)
        if valor not in (None, ""):
            linhas.append(f"{rotulo}: {md(valor)}")

    if pedido_local:
        if pedido_local.get("catalogo"):
            linhas.append(f"🗂️ *Catálogo:* {md(pedido_local.get('catalogo'))}")
        if pedido_local.get("servico"):
            linhas.append(f"🛒 *Serviço:* {md(pedido_local.get('servico'))}")
        if pedido_local.get("quantidade"):
            linhas.append(f"🔢 *Quantidade:* {md(pedido_local.get('quantidade'))}")

    linhas.extend([
        "",
        "✅ Consulta feita diretamente na plataforma.",
    ])
    return "\n".join(linhas)


def extrair_refil_id(resultado: dict) -> str:
    if not isinstance(resultado, dict):
        return ""
    for chave in ("refill", "refill_id", "id", "order"):
        valor = resultado.get(chave)
        if valor not in (None, ""):
            return str(valor)
    return ""


def texto_refil_solicitado(order_id: str, resultado: dict) -> str:
    refil_id = extrair_refil_id(resultado)
    linhas = [
        "🔄 *Solicitação de reposição enviada*",
        "",
        f"🚀 *ID do pedido na plataforma:* `{md(order_id)}`",
    ]
    if refil_id:
        linhas.append(f"🧾 *ID da solicitação:* `{md(refil_id)}`")
    linhas.extend([
        "",
        "✅ Sua solicitação foi enviada para a plataforma.",
        "Você pode consultar pelo botão *🔎 Consultar Status* usando o mesmo ID.",
    ])
    return "\n".join(linhas)


def obter_order_id_para_refil(consulta_id: str) -> tuple[str | None, dict | None, str | None]:
    consulta_id = normalizar_id_consulta(consulta_id)
    pedido_local, origem = buscar_pedido_local_por_id(consulta_id)

    if pedido_local and pedido_tem_id_plataforma(pedido_local.get("plataforma_order_id")):
        return str(pedido_local.get("plataforma_order_id")), pedido_local, origem

    if consulta_id.isdigit() and pedido_tem_id_plataforma(consulta_id):
        return consulta_id, pedido_local, origem

    return None, pedido_local, origem


def botoes_consulta_pedido(plataforma_order_id: str | None = None) -> InlineKeyboardMarkup:
    keyboard = []
    if pedido_tem_id_plataforma(plataforma_order_id):
        order_id = str(plataforma_order_id)
        # O Telegram limita callback_data a 64 bytes. IDs comuns de painel são curtos;
        # se vier um ID grande, o cliente informa manualmente pelo submenu de refil.
        if len(f"pedido:refil:{order_id}".encode("utf-8")) <= 64:
            keyboard.append([btn("🔄 Solicitar Reposição", f"pedido:refil:{order_id}")])
        else:
            keyboard.append([btn("🔄 Solicitar Reposição", "pedido:solicitar_refil")])
    keyboard.append([btn("📦 Consultar outro pedido", "pedido:consultar_status")])
    keyboard.append([btn("🏠 Menu inicial", "voltar:inicio")])
    return InlineKeyboardMarkup(keyboard)


def menu_consultar_pedido() -> InlineKeyboardMarkup:
    keyboard = [
        [btn("🔎 Consultar Status", "pedido:consultar_status")],
        [btn("🔄 Solicitar Reposição", "pedido:solicitar_refil")],
        [btn("🏠 Voltar ao início", "voltar:inicio")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def enviar_pedido_para_plataforma(pedido: dict):
    if pedido.get("catalogo") not in CATALOGOS_COM_ENVIO_API:
        return

    if pedido_ja_enviado_para_plataforma(pedido):
        pedido["plataforma_api_status"] = "enviado"
        return

    if envio_plataforma_estava_processando(pedido):
        marcar_envio_plataforma_para_revisao_manual(pedido, origem="aprovacao_admin_restart_guard")
        if pedido.get("pedido_id"):
            salvar_pedido_pendente(pedido)
        return

    pedido["plataforma_api_status"] = "processando"
    pedido["plataforma_processando_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
    if pedido.get("pedido_id"):
        salvar_pedido_pendente(pedido)

    try:
        resultado = await asyncio.to_thread(criar_pedido_plataforma_sync, pedido)
    except (PlataformaAPIConfigError, PlataformaAPIRequestError) as exc:
        pedido["plataforma_api_status"] = "erro"
        pedido["plataforma_api_erro"] = limpar_erro_api(exc)
        if pedido.get("pedido_id"):
            salvar_pedido_pendente(pedido)
        return
    except Exception as exc:
        pedido["plataforma_api_status"] = "erro"
        pedido["plataforma_api_erro"] = limpar_erro_api(f"Erro inesperado: {exc}")
        if pedido.get("pedido_id"):
            salvar_pedido_pendente(pedido)
        return

    pedido["plataforma_api_status"] = "enviado"
    pedido["plataforma_service_id"] = resultado.get("service_id")
    pedido["plataforma_quantidade"] = resultado.get("quantity")
    pedido["plataforma_order_id"] = resultado.get("order_id") or "Não informado"
    pedido["plataforma_resposta"] = resultado.get("response")
    if pedido.get("pedido_id"):
        salvar_pedido_pendente(pedido)


def btn(texto: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(texto, callback_data=data)


def menu_principal() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("📖 Catálogo de Serviços", "menu:catalogo")],
        [btn("🔎 Consultar Pedido", "pedido:consultar")],
        [btn("🎟️ Abrir Ticket", "extra:atendimento")],
    ])


def menu_catalogos() -> InlineKeyboardMarkup:
    keyboard = [
        [btn("🚀 Engajamentos", "catalogo:redes_sociais")],
        [btn("🎫 Assinaturas", "catalogo:assinaturas")],
        [btn("🎞️ IPTV XCIPTV", "catalogo:iptv")],
        [btn("🛜 Internet Ilimitada", "catalogo:internet")],
        [btn("⬅️ Voltar", "voltar:inicio")],
    ]
    return InlineKeyboardMarkup(keyboard)


def menu_redes_sociais() -> InlineKeyboardMarkup:
    keyboard = [
        [btn("🟣 Instagram", "catalogo:instagram")],
        [btn("⚫ TikTok", "catalogo:tiktok")],
        [btn("🟠 Kwai", "catalogo:kwai")],
        [btn("⬅️ Voltar ao catálogo", "menu:catalogo")],
    ]
    return InlineKeyboardMarkup(keyboard)


def menu_instagram() -> InlineKeyboardMarkup:
    keyboard = [
        [btn("🌏 Serviços Estrangeiros", "catalogo_instagram:estrangeiros")],
        [btn("🇧🇷 Serviços Brasileiros", "catalogo_instagram:brasileiros")],
        [btn("⬅️ Voltar aos engajamentos", "catalogo:redes_sociais")],
    ]
    return InlineKeyboardMarkup(keyboard)


def menu_instagram_estrangeiros() -> InlineKeyboardMarkup:
    servicos = CATALOGO["catalogos"]["instagram"]["servicos"]
    nomes_botoes = {
        "seguidores": "👥 Seguidores",
        "curtidas": "❤️ Curtidas",
        "visualizacoes": "👁️‍🗨️ Visualizações",
    }
    keyboard = []
    for chave, servico in servicos.items():
        keyboard.append([btn(nomes_botoes.get(chave, servico["nome"]), f"servico:{chave}")])
    keyboard.append([btn("⬅️ Voltar ao Instagram", "catalogo:instagram")])
    return InlineKeyboardMarkup(keyboard)


def menu_instagram_brasileiros() -> InlineKeyboardMarkup:
    servicos = CATALOGO["catalogos"]["instagram"].get("servicos_brasileiros", {})
    nomes_botoes = {
        "seguidores": "👥 Seguidores",
    }
    keyboard = []
    for chave, servico in servicos.items():
        if chave != "seguidores":
            continue
        keyboard.append([btn(nomes_botoes.get(chave, servico.get("nome", chave.title())), f"servico_instagram_br:{chave}")])
    keyboard.append([btn("⬅️ Voltar ao Instagram", "catalogo:instagram")])
    return InlineKeyboardMarkup(keyboard)


def menu_tiktok() -> InlineKeyboardMarkup:
    keyboard = [
        [btn("🌏 Serviços Estrangeiros", "catalogo_tiktok:estrangeiros")],
        [btn("⬅️ Voltar aos engajamentos", "catalogo:redes_sociais")],
    ]
    return InlineKeyboardMarkup(keyboard)


def menu_tiktok_estrangeiros() -> InlineKeyboardMarkup:
    servicos = CATALOGO["catalogos"]["tiktok"]["servicos"]
    nomes_botoes = {
        "seguidores": "👤 Seguidores",
        "curtidas": "♥️ Curtidas",
        "visualizacoes": "👁️‍🗨️ Visualizações",
    }
    keyboard = []
    for chave, servico in servicos.items():
        keyboard.append([btn(nomes_botoes.get(chave, servico["nome"]), f"servico_tiktok:{chave}")])
    keyboard.append([btn("⬅️ Voltar", "catalogo:tiktok")])
    return InlineKeyboardMarkup(keyboard)


def menu_itens_tiktok(servico_chave: str) -> InlineKeyboardMarkup:
    servico = CATALOGO["catalogos"]["tiktok"]["servicos"][servico_chave]
    keyboard = []
    for item in servico["itens"]:
        texto = f'{item["quantidade_texto"]} {servico["nome"]} — {money(item["valor"])}'
        keyboard.append([btn(texto, f'item_tiktok:{servico_chave}:{item["quantidade"]}')])
    keyboard.append([btn("⬅️ Voltar", "catalogo_tiktok:estrangeiros")])
    return InlineKeyboardMarkup(keyboard)


def get_item_tiktok(servico_chave: str, quantidade: int) -> dict:
    servico = CATALOGO["catalogos"]["tiktok"]["servicos"][servico_chave]
    for item in servico["itens"]:
        if int(item["quantidade"]) == int(quantidade):
            return item
    raise KeyError("Item não encontrado")




def menu_kwai() -> InlineKeyboardMarkup:
    keyboard = [
        [btn("🇧🇷 Serviço Brasileiros", "catalogo_kwai:brasileiros")],
        [btn("⬅️ Voltar aos engajamentos", "catalogo:redes_sociais")],
    ]
    return InlineKeyboardMarkup(keyboard)


def menu_kwai_brasileiros() -> InlineKeyboardMarkup:
    servicos = CATALOGO["catalogos"]["kwai"]["servicos"]
    nomes_botoes = {
        "seguidores": "👤 Seguidores",
        "curtidas": "❤️ Curtidas",
        "visualizacoes": "👁️ Visualizações",
    }
    keyboard = []
    for chave, servico in servicos.items():
        keyboard.append([btn(nomes_botoes.get(chave, servico["nome"]), f"servico_kwai:{chave}")])
    keyboard.append([btn("⬅️ Voltar ao Kwai", "catalogo:kwai")])
    return InlineKeyboardMarkup(keyboard)


def menu_itens_kwai(servico_chave: str) -> InlineKeyboardMarkup:
    servico = CATALOGO["catalogos"]["kwai"]["servicos"][servico_chave]
    keyboard = []
    for item in servico["itens"]:
        texto = f'{item["quantidade_texto"]} {servico["nome"]} — {money(item["valor"])}'
        keyboard.append([btn(texto, f'item_kwai:{servico_chave}:{item["quantidade"]}')])
    keyboard.append([btn("⬅️ Voltar", "catalogo_kwai:brasileiros")])
    return InlineKeyboardMarkup(keyboard)


def get_item_kwai(servico_chave: str, quantidade: int) -> dict:
    servico = CATALOGO["catalogos"]["kwai"]["servicos"][servico_chave]
    for item in servico["itens"]:
        if int(item["quantidade"]) == int(quantidade):
            return item
    raise KeyError("Item Kwai não encontrado")


def menu_assinaturas() -> InlineKeyboardMarkup:
    servicos = CATALOGO["catalogos"]["assinaturas"]["servicos"]
    keyboard = [
        [btn(f'{servico["nome"]} — {money(servico["valor"])}', f"assinatura:{chave}")]
        for chave, servico in servicos.items()
    ]
    keyboard.append([btn("⬅️ Voltar ao catálogo", "menu:catalogo")])
    return InlineKeyboardMarkup(keyboard)


def menu_iptv() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("1 mês — R$ 15,00", "item_iptv:1mes:1")],
            [btn("⬅️ Voltar", "menu:catalogo")],
        ]
    )


def botoes_confirmar_email_iptv() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("✅ Confirmar e ir para pagamento", "confirmar_email_iptv")],
            [btn("✏️ Alterar e-mail", "alterar_email_iptv")],
            [btn("🏠 Cancelar / Menu", "voltar:inicio")],
        ]
    )


def menu_itens(servico_chave: str) -> InlineKeyboardMarkup:
    servico = CATALOGO["catalogos"]["instagram"]["servicos"][servico_chave]
    keyboard = []
    for item in servico["itens"]:
        texto = f'{item["quantidade_texto"]} {servico["nome"]} — {money(item["valor"])}'
        keyboard.append([btn(texto, f'item:{servico_chave}:{item["quantidade"]}')])
    keyboard.append([btn("⬅️ Voltar", "catalogo_instagram:estrangeiros")])
    return InlineKeyboardMarkup(keyboard)


def get_item(servico_chave: str, quantidade: int) -> dict:
    servico = CATALOGO["catalogos"]["instagram"]["servicos"][servico_chave]
    for item in servico["itens"]:
        if int(item["quantidade"]) == int(quantidade):
            return item
    raise KeyError("Item não encontrado")



def menu_itens_instagram_brasileiros(servico_chave: str) -> InlineKeyboardMarkup:
    servico = CATALOGO["catalogos"]["instagram"]["servicos_brasileiros"][servico_chave]
    keyboard = []
    for item in servico.get("itens", []):
        texto = f'{item["quantidade_texto"]} {servico["nome"]} — {money(item["valor"])}'
        keyboard.append([btn(texto, f'item_instagram_br:{servico_chave}:{item["quantidade"]}')])
    keyboard.append([btn("⬅️ Voltar aos serviços brasileiros", "catalogo_instagram:brasileiros")])
    return InlineKeyboardMarkup(keyboard)


def get_item_instagram_brasileiros(servico_chave: str, quantidade: int) -> dict:
    servico = CATALOGO["catalogos"]["instagram"]["servicos_brasileiros"][servico_chave]
    for item in servico.get("itens", []):
        if int(item["quantidade"]) == int(quantidade):
            return item
    raise KeyError("Item não encontrado")


def texto_pagamento(pedido: dict) -> str:
    # Monta a etapa de pagamento usando Pix dinâmico do Mercado Pago quando disponível.
    destino_label = "E-mail informado" if catalogo_exige_email(pedido) else "Link/@ enviado"
    destino_valor = pedido.get("link", "")

    resumo_base = (
        "💳 Etapa 2 de 3 — Pagamento\n\n"
        "✅ Seu pedido já foi separado com sucesso.\n"
        "Finalize o pagamento pelo Pix abaixo.\n\n"
        "📋 Resumo do Pedido\n\n"
        f"• Catálogo: {pedido.get('catalogo', '')}\n"
        f"• Serviço: {pedido.get('servico', '')}\n"
        f"• Quantidade: {pedido.get('quantidade', '')}\n"
        + linha_solicitacoes_possiveis_pagamento(pedido)
        + f"• {destino_label}: {destino_valor}\n"
        f"• Valor exato: R$ {pedido.get('valor', '')}\n\n"
    )

    if pedido.get("mp_qr_code"):
        return (
            resumo_base
            + "⌛️ Após o pagamento, aguarde alguns segundos.\n"
            "A confirmação é feita automaticamente pelo Mercado Pago.\n"
            "Caso necessário, toque em Verificar Pagamento."
        )

    return (
        resumo_base
        + "⌛️ Após o pagamento, envie o comprovante aqui na conversa.\n"
        "O pedido será liberado após a aprovação do pagamento."
    )

def fonte_pagamento(tamanho: int, negrito: bool = False):
    """Carrega uma fonte do sistema para gerar a arte de pagamento."""
    if ImageFont is None:
        return None

    candidatos = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if negrito else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if negrito else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if negrito else "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for caminho in candidatos:
        if os.path.exists(caminho):
            return ImageFont.truetype(caminho, tamanho)
    return ImageFont.load_default()


def texto_largura(draw, texto: str, fonte) -> int:
    bbox = draw.textbbox((0, 0), texto, font=fonte)
    return bbox[2] - bbox[0]


def normalizar_link_para_exibicao(link: str) -> str:
    texto = str(link or "").strip()
    if not texto:
        return ""

    if texto.startswith("@"):
        return texto

    match = re.search(r"instagram\.com/([A-Za-z0-9._]+)", texto, flags=re.IGNORECASE)
    if match:
        usuario = match.group(1).strip().strip("/")
        if usuario:
            return f"@{usuario}"

    match = re.search(r"tiktok\.com/@?([A-Za-z0-9._]+)", texto, flags=re.IGNORECASE)
    if match:
        usuario = match.group(1).strip().strip("/")
        if usuario:
            return f"@{usuario}"

    return texto


def quebrar_texto_inteligente(draw, texto: str, fonte, largura_max: int) -> list[str]:
    texto = str(texto or "").strip()
    if not texto:
        return [""]

    palavras = texto.split()
    if len(palavras) <= 1:
        if texto_largura(draw, texto, fonte) <= largura_max:
            return [texto]
        partes = []
        atual = ""
        for ch in texto:
            teste = atual + ch
            if atual and texto_largura(draw, teste, fonte) > largura_max:
                partes.append(atual)
                atual = ch
            else:
                atual = teste
        if atual:
            partes.append(atual)
        return partes or [texto]

    linhas = []
    linha = palavras[0]
    for palavra in palavras[1:]:
        teste = f"{linha} {palavra}"
        if texto_largura(draw, teste, fonte) <= largura_max:
            linha = teste
        else:
            linhas.append(linha)
            linha = palavra
    linhas.append(linha)
    return linhas


def ajustar_fonte_e_linhas(draw, texto: str, caixa, tamanho_max: int, tamanho_min: int = 18, negrito: bool = True, max_linhas: int = 1):
    x1, y1, x2, y2 = caixa
    largura_max = max(10, x2 - x1 - 12)
    altura_max = max(10, y2 - y1 - 8)

    for tamanho in range(tamanho_max, tamanho_min - 1, -1):
        fonte = fonte_pagamento(tamanho, negrito)
        linhas = quebrar_texto_inteligente(draw, texto, fonte, largura_max)
        if len(linhas) > max_linhas:
            continue

        alturas = []
        for linha in linhas:
            bbox = draw.textbbox((0, 0), linha, font=fonte)
            alturas.append(bbox[3] - bbox[1])
        altura_total = sum(alturas) + (len(linhas) - 1) * 4
        if altura_total <= altura_max:
            return fonte, linhas

    fonte = fonte_pagamento(tamanho_min, negrito)
    linhas = quebrar_texto_inteligente(draw, texto, fonte, largura_max)[:max_linhas]

    if linhas:
        ultima = linhas[-1]
        while ultima:
            teste = ultima + "…"
            if texto_largura(draw, teste, fonte) <= largura_max:
                linhas[-1] = teste
                break
            ultima = ultima[:-1]
        else:
            linhas[-1] = ""

    return fonte, linhas


def gerar_imagem_pagamento_instagram(pedido: dict) -> BytesIO | None:
    """Preenche o layout original enviado pelo cliente com os dados variáveis do pedido."""
    if Image is None or ImageDraw is None or ImageFont is None:
        return None
    if not PAGAMENTO_INSTAGRAM_LAYOUT_PATH.exists():
        return None

    img = Image.open(PAGAMENTO_INSTAGRAM_LAYOUT_PATH).convert("RGB")
    draw = ImageDraw.Draw(img)

    largura, altura = img.size
    sx = largura / 1024
    sy = altura / 1536

    def escala_caixa(caixa):
        x1, y1, x2, y2 = caixa
        return (
            int(x1 * sx),
            int(y1 * sy),
            int(x2 * sx),
            int(y2 * sy),
        )

    def escrever_caixa(texto: str, caixa_base, tamanho_max: int, tamanho_min: int = 22, cor=(255, 255, 255), negrito: bool = True, max_linhas: int = 1, align: str = "center"):
        caixa = escala_caixa(caixa_base)
        x1, y1, x2, y2 = caixa
        fonte, linhas = ajustar_fonte_e_linhas(
            draw,
            str(texto or "").strip(),
            caixa,
            max(12, int(tamanho_max * min(sx, sy))),
            max(10, int(tamanho_min * min(sx, sy))),
            negrito=negrito,
            max_linhas=max_linhas,
        )

        metricas = []
        for linha in linhas:
            bbox = draw.textbbox((0, 0), linha, font=fonte)
            metricas.append((linha, bbox, bbox[2] - bbox[0], bbox[3] - bbox[1]))

        altura_total = sum(m[3] for m in metricas) + max(0, len(metricas) - 1) * 4
        y = y1 + ((y2 - y1) - altura_total) / 2

        for linha, bbox, tw, th in metricas:
            if align == "left":
                tx = x1 + 10
            else:
                tx = x1 + ((x2 - x1) - tw) / 2
            ty = y - bbox[1]
            draw.text(
                (tx, ty),
                linha,
                font=fonte,
                fill=cor,
                stroke_width=1,
                stroke_fill=(0, 0, 0),
            )
            y += th + 4

    def apagar_area(caixa_base, margem=0):
        caixa = escala_caixa(caixa_base)
        x1, y1, x2, y2 = caixa
        m = int(margem * min(sx, sy))
        draw.rectangle([x1 - m, y1 - m, x2 + m, y2 + m], fill=(0, 0, 0))

    valor = str(pedido.get("valor", "0,00")).replace("R$", "").strip()
    catalogo = str(pedido.get("catalogo", "Instagram")).strip() or "Instagram"
    servico = str(pedido.get("servico", "")).strip()
    quantidade = str(pedido.get("quantidade", "")).strip()
    link = normalizar_link_para_exibicao(pedido.get("link", ""))

    # Campos dinâmicos em fonte maior e mais visível.
    # As caixas foram alargadas para o texto não encolher demais no Telegram.
    escrever_caixa(f"R$ {valor}", (255, 586, 615, 724), 90, 54, cor=(255, 255, 255), negrito=True, max_linhas=1)
    escrever_caixa(catalogo, (275, 850, 705, 980), 90, 54, cor=(255, 255, 255), negrito=True, max_linhas=1)
    escrever_caixa(servico, (275, 940, 705, 1072), 90, 50, cor=(255, 255, 255), negrito=True, max_linhas=2)
    escrever_caixa(quantidade, (275, 1040, 705, 1170), 90, 54, cor=(255, 255, 255), negrito=True, max_linhas=1)
    escrever_caixa(link, (295, 1134, 705, 1264), 90, 50, cor=(255, 255, 255), negrito=True, max_linhas=2)

    if PIX_CHAVE:
        apagar_area((201, 476, 640, 535), margem=2)
        escrever_caixa(PIX_CHAVE, (192, 458, 648, 552), 56, 30, cor=(255, 255, 255), negrito=True, max_linhas=1)

    arquivo = BytesIO()
    img.save(arquivo, format="PNG", optimize=True)
    arquivo.seek(0)
    arquivo.name = "pagamento_instagram.png"
    return arquivo

def gerar_imagem_pagamento_tiktok(pedido: dict) -> BytesIO | None:
    """Preenche o layout do TikTok com os dados variáveis do pedido."""
    if Image is None or ImageDraw is None or ImageFont is None:
        return None
    if not PAGAMENTO_TIKTOK_LAYOUT_PATH.exists():
        return None

    img = Image.open(PAGAMENTO_TIKTOK_LAYOUT_PATH).convert("RGB")
    draw = ImageDraw.Draw(img)

    largura, altura = img.size
    sx = largura / 1024
    sy = altura / 1536

    def escala_caixa(caixa):
        x1, y1, x2, y2 = caixa
        return (
            int(x1 * sx),
            int(y1 * sy),
            int(x2 * sx),
            int(y2 * sy),
        )

    def escrever_caixa(texto: str, caixa_base, tamanho_max: int, tamanho_min: int = 22, cor=(255, 255, 255), negrito: bool = True, max_linhas: int = 1, align: str = "center"):
        caixa = escala_caixa(caixa_base)
        x1, y1, x2, y2 = caixa
        fonte, linhas = ajustar_fonte_e_linhas(
            draw,
            str(texto or "").strip(),
            caixa,
            max(12, int(tamanho_max * min(sx, sy))),
            max(10, int(tamanho_min * min(sx, sy))),
            negrito=negrito,
            max_linhas=max_linhas,
        )

        metricas = []
        for linha in linhas:
            bbox = draw.textbbox((0, 0), linha, font=fonte)
            metricas.append((linha, bbox, bbox[2] - bbox[0], bbox[3] - bbox[1]))

        altura_total = sum(m[3] for m in metricas) + max(0, len(metricas) - 1) * 4
        y = y1 + ((y2 - y1) - altura_total) / 2

        for linha, bbox, tw, th in metricas:
            if align == "left":
                tx = x1 + 10
            else:
                tx = x1 + ((x2 - x1) - tw) / 2
            ty = y - bbox[1]
            draw.text(
                (tx, ty),
                linha,
                font=fonte,
                fill=cor,
                stroke_width=1,
                stroke_fill=(0, 0, 0),
            )
            y += th + 4

    def apagar_area(caixa_base, margem=0):
        caixa = escala_caixa(caixa_base)
        x1, y1, x2, y2 = caixa
        m = int(margem * min(sx, sy))
        draw.rectangle([x1 - m, y1 - m, x2 + m, y2 + m], fill=(0, 0, 0))

    valor = str(pedido.get("valor", "0,00")).replace("R$", "").strip()
    catalogo = str(pedido.get("catalogo", "TikTok")).strip() or "TikTok"
    servico = str(pedido.get("servico", "")).strip()
    quantidade = str(pedido.get("quantidade", "")).strip()
    link = normalizar_link_para_exibicao(pedido.get("link", ""))

    # Campos dinâmicos em fonte maior e mais visível.
    # As caixas foram alargadas para o texto não encolher demais no Telegram.
    escrever_caixa(f"R$ {valor}", (255, 586, 615, 724), 90, 54, cor=(255, 255, 255), negrito=True, max_linhas=1)
    escrever_caixa(catalogo, (275, 850, 705, 980), 90, 54, cor=(255, 255, 255), negrito=True, max_linhas=1)
    escrever_caixa(servico, (275, 940, 705, 1072), 90, 50, cor=(255, 255, 255), negrito=True, max_linhas=2)
    escrever_caixa(quantidade, (275, 1040, 705, 1170), 90, 54, cor=(255, 255, 255), negrito=True, max_linhas=1)
    escrever_caixa(link, (295, 1134, 705, 1264), 90, 50, cor=(255, 255, 255), negrito=True, max_linhas=2)

    if PIX_CHAVE:
        apagar_area((201, 476, 640, 535), margem=2)
        escrever_caixa(PIX_CHAVE, (192, 458, 648, 552), 56, 30, cor=(255, 255, 255), negrito=True, max_linhas=1)

    arquivo = BytesIO()
    img.save(arquivo, format="PNG", optimize=True)
    arquivo.seek(0)
    arquivo.name = "pagamento_tiktok.png"
    return arquivo


def guardar_mensagem_bot(context: ContextTypes.DEFAULT_TYPE, mensagem):
    if not mensagem:
        return
    context.user_data["ultima_chat_id_bot"] = mensagem.chat_id
    context.user_data["ultima_mensagem_bot_id"] = mensagem.message_id


async def apagar_ultima_mensagem_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.user_data.get("ultima_chat_id_bot") or update.effective_chat.id
    message_id = context.user_data.get("ultima_mensagem_bot_id")
    if not chat_id or not message_id:
        return

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass
    finally:
        context.user_data.pop("ultima_mensagem_bot_id", None)
        context.user_data.pop("ultima_chat_id_bot", None)


async def enviar_texto_sequencial(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None, parse_mode=ParseMode.MARKDOWN):
    await apagar_ultima_mensagem_bot(update, context)
    mensagem = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    guardar_mensagem_bot(context, mensagem)
    return mensagem


async def enviar_foto_sequencial(update: Update, context: ContextTypes.DEFAULT_TYPE, photo, reply_markup=None, caption: str | None = None):
    await apagar_ultima_mensagem_bot(update, context)
    mensagem = await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=photo,
        caption=caption,
        parse_mode=ParseMode.MARKDOWN if caption else None,
        reply_markup=reply_markup,
    )
    guardar_mensagem_bot(context, mensagem)
    return mensagem


async def enviar_pagamento_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE, pedido: dict):
    """Troca a aba atual pela aba de pagamento, sem empilhar outra mensagem do bot."""
    if not await verificar_reposicao_antes_pagamento(update, context, pedido):
        return

    if mercado_pago_configurado():
        ok, mensagem = await garantir_pagamento_mercado_pago(pedido)
        if not ok:
            await enviar_texto_sequencial(
                update,
                context,
                (
                    "⚠️ Não consegui gerar o Pix automático pelo Mercado Pago.\n\n"
                    f"*Erro:* {md(mensagem)}\n\n"
                    "Verifique se a variável `MERCADO_PAGO_ACCESS_TOKEN` está configurada no Railway."
                ),
                InlineKeyboardMarkup([[btn("🏠 Menu inicial", "voltar:inicio")]]),
            )
            return

        await enviar_texto_sequencial(update, context, texto_pagamento(pedido), botoes_pagamento(pedido), parse_mode=None)
        return

    imagem = None
    if pedido.get("catalogo") == "Instagram":
        imagem = gerar_imagem_pagamento_instagram(pedido)
    elif pedido.get("catalogo") == "TikTok":
        imagem = gerar_imagem_pagamento_tiktok(pedido)

    if imagem is not None:
        await enviar_foto_sequencial(update, context, imagem, botoes_pagamento(pedido))
        return

    await enviar_texto_sequencial(update, context, texto_pagamento(pedido), botoes_pagamento(pedido), parse_mode=None)


def botoes_pagamento(pedido: dict | None = None) -> InlineKeyboardMarkup:
    pix_copia = (pedido or {}).get("mp_qr_code") or PIX_COPIA_COLA or PIX_CHAVE or "PIX_NAO_CONFIGURADO"
    texto_botao = "📋 Copiar Pix" if (pedido or {}).get("mp_qr_code") else "📋 Copiar chave Pix"
    texto_alterar = "✏️ Alterar e-mail" if catalogo_exige_email(pedido or {}) else "✏️ Alterar link/@"
    keyboard = [
        [InlineKeyboardButton(texto_botao, copy_text=CopyTextButton(pix_copia))],
    ]
    if (pedido or {}).get("mp_payment_id"):
        keyboard.append([btn("✅ Verificar Pagamento", "verificar_pagamento")])
    keyboard.extend([
        [btn(texto_alterar, "alterar_link")],
        [btn("🏠 Cancelar / Menu", "voltar:inicio")],
    ])
    return InlineKeyboardMarkup(keyboard)


def botoes_confirmar_pagamento() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("⏳ Comprovante em análise", "aguardando_aprovacao")],
            [btn("✏️ Alterar link/@", "alterar_link")],
            [btn("🏠 Cancelar / Menu", "voltar:inicio")],
        ]
    )


def botoes_aprovacao_admin(pedido_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("✅ Aprovar e enviar pedido", f"admin_aprovar_pagamento:{pedido_id}")],
            [btn("❌ Reprovar comprovante", f"admin_reprovar_pagamento:{pedido_id}")],
        ]
    )


def texto_pedido_pendente_admin(pedido: dict) -> str:
    username = f'@{pedido["username"]}' if pedido.get("username") else "Sem username"
    destino_label = "E-mail" if catalogo_exige_email(pedido) else "Link/@"
    destino_emoji = "📧" if catalogo_exige_email(pedido) else "🔗"
    return (
        "🧾 *COMPROVANTE AGUARDANDO VALIDAÇÃO*\n\n"
        f"🆔 *Pedido:* `{md(pedido.get('pedido_id', ''))}`\n"
        f"🗂️ *Catálogo:* {md(pedido.get('catalogo', ''))}\n"
        f"📌 *Serviço:* {md(pedido.get('servico', ''))}\n"
        f"🔢 *Quantidade:* {md(pedido.get('quantidade', ''))}\n"
        f"💰 *Valor esperado:* R$ {md(pedido.get('valor', ''))}\n"
        f"{destino_emoji} *{destino_label}:* {md(pedido.get('link', ''))}\n\n"
        f"👤 *Cliente:* {md(pedido.get('usuario', 'Cliente'))}\n"
        f"📱 *Telegram:* {md(username)}\n"
        f"🆔 *ID Telegram:* `{pedido.get('user_id', '')}`\n"
        f"🕒 *Enviado em:* {md(pedido.get('comprovante_recebido_em', ''))}\n\n"
        "Confira se o comprovante é real, se o valor bate e se é deste pedido. "
        "O envio para a plataforma só acontece ao aprovar."
    )


async def enviar_para_aprovacao_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, pedido: dict) -> bool:
    if not ADMIN_CHAT_ID:
        return False

    texto = texto_pedido_pendente_admin(pedido)
    comprovante_file_id = pedido.get("comprovante_file_id")
    markup = botoes_aprovacao_admin(str(pedido.get("pedido_id")))

    if comprovante_file_id:
        try:
            if len(texto) <= 1000:
                await context.bot.send_photo(
                    chat_id=ADMIN_CHAT_ID,
                    photo=comprovante_file_id,
                    caption=texto,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=markup,
                )
            else:
                await context.bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=comprovante_file_id)
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=texto,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
            return True
        except Exception as exc:
            logging.warning("Falha ao enviar comprovante como foto para aprovação: %s", exc)

    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=texto,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=markup,
        disable_web_page_preview=True,
    )
    return True


async def safe_edit_or_reply(update: Update, text: str, reply_markup=None, parse_mode=ParseMode.MARKDOWN):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        try:
            await query.edit_message_text(
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return query.message
        except BadRequest as exc:
            # Evita duplicar mensagem quando o usuário toca em um botão que
            # tenta abrir exatamente a mesma tela/menu já exibido.
            if "Message is not modified" in str(exc):
                return query.message
            mensagem = await query.message.reply_text(
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            try:
                await query.message.delete()
            except Exception:
                pass
            return mensagem
        except Exception:
            mensagem = await query.message.reply_text(
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            try:
                await query.message.delete()
            except Exception:
                pass
            return mensagem
    else:
        return await update.message.reply_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


async def enviar_assinatura_cliente(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    servico_chave: str,
    texto: str,
    reply_markup=None,
):
    """Exibe a tela da assinatura com sua arte, quando houver uma configurada."""
    imagem_path = ASSINATURA_IMAGE_PATHS.get(servico_chave)
    bot_contexto = getattr(context, "bot", None)
    chat = getattr(update, "effective_chat", None)
    if (
        imagem_path is None
        or not imagem_path.exists()
        or bot_contexto is None
        or chat is None
    ):
        return await safe_edit_or_reply(
            update,
            texto,
            reply_markup,
            parse_mode=None,
        )

    if update.callback_query:
        query = update.callback_query
        responder_callback = getattr(query, "answer", None)
        if callable(responder_callback):
            await responder_callback()
        try:
            await query.message.delete()
        except Exception:
            pass

    try:
        with open(imagem_path, "rb") as photo:
            mensagem = await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=photo,
                caption=texto,
                parse_mode=None,
                reply_markup=reply_markup,
            )
        guardar_mensagem_bot(context, mensagem)
        return mensagem
    except Exception as exc:
        logging.warning(
            "Falha ao enviar imagem da assinatura %s: %s",
            servico_chave,
            exc,
        )

    mensagem = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=texto,
        parse_mode=None,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    guardar_mensagem_bot(context, mensagem)
    return mensagem


async def enviar_atendimento_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str, reply_markup=None):
    """Envia a tela de Fale Conosco com a arte de suporte."""
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass

    if SUPORTE_IMAGE_PATH.exists():
        try:
            with open(SUPORTE_IMAGE_PATH, "rb") as photo:
                mensagem = await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=photo,
                    caption=texto,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup,
                )
            guardar_mensagem_bot(context, mensagem)
            return mensagem
        except Exception as exc:
            logging.warning("Falha ao enviar imagem de suporte: %s", exc)

    if update.callback_query:
        return await update.callback_query.message.reply_text(
            text=texto,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )

    return await update.message.reply_text(
        text=texto,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


def ticket_id_texto(ticket_id) -> str:
    try:
        return f"{int(ticket_id):06d}"
    except (TypeError, ValueError):
        return str(ticket_id or "")


def botoes_ticket(ticket_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("🔒 Fechar ticket", f"ticket:fechar:{int(ticket_id)}")],
    ])


def texto_ticket_aguardando(ticket: dict) -> str:
    return (
        "🎫 *Ticket de atendimento aberto*\n\n"
        f"🆔 *Ticket:* `#{md(ticket_id_texto(ticket.get('id')))}`\n"
        "📌 *Status:* aguardando um atendente\n\n"
        "O administrador receberá a solicitação. Assim que o atendimento for assumido, "
        "você receberá uma mensagem aqui."
    )


def texto_ticket_em_atendimento(ticket: dict, para_atendente: bool = False) -> str:
    numero = ticket_id_texto(ticket.get("id"))
    if para_atendente:
        pessoa = ticket.get("usuario_nome") or f"ID {ticket.get('usuario_id')}"
        return (
            "💬 *Atendimento iniciado*\n\n"
            f"🆔 *Ticket:* `#{md(numero)}`\n"
            f"👤 *Cliente:* {md(pessoa)}\n\n"
            "Envie mensagens neste chat do bot. Elas serão repassadas ao cliente dentro do ticket."
        )
    atendente = ticket.get("atendente_nome") or "Equipe de suporte"
    return (
        "💬 *Atendimento iniciado*\n\n"
        f"🆔 *Ticket:* `#{md(numero)}`\n"
        f"🧑‍💻 *Atendente:* {md(atendente)}\n\n"
        "Envie mensagens neste chat do bot. Elas serão repassadas ao atendente dentro do ticket."
    )


async def atualizar_notificacoes_ticket(context: ContextTypes.DEFAULT_TYPE, ticket: dict, texto: str):
    dados = ticket.get("dados") or {}
    for item in dados.get("notificacoes") or []:
        try:
            await context.bot.edit_message_text(
                chat_id=item.get("chat_id"),
                message_id=item.get("message_id"),
                text=texto,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=None,
                disable_web_page_preview=True,
            )
        except Exception:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=item.get("chat_id"),
                    message_id=item.get("message_id"),
                    reply_markup=None,
                )
            except Exception:
                pass


async def notificar_equipe_novo_ticket(
    context: ContextTypes.DEFAULT_TYPE,
    ticket: dict,
    destinatarios: list[str] | None = None,
):
    dados = dict(ticket.get("dados") or {})
    notificacoes = list(dados.get("notificacoes") or [])
    usuario_id = str(ticket.get("usuario_id") or "")
    numero = ticket_id_texto(ticket.get("id"))
    nome = ticket.get("usuario_nome") or "Cliente"
    username = ticket.get("usuario_username") or "Sem @"
    texto = (
        "🆕 *Novo ticket de atendimento*\n\n"
        f"🆔 *Ticket:* `#{md(numero)}`\n"
        f"👤 *Cliente:* {md(nome)}\n"
        f"📲 *Telegram:* {md(username)}\n"
        f"🆔 *Telegram ID:* `{md(usuario_id)}`\n\n"
        "Toque abaixo para assumir. O primeiro atendente que aceitar ficará responsável pelo ticket."
    )
    markup = InlineKeyboardMarkup([
        [btn("🙋 Assumir atendimento", f"ticket:assumir:{int(ticket['id'])}")],
    ])

    equipe_destino = destinatarios or ids_unicos(ADMIN_CHAT_ID)
    for equipe_id in ids_unicos(*equipe_destino):
        if str(equipe_id) == usuario_id:
            continue
        try:
            mensagem = await context.bot.send_message(
                chat_id=equipe_id,
                text=texto,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            notificacoes.append(
                {
                    "chat_id": str(mensagem.chat.id if mensagem.chat else equipe_id),
                    "message_id": mensagem.message_id,
                }
            )
        except Exception as exc:
            logging.warning("Falha ao notificar suporte %s sobre ticket %s: %s", equipe_id, numero, exc)

    dados["notificacoes"] = notificacoes
    DB.atualizar_dados_ticket(ticket["id"], dados)


async def abrir_ticket_suporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        await update.callback_query.answer("Não consegui identificar sua conta.", show_alert=True)
        return

    ticket, criado = DB.criar_ticket(
        user.id,
        user.full_name,
        f"@{user.username}" if user.username else "",
    )
    if ticket.get("status") == "em_atendimento":
        texto = texto_ticket_em_atendimento(ticket)
    else:
        texto = texto_ticket_aguardando(ticket)

    await safe_edit_or_reply(update, texto, botoes_ticket(ticket["id"]))
    if criado:
        await notificar_equipe_novo_ticket(context, ticket)


async def assumir_ticket_suporte(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ticket_id: str,
):
    query = update.callback_query
    if not pode_atender_suporte(update):
        await query.answer("Apenas o administrador pode assumir atendimentos.", show_alert=True)
        return

    atendente = update.effective_user
    if not atendente:
        await query.answer("Não consegui identificar seu usuário.", show_alert=True)
        return

    ticket_atual = DB.obter_ticket(ticket_id)
    if ticket_atual and str(ticket_atual.get("usuario_id")) == str(atendente.id):
        await query.answer("Você não pode assumir o próprio ticket.", show_alert=True)
        return

    ticket, resultado = DB.assumir_ticket(ticket_id, atendente.id, atendente.full_name)
    mensagens_erro = {
        "nao_encontrado": "Ticket não encontrado.",
        "fechado": "Este ticket já foi fechado.",
        "ja_assumido": "Outro atendente já assumiu este ticket.",
        "atendente_ocupado": "Feche seu atendimento atual antes de assumir outro.",
    }
    if resultado in mensagens_erro:
        await query.answer(mensagens_erro[resultado], show_alert=True)
        return

    if resultado == "ja_assumido_por_voce":
        await query.answer("Este ticket já está com você.", show_alert=True)
        await context.bot.send_message(
            chat_id=atendente.id,
            text=texto_ticket_em_atendimento(ticket, para_atendente=True),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=botoes_ticket(ticket["id"]),
        )
        return

    await query.answer("Atendimento assumido.")
    numero = ticket_id_texto(ticket.get("id"))
    await atualizar_notificacoes_ticket(
        context,
        ticket,
        (
            "✅ *Atendimento assumido*\n\n"
            f"🆔 *Ticket:* `#{md(numero)}`\n"
            f"🧑‍💻 *Atendente:* {md(atendente.full_name)}"
        ),
    )

    try:
        await context.bot.send_message(
            chat_id=ticket["usuario_id"],
            text=texto_ticket_em_atendimento(ticket),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=botoes_ticket(ticket["id"]),
        )
    except Exception as exc:
        logging.warning("Falha ao avisar cliente sobre ticket assumido %s: %s", numero, exc)

    await context.bot.send_message(
        chat_id=atendente.id,
        text=texto_ticket_em_atendimento(ticket, para_atendente=True),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=botoes_ticket(ticket["id"]),
    )


async def fechar_ticket_suporte(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ticket_id: str,
):
    query = update.callback_query
    ticket = DB.obter_ticket(ticket_id)
    if not ticket:
        await query.answer("Ticket não encontrado.", show_alert=True)
        return

    autor_id = telegram_id_update(update)
    participante = autor_id in {
        str(ticket.get("usuario_id") or ""),
        str(ticket.get("atendente_id") or ""),
    }
    if not participante and not eh_dono(update):
        await query.answer("Somente os participantes podem fechar este ticket.", show_alert=True)
        return

    fechado_por = (
        update.effective_user.full_name if update.effective_user else f"ID {autor_id}"
    )
    ticket, resultado = DB.fechar_ticket(ticket_id, fechado_por)
    if resultado == "ja_fechado":
        await query.answer("Este ticket já estava fechado.", show_alert=True)
        return

    await query.answer("Ticket fechado.")
    numero = ticket_id_texto(ticket.get("id"))
    texto = (
        "🔒 *Ticket fechado*\n\n"
        f"🆔 *Ticket:* `#{md(numero)}`\n"
        f"👤 *Fechado por:* {md(fechado_por)}\n\n"
        "Para solicitar outro atendimento, abra o menu de suporte."
    )
    await atualizar_notificacoes_ticket(context, ticket, texto)

    destinatarios = ids_unicos(ticket.get("usuario_id"), ticket.get("atendente_id"))
    for destinatario in destinatarios:
        try:
            await context.bot.send_message(
                chat_id=destinatario,
                text=texto,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[btn("🏠 Menu inicial", "voltar:inicio")]]),
            )
        except Exception as exc:
            logging.warning("Falha ao avisar %s sobre fechamento do ticket %s: %s", destinatario, numero, exc)


def localizar_ticket_remetente(telegram_id: str) -> tuple[dict | None, str | None, str | None]:
    """Retorna ticket, destinatário e tipo do remetente para o relay privado."""
    ticket_usuario = DB.obter_ticket_ativo_usuario(telegram_id)
    if ticket_usuario:
        if ticket_usuario.get("status") == "aberto":
            return ticket_usuario, None, "aguardando"
        return ticket_usuario, str(ticket_usuario.get("atendente_id") or ""), "cliente"

    ticket_atendente = DB.obter_ticket_ativo_atendente(telegram_id)
    if ticket_atendente:
        return ticket_atendente, str(ticket_atendente.get("usuario_id") or ""), "atendente"
    return None, None, None


async def processar_mensagem_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    mensagem = update.effective_message
    remetente_id = telegram_id_update(update)
    if not mensagem or not remetente_id:
        return False

    ticket, destinatario, tipo = localizar_ticket_remetente(remetente_id)
    if not ticket:
        return False
    if tipo == "aguardando":
        await mensagem.reply_text(
            "⏳ Seu ticket ainda está aguardando um atendente. "
            "Você receberá um aviso assim que alguém assumir.",
            reply_markup=botoes_ticket(ticket["id"]),
        )
        return True
    if not destinatario:
        await mensagem.reply_text("⚠️ Não encontrei o outro participante deste ticket.")
        return True

    numero = ticket_id_texto(ticket.get("id"))
    if tipo == "cliente":
        titulo = f"💬 Ticket #{numero} — mensagem do cliente"
    else:
        titulo = f"💬 Ticket #{numero} — mensagem do atendimento"

    try:
        if mensagem.text is not None:
            await context.bot.send_message(
                chat_id=destinatario,
                text=f"{titulo}\n\n{mensagem.text}",
                reply_markup=botoes_ticket(ticket["id"]),
                disable_web_page_preview=True,
            )
        else:
            await context.bot.send_message(chat_id=destinatario, text=titulo)
            await context.bot.copy_message(
                chat_id=destinatario,
                from_chat_id=mensagem.chat_id,
                message_id=mensagem.message_id,
                reply_markup=botoes_ticket(ticket["id"]),
            )
    except Exception as exc:
        logging.warning("Falha ao encaminhar mensagem do ticket %s: %s", numero, exc)
        await mensagem.reply_text(
            "⚠️ Não consegui encaminhar esta mensagem. Tente novamente em alguns instantes."
        )
    return True


async def enviar_inicio_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envia o menu inicial com a arte de boas-vindas.

    Quando o cliente vem de um botão antigo, a mensagem anterior é apagada
    antes de continuar o fluxo, evitando que a imagem fique poluindo o chat.
    """
    texto = CATALOGO["mensagens"]["inicio"]
    reply_markup = menu_principal()

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass

    if WELCOME_IMAGE_PATH.exists():
        try:
            with open(WELCOME_IMAGE_PATH, "rb") as photo:
                mensagem = await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=photo,
                    caption=texto,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup,
                )
            guardar_mensagem_bot(context, mensagem)
            return mensagem
        except Exception as exc:
            logging.warning("Falha ao enviar imagem de boas-vindas: %s", exc)

    mensagem = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=texto,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    guardar_mensagem_bot(context, mensagem)
    return mensagem


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Abre o bot diretamente; cadastro e aprovação não são necessários."""
    context.user_data.clear()
    await enviar_inicio_cliente(update, context)


def texto_final_pedido(pedido: dict) -> str:
    if pedido.get("catalogo") in CATALOGOS_COM_ENVIO_API:
        if pedido.get("plataforma_api_status") == "enviado":
            return (
                "✅ *Etapa 3 de 3 — Pedido aprovado*\n\n"
                "🎉 *Pagamento confirmado com sucesso!*\n\n"
                f"📦 *Produto:* {md(pedido.get('catalogo', ''))}\n"
                f"📌 *Serviço:* {md(pedido.get('servico', ''))}\n"
                f"🔢 *Quantidade:* {md(pedido.get('quantidade', ''))}\n"
                f"🚀 *ID na plataforma:* `{md(pedido.get('plataforma_order_id', 'Não informado'))}`\n\n"
                "📌 *Status do pedido*\n"
                "• Pagamento aprovado\n"
                "• Pedido enviado para a plataforma\n"
                "• Processamento iniciado automaticamente\n\n"
                "⏳ O tempo de conclusão pode variar conforme o volume do serviço.\n\n"
                "🎫 Precisa de ajuda? Fale com o suporte."
            )

        erro = pedido.get("plataforma_api_erro") or "Erro não informado."
        if pedido.get("plataforma_api_status") == "revisao_manual":
            return (
                "✅ *Etapa 3 de 3 — Pagamento aprovado*\n\n"
                "⚠️ Para evitar pedido duplicado, o envio automático foi pausado e enviado para revisão manual.\n"
                "O administrador vai conferir se esse pedido já apareceu na plataforma antes de reenviar.\n\n"
                f"*Motivo:* {md(erro)}"
            )

        return (
            "✅ *Etapa 3 de 3 — Pagamento aprovado*\n\n"
            "⚠️ O administrador foi avisado porque o envio automático para a plataforma falhou.\n\n"
            f"*Motivo:* {md(erro)}"
        )

    if catalogo_exige_email(pedido):
        return (
            "✅ *Etapa 3 de 3 — Pedido aprovado*\n\n"
            "🎉 *Pagamento confirmado com sucesso!*\n\n"
            f"📦 *Produto:* {md(pedido.get('catalogo', ''))}\n"
            f"📌 *Serviço:* {md(pedido.get('servico', ''))}\n"
            f"🆔 *Pedido:* `{md(pedido.get('pedido_id', ''))}`\n\n"
            "📌 *Status do pedido*\n"
            "• Pagamento aprovado\n"
            "• Pedido recebido pela equipe\n"
            "• Aguardando ativação/envio dos dados\n\n"
            "🛠️ *Próximo passo*\n"
            "Nossa equipe vai processar seu acesso e enviar as informações assim que estiver tudo pronto.\n\n"
            "🎫 Precisa de ajuda? Fale com o suporte."
        )

    return (
        "✅ *Etapa 3 de 3 — Pedido aprovado*\n\n"
        "🎉 *Pagamento confirmado com sucesso!*\n\n"
        "📌 *Status do pedido*\n"
        "• Pagamento aprovado\n"
        "• Pedido recebido pela equipe\n"
        "• Aguardando processamento\n\n"
        "🎫 Precisa de ajuda? Fale com o suporte."
    )


async def finalizar_pedido_confirmado(update: Update, context: ContextTypes.DEFAULT_TYPE, pedido: dict):
    if not pedido or not pedido.get("link"):
        await safe_edit_or_reply(update, "Não encontrei um pedido completo. Toque em /start para começar novamente.")
        return

    if not pedido.get("comprovante_file_id"):
        await safe_edit_or_reply(update, "Envie primeiro uma imagem do comprovante para liberar a confirmação.")
        return

    if pedido.get("status") != "pagamento_aprovado":
        await safe_edit_or_reply(
            update,
            "⏳ Seu comprovante precisa ser validado antes de liberar o pedido. "
            "A confirmação automática pelo cliente foi bloqueada por segurança.",
        )
        return

    if pedido.get("catalogo") in CATALOGOS_COM_ENVIO_API:
        await enviar_texto_sequencial(
            update,
            context,
            "⏳ Pagamento confirmado. Enviando pedido diretamente para a plataforma...",
        )
        await enviar_pedido_para_plataforma(pedido)

    salvar_pedido_historico(pedido)
    await avisar_admin_se_houver_problema(update, context, pedido)
    await enviar_texto_sequencial(
        update,
        context,
        texto_final_pedido(pedido),
        InlineKeyboardMarkup([[btn("🏠 Menu inicial", "voltar:inicio")]]),
    )
    context.user_data.clear()


async def verificar_pagamento_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    pedido = context.user_data.get("pedido")
    if not pedido or not pedido.get("mp_payment_id"):
        await query.answer("Não encontrei pagamento Mercado Pago neste pedido.", show_alert=True)
        return

    if await encerrar_interacao_se_pagamento_expirado(update, context, pedido):
        await query.answer("Pedido expirado.", show_alert=True)
        return

    await query.answer("Verificando pagamento...")
    try:
        pagamento = await asyncio.to_thread(consultar_pagamento_mercado_pago_sync, str(pedido.get("mp_payment_id")))
    except Exception as exc:
        await safe_edit_or_reply(update, f"⚠️ Falha ao consultar Mercado Pago: {md(limpar_erro_api(exc))}", botoes_pagamento(pedido))
        return

    status_pagamento_mp = str(pagamento.get("status") or "").lower()

    if status_pagamento_mp in {"cancelled", "canceled", "expired"}:
        pedido_id = str(pedido.get("pedido_id") or "")
        await asyncio.to_thread(
            fechar_pagamento_expirado,
            pedido_id,
            pedido,
            f"Mercado Pago retornou status {status_pagamento_mp}",
        )
        context.user_data.clear()
        await safe_edit_or_reply(
            update,
            (
                "⌛️ Esse link de pagamento não está mais disponível.\n\n"
                f"ID do pedido: `{md(pedido_id)}`\n\n"
                "Para comprar, toque em *Fazer novo pedido* e comece do início."
            ),
            botoes_pedido_expirado(),
        )
        return

    if str(pagamento.get("status")) == "approved":
        payment_id = str(pagamento.get("id") or pedido.get("mp_payment_id") or "")
        if payment_id and pagamento_ja_processado(payment_id):
            context.user_data.clear()
            await safe_edit_or_reply(
                update,
                "✅ Pagamento já confirmado e pedido já processado. Verifique a mensagem de confirmação enviada pelo bot.",
                InlineKeyboardMarkup([[btn("🏠 Menu inicial", "voltar:inicio")]]),
            )
            return

        processado = await asyncio.to_thread(processar_pagamento_aprovado_sync, pedido, pagamento, "verificacao_cliente")
        if processado:
            context.user_data.clear()
            try:
                await query.message.delete()
            except Exception:
                pass
        else:
            await safe_edit_or_reply(update, "⚠️ Pagamento encontrado, mas não foi possível validar valor/referência. Fale com o atendimento.", botoes_pagamento(pedido))
        return

    await safe_edit_or_reply(
        update,
        (
            "⏳ *Pagamento em análise*\n\n"
            "Ainda não identificamos a confirmação do seu Pix.\n\n"
            "📌 *O que fazer agora?*\n"
            "• Confira se o pagamento foi concluído no seu banco\n"
            "• Aguarde alguns segundos\n"
            "• Toque novamente em “Verificar pagamento”\n\n"
            "Assim que o pagamento for confirmado, seu pedido continuará automaticamente."
        ),
        botoes_pagamento(pedido),
    )


async def aprovar_pagamento_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, pedido_id: str):
    query = update.callback_query
    if not eh_admin(update):
        await query.answer("Apenas o administrador pode aprovar este pedido.", show_alert=True)
        return

    pedido = obter_pedido_pendente(pedido_id)
    if not pedido:
        await query.answer("Pedido pendente não encontrado ou já processado.", show_alert=True)
        return

    file_unique_id = pedido.get("comprovante_unique_id")
    if comprovante_ja_usado(file_unique_id):
        remover_pedido_pendente(pedido_id)
        await query.answer("Este comprovante já foi usado em outro pedido.", show_alert=True)
        await query.message.reply_text(
            f"🚫 Pedido `{md(pedido_id)}` bloqueado: comprovante já utilizado anteriormente.",
            parse_mode=ParseMode.MARKDOWN,
        )
        try:
            await context.bot.send_message(
                chat_id=pedido.get("user_id"),
                text=(
                    "🚫 Seu comprovante não foi aprovado porque este arquivo já apareceu em outro pedido.\n\n"
                    "Envie um comprovante válido ou fale com o atendimento."
                ),
            )
        except Exception as exc:
            logging.warning("Falha ao avisar cliente sobre comprovante duplicado: %s", exc)
        return

    pedido["status"] = "pagamento_aprovado"
    pedido["aprovado_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
    pedido["aprovado_por"] = update.effective_user.full_name if update.effective_user else "Administrador"

    await query.answer("Pagamento aprovado. Processando pedido...")
    await query.message.reply_text(
        f"✅ Pagamento do pedido `{md(pedido_id)}` aprovado. Processando envio...",
        parse_mode=ParseMode.MARKDOWN,
    )

    if pedido.get("catalogo") in CATALOGOS_COM_ENVIO_API:
        await enviar_pedido_para_plataforma(pedido)

    salvar_pedido_historico(pedido)
    marcar_comprovante_usado(file_unique_id, pedido)
    remover_pedido_pendente(pedido_id)

    await avisar_admin_se_houver_problema(update, context, pedido)

    try:
        await context.bot.send_message(
            chat_id=pedido.get("user_id"),
            text=texto_final_pedido(pedido),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[btn("🏠 Menu inicial", "voltar:inicio")]]),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logging.warning("Falha ao avisar cliente sobre aprovação: %s", exc)


def eh_dono(update: Update) -> bool:
    return eh_admin(update)


def nome_admin(update: Update) -> str:
    return update.effective_user.full_name if update.effective_user else "Administrador"


def salvar_pedido_resolvido_revisao(pedido: dict):
    if not pedido:
        return
    salvar_pedido_historico(pedido)
    remover_pedido_pendente(str(pedido.get("pedido_id") or ""))
    payment_id = str(pedido.get("mp_payment_id") or "").strip()
    if payment_id:
        marcar_pagamento_processado(payment_id, pedido)


def buscar_pedido_revisao_manual(pedido_id: str) -> tuple[dict | None, str | None]:
    pedido, origem = buscar_pedido_local_por_id(pedido_id)
    if not pedido:
        return None, None
    return pedido, origem


async def limpar_botoes_revisao(query):
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def admin_revisao_ja_foi(update: Update, context: ContextTypes.DEFAULT_TYPE, pedido_id: str):
    query = update.callback_query
    if not eh_admin(update):
        await query.answer("Apenas o administrador pode resolver revisão manual.", show_alert=True)
        return

    pedido, _origem = buscar_pedido_revisao_manual(pedido_id)
    if not pedido:
        await query.answer("Pedido não encontrado no histórico/pendentes.", show_alert=True)
        return

    if pedido_ja_enviado_para_plataforma(pedido) and pedido.get("plataforma_resolucao_manual") != "ja_foi_feito":
        await query.answer("Esse pedido já está marcado como enviado.", show_alert=True)
        await limpar_botoes_revisao(query)
        return

    pedido["status"] = pedido.get("status") or "pagamento_aprovado"
    pedido["plataforma_api_status"] = "enviado"
    if not pedido_tem_id_plataforma(pedido.get("plataforma_order_id")):
        pedido["plataforma_order_id"] = "Feito manualmente pelo admin"
    pedido["plataforma_api_erro"] = ""
    pedido["plataforma_resolucao_manual"] = "ja_foi_feito"
    pedido["plataforma_resolvido_manual_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
    pedido["plataforma_resolvido_por"] = nome_admin(update)
    salvar_pedido_resolvido_revisao(pedido)

    await query.answer("Marcado como já feito.")
    await limpar_botoes_revisao(query)
    await query.message.reply_text(
        f"✅ Pedido `{md(pedido.get('pedido_id', pedido_id))}` marcado como *já feito*.\n\n"
        "Ele foi salvo como resolvido e não será reenviado após reiniciar o Railway.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def reenviar_pedido_revisao_manual_para_plataforma(pedido: dict, admin_nome: str) -> tuple[bool, str]:
    if pedido.get("catalogo") not in CATALOGOS_COM_ENVIO_API:
        return False, "Esse catálogo não tem envio automático configurado."

    if pedido_ja_enviado_para_plataforma(pedido):
        pedido["plataforma_api_status"] = "enviado"
        salvar_pedido_resolvido_revisao(pedido)
        return True, "Esse pedido já estava marcado como enviado."

    pedido["status"] = pedido.get("status") or "pagamento_aprovado"
    pedido["plataforma_api_status"] = "processando"
    pedido["plataforma_reenvio_manual_por"] = admin_nome
    pedido["plataforma_reenvio_manual_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
    pedido["plataforma_processando_em"] = pedido["plataforma_reenvio_manual_em"]
    salvar_pedido_pendente(pedido)

    try:
        resultado = await asyncio.to_thread(criar_pedido_plataforma_sync, pedido)
    except Exception as exc:
        marcar_envio_plataforma_para_revisao_manual(
            pedido,
            origem="botao_reenviar_admin",
            motivo=(
                "Reenvio manual solicitado pelo admin falhou ou não retornou com segurança. "
                f"Erro: {limpar_erro_api(exc)}. Confira na plataforma antes de tentar novamente."
            ),
        )
        pedido["plataforma_ultimo_reenvio_manual_erro_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
        salvar_pedido_resolvido_revisao(pedido)
        return False, str(pedido.get("plataforma_api_erro") or "Falha ao reenviar.")

    pedido["plataforma_api_status"] = "enviado"
    pedido["plataforma_service_id"] = resultado.get("service_id")
    pedido["plataforma_quantidade"] = resultado.get("quantity")
    pedido["plataforma_order_id"] = resultado.get("order_id") or "Não informado"
    pedido["plataforma_resposta"] = resultado.get("response")
    pedido["plataforma_api_erro"] = ""
    pedido["plataforma_resolucao_manual"] = "reenviado"
    pedido["plataforma_resolvido_manual_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
    pedido["plataforma_resolvido_por"] = admin_nome
    salvar_pedido_resolvido_revisao(pedido)
    return True, f"Pedido reenviado para a plataforma. ID: {pedido.get('plataforma_order_id', 'Não informado')}"


async def admin_revisao_reenviar(update: Update, context: ContextTypes.DEFAULT_TYPE, pedido_id: str):
    query = update.callback_query
    if not eh_admin(update):
        await query.answer("Apenas o administrador pode reenviar revisão manual.", show_alert=True)
        return

    pedido, _origem = buscar_pedido_revisao_manual(pedido_id)
    if not pedido:
        await query.answer("Pedido não encontrado no histórico/pendentes.", show_alert=True)
        return

    await query.answer("Reenviando para a plataforma...")
    await query.message.reply_text(
        f"🔁 Reenvio manual iniciado para o pedido `{md(pedido.get('pedido_id', pedido_id))}`...",
        parse_mode=ParseMode.MARKDOWN,
    )
    ok, mensagem = await reenviar_pedido_revisao_manual_para_plataforma(pedido, nome_admin(update))

    if ok:
        await limpar_botoes_revisao(query)
        await query.message.reply_text(
            f"✅ {md(mensagem)}\n\nO pedido foi salvo como enviado e não será reenviado no restart.",
            parse_mode=ParseMode.MARKDOWN,
        )
        try:
            await context.bot.send_message(
                chat_id=pedido.get("user_id"),
                text=texto_final_pedido(pedido),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[btn("🏠 Menu inicial", "voltar:inicio")]]),
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logging.warning("Falha ao avisar cliente sobre reenvio manual: %s", exc)
        return

    await query.message.reply_text(
        f"⚠️ Não consegui reenviar o pedido `{md(pedido.get('pedido_id', pedido_id))}`.\n\n"
        f"Motivo: {md(mensagem)}\n\n"
        "Os botões continuam válidos para você tentar novamente, marcar como já feito ou ignorar.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_revisao_ignorar(update: Update, context: ContextTypes.DEFAULT_TYPE, pedido_id: str):
    query = update.callback_query
    if not eh_admin(update):
        await query.answer("Apenas o administrador pode ignorar revisão manual.", show_alert=True)
        return

    pedido, _origem = buscar_pedido_revisao_manual(pedido_id)
    if not pedido:
        await query.answer("Pedido não encontrado no histórico/pendentes.", show_alert=True)
        return

    pedido["status"] = pedido.get("status") or "pagamento_aprovado"
    pedido["plataforma_api_status"] = "ignorado_manual"
    pedido["plataforma_api_erro"] = "Pendência ignorada manualmente pelo admin. O bot não reenviará este pedido automaticamente."
    pedido["plataforma_resolucao_manual"] = "ignorado"
    pedido["plataforma_resolvido_manual_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
    pedido["plataforma_resolvido_por"] = nome_admin(update)
    salvar_pedido_resolvido_revisao(pedido)

    await query.answer("Pendência ignorada.")
    await limpar_botoes_revisao(query)
    await query.message.reply_text(
        f"❌ Pendência do pedido `{md(pedido.get('pedido_id', pedido_id))}` ignorada.\n\n"
        "Ela foi salva no histórico e não será reenviada após reiniciar o Railway.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def reprovar_pagamento_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, pedido_id: str):
    query = update.callback_query
    if not eh_admin(update):
        await query.answer("Apenas o administrador pode reprovar este pedido.", show_alert=True)
        return

    pedido = obter_pedido_pendente(pedido_id)
    if not pedido:
        await query.answer("Pedido pendente não encontrado ou já processado.", show_alert=True)
        return

    pedido["status"] = "comprovante_reprovado"
    pedido["reprovado_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
    pedido["reprovado_por"] = update.effective_user.full_name if update.effective_user else "Administrador"
    salvar_pedido_historico(pedido)
    remover_pedido_pendente(pedido_id)
    await query.answer("Comprovante reprovado.")
    await query.message.reply_text(
        f"❌ Comprovante do pedido `{md(pedido_id)}` reprovado. O pedido não foi enviado.",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        await context.bot.send_message(
            chat_id=pedido.get("user_id"),
            text=(
                "❌ Seu comprovante não foi aprovado. O pedido não foi enviado.\n\n"
                f"ID do pedido: `{md(pedido_id)}`\n"
                "Verifique se o valor, destinatário e data estão corretos e envie um novo comprovante."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        logging.warning("Falha ao avisar cliente sobre reprovação: %s", exc)


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    if query and query.message:
        guardar_mensagem_bot(context, query.message)

    # Rejeita callbacks de funcionalidades removidas.
    if (
        data.startswith(("registro:", "admin_registro_", "admin_cargos:", "admin_cargo:"))
        or data in {
            "perfil:meu", "admin_painel:relatorios", "admin_painel:relatorio_semanal",
            "admin_painel:relatorio_diario", "admin_painel:consultar_cadastros",
            "admin_painel:consultar_vendedores", "admin_painel:usuarios",
            "admin_painel:buscar_usuario", "admin_painel:remover_registro",
            "admin_painel:banir_desbanir", "admin_painel:banir", "admin_painel:desbanir",
            "admin_painel:cargos",
        }
    ):
        await query.answer("Essa opção foi removida.", show_alert=True)
        return

    if data.startswith("admin_revisao_feito:"):
        pedido_id = data.split(":", 1)[1]
        await admin_revisao_ja_foi(update, context, pedido_id)
        return

    if data.startswith("admin_revisao_reenviar:"):
        pedido_id = data.split(":", 1)[1]
        await admin_revisao_reenviar(update, context, pedido_id)
        return

    if data.startswith("admin_revisao_ignorar:"):
        pedido_id = data.split(":", 1)[1]
        await admin_revisao_ignorar(update, context, pedido_id)
        return

    if data == "admin_painel:inicio":
        await mostrar_painel_admin(update, context)
        return

    if data == "admin_painel:resumo":
        await mostrar_resumo_admin(update, context)
        return

    if data == "admin_painel:ultimos":
        await mostrar_ultimos_pedidos_admin(update, context)
        return

    if data == "admin_painel:pagamentos_pendentes":
        await mostrar_pagamentos_pendentes_admin(update, context)
        return

    if data.startswith("ticket:assumir:"):
        ticket_id = data.split(":", 2)[2]
        await assumir_ticket_suporte(update, context, ticket_id)
        return

    if data.startswith("ticket:fechar:"):
        ticket_id = data.split(":", 2)[2]
        await fechar_ticket_suporte(update, context, ticket_id)
        return

    if data == "suporte:chat":
        context.user_data.clear()
        await abrir_ticket_suporte(update, context)
        return

    if data.startswith("admin_aprovar_pagamento:"):
        pedido_id = data.split(":", 1)[1]
        await aprovar_pagamento_admin(update, context, pedido_id)
        return

    if data.startswith("admin_reprovar_pagamento:"):
        pedido_id = data.split(":", 1)[1]
        await reprovar_pagamento_admin(update, context, pedido_id)
        return

    if data == "aguardando_aprovacao":
        await query.answer("O comprovante já foi enviado para validação. Aguarde a aprovação.", show_alert=True)
        return

    if data == "verificar_pagamento":
        await verificar_pagamento_cliente(update, context)
        return

    if data == "voltar:inicio":
        context.user_data.clear()
        await enviar_inicio_cliente(update, context)
        return


    if data == "pedido:consultar":
        context.user_data.clear()
        await safe_edit_or_reply(
            update,
            (
                "📦 *Central de pedidos*\n\n"
                "Acompanhe seus pedidos de forma rápida e organizada.\n\n"
                "Escolha uma opção abaixo:"
            ),
            menu_consultar_pedido(),
        )
        return

    if data == "pedido:consultar_status":
        context.user_data.clear()
        context.user_data["consulta_pedido"] = True
        await safe_edit_or_reply(
            update,
            (
                "🔎 *Consultar Status*\n\n"
                "Envie o ID do pedido que você quer consultar.\n\n"
                "Pode ser o *ID do pedido no bot* ou o *ID da plataforma*.\n"
                "Assim eu busco o status certinho para você."
            ),
            InlineKeyboardMarkup([[btn("⬅️ Voltar para pedidos", "pedido:consultar")]]),
        )
        return

    if data == "pedido:solicitar_refil":
        context.user_data.clear()
        context.user_data["refil_pedido"] = True
        await safe_edit_or_reply(
            update,
            (
                "🔄 *Solicitar Reposição*\n\n"
                "Envie o ID do pedido que precisa de reposição.\n\n"
                "Eu vou conferir se o pedido tem ID na plataforma e se esse serviço permite refil.\n"
                "Se estiver tudo certo, envio a solicitação para você."
            ),
            InlineKeyboardMarkup([[btn("⬅️ Voltar para pedidos", "pedido:consultar")]]),
        )
        return

    if data.startswith("pedido:refil:"):
        order_id = data.split(":", 2)[2]
        await processar_solicitacao_refil(update, context, order_id)
        return

    if data == "menu:catalogo":
        await safe_edit_or_reply(update, CATALOGO["mensagens"]["catalogo"], menu_catalogos())
        return

    if data == "catalogo:redes_sociais":
        await safe_edit_or_reply(
            update,
            "🚀 *Engajamentos*\n\n"
            "Escolha abaixo a plataforma que deseja impulsionar.\n\n"
            "✅ Entrega organizada\n"
            "✅ Pedido conferido antes da finalização\n"
            "✅ Suporte caso precise de ajuda\n\n"
            "Toque em uma opção para continuar:",
            menu_redes_sociais(),
        )
        return

    if data == "catalogo:assinaturas":
        context.user_data.pop("pedido", None)
        await safe_edit_or_reply(
            update,
            CATALOGO["catalogos"]["assinaturas"]["mensagem"],
            menu_assinaturas(),
        )
        return

    if data.startswith("assinatura:"):
        servico_chave = data.split(":", 1)[1]
        servico = CATALOGO["catalogos"]["assinaturas"]["servicos"].get(servico_chave)
        if not servico:
            await safe_edit_or_reply(
                update,
                "❌ Assinatura não encontrada no catálogo.",
                InlineKeyboardMarkup([[btn("⬅️ Voltar às assinaturas", "catalogo:assinaturas")]]),
            )
            return

        context.user_data["pedido"] = preparar_pedido({
            "catalogo": "Assinaturas",
            "servico_chave": servico_chave,
            "servico": servico["nome"],
            "quantidade": "1 assinatura",
            "valor": servico["valor"],
            "link": None,
            "tipo_destino": "email",
            "status": "aguardando_email_iptv",
            "usuario": update.effective_user.full_name,
            "username": update.effective_user.username,
            "user_id": update.effective_user.id,
        })

        await enviar_assinatura_cliente(
            update,
            context,
            servico_chave,
            servico["mensagem"],
            InlineKeyboardMarkup([[btn("⬅️ Voltar às assinaturas", "catalogo:assinaturas")]]),
        )
        return

    if data.startswith("extra:"):
        extra = data.split(":", 1)[1]
        texto = CATALOGO.get("menus_extras", {}).get(extra)
        if not texto:
            context.user_data.clear()
            await enviar_inicio_cliente(update, context)
            return
        keyboard = [[btn("⬅️ Voltar", "voltar:inicio")]]
        if extra == "atendimento":
            keyboard.insert(0, [btn("🎟️ Abrir Ticket", "suporte:chat")])
            await enviar_atendimento_cliente(update, context, texto, InlineKeyboardMarkup(keyboard))
            return
        await safe_edit_or_reply(update, texto, InlineKeyboardMarkup(keyboard))
        return


    if data == "catalogo:instagram":
        await safe_edit_or_reply(update, CATALOGO["catalogos"]["instagram"]["mensagem"], menu_instagram())
        return

    if data == "catalogo_instagram:estrangeiros":
        await safe_edit_or_reply(
            update,
            "🌏 *Instagram — Serviços Estrangeiros*\n\n"
            "Pacotes com entrega gradual para perfis e publicações do Instagram.\n\n"
            "📌 *Opções disponíveis:*\n"
            "• Seguidores para perfil\n"
            "• Curtidas para publicação\n"
            "• Visualizações para publicação\n\n"
            "Escolha o serviço que deseja:",
            menu_instagram_estrangeiros(),
        )
        return

    if data == "catalogo_instagram:brasileiros":
        await safe_edit_or_reply(
            update,
            "🇧🇷 *Instagram — Serviços Brasileiros*\n\n"
            "Pacotes com entrega gradual para perfis brasileiros do Instagram.\n\n"
            "📌 *Opção disponível:*\n"
            "• Seguidores brasileiros para perfil\n\n"
            "Escolha o serviço que deseja:",
            menu_instagram_brasileiros(),
        )
        return

    if data.startswith("servico_instagram_br:"):
        servico_chave = data.split(":", 1)[1]
        servico = CATALOGO["catalogos"]["instagram"].get("servicos_brasileiros", {}).get(servico_chave)
        if not servico:
            await safe_edit_or_reply(
                update,
                "❌ Serviço brasileiro não encontrado no catálogo.",
                InlineKeyboardMarkup([[btn("⬅️ Voltar aos serviços brasileiros", "catalogo_instagram:brasileiros")]]),
            )
            return

        if not servico.get("itens"):
            await safe_edit_or_reply(
                update,
                servico.get("mensagem") or (
                    f"🇧🇷 *Instagram — {servico.get('nome', 'Serviço brasileiro')}*\n\n"
                    "Os botões já foram adicionados, mas os valores/pacotes ainda não foram configurados.\n\n"
                    "Quando você adicionar os valores no catálogo, eles aparecem aqui."
                ),
                menu_itens_instagram_brasileiros(servico_chave),
            )
            return

        await safe_edit_or_reply(update, servico["mensagem"], menu_itens_instagram_brasileiros(servico_chave))
        return

    if data.startswith("item_instagram_br:"):
        _, servico_chave, quantidade_str = data.split(":")
        quantidade = int(quantidade_str)
        item = get_item_instagram_brasileiros(servico_chave, quantidade)
        servico = CATALOGO["catalogos"]["instagram"]["servicos_brasileiros"][servico_chave]

        pedido = preparar_pedido({
            "catalogo": "Instagram — Serviços Brasileiros",
            "catalogo_api": "Instagram_Brasileiros",
            "servico_chave": servico_chave,
            "servico": servico["nome"],
            "quantidade": item["quantidade_texto"],
            "quantidade_api": item["quantidade"],
            "api_service_id": item.get("api_service_id") or servico.get("api_service_id"),
            "valor": item["valor"],
            "link": None,
            "status": "aguardando_link",
            "usuario": update.effective_user.full_name,
            "username": update.effective_user.username,
            "user_id": update.effective_user.id,
        })
        info_limite = await obter_limite_solicitacoes_item("Instagram_Brasileiros", servico_chave, item, servico)
        aplicar_limite_solicitacoes_no_pedido(pedido, info_limite)
        context.user_data["pedido"] = pedido
        mensagem_item = aplicar_limite_solicitacoes_na_mensagem(item["mensagem"], info_limite)

        await safe_edit_or_reply(
            update,
            mensagem_item,
            InlineKeyboardMarkup([[btn("⬅️ Voltar", f"servico_instagram_br:{servico_chave}")]]),
            parse_mode=None,
        )
        return

    if data == "catalogo:tiktok":
        await safe_edit_or_reply(update, CATALOGO["catalogos"]["tiktok"]["mensagem"], menu_tiktok())
        return

    if data == "catalogo_tiktok:estrangeiros":
        await safe_edit_or_reply(
            update,
            "🌏 *TikTok — Serviços Estrangeiros*\n\n"
            "Pacotes com entrega gradual para perfis e publicações do TikTok.\n\n"
            "📌 *Opções disponíveis:*\n"
            "• Seguidores para perfil\n"
            "• Curtidas para publicação\n"
            "• Visualizações para publicação\n\n"
            "Escolha o serviço que deseja:",
            menu_tiktok_estrangeiros(),
        )
        return

    if data.startswith("servico_tiktok:"):
        servico_chave = data.split(":", 1)[1]
        servico = CATALOGO["catalogos"]["tiktok"]["servicos"][servico_chave]
        await safe_edit_or_reply(update, servico["mensagem"], menu_itens_tiktok(servico_chave))
        return

    if data.startswith("item_tiktok:"):
        _, servico_chave, quantidade_str = data.split(":")
        quantidade = int(quantidade_str)
        item = get_item_tiktok(servico_chave, quantidade)
        servico = CATALOGO["catalogos"]["tiktok"]["servicos"][servico_chave]

        pedido = preparar_pedido({
            "catalogo": "TikTok",
            "servico_chave": servico_chave,
            "servico": servico["nome"],
            "quantidade": item["quantidade_texto"],
            "quantidade_api": item["quantidade"],
            "api_service_id": item.get("api_service_id") or servico.get("api_service_id"),
            "valor": item["valor"],
            "link": None,
            "status": "aguardando_link",
            "usuario": update.effective_user.full_name,
            "username": update.effective_user.username,
            "user_id": update.effective_user.id,
        })
        info_limite = await obter_limite_solicitacoes_item("TikTok", servico_chave, item, servico)
        aplicar_limite_solicitacoes_no_pedido(pedido, info_limite)
        context.user_data["pedido"] = pedido
        mensagem_item = aplicar_limite_solicitacoes_na_mensagem(item["mensagem"], info_limite)

        await safe_edit_or_reply(
            update,
            mensagem_item,
            InlineKeyboardMarkup([[btn("⬅️ Voltar", f"servico_tiktok:{servico_chave}")]]),
            parse_mode=None,
        )
        return

    

    if data == "catalogo:kwai":
        context.user_data.pop("pedido", None)
        await safe_edit_or_reply(update, CATALOGO["catalogos"]["kwai"]["mensagem"], menu_kwai())
        return

    if data == "catalogo_kwai:brasileiros":
        context.user_data.pop("pedido", None)
        await safe_edit_or_reply(
            update,
            "🇧🇷 *Kwai — Serviços Brasileiros*\n\nEscolha o serviço que deseja contratar:",
            menu_kwai_brasileiros(),
        )
        return

    if data.startswith("servico_kwai:"):
        context.user_data.pop("pedido", None)
        servico_chave = data.split(":", 1)[1]
        servico = CATALOGO["catalogos"]["kwai"]["servicos"].get(servico_chave)
        if not servico:
            await safe_edit_or_reply(
                update,
                "❌ Serviço Kwai não encontrado no catálogo.",
                InlineKeyboardMarkup([[btn("⬅️ Voltar aos serviços Kwai", "catalogo_kwai:brasileiros")]]),
            )
            return
        await safe_edit_or_reply(update, servico["mensagem"], menu_itens_kwai(servico_chave))
        return

    if data.startswith("item_kwai:"):
        _, servico_chave, quantidade_str = data.split(":")
        quantidade = int(quantidade_str)
        item = get_item_kwai(servico_chave, quantidade)
        servico = CATALOGO["catalogos"]["kwai"]["servicos"][servico_chave]

        pedido = preparar_pedido({
            "catalogo": "Kwai",
            "servico_chave": servico_chave,
            "servico": servico["nome"],
            "quantidade": item["quantidade_texto"],
            "quantidade_api": item["quantidade"],
            "api_service_id": item.get("api_service_id") or servico.get("api_service_id"),
            "valor": item["valor"],
            "link": None,
            "status": "aguardando_link",
            "usuario": update.effective_user.full_name,
            "username": update.effective_user.username,
            "user_id": update.effective_user.id,
        })
        info_limite = await obter_limite_solicitacoes_item("Kwai", servico_chave, item, servico)
        aplicar_limite_solicitacoes_no_pedido(pedido, info_limite)
        context.user_data["pedido"] = pedido
        mensagem_item = aplicar_limite_solicitacoes_na_mensagem(item["mensagem"], info_limite)

        await safe_edit_or_reply(
            update,
            mensagem_item,
            InlineKeyboardMarkup([[btn("⬅️ Voltar", f"servico_kwai:{servico_chave}")]]),
            parse_mode=None,
        )
        return

    if data == "catalogo:internet":
        await safe_edit_or_reply(
            update,
            CATALOGO["catalogos"]["internet_ilimitada"]["mensagem"],
            InlineKeyboardMarkup([
                [btn("1 mês — R$ 15,00", "internet:1mes")],
                [btn("⬅️ Voltar", "menu:catalogo")]
            ]),
        )
        return

    if data == "internet:1mes":
        servico = CATALOGO["catalogos"]["internet_ilimitada"]["servicos"]["1mes"]
        item = servico["itens"][0]
        context.user_data["pedido"] = preparar_pedido({
            "catalogo": "Internet Ilimitada",
            "servico_chave": "1mes",
            "servico": servico.get("nome", "1 mês"),
            "quantidade": item.get("quantidade_texto", "1 mês"),
            "valor": item.get("valor", "15,00"),
            "link": None,
            "status": "aguardando_email_iptv",
            "usuario": update.effective_user.full_name,
            "username": update.effective_user.username,
            "user_id": update.effective_user.id,
        })

        await safe_edit_or_reply(
            update,
            item.get("mensagem") or servico.get("mensagem") or "📧 Envie o e-mail para ativação do serviço.",
            InlineKeyboardMarkup([[btn("⬅️ Voltar", "catalogo:internet")]]),
            parse_mode=None,
        )
        return


    if data == "catalogo:iptv":
        await safe_edit_or_reply(update, CATALOGO["catalogos"]["iptv"]["mensagem"], menu_iptv())
        return

    if data.startswith("item_iptv:"):
        _, servico_chave, quantidade_str = data.split(":")
        servico = CATALOGO["catalogos"]["iptv"]["servicos"][servico_chave]
        item = servico["itens"][0]

        context.user_data["pedido"] = preparar_pedido({
            "catalogo": "IPTV XCIPTV",
            "servico_chave": servico_chave,
            "servico": servico["nome"],
            "quantidade": item["quantidade_texto"],
            "valor": item["valor"],
            "link": None,
            "status": "aguardando_email_iptv",
            "usuario": update.effective_user.full_name,
            "username": update.effective_user.username,
            "user_id": update.effective_user.id,
        })

        await safe_edit_or_reply(
            update,
            item["mensagem"],
            InlineKeyboardMarkup([[btn("⬅️ Voltar", "catalogo:iptv")]]),
        )
        return

    if data == "alterar_email_iptv":
        pedido = context.user_data.get("pedido")
        if not pedido:
            await safe_edit_or_reply(update, "Não encontrei um pedido em andamento. Toque em /start para começar novamente.")
            return
        if pedido.get("pedido_id"):
            remover_pedido_pendente(str(pedido.get("pedido_id")))
        pedido.pop("comprovante_file_id", None)
        pedido.pop("comprovante_unique_id", None)
        pedido.pop("link_validado_antes_pagamento", None)
        pedido.pop("ultima_verificacao_link", None)
        pedido.pop("motivo_bloqueio_link", None)
        pedido["link"] = None
        pedido["status"] = "aguardando_email_iptv"
        await safe_edit_or_reply(update, "✏️ Envie novamente o e-mail correto para continuar.")
        return

    if data == "confirmar_email_iptv":
        pedido = context.user_data.get("pedido")
        if not pedido or not catalogo_exige_email(pedido) or not pedido.get("link"):
            await safe_edit_or_reply(update, "Não encontrei o e-mail do pedido. Envie o e-mail novamente.")
            return
        pedido["status"] = "aguardando_pagamento"
        await enviar_pagamento_cliente(update, context, pedido)
        return

    if data.startswith("servico:"):
        servico_chave = data.split(":", 1)[1]
        servico = CATALOGO["catalogos"]["instagram"]["servicos"][servico_chave]
        await safe_edit_or_reply(update, servico["mensagem"], menu_itens(servico_chave))
        return

    if data.startswith("item:"):
        _, servico_chave, quantidade_str = data.split(":")
        quantidade = int(quantidade_str)
        item = get_item(servico_chave, quantidade)
        servico = CATALOGO["catalogos"]["instagram"]["servicos"][servico_chave]

        pedido = preparar_pedido({
            "catalogo": "Instagram",
            "servico_chave": servico_chave,
            "servico": servico["nome"],
            "quantidade": item["quantidade_texto"],
            "quantidade_api": item["quantidade"],
            "api_service_id": item.get("api_service_id") or servico.get("api_service_id"),
            "valor": item["valor"],
            "link": None,
            "status": "aguardando_link",
            "usuario": update.effective_user.full_name,
            "username": update.effective_user.username,
            "user_id": update.effective_user.id,
        })
        info_limite = await obter_limite_solicitacoes_item("Instagram", servico_chave, item, servico)
        aplicar_limite_solicitacoes_no_pedido(pedido, info_limite)
        context.user_data["pedido"] = pedido
        mensagem_item = aplicar_limite_solicitacoes_na_mensagem(item["mensagem"], info_limite)

        await safe_edit_or_reply(
            update,
            mensagem_item,
            InlineKeyboardMarkup([[btn("⬅️ Voltar", f"servico:{servico_chave}")]]),
            parse_mode=None,
        )
        return

    if data == "alterar_link":
        pedido = context.user_data.get("pedido")
        if not pedido:
            await safe_edit_or_reply(update, "Não encontrei um pedido em andamento. Toque em /start para começar novamente.")
            return
        if pedido.get("pedido_id"):
            remover_pedido_pendente(str(pedido.get("pedido_id")))
        pedido.pop("comprovante_file_id", None)
        pedido.pop("comprovante_unique_id", None)
        pedido.pop("link_validado_antes_pagamento", None)
        pedido.pop("ultima_verificacao_link", None)
        pedido.pop("motivo_bloqueio_link", None)
        pedido["link"] = None
        if catalogo_exige_email(pedido):
            pedido["status"] = "aguardando_email_iptv"
            await enviar_texto_sequencial(update, context, "✏️ Envie novamente o e-mail correto para continuar.")
        else:
            pedido["status"] = "aguardando_link"
            await enviar_texto_sequencial(update, context, "✏️ Envie novamente o link ou @ correto para continuar.")
        return

    if data == "confirmar_pedido":
        pedido = context.user_data.get("pedido")
        await finalizar_pedido_confirmado(update, context, pedido)
        return


async def processar_solicitacao_refil(update: Update, context: ContextTypes.DEFAULT_TYPE, consulta_id: str):
    order_id, pedido_local, origem = obter_order_id_para_refil(consulta_id)

    if not order_id:
        texto = (
            "❌ Não foi possível solicitar reposição/refil para esse ID.\n\n"
            "O pedido precisa ter um *ID na plataforma* para que o refil seja solicitado."
        )
        if pedido_local:
            texto += "\n\n" + texto_status_pedido_local(pedido_local, origem)
        await safe_edit_or_reply(
            update,
            texto,
            InlineKeyboardMarkup([
                [btn("🔁 Enviar outro ID", "pedido:solicitar_refil")],
                [btn("🏠 Menu inicial", "voltar:inicio")],
            ]),
        )
        context.user_data.clear()
        return

    try:
        # Antes de enviar o refil, consulta o status para evitar solicitar em pedido ainda em andamento.
        status_resultado = await asyncio.to_thread(consultar_status_pedido_plataforma_sync, order_id)
        status_atual = str(
            status_resultado.get("status")
            or status_resultado.get("Status")
            or status_resultado.get("state")
            or ""
        ).strip().lower()
        if status_atual in {"pending", "in progress", "inprogress", "processing"}:
            await safe_edit_or_reply(
                update,
                (
                    "⏳ *Refil ainda não disponível*\n\n"
                    f"🚀 *ID na plataforma:* `{md(order_id)}`\n"
                    f"📌 *Status atual:* {md(traduzir_status_plataforma(status_atual))}\n\n"
                    "Esse pedido ainda está em andamento. Assim que finalizar, você pode pedir a reposição/refil."
                ),
                botoes_consulta_pedido(order_id),
            )
            context.user_data.clear()
            return

        resultado = await asyncio.to_thread(solicitar_refil_pedido_plataforma_sync, order_id)

        if pedido_local:
            pedido_local["ultimo_refil_solicitado_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
            pedido_local["ultimo_refil_resposta"] = resultado
            refil_id = extrair_refil_id(resultado)
            if refil_id:
                pedido_local["ultimo_refil_id"] = refil_id
            salvar_pedido_historico(pedido_local)

        await safe_edit_or_reply(
            update,
            texto_refil_solicitado(order_id, resultado),
            InlineKeyboardMarkup([
                [btn("🔎 Consultar Status", "pedido:consultar_status")],
                [btn("🏠 Menu inicial", "voltar:inicio")],
            ]),
        )
        context.user_data.clear()
        return

    except (PlataformaAPIConfigError, PlataformaAPIRequestError) as exc:
        await safe_edit_or_reply(
            update,
            (
                "⚠️ *Não foi possível pedir o refil agora*\n\n"
                f"🚀 *ID na plataforma:* `{md(order_id)}`\n"
                f"*Motivo:* {md(limpar_erro_api(exc))}\n\n"
                "Isso pode acontecer quando o serviço não possui refil, o prazo expirou ou o pedido ainda não está pronto para reposição."
            ),
            botoes_consulta_pedido(order_id),
        )
        context.user_data.clear()
        return


async def responder_consulta_pedido(update: Update, context: ContextTypes.DEFAULT_TYPE, texto_usuario: str):
    consulta_id = normalizar_id_consulta(texto_usuario)
    if not consulta_id:
        await update.message.reply_text(
            "⚠️ Envie um ID de pedido válido para eu consultar.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[btn("⬅️ Voltar", "voltar:inicio")]]),
        )
        return

    pedido_local, origem = buscar_pedido_local_por_id(consulta_id)
    plataforma_order_id = None
    if pedido_local and pedido_tem_id_plataforma(pedido_local.get("plataforma_order_id")):
        plataforma_order_id = str(pedido_local.get("plataforma_order_id"))
    elif consulta_id.isdigit() and pedido_tem_id_plataforma(consulta_id):
        plataforma_order_id = consulta_id

    if plataforma_order_id:
        try:
            resultado = await asyncio.to_thread(consultar_status_pedido_plataforma_sync, plataforma_order_id)
            await update.message.reply_text(
                texto_status_pedido_plataforma(plataforma_order_id, resultado, pedido_local),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=botoes_consulta_pedido(plataforma_order_id),
                disable_web_page_preview=True,
            )
            context.user_data.clear()
            return
        except (PlataformaAPIConfigError, PlataformaAPIRequestError) as exc:
            if pedido_local:
                await update.message.reply_text(
                    texto_status_pedido_local(pedido_local, origem)
                    + "\n\n⚠️ Não consegui consultar a plataforma neste momento.\n"
                    + f"*Motivo:* {md(limpar_erro_api(exc))}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=botoes_consulta_pedido(pedido_local.get("plataforma_order_id") if pedido_local else None),
                    disable_web_page_preview=True,
                )
                context.user_data.clear()
                return

            await update.message.reply_text(
                "⚠️ Não consegui consultar esse ID na plataforma.\n\n"
                f"*Motivo:* {md(limpar_erro_api(exc))}\n\n"
                "Confira se o ID está correto e tente novamente em alguns instantes.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[btn("⬅️ Voltar", "voltar:inicio")]]),
                disable_web_page_preview=True,
            )
            return

    if pedido_local:
        await update.message.reply_text(
            texto_status_pedido_local(pedido_local, origem),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=botoes_consulta_pedido(pedido_local.get("plataforma_order_id") if pedido_local else None),
            disable_web_page_preview=True,
        )
        context.user_data.clear()
        return

    await update.message.reply_text(
        "❌ Não encontrei esse pedido por aqui.\n\n"
        "Confira se o ID foi digitado certinho. Se o pedido já foi enviado para a plataforma, tente enviar o ID da plataforma.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[btn("⬅️ Voltar", "voltar:inicio")]]),
    )


async def receber_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_usuario = (update.message.text or "").strip()

    if await processar_mensagem_ticket(update, context):
        return

    if context.user_data.get("consulta_pedido"):
        await responder_consulta_pedido(update, context, texto_usuario)
        return

    if context.user_data.get("refil_pedido"):
        await processar_solicitacao_refil(update, context, texto_usuario)
        return

    pedido = context.user_data.get("pedido")

    if not pedido:
        await update.message.reply_text(
            "Para iniciar um pedido, toque em /start e escolha uma opção do catálogo.",
            reply_markup=menu_principal(),
        )
        return

    if await encerrar_interacao_se_pagamento_expirado(update, context, pedido):
        return

    if pedido.get("status") == "aguardando_pagamento" and texto_usuario == "1":
        await finalizar_pedido_confirmado(update, context, pedido)
        return

    if pedido.get("status") == "aguardando_aprovacao_admin":
        await update.message.reply_text(
            "⏳ Seu comprovante já está em validação. O pedido só será liberado depois da aprovação.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not pedido.get("link"):
        valido_destino, destino_normalizado, erro_destino = validar_destino_pedido(pedido, texto_usuario)
        if not valido_destino:
            await update.message.reply_text(
                erro_destino,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[btn("⬅️ Alterar serviço", "menu:catalogo")]]),
                disable_web_page_preview=True,
            )
            return

        pedido["link"] = destino_normalizado
        pedido.pop("link_validado_antes_pagamento", None)
        pedido.pop("ultima_verificacao_link", None)
        pedido.pop("motivo_bloqueio_link", None)

        if catalogo_exige_email(pedido) and pedido.get("status") == "aguardando_email_iptv":
            await update.message.reply_text(
                (
                    "📧 *Etapa 1 de 3 — Dados recebidos*\n\n"
                    "Confira o e-mail informado:\n\n"
                    f"`{md(pedido['link'])}`\n\n"
                    "Se estiver correto, toque em *Confirmar e ir para pagamento*."
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=botoes_confirmar_email_iptv(),
                disable_web_page_preview=True,
            )
            return

        pedido["status"] = "aguardando_pagamento"
        await enviar_pagamento_cliente(update, context, pedido)
        return

    if pedido.get("catalogo") == "Instagram" and pedido.get("status") == "aguardando_pagamento":
        await enviar_pagamento_cliente(update, context, pedido)
        return

    destino_recebido = "e-mail" if catalogo_exige_email(pedido) else "link/@"
    await update.message.reply_text(
        f"✅ Já recebi o {destino_recebido} do cliente. Agora finalize pela aba de pagamento abaixo.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=botoes_pagamento(pedido),
    )


async def receber_comprovante(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await processar_mensagem_ticket(update, context):
        return

    pedido = context.user_data.get("pedido")

    if not pedido:
        await update.message.reply_text(
            "Para iniciar um pedido, toque em /start e escolha uma opção do catálogo.",
            reply_markup=menu_principal(),
        )
        return

    if pedido.get("status") not in ("aguardando_pagamento", "aguardando_aprovacao_admin") or not pedido.get("link"):
        destino_necessario = "e-mail" if catalogo_exige_email(pedido) else "link/@"
        await update.message.reply_text(f"Recebi a imagem, mas ainda preciso do {destino_necessario} do pedido primeiro.")
        return

    if await encerrar_interacao_se_pagamento_expirado(update, context, pedido):
        return

    if pedido.get("mp_payment_id"):
        await update.message.reply_text(
            "✅ Neste pedido o pagamento é confirmado automaticamente pelo Mercado Pago. "
            "Não precisa enviar comprovante; pague o Pix e toque em ‘Verificar Pagamento’."
        )
        return

    file_id = None
    file_unique_id = None
    if update.message.photo:
        arquivo = update.message.photo[-1]
        file_id = arquivo.file_id
        file_unique_id = arquivo.file_unique_id
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        arquivo = update.message.document
        file_id = arquivo.file_id
        file_unique_id = arquivo.file_unique_id
    else:
        await update.message.reply_text("Envie o comprovante como imagem para eu encaminhar para validação.")
        return

    if comprovante_ja_usado(file_unique_id):
        await update.message.reply_text(
            "🚫 Esse mesmo arquivo de comprovante já foi usado em outro pedido. "
            "Envie um comprovante válido e exclusivo deste pedido."
        )
        return

    pedido["comprovante_file_id"] = file_id
    pedido["comprovante_unique_id"] = file_unique_id
    pedido["status"] = "aguardando_aprovacao_admin"
    pedido["comprovante_recebido_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")

    salvar_pedido_pendente(pedido)
    enviado_admin = await enviar_para_aprovacao_admin(update, context, pedido)

    if not enviado_admin:
        await update.message.reply_text(
            "⚠️ Comprovante recebido, mas o ADMIN_CHAT_ID não está configurado. "
            "Configure o administrador antes de liberar pedidos."
        )
        return

    await enviar_texto_sequencial(
        update,
        context,
        (
            "✅ Comprovante recebido e enviado para validação.\n\n"
            f"🆔 *ID do pedido:* `{md(pedido.get('pedido_id', ''))}`\n"
            "O pedido só será enviado para a plataforma depois que o administrador aprovar o comprovante."
        ),
        InlineKeyboardMarkup([[btn("🏠 Menu inicial", "voltar:inicio")]]),
    )




def pedido_tem_problema_operacional(pedido: dict | None) -> bool:
    """Retorna True apenas para falha, falta de estoque ou revisão manual."""
    if not isinstance(pedido, dict):
        return False
    status = str(pedido.get("plataforma_api_status") or "").strip().lower()
    status_local = str(pedido.get("status") or "").strip().lower()
    if status in {"erro", "revisao_manual", "bloqueado", "bloqueado_sem_reposicao", "estoque_indisponivel", "sem_estoque", "falha"}:
        return True
    if status_local in {"erro", "revisao_manual", "bloqueado_sem_reposicao", "estoque_indisponivel", "sem_estoque", "falha"}:
        return True
    return bool(pedido.get("plataforma_api_erro") or pedido.get("motivo_bloqueio_estoque") or pedido.get("motivo_bloqueio"))


def texto_alerta_problema_pedido_admin(pedido: dict) -> str:
    status = str(pedido.get("plataforma_api_status") or pedido.get("status") or "problema")
    motivo = pedido.get("plataforma_api_erro") or pedido.get("motivo_bloqueio_estoque") or pedido.get("motivo_bloqueio") or "Problema operacional não informado"
    return (
        "⚠️ *PROBLEMA NO PROCESSAMENTO DO PEDIDO*\n\n"
        f"🆔 *Pedido:* `{md(pedido.get('pedido_id', 'Não informado'))}`\n"
        f"📦 *Serviço:* {md(pedido.get('servico') or pedido.get('catalogo') or 'Não informado')}\n"
        f"📌 *Status:* {md(status)}\n"
        f"📝 *Motivo:* {md(motivo)}\n"
        f"👤 *Cliente:* {md(pedido.get('usuario') or 'Não informado')}\n"
        f"🆔 *Telegram ID:* `{md(pedido.get('user_id') or 'Não informado')}`"
    )


def notificar_admin_problema_pedido_sync(pedido: dict | None) -> bool:
    """Envia ao Admin 1 somente problemas; pedidos normais não geram relatório."""
    if not pedido_tem_problema_operacional(pedido):
        return False
    if pedido.get("alerta_problema_admin_enviado_em"):
        return False
    destino = str(ADMIN_CHAT_ID or "").strip()
    if not destino:
        logging.error("ADMIN_CHAT_ID não configurado para alerta operacional do pedido %s", (pedido or {}).get("pedido_id"))
        return False
    enviado = enviar_telegram_sync(destino, texto_alerta_problema_pedido_admin(pedido))
    if enviado:
        pedido["alerta_problema_admin_enviado_em"] = agora_br().strftime("%d/%m/%Y %H:%M:%S")
        try:
            salvar_pedido_historico(pedido)
        except Exception:
            pass
    return bool(enviado)


async def avisar_admin_se_houver_problema(update: Update, context: ContextTypes.DEFAULT_TYPE, pedido: dict) -> bool:
    return await asyncio.to_thread(notificar_admin_problema_pedido_sync, pedido)


async def enviar_relatorio_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, pedido: dict):
    """Compatibilidade: o antigo relatório de pedido virou alerta de problema."""
    return await avisar_admin_se_houver_problema(update, context, pedido)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Configure a variável BOT_TOKEN com o token do BotFather.")
    reconstruir_pagamentos_processados_do_historico()
    limpar_pedidos_pendentes_salvos_no_startup()
    limpar_persistencia_transiente_no_startup()
    corrigir_pedidos_com_envio_interrompido()
    webhooks_recuperados = DB.recuperar_webhooks_processando_interrompidos()
    if webhooks_recuperados:
        logging.warning("Webhook(s) travados em processamento foram liberados para rechecagem: %s", webhooks_recuperados)
    iniciar_servidor_web()
    iniciar_rotina_webhook_queue()
    persistence = PicklePersistence(filepath=str(BOT_PERSISTENCE_PATH))
    app = Application.builder().token(BOT_TOKEN).persistence(persistence).post_init(iniciar_rotinas).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("painel", painel_admin))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.ALL), receber_comprovante))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receber_texto))
    print("Bot TW STORE iniciado.")
    print(f"Pasta de dados em: {DATA_DIR}")
    print(f"Banco SQLite em: {DATABASE_PATH}")
    # Evita que o Telegram entregue callbacks/mensagens antigas quando o bot reinicia.
    # Sem isso, cliques antigos em botões de pagamento podem ser processados novamente.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
