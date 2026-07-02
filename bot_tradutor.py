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
TEMPO_RETORNO_SEGUNDOS = 30

# TESTE_DIAGNOSTICO = "0" -> traduz normalmente, mas com logs completos.
# TESTE_DIAGNOSTICO = "1" -> NÃO traduz; tenta regravar o texto original + " TESTE".
# Esse modo serve para provar se a Bot API consegue editar captions grandes.
TESTE_DIAGNOSTICO = os.getenv("TESTE_DIAGNOSTICO", "0") == "1"

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


def log(msg):
    print(msg, flush=True)


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

    # Compatível com a tabela atual do Neon. Não usa atualizado_em.
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
    log(f"LIMPEZA BANCO: {removidos}")


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
            dados.get("bot_message_id"),
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
            int(message_id),
        )

    if not row:
        return None

    return {
        "chat_id": row["chat_id"],
        "texto": row["texto"],
        "tem_caption": row["tem_caption"],
        "entities": json_para_entities(row["entities"], bot),
        "modo": row["modo"] or "original",
        "bot_message_id": row["bot_message_id"],
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
            modo,
        )


def dividir_texto_para_traducao(texto, limite=4500):
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
    log(f"PARTES PARA GOOGLE TRANSLATOR: {len(partes)}")

    traduzidas = []
    for i, parte in enumerate(partes, start=1):
        log(f"TRADUZINDO PARTE {i}/{len(partes)} | tamanho={len(parte)}")
        if parte.strip():
            traduzidas.append(GoogleTranslator(source="auto", target=idioma).translate(parte))
        else:
            traduzidas.append(parte)

    return "".join(traduzidas)


def ajustar_portugues_portugal(texto):
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
    log("TENTANDO EDITAR O POST...")
    log(f"TIPO: {'CAPTION/MÍDIA' if tem_caption else 'TEXTO'}")
    log(f"TAMANHO ENVIADO AO TELEGRAM: {len(texto)}")

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


async def adicionar_botoes_no_post(msg):
    try:
        log("ADICIONANDO BOTÕES NO POST...")
        log(f"CHAT_ID: {msg.chat_id} | MESSAGE_ID: {msg.message_id}")
        log(f"TEM TEXTO: {bool(msg.text)} | TEM CAPTION: {bool(msg.caption)}")
        log(f"TAMANHO TEXTO/CAPTION: {len(msg.text or msg.caption or '')}")
        await msg.edit_reply_markup(reply_markup=teclado_bandeiras(msg.message_id))
        log("BOTÕES ADICIONADOS COM SUCESSO")
    except Exception as erro:
        log("=" * 80)
        log("ERRO AO ADICIONAR BOTÕES")
        log(f"TIPO ERRO: {type(erro).__name__}")
        log(f"ERRO: {erro}")
        log("=" * 80)


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

    log("-" * 80)
    log("UPDATE RECEBIDO")
    log(f"CHAT_ID: {msg.chat_id} | MESSAGE_ID: {msg.message_id}")
    log(f"TIPO CHAT: {msg.chat.type if msg.chat else 'desconhecido'}")
    log(f"TEM TEXTO: {bool(msg.text)} | TEM CAPTION: {bool(msg.caption)}")
    log(f"EDIT_DATE: {msg.edit_date}")

    if msg.chat_id == CASA_DOS_NINJAS_ID:
        if eh_mensagem_auxiliar_do_bot(msg):
            try:
                await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
            except Exception as erro:
                log(f"ERRO AO APAGAR AUXILIAR: {erro}")
        return

    texto = msg.text or msg.caption
    if not texto:
        log("IGNORADO: sem texto/caption")
        return

    chave = chave_post(msg.chat_id, msg.message_id)
    dados_existentes = POSTS_ORIGINAIS.get(chave)

    if dados_existentes and dados_existentes.get("modo") != "original":
        log("IGNORADO: post está em modo tradução")
        return

    tem_caption = bool(msg.caption)
    entities = msg.caption_entities if tem_caption else msg.entities

    dados = {
        "chat_id": msg.chat_id,
        "texto": texto,
        "tem_caption": tem_caption,
        "entities": entities,
        "modo": "original",
        "bot_message_id": None,
    }

    log(f"SALVANDO ORIGINAL | tamanho={len(texto)} | tem_caption={tem_caption}")
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
        log("VOLTAR ORIGINAL: dados não encontrados")
        return

    try:
        log("=" * 80)
        log("VOLTANDO AO ORIGINAL")
        log(f"POST: {post_id} | tamanho original={len(dados['texto'])}")
        await editar_post(
            context,
            dados["chat_id"],
            post_id,
            dados["texto"],
            dados["tem_caption"],
            dados.get("entities"),
        )
        dados["modo"] = "original"
        await atualizar_modo_banco(chat_id, post_id, "original")
        log("ORIGINAL RESTAURADO COM SUCESSO")
    except Exception as erro:
        log("=" * 80)
        log("ERRO AO VOLTAR ORIGINAL")
        log(f"TIPO ERRO: {type(erro).__name__}")
        log(f"ERRO: {erro}")
        log("=" * 80)
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
            log("CLIQUE IGNORADO: sem query.message")
            return

        chat_id = query.message.chat_id
        chave = chave_post(chat_id, post_id)

        dados = POSTS_ORIGINAIS.get(chave)
        if not dados:
            dados = await buscar_post_banco(chat_id, post_id, context.bot)
            if dados:
                POSTS_ORIGINAIS[chave] = dados

        if not dados:
            log("CLIQUE IGNORADO: dados do post não encontrados")
            return

        texto_original = dados["texto"]
        tem_caption = dados["tem_caption"]
        entities = dados.get("entities")

        log("=" * 80)
        log("CLIQUE EM BANDEIRA")
        log(f"POST: {post_id}")
        log(f"IDIOMA: {pais} | {LANGS.get(pais, ('?', '?'))[1]}")
        log(f"TEM CAPTION: {tem_caption}")
        log(f"TAMANHO ORIGINAL: {len(texto_original)}")
        log(f"QTD ENTITIES: {len(entities) if entities else 0}")
        log(f"TESTE_DIAGNOSTICO: {TESTE_DIAGNOSTICO}")

        if pais == "brasil":
            if chave in TAREFAS_RETORNO:
                TAREFAS_RETORNO[chave].cancel()
                del TAREFAS_RETORNO[chave]

            await editar_post(context, chat_id, post_id, texto_original, tem_caption, entities)
            dados["modo"] = "original"
            await atualizar_modo_banco(chat_id, post_id, "original")
            log("RETORNOU AO ORIGINAL VIA 🇧🇷")
            return

        idioma, idioma_nome = LANGS[pais]

        if TESTE_DIAGNOSTICO:
            traducao = texto_original + " TESTE"
            log("MODO TESTE: usando texto original + ' TESTE', sem Google Translator")
        else:
            traducao = traduzir_texto_completo(texto_original, idioma)
            if pais == "portugal":
                traducao = ajustar_portugues_portugal(traducao)

        log(f"TAMANHO TRADUÇÃO/TESTE ({idioma_nome}): {len(traducao)}")
        log(f"DIFERENÇA: {len(traducao) - len(texto_original)} caracteres")

        dados["modo"] = pais
        await atualizar_modo_banco(chat_id, post_id, pais)

        try:
            await editar_post(context, chat_id, post_id, traducao, tem_caption, None)
            log("POST EDITADO COM SUCESSO")
        except Exception as erro:
            log("=" * 80)
            log("ERRO AO EDITAR/TRADUZIR")
            log(f"TIPO ERRO: {type(erro).__name__}")
            log(f"ERRO: {erro}")
            log(f"TAMANHO ORIGINAL: {len(texto_original)}")
            log(f"TAMANHO TRADUÇÃO/TESTE: {len(traducao)}")
            log(f"TEM CAPTION: {tem_caption}")
            log("=" * 80)
            dados["modo"] = "original"
            await atualizar_modo_banco(chat_id, post_id, "original")
            return

        if chave in TAREFAS_RETORNO:
            TAREFAS_RETORNO[chave].cancel()
            del TAREFAS_RETORNO[chave]

        TAREFAS_RETORNO[chave] = asyncio.create_task(voltar_original(context, chat_id, post_id))

    except Exception as erro:
        log("=" * 80)
        log("ERRO GERAL AO TRADUZIR")
        log(f"TIPO ERRO: {type(erro).__name__}")
        log(f"ERRO: {erro}")
        log("=" * 80)


async def erro_global(update, context):
    log("=" * 80)
    log("ERRO GLOBAL CAPTURADO")
    log(f"ERRO: {context.error}")
    log("=" * 80)


async def post_init(app):
    await iniciar_banco()
    await limpar_posts_antigos()


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(MessageHandler(filters.ALL, novo_post))
    app.add_handler(CallbackQueryHandler(clicar_bandeira, pattern="^traduzir:"))
    app.add_error_handler(erro_global)

    log("BOT DE TRADUÇÃO INICIADO")
    log(f"TESTE_DIAGNOSTICO={'ATIVADO' if TESTE_DIAGNOSTICO else 'DESATIVADO'}")
    app.run_polling()


if __name__ == "__main__":
    main()
