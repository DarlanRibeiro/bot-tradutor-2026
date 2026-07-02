import os
import json
import asyncio
import logging
from typing import Optional, Tuple, Dict, Any

import asyncpg
from dotenv import load_dotenv
from deep_translator import GoogleTranslator

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.error import BadRequest, Forbidden, TimedOut, NetworkError, TelegramError
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()

# =========================
# CONFIGURAÇÕES
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN não encontrado no .env ou nas variáveis do Railway.")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL não encontrado no .env ou nas variáveis do Railway.")

# Grupo Casa dos Ninjas: mensagens auxiliares do bot são apagadas silenciosamente aqui.
CASA_DOS_NINJAS_ID = -1002884618014

# Tempo para voltar ao texto original depois da tradução.
TEMPO_RETORNO_SEGUNDOS = 30

# Limites do Telegram.
LIMITE_TEXTO = 4096
LIMITE_CAPTION = 1024

LANGS = {
    "china": ("zh-CN", "🇨🇳 Chinês"),
    "brasil": ("pt", "🇧🇷 Português Brasil"),
    "espanha": ("es", "🇪🇸 Espanhol"),
    "portugal": ("pt", "🇵🇹 Português Portugal"),
    "eua": ("en", "🇺🇸 Inglês"),
}

POSTS_ORIGINAIS: Dict[Tuple[int, int], Dict[str, Any]] = {}
TAREFAS_RETORNO: Dict[Tuple[int, int], asyncio.Task] = {}
DB_POOL: Optional[asyncpg.Pool] = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("bot_tradutor")


# =========================
# UTILITÁRIOS
# =========================

def chave_post(chat_id: int, message_id: int) -> Tuple[int, int]:
    return int(chat_id), int(message_id)


def teclado_bandeiras(message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇨🇳", callback_data=f"traduzir:china:{message_id}"),
            InlineKeyboardButton("🇧🇷", callback_data=f"traduzir:brasil:{message_id}"),
            InlineKeyboardButton("🇪🇸", callback_data=f"traduzir:espanha:{message_id}"),
            InlineKeyboardButton("🇵🇹", callback_data=f"traduzir:portugal:{message_id}"),
            InlineKeyboardButton("🇺🇸", callback_data=f"traduzir:eua:{message_id}"),
        ]
    ])


def texto_menu() -> str:
    return "🌐 Traduzir este post:"


def cortar_texto(texto: str, tem_caption: bool) -> str:
    limite = LIMITE_CAPTION if tem_caption else LIMITE_TEXTO
    if len(texto) <= limite:
        return texto
    return texto[: limite - 1] + "…"


def limpar_texto_portugal(texto: str) -> str:
    # Ajuste simples para diferenciar um pouco o PT-PT do PT-BR.
    substituicoes = {
        "você": "tu",
        "Você": "Tu",
        "vocês": "vós",
        "Vocês": "Vós",
    }
    for origem, destino in substituicoes.items():
        texto = texto.replace(origem, destino)
    return texto


def entities_para_json(entities) -> Optional[str]:
    if not entities:
        return None
    return json.dumps([e.to_dict() for e in entities], ensure_ascii=False)


def json_para_entities(valor, bot) -> Optional[list]:
    if not valor:
        return None

    if isinstance(valor, str):
        valor = json.loads(valor)

    return [MessageEntity.de_json(e, bot) for e in valor]


def eh_mensagem_auxiliar_do_bot(msg) -> bool:
    texto = msg.text or msg.caption or ""
    return (
        texto_menu() in texto
        or texto.startswith("🇨🇳 Chinês")
        or texto.startswith("🇧🇷 Português Brasil")
        or texto.startswith("🇪🇸 Espanhol")
        or texto.startswith("🇵🇹 Português Portugal")
        or texto.startswith("🇺🇸 Inglês")
    )


# =========================
# BANCO DE DADOS
# =========================

async def iniciar_banco() -> None:
    global DB_POOL
    DB_POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_posts (
                id BIGSERIAL PRIMARY KEY,
                message_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                texto TEXT NOT NULL,
                tem_caption BOOLEAN NOT NULL DEFAULT FALSE,
                entities JSONB,
                modo TEXT NOT NULL DEFAULT 'original',
                bot_message_id BIGINT,
                criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (chat_id, message_id)
            );
            """
        )

    logger.info("Banco iniciado e tabela telegram_posts verificada.")


async def fechar_banco() -> None:
    global DB_POOL
    if DB_POOL:
        await DB_POOL.close()
        DB_POOL = None
        logger.info("Conexão com banco fechada.")


async def limpar_posts_antigos() -> None:
    if not DB_POOL:
        return

    async with DB_POOL.acquire() as conn:
        removidos = await conn.execute(
            """
            DELETE FROM telegram_posts
            WHERE criado_em < NOW() - INTERVAL '30 days'
            """
        )

    logger.info("Limpeza do banco: %s", removidos)


async def salvar_post_banco(message_id: int, dados: Dict[str, Any]) -> None:
    if not DB_POOL:
        return

    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO telegram_posts (
                message_id,
                chat_id,
                texto,
                tem_caption,
                entities,
                modo,
                bot_message_id
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
            ON CONFLICT (chat_id, message_id)
            DO UPDATE SET
                texto = EXCLUDED.texto,
                tem_caption = EXCLUDED.tem_caption,
                entities = EXCLUDED.entities,
                modo = EXCLUDED.modo,
                bot_message_id = EXCLUDED.bot_message_id,
                atualizado_em = NOW();
            """,
            int(message_id),
            int(dados["chat_id"]),
            dados["texto"],
            bool(dados["tem_caption"]),
            entities_para_json(dados.get("entities")),
            dados.get("modo", "original"),
            dados.get("bot_message_id"),
        )


async def buscar_post_banco(chat_id: int, message_id: int, bot) -> Optional[Dict[str, Any]]:
    if not DB_POOL:
        return None

    async with DB_POOL.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT chat_id, texto, tem_caption, entities, modo, bot_message_id
            FROM telegram_posts
            WHERE chat_id = $1 AND message_id = $2
            """,
            int(chat_id),
            int(message_id),
        )

    if not row:
        return None

    return {
        "chat_id": row["chat_id"],
        "texto": row["texto"],
        "tem_caption": row["tem_caption"],
        "entities": json_para_entities(row["entities"], bot),
        "modo": row["modo"],
        "bot_message_id": row["bot_message_id"],
    }


# =========================
# EDIÇÃO DAS MENSAGENS
# =========================

async def editar_original(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    texto: str,
    tem_caption: bool,
    entities=None,
) -> None:
    texto = cortar_texto(texto, tem_caption)

    if tem_caption:
        await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=texto,
            caption_entities=entities,
            reply_markup=teclado_bandeiras(message_id),
        )
    else:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=texto,
            entities=entities,
            reply_markup=teclado_bandeiras(message_id),
        )


async def adicionar_botoes_ao_post(context: ContextTypes.DEFAULT_TYPE, msg) -> None:
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            reply_markup=teclado_bandeiras(msg.message_id),
        )
    except BadRequest as erro:
        # Algumas mensagens não permitem editar markup. Não quebra o bot.
        logger.info("Não foi possível adicionar botões ao post %s: %s", msg.message_id, erro)
    except TelegramError as erro:
        logger.warning("Erro ao adicionar botões: %s", erro)


async def apagar_auxiliar_no_grupo(context: ContextTypes.DEFAULT_TYPE, msg) -> None:
    try:
        await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
    except TelegramError:
        pass


# =========================
# HANDLERS
# =========================

async def novo_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message

    if not msg:
        return

    # No grupo Casa dos Ninjas, apaga mensagens auxiliares do bot e não processa como post novo.
    if msg.chat_id == CASA_DOS_NINJAS_ID:
        if eh_mensagem_auxiliar_do_bot(msg):
            await apagar_auxiliar_no_grupo(context, msg)
        return

    # Ignora edições antigas para não sobrescrever o original salvo.
    if msg.edit_date:
        return

    texto = msg.text or msg.caption

    if not texto:
        return

    if eh_mensagem_auxiliar_do_bot(msg):
        return

    tem_caption = bool(msg.caption)
    entities = msg.caption_entities if tem_caption else msg.entities
    chave = chave_post(msg.chat_id, msg.message_id)

    dados = {
        "chat_id": msg.chat_id,
        "texto": texto,
        "tem_caption": tem_caption,
        "entities": entities,
        "modo": "original",
        "bot_message_id": None,
    }

    POSTS_ORIGINAIS[chave] = dados
    await salvar_post_banco(msg.message_id, dados)
    await adicionar_botoes_ao_post(context, msg)


async def voltar_original(context: ContextTypes.DEFAULT_TYPE, chat_id: int, post_id: int) -> None:
    await asyncio.sleep(TEMPO_RETORNO_SEGUNDOS)

    chave = chave_post(chat_id, post_id)
    dados = POSTS_ORIGINAIS.get(chave)

    if not dados:
        dados = await buscar_post_banco(chat_id, post_id, context.bot)
        if dados:
            POSTS_ORIGINAIS[chave] = dados

    if not dados:
        TAREFAS_RETORNO.pop(chave, None)
        return

    try:
        await editar_original(
            context=context,
            chat_id=dados["chat_id"],
            message_id=post_id,
            texto=dados["texto"],
            tem_caption=dados["tem_caption"],
            entities=dados.get("entities"),
        )
    except BadRequest as erro:
        logger.info("Não foi possível voltar original do post %s: %s", post_id, erro)
    except TelegramError as erro:
        logger.warning("Erro ao voltar original: %s", erro)
    finally:
        TAREFAS_RETORNO.pop(chave, None)


async def clicar_bandeira(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if not query:
        return

    try:
        await query.answer()
    except TelegramError:
        pass

    try:
        _, pais, post_id_texto = query.data.split(":")
        post_id = int(post_id_texto)
    except Exception:
        return

    if pais not in LANGS:
        return

    if not query.message:
        return

    chat_id = query.message.chat_id
    chave = chave_post(chat_id, post_id)
    dados = POSTS_ORIGINAIS.get(chave)

    if not dados:
        dados = await buscar_post_banco(chat_id, post_id, context.bot)
        if dados:
            POSTS_ORIGINAIS[chave] = dados

    if not dados:
        return

    texto_original = dados["texto"]
    tem_caption = dados["tem_caption"]
    entities = dados.get("entities")

    # 🇧🇷 retorna imediatamente ao original.
    if pais == "brasil":
        tarefa = TAREFAS_RETORNO.pop(chave, None)
        if tarefa:
            tarefa.cancel()

        try:
            await editar_original(
                context=context,
                chat_id=chat_id,
                message_id=post_id,
                texto=texto_original,
                tem_caption=tem_caption,
                entities=entities,
            )
        except TelegramError as erro:
            logger.warning("Erro ao restaurar original manualmente: %s", erro)
        return

    idioma, _nome = LANGS[pais]

    try:
        traducao = GoogleTranslator(source="auto", target=idioma).translate(texto_original)
    except Exception as erro:
        logger.warning("Erro na tradução: %s", erro)
        return

    if pais == "portugal":
        traducao = limpar_texto_portugal(traducao)

    traducao = cortar_texto(traducao, tem_caption)

    try:
        # Na tradução, não reaplicamos entities para evitar offsets quebrados.
        await editar_original(
            context=context,
            chat_id=chat_id,
            message_id=post_id,
            texto=traducao,
            tem_caption=tem_caption,
            entities=None,
        )
    except BadRequest as erro:
        logger.info("Não foi possível editar post traduzido %s: %s", post_id, erro)
        return
    except TelegramError as erro:
        logger.warning("Erro ao editar tradução: %s", erro)
        return

    tarefa_antiga = TAREFAS_RETORNO.pop(chave, None)
    if tarefa_antiga:
        tarefa_antiga.cancel()

    TAREFAS_RETORNO[chave] = asyncio.create_task(voltar_original(context, chat_id, post_id))


async def erro_global(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    erro = context.error
    if isinstance(erro, (TimedOut, NetworkError)):
        logger.warning("Erro temporário de rede/Telegram: %s", erro)
    elif isinstance(erro, Forbidden):
        logger.warning("Bot sem permissão ou removido de chat: %s", erro)
    else:
        logger.exception("Erro não tratado: %s", erro)


# =========================
# INICIALIZAÇÃO
# =========================

async def post_init(app: Application) -> None:
    await iniciar_banco()
    await limpar_posts_antigos()


async def post_shutdown(app: Application) -> None:
    for tarefa in list(TAREFAS_RETORNO.values()):
        tarefa.cancel()
    TAREFAS_RETORNO.clear()
    await fechar_banco()


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CallbackQueryHandler(clicar_bandeira, pattern=r"^traduzir:"))
    app.add_handler(MessageHandler(filters.ALL, novo_post))
    app.add_error_handler(erro_global)

    logger.info("BOT DE TRADUÇÃO INICIADO")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
