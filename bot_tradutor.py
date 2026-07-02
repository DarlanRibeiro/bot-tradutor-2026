import os
import json
import asyncio
import re
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

# Tempo para voltar ao texto original após uma tradução
TEMPO_RETORNO_SEGUNDOS = 30

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


def chave_post(chat_id, message_id):
    return (int(chat_id), int(message_id))


def teclado_bandeiras(message_id):
    return InlineKeyboardMarkup([[ 
        InlineKeyboardButton("🇨🇳", callback_data=f"traduzir:china:{message_id}"),
        InlineKeyboardButton("🇧🇷", callback_data=f"traduzir:brasil:{message_id}"),
        InlineKeyboardButton("🇪🇸", callback_data=f"traduzir:espanha:{message_id}"),
        InlineKeyboardButton("🇵🇹", callback_data=f"traduzir:portugal:{message_id}"),
        InlineKeyboardButton("🇺🇸", callback_data=f"traduzir:eua:{message_id}"),
    ]])


def entities_para_json(entities):
    if not entities:
        return None
    return json.dumps([e.to_dict() for e in entities], ensure_ascii=False)


def json_para_entities(valor, bot):
    if not valor:
        return None

    if isinstance(valor, str):
        valor = json.loads(valor)

    return [MessageEntity.de_json(e, bot) for e in valor]


async def iniciar_banco():
    global DB_POOL
    DB_POOL = await asyncpg.create_pool(DATABASE_URL)

    async with DB_POOL.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_posts (
                id BIGSERIAL PRIMARY KEY,
                message_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                texto TEXT NOT NULL,
                tem_caption BOOLEAN NOT NULL DEFAULT FALSE,
                entities JSONB,
                modo TEXT DEFAULT 'original',
                bot_message_id BIGINT,
                criado_em TIMESTAMPTZ DEFAULT NOW(),
                
                UNIQUE (chat_id, message_id)
            );
        """)


async def limpar_posts_antigos():
    if not DB_POOL:
        return

    async with DB_POOL.acquire() as conn:
        removidos = await conn.execute("""
            DELETE FROM telegram_posts
            WHERE criado_em < NOW() - INTERVAL '30 days'
        """)

    print(f"LIMPEZA BANCO: {removidos}")


async def salvar_post_banco(message_id, dados):
    """
    Salva ou atualiza o post original completo.
    Importante: nunca corta texto aqui.
    """
    if not DB_POOL:
        return

    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO telegram_posts (
                message_id, chat_id, texto, tem_caption, entities, modo, bot_message_id
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
            ON CONFLICT (chat_id, message_id)
            DO UPDATE SET
                texto = EXCLUDED.texto,
                tem_caption = EXCLUDED.tem_caption,
                entities = EXCLUDED.entities,
                modo = EXCLUDED.modo,
                bot_message_id = EXCLUDED.bot_message_id;
            """,
            int(message_id),
            int(dados["chat_id"]),
            dados["texto"],
            bool(dados["tem_caption"]),
            entities_para_json(dados.get("entities")),
            dados.get("modo", "original"),
            dados.get("bot_message_id")
        )


async def buscar_post_banco(chat_id, message_id, bot):
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
            int(message_id)
        )

    if not row:
        return None

    return {
        "chat_id": row["chat_id"],
        "texto": row["texto"],
        "tem_caption": row["tem_caption"],
        "entities": json_para_entities(row["entities"], bot),
        "modo": row["modo"] or "original",
        "bot_message_id": row["bot_message_id"]
    }


async def atualizar_modo_banco(chat_id, message_id, modo):
    if not DB_POOL:
        return

    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            UPDATE telegram_posts
            SET modo = $3
            WHERE chat_id = $1 AND message_id = $2
            """,
            int(chat_id),
            int(message_id),
            modo
        )


def dividir_texto_para_traducao(texto, limite=4500):
    """
    Divide apenas para enviar ao Google Translator em pedaços seguros.
    Depois junta tudo de volta. Não corta conteúdo.
    """
    if len(texto) <= limite:
        return [texto]

    partes = []
    atual = ""

    blocos = re.split(r"(\n\n+)", texto)
    for bloco in blocos:
        if len(atual) + len(bloco) <= limite:
            atual += bloco
        else:
            if atual:
                partes.append(atual)
                atual = ""

            if len(bloco) <= limite:
                atual = bloco
            else:
                # Último recurso: quebra por frases/palavras, sem perder conteúdo.
                palavras = bloco.split(" ")
                pedaco = ""
                for palavra in palavras:
                    extra = palavra if not pedaco else " " + palavra
                    if len(pedaco) + len(extra) <= limite:
                        pedaco += extra
                    else:
                        if pedaco:
                            partes.append(pedaco)
                        pedaco = palavra
                atual = pedaco

    if atual:
        partes.append(atual)

    return partes


def traduzir_texto_completo(texto, idioma):
    partes = dividir_texto_para_traducao(texto)
    traduzidas = []

    for parte in partes:
        if parte.strip():
            traduzidas.append(
                GoogleTranslator(source="auto", target=idioma).translate(parte)
            )
        else:
            traduzidas.append(parte)

    return "".join(traduzidas)


def ajustar_portugues_portugal(texto):
    # Ajuste simples, mantendo a lógica que já existia.
    trocas = {
        "você": "tu",
        "Você": "Tu",
        "vocês": "vós",
        "Vocês": "Vós",
    }
    for original, novo in trocas.items():
        texto = texto.replace(original, novo)
    return texto


async def editar_post(context, chat_id, message_id, texto, tem_caption, entities=None):
    """
    Edita o próprio post. Não faz truncamento.
    Se o Telegram aceitar o tamanho, publica inteiro.
    """
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


async def adicionar_botoes_no_post(msg):
    """
    Adiciona somente o rodapé de botões no próprio post.
    Não altera texto nem legenda.
    """
    try:
        await msg.edit_reply_markup(reply_markup=teclado_bandeiras(msg.message_id))
    except Exception as erro:
        print(f"ERRO AO ADICIONAR BOTÕES: {erro}")


def eh_mensagem_auxiliar_do_bot(msg):
    texto = msg.text or msg.caption or ""
    return (
        "🌐 Traduzir este post" in texto
        or texto.startswith("🇨🇳 Chinês")
        or texto.startswith("🇧🇷 Português Brasil")
        or texto.startswith("🇪🇸 Espanhol")
        or texto.startswith("🇵🇹 Português Portugal")
        or texto.startswith("🇺🇸 Inglês")
    )


async def novo_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message

    if not msg:
        return

    # No grupo vinculado, remove mensagens auxiliares antigas do bot, se existirem.
    if msg.chat_id == CASA_DOS_NINJAS_ID:
        if eh_mensagem_auxiliar_do_bot(msg):
            try:
                await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
            except Exception:
                pass
        return

    texto = msg.text or msg.caption
    if not texto:
        return

    chave = chave_post(msg.chat_id, msg.message_id)
    dados_existentes = POSTS_ORIGINAIS.get(chave)

    # Se o post estiver em tradução, não salva a tradução como se fosse original.
    if dados_existentes and dados_existentes.get("modo") != "original":
        return

    tem_caption = bool(msg.caption)
    entities = msg.caption_entities if tem_caption else msg.entities

    dados = {
        "chat_id": msg.chat_id,
        "texto": texto,
        "tem_caption": tem_caption,
        "entities": entities,
        "modo": "original",
        "bot_message_id": None
    }

    POSTS_ORIGINAIS[chave] = dados
    await salvar_post_banco(msg.message_id, dados)
    await adicionar_botoes_no_post(msg)


async def voltar_original(context, chat_id, post_id):
    await asyncio.sleep(TEMPO_RETORNO_SEGUNDOS)

    chave = chave_post(chat_id, post_id)
    dados = POSTS_ORIGINAIS.get(chave)

    if not dados:
        dados = await buscar_post_banco(chat_id, post_id, context.bot)
        if dados:
            POSTS_ORIGINAIS[chave] = dados

    if not dados:
        return

    try:
        await editar_post(
            context,
            dados["chat_id"],
            post_id,
            dados["texto"],
            dados["tem_caption"],
            dados.get("entities")
        )
        dados["modo"] = "original"
        await atualizar_modo_banco(chat_id, post_id, "original")
    except Exception as erro:
        print(f"ERRO AO VOLTAR ORIGINAL: {erro}")
    finally:
        if chave in TAREFAS_RETORNO:
            del TAREFAS_RETORNO[chave]


async def clicar_bandeira(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    try:
        _, pais, post_id = query.data.split(":")
        post_id = int(post_id)

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

        if pais == "brasil":
            if chave in TAREFAS_RETORNO:
                TAREFAS_RETORNO[chave].cancel()
                del TAREFAS_RETORNO[chave]

            await editar_post(
                context,
                chat_id,
                post_id,
                texto_original,
                tem_caption,
                entities
            )
            dados["modo"] = "original"
            await atualizar_modo_banco(chat_id, post_id, "original")
            return

        idioma, _ = LANGS[pais]

        traducao = traduzir_texto_completo(texto_original, idioma)

        if pais == "portugal":
            traducao = ajustar_portugues_portugal(traducao)

        # Regra principal: nunca cortar a tradução.
        dados["modo"] = pais
        await atualizar_modo_banco(chat_id, post_id, pais)

        await editar_post(
            context,
            chat_id,
            post_id,
            traducao,
            tem_caption,
            None
        )

        if chave in TAREFAS_RETORNO:
            TAREFAS_RETORNO[chave].cancel()
            del TAREFAS_RETORNO[chave]

        TAREFAS_RETORNO[chave] = asyncio.create_task(
            voltar_original(context, chat_id, post_id)
        )

    except Exception as erro:
        print(f"ERRO AO TRADUZIR: {erro}")


async def post_init(app):
    await iniciar_banco()
    await limpar_posts_antigos()


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(MessageHandler(filters.ALL, novo_post))
    app.add_handler(CallbackQueryHandler(clicar_bandeira, pattern="^traduzir:"))

    print("BOT DE TRADUÇÃO INICIADO")
    app.run_polling()


if __name__ == "__main__":
    main()
