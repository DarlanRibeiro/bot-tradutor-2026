import os
import asyncio
from dotenv import load_dotenv
from deep_translator import GoogleTranslator

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN não encontrado. Configure a variável no Railway.")

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
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇨🇳", callback_data=f"traduzir:china:{message_id}"),
            InlineKeyboardButton("🇧🇷", callback_data=f"traduzir:brasil:{message_id}"),
            InlineKeyboardButton("🇪🇸", callback_data=f"traduzir:espanha:{message_id}"),
            InlineKeyboardButton("🇵🇹", callback_data=f"traduzir:portugal:{message_id}"),
            InlineKeyboardButton("🇺🇸", callback_data=f"traduzir:eua:{message_id}"),
        ]
    ])


async def novo_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post

    if not msg:
        return

    texto = msg.text or msg.caption

    if not texto:
        return

    POSTS_ORIGINAIS[msg.message_id] = {
        "chat_id": msg.chat_id,
        "texto": texto
    }

    try:
        await context.bot.edit_message_reply_markup(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            reply_markup=teclado_bandeiras(msg.message_id)
        )
        print(f"Botões adicionados ao post {msg.message_id}")

    except Exception as e:
        print(f"Erro ao adicionar botões: {e}")


async def voltar_original(context, chat_id, message_id, texto_original):
    await asyncio.sleep(120)

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=texto_original,
            reply_markup=teclado_bandeiras(message_id)
        )
        print(f"Post {message_id} voltou ao original")

    except Exception as e:
        print(f"Erro ao voltar original: {e}")


async def clicar_bandeira(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Traduzindo por 1 minuto...")

    try:
        _, pais, message_id = query.data.split(":")
        message_id = int(message_id)

        dados_post = POSTS_ORIGINAIS.get(message_id)

        if not dados_post:
            await query.answer("Texto original não encontrado.", show_alert=True)
            return

        chat_id = dados_post["chat_id"]
        texto_original = dados_post["texto"]

        idioma, nome_idioma = LANGS[pais]

        traducao = GoogleTranslator(
            source="auto",
            target=idioma
        ).translate(texto_original)

        if pais == "portugal":
            traducao = traducao.replace("você", "tu").replace("Você", "Tu")

        if len(traducao) > 4000:
            traducao = traducao[:4000]

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=traducao,
            reply_markup=teclado_bandeiras(message_id)
        )

        if message_id in TAREFAS_RETORNO:
            TAREFAS_RETORNO[message_id].cancel()

        TAREFAS_RETORNO[message_id] = asyncio.create_task(
            voltar_original(context, chat_id, message_id, texto_original)
        )

        print(f"Post {message_id} traduzido para {nome_idioma}")

    except Exception as e:
        print(f"Erro na tradução: {e}")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(
        MessageHandler(
            filters.ChatType.CHANNEL & filters.TEXT,
            novo_post
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            clicar_bandeira,
            pattern="^traduzir:"
        )
    )

    print("BOT DE TRADUÇÃO iNICIADO")
    app.run_polling()


if __name__ == "__main__":
    main()