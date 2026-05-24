import os
import asyncio

from dotenv import load_dotenv
from deep_translator import GoogleTranslator

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)

from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# =========================================
# CARREGA TOKEN
# =========================================

load_dotenv()

BOT_TOKEN = os.getenv("8890493813:AAHeg0jkn-RMop069dFrXiu5rhT8WYUk5m0")

# =========================================
# IDIOMAS
# =========================================

LANGS = {
    "china": ("zh-CN", "🇨🇳 Chinês"),
    "brasil": ("pt", "🇧🇷 Português Brasil"),
    "espanha": ("es", "🇪🇸 Espanhol"),
    "portugal": ("pt-PT", "🇵🇹 Português Portugal"),
    "eua": ("en", "🇺🇸 Inglês"),
}

# =========================================
# ARMAZENAMENTO DOS POSTS
# =========================================

POSTS_ORIGINAIS = {}

# =========================================
# CRIA TECLADO COM BANDEIRAS
# =========================================

def teclado_bandeiras(message_id):

    keyboard = [
        [
            InlineKeyboardButton(
                "🇨🇳",
                callback_data=f"traduzir:china:{message_id}"
            ),

            InlineKeyboardButton(
                "🇧🇷",
                callback_data=f"traduzir:brasil:{message_id}"
            ),

            InlineKeyboardButton(
                "🇪🇸",
                callback_data=f"traduzir:espanha:{message_id}"
            ),

            InlineKeyboardButton(
                "🇵🇹",
                callback_data=f"traduzir:portugal:{message_id}"
            ),

            InlineKeyboardButton(
                "🇺🇸",
                callback_data=f"traduzir:eua:{message_id}"
            ),
        ]
    ]

    return InlineKeyboardMarkup(keyboard)

# =========================================
# NOVO POST NO CANAL
# =========================================

async def novo_post(update: Update, context: ContextTypes.DEFAULT_TYPE):

    msg = update.channel_post

    if not msg:
        return

    if not msg.text:
        return

    # Salva o texto original
    POSTS_ORIGINAIS[msg.message_id] = msg.text

    try:

        # Adiciona as bandeiras no post
        await context.bot.edit_message_reply_markup(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            reply_markup=teclado_bandeiras(msg.message_id)
        )

        print(f"Botões adicionados ao post {msg.message_id}")

    except Exception as e:
        print(f"Erro ao adicionar botões: {e}")

# =========================================
# CLIQUE NA BANDEIRA
# =========================================

async def clicar_bandeira(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer("Traduzindo...")

    try:

        # callback_data:
        # traduzir:eua:123

        _, pais, message_id = query.data.split(":")

        message_id = int(message_id)

        texto_original = POSTS_ORIGINAIS.get(message_id)

        if not texto_original:

            await query.answer(
                "Texto original não encontrado.",
                show_alert=True
            )

            return

        idioma, nome_idioma = LANGS[pais]

        # Traduz
        traducao = GoogleTranslator(
            source="auto",
            target=idioma
        ).translate(texto_original)

        # Limite de segurança
        if len(traducao) > 4000:
            traducao = traducao[:4000]

        # Edita o post para o idioma escolhido
        await query.edit_message_text(
            text=traducao,
            reply_markup=teclado_bandeiras(message_id)
        )

        print(f"Post {message_id} traduzido para {nome_idioma}")

        # Espera 60 segundos
        await asyncio.sleep(60)

        # Volta para o original
        await query.edit_message_text(
            text=texto_original,
            reply_markup=teclado_bandeiras(message_id)
        )

        print(f"Post {message_id} voltou ao original")

    except Exception as e:
        print(f"Erro na tradução: {e}")

# =========================================
# MAIN
# =========================================

def main():

    app = Application.builder().token(BOT_TOKEN).build()

    # Detecta novos posts no canal
    app.add_handler(
        MessageHandler(
            filters.ChatType.CHANNEL & filters.TEXT,
            novo_post
        )
    )

    # Detecta clique nas bandeiras
    app.add_handler(
        CallbackQueryHandler(
            clicar_bandeira,
            pattern="^traduzir:"
        )
    )

    print("BOT DE TRADUÇÃO INICIADO")

    app.run_polling()

# =========================================

if __name__ == "__main__":
    main()