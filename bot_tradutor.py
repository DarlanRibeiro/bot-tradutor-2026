import os
import json
import asyncio
import asyncpg
from dotenv import load_dotenv
from deep_translator import GoogleTranslator

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN não encontrado.")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL não encontrado.")

CASA_DOS_NINJAS_ID = -1002884618014

LANGS = {
    "china": ("zh-CN", "🇨🇳 Chinês"),
    "brasil": ("pt", "🇧🇷 Português Brasil"),
    "espanha": ("es", "🇪🇸 Espanhol"),
    "portugal": ("pt", "🇵🇹 Português Portugal"),
    "eua": ("en", "🇺🇸 Inglês"),
}

POSTS_ORIGINAIS = {}
TAREFAS_RETORNO = {}
DB_POOL = None


def teclado_bandeiras(message_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🇨🇳", callback_data=f"traduzir:china:{message_id}"),
        InlineKeyboardButton("🇧🇷", callback_data=f"traduzir:brasil:{message_id}"),
        InlineKeyboardButton("🇪🇸", callback_data=f"traduzir:espanha:{message_id}"),
        InlineKeyboardButton("🇵🇹", callback_data=f"traduzir:portugal:{message_id}"),
        InlineKeyboardButton("🇺🇸", callback_data=f"traduzir:eua:{message_id}"),
    ]])


def texto_menu():
    return "🌐 Traduzir este post:"


async def iniciar_banco():
    global DB_POOL
    DB_POOL = await asyncpg.create_pool(DATABASE_URL)


def entities_para_json(entities):
    if not entities:
        return None
    return json.dumps([e.to_dict() for e in entities])


def json_para_entities(valor, bot):
    if not valor:
        return None

    if isinstance(valor, str):
        valor = json.loads(valor)

    return [MessageEntity.de_json(e, bot) for e in valor]


async def salvar_post_banco(message_id, dados):
    if not DB_POOL:
        return

    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO telegram_posts (
                message_id, chat_id, texto, tem_caption, entities, modo, bot_message_id
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
            ON CONFLICT (message_id)
            DO UPDATE SET
                chat_id = EXCLUDED.chat_id,
                texto = EXCLUDED.texto,
                tem_caption = EXCLUDED.tem_caption,
                entities = EXCLUDED.entities,
                modo = EXCLUDED.modo,
                bot_message_id = EXCLUDED.bot_message_id;
            """,
            message_id,
            dados["chat_id"],
            dados["texto"],
            dados["tem_caption"],
            entities_para_json(dados.get("entities")),
            dados.get("modo", "original"),
            dados.get("bot_message_id")
        )


async def buscar_post_banco(message_id, bot):
    if not DB_POOL:
        return None

    async with DB_POOL.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM telegram_posts WHERE message_id = $1",
            message_id
        )

    if not row:
        return None

    return {
        "chat_id": row["chat_id"],
        "texto": row["texto"],
        "tem_caption": row["tem_caption"],
        "entities": json_para_entities(row["entities"], bot),
        "modo": row["modo"],
        "bot_message_id": row["bot_message_id"]
    }


async def editar_original(context, chat_id, message_id, texto, tem_caption, entities=None):
    if tem_caption:
        await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=texto,
            caption_entities=entities,
            reply_markup=teclado_bandeiras(message_id)
        )
    else:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=texto,
            entities=entities,
            reply_markup=teclado_bandeiras(message_id)
        )


async def novo_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message

    if not msg:
        return

    if msg.chat_id == CASA_DOS_NINJAS_ID:
        if msg.text and (
            "🌐 Traduzir este post" in msg.text
            or msg.text.startswith("🇨🇳 Chinês")
            or msg.text.startswith("🇧🇷 Português Brasil")
            or msg.text.startswith("🇪🇸 Espanhol")
            or msg.text.startswith("🇵🇹 Português Portugal")
            or msg.text.startswith("🇺🇸 Inglês")
        ):
            try:
                await context.bot.delete_message(
                    chat_id=msg.chat_id,
                    message_id=msg.message_id
                )
            except Exception:
                pass
        return

    texto = msg.text or msg.caption

    if not texto:
        return

    if msg.text and "🌐 Traduzir este post" in msg.text:
        return

    tem_caption = bool(msg.caption)
    entities = msg.caption_entities if tem_caption else msg.entities

    POSTS_ORIGINAIS[msg.message_id] = {
        "chat_id": msg.chat_id,
        "texto": texto,
        "tem_caption": tem_caption,
        "entities": entities,
        "modo": "original",
        "bot_message_id": None
    }

    await salvar_post_banco(msg.message_id, POSTS_ORIGINAIS[msg.message_id])

    try:
        await context.bot.edit_message_reply_markup(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            reply_markup=teclado_bandeiras(msg.message_id)
        )
    except Exception:
        pass


async def voltar_original(context, post_id):
    await asyncio.sleep(60)

    dados = POSTS_ORIGINAIS.get(post_id)

    if not dados:
        dados = await buscar_post_banco(post_id, context.bot)
        if dados:
            POSTS_ORIGINAIS[post_id] = dados

    if not dados:
        return

    try:
        await editar_original(
            context,
            dados["chat_id"],
            post_id,
            dados["texto"],
            dados["tem_caption"],
            dados.get("entities")
        )
    except Exception:
        pass


async def clicar_bandeira(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    try:
        _, pais, post_id = query.data.split(":")
        post_id = int(post_id)

        dados = POSTS_ORIGINAIS.get(post_id)

        if not dados:
            dados = await buscar_post_banco(post_id, context.bot)
            if dados:
                POSTS_ORIGINAIS[post_id] = dados

        if not dados:
            return

        chat_id = dados["chat_id"]
        texto_original = dados["texto"]
        tem_caption = dados["tem_caption"]
        entities = dados.get("entities")

        if pais == "brasil":
            if post_id in TAREFAS_RETORNO:
                TAREFAS_RETORNO[post_id].cancel()
                del TAREFAS_RETORNO[post_id]

            await editar_original(
                context,
                chat_id,
                post_id,
                texto_original,
                tem_caption,
                entities
            )
            return

        idioma, _ = LANGS[pais]

        traducao = GoogleTranslator(
            source="auto",
            target=idioma
        ).translate(texto_original)

        if pais == "portugal":
            traducao = traducao.replace("você", "tu").replace("Você", "Tu")

        if len(traducao) > 1000 and tem_caption:
            traducao = traducao[:1000]

        if len(traducao) > 4000:
            traducao = traducao[:4000]

        try:
            await editar_original(
                context,
                chat_id,
                post_id,
                traducao,
                tem_caption,
                None
            )
        except Exception:
            return

        if post_id in TAREFAS_RETORNO:
            TAREFAS_RETORNO[post_id].cancel()
            del TAREFAS_RETORNO[post_id]

        TAREFAS_RETORNO[post_id] = asyncio.create_task(
            voltar_original(context, post_id)
        )

    except Exception:
        pass


def main():

    async def post_init(app):
        await iniciar_banco()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(MessageHandler(filters.ALL, novo_post))
    app.add_handler(CallbackQueryHandler(clicar_bandeira, pattern="^traduzir:"))

    print("BOT DE TRADUÇÃO INICIADO")
    app.run_polling()


if __name__ == "__main__":
    main()