import os
import asyncio
from dotenv import load_dotenv
from deep_translator import GoogleTranslator

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN não encontrado. Configure a variável no Railway/Render.")

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


async def editar_original(
    context,
    chat_id,
    message_id,
    texto,
    tem_caption,
    entities=None
):
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

    # CASA DOS NINJAS
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

    # evita loop
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

    # PRIMEIRO tenta colocar bandeiras direto no post
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            reply_markup=teclado_bandeiras(msg.message_id)
        )

        return

    # se falhar, usa fallback

    except Exception:
        pass


async def voltar_original(context, post_id):

    await asyncio.sleep(60)

    dados = POSTS_ORIGINAIS.get(post_id)

    if not dados:
        return

    chat_id = dados["chat_id"]
    texto_original = dados["texto"]
    tem_caption = dados["tem_caption"]
    entities = dados.get("entities")
    modo = dados["modo"]
    bot_message_id = dados.get("bot_message_id")

    try:

        if modo == "original":

            await editar_original(
                context,
                chat_id,
                post_id,
                texto_original,
                tem_caption,
                entities
            )

        else:

            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=bot_message_id,
                text=texto_menu(),
                reply_markup=teclado_bandeiras(post_id)
            )

    except Exception:
        pass


async def clicar_bandeira(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    try:
        await query.answer("Traduzindo por 2 minutos...")
    except Exception:
        pass

    try:

        _, pais, post_id = query.data.split(":")
        post_id = int(post_id)

        dados = POSTS_ORIGINAIS.get(post_id)

        if not dados:
            return

        chat_id = dados["chat_id"]
        texto_original = dados["texto"]
        tem_caption = dados["tem_caption"]
        modo = dados["modo"]
        bot_message_id = dados.get("bot_message_id")

        # 🇧🇷 = voltar imediatamente ao original
        if pais == "brasil":

            if post_id in TAREFAS_RETORNO:
                TAREFAS_RETORNO[post_id].cancel()

            if modo == "original":

                await editar_original(
                    context,
                    chat_id,
                    post_id,
                    texto_original,
                    tem_caption,
                    dados.get("entities")
                )

            else:

                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=bot_message_id,
                    text=texto_menu(),
                    reply_markup=teclado_bandeiras(post_id)
                )

            return

        idioma, nome_idioma = LANGS[pais]

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

        # tenta traduzir o post original
        if modo == "original":

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
                pass

        else:

            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=bot_message_id,
                text=f"{nome_idioma}\n\n{traducao}",
                reply_markup=teclado_bandeiras(post_id)
            )

        if post_id in TAREFAS_RETORNO:
            TAREFAS_RETORNO[post_id].cancel()

        TAREFAS_RETORNO[post_id] = asyncio.create_task(
            voltar_original(context, post_id)
        )

    except Exception:
        pass


def main():

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(
        MessageHandler(
            filters.ALL,
            novo_post
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            clicar_bandeira,
            pattern="^traduzir:"
        )
    )

    print("BOT DE TRADUÇÃO INICIADO")

    app.run_polling()


if __name__ == "__main__":
    main()