import os
import json
import asyncio
import logging
from collections import defaultdict

import httpx
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("media-bot")

# ── Configuración ──────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

RADARR_URL = os.environ.get("RADARR_URL", "http://radarr:7878").rstrip("/")
RADARR_API_KEY = os.environ["RADARR_API_KEY"]
SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989").rstrip("/")
SONARR_API_KEY = os.environ["SONARR_API_KEY"]

# Proxy SOCKS (Warp). Solo se aplica al tráfico hacia Google/Gemini.
WARP_PROXY = os.environ.get("WARP_PROXY", "socks5://warp:1080")
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))

# Cliente Gemini: async, con todo el tráfico saliendo por el proxy Warp.
# Requiere httpx[socks] para el esquema socks5://.
gemini = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(
        client_args={"proxy": WARP_PROXY},
        async_client_args={"proxy": WARP_PROXY},
        timeout=int(HTTP_TIMEOUT * 1000),  # ms
    ),
)

# Cliente HTTP async para Radarr/Sonarr (red local, sin proxy).
arr = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

RADARR_HEADERS = {"X-Api-Key": RADARR_API_KEY}
SONARR_HEADERS = {"X-Api-Key": SONARR_API_KEY}


# ── Funciones de Radarr / Sonarr (async) ───────────────────

async def search_movie(query: str) -> list[dict]:
    """Busca películas en Radarr vía TMDB."""
    resp = await arr.get(
        f"{RADARR_URL}/api/v3/movie/lookup",
        params={"term": query},
        headers=RADARR_HEADERS,
    )
    resp.raise_for_status()
    results = resp.json()[:5]
    return [
        {
            "title": m.get("title"),
            "year": m.get("year"),
            "tmdbId": m.get("tmdbId"),
            "overview": (m.get("overview") or "")[:200],
        }
        for m in results
    ]


async def add_movie(tmdb_id: int, quality_profile_id: int = 1) -> str:
    """Agrega una película a Radarr para descarga."""
    lookup = await arr.get(
        f"{RADARR_URL}/api/v3/movie/lookup/tmdb",
        params={"tmdbId": tmdb_id},
        headers=RADARR_HEADERS,
    )
    lookup.raise_for_status()
    movie_data = lookup.json()
    if not movie_data:
        return "No encontré datos para ese TMDB ID en Radarr."

    folders_resp = await arr.get(
        f"{RADARR_URL}/api/v3/rootfolder",
        headers=RADARR_HEADERS,
    )
    folders_resp.raise_for_status()
    folders = folders_resp.json()
    if not folders:
        return "Radarr no tiene ninguna carpeta raíz configurada. Configurala primero."
    root_path = folders[0]["path"]

    payload = {
        "title": movie_data["title"],
        "tmdbId": tmdb_id,
        "year": movie_data.get("year"),
        "qualityProfileId": quality_profile_id,
        "rootFolderPath": root_path,
        "monitored": True,
        "addOptions": {"searchForMovie": True},
    }

    resp = await arr.post(
        f"{RADARR_URL}/api/v3/movie",
        json=payload,
        headers=RADARR_HEADERS,
    )
    if resp.status_code == 400 and "already been added" in resp.text.lower():
        return f"'{movie_data['title']}' ya está en tu biblioteca."
    resp.raise_for_status()
    return f"'{movie_data['title']}' agregada. Radarr buscará y descargará automáticamente."


async def search_series(query: str) -> list[dict]:
    """Busca series en Sonarr vía TVDB."""
    resp = await arr.get(
        f"{SONARR_URL}/api/v3/series/lookup",
        params={"term": query},
        headers=SONARR_HEADERS,
    )
    resp.raise_for_status()
    results = resp.json()[:5]
    return [
        {
            "title": s.get("title"),
            "year": s.get("year"),
            "tvdbId": s.get("tvdbId"),
            "overview": (s.get("overview") or "")[:200],
        }
        for s in results
    ]


async def add_series(tvdb_id: int, quality_profile_id: int = 1) -> str:
    """Agrega una serie a Sonarr para descarga."""
    lookup = await arr.get(
        f"{SONARR_URL}/api/v3/series/lookup",
        params={"term": f"tvdb:{tvdb_id}"},
        headers=SONARR_HEADERS,
    )
    lookup.raise_for_status()
    data = lookup.json()
    if not data:
        return "No encontré esa serie en Sonarr con ese TVDB ID."
    series_data = data[0]

    folders_resp = await arr.get(
        f"{SONARR_URL}/api/v3/rootfolder",
        headers=SONARR_HEADERS,
    )
    folders_resp.raise_for_status()
    folders = folders_resp.json()
    if not folders:
        return "Sonarr no tiene ninguna carpeta raíz configurada. Configurala primero."
    root_path = folders[0]["path"]

    payload = {
        "title": series_data["title"],
        "tvdbId": tvdb_id,
        "qualityProfileId": quality_profile_id,
        "rootFolderPath": root_path,
        "monitored": True,
        "seasonFolder": True,
        "addOptions": {"searchForMissingEpisodes": True},
    }

    resp = await arr.post(
        f"{SONARR_URL}/api/v3/series",
        json=payload,
        headers=SONARR_HEADERS,
    )
    if resp.status_code == 400 and "already been added" in resp.text.lower():
        return f"'{series_data['title']}' ya está en tu biblioteca."
    resp.raise_for_status()
    return f"'{series_data['title']}' agregada. Sonarr buscará y descargará automáticamente."


async def search_episode(tvdb_id: int, season: int, episode: int) -> dict:
    """Busca un episodio específico de una serie que ya está en Sonarr."""
    resp = await arr.get(
        f"{SONARR_URL}/api/v3/series",
        headers=SONARR_HEADERS,
    )
    resp.raise_for_status()
    series_list = resp.json()

    series_id = None
    series_title = None
    for s in series_list:
        if s.get("tvdbId") == tvdb_id:
            series_id = s["id"]
            series_title = s["title"]
            break

    if not series_id:
        return {"error": "La serie no está en Sonarr. Primero agregala con add_series."}

    resp = await arr.get(
        f"{SONARR_URL}/api/v3/episode",
        params={"seriesId": series_id},
        headers=SONARR_HEADERS,
    )
    resp.raise_for_status()
    episodes = resp.json()

    for ep in episodes:
        if ep["seasonNumber"] == season and ep["episodeNumber"] == episode:
            return {
                "series": series_title,
                "season": season,
                "episode": episode,
                "title": ep.get("title", "Sin título"),
                "episodeId": ep["id"],
                "hasFile": ep.get("hasFile", False),
                "monitored": ep.get("monitored", False),
            }

    return {"error": f"No se encontró S{season:02d}E{episode:02d} de {series_title}."}


async def download_episode(episode_id: int) -> str:
    """Fuerza la búsqueda y descarga de un episodio específico."""
    ep_resp = await arr.get(
        f"{SONARR_URL}/api/v3/episode/{episode_id}",
        headers=SONARR_HEADERS,
    )
    ep_resp.raise_for_status()
    ep_data = ep_resp.json()

    if ep_data.get("hasFile"):
        return (
            f"'{ep_data.get('title', '')}' "
            f"(S{ep_data['seasonNumber']:02d}E{ep_data['episodeNumber']:02d}) ya está descargado."
        )

    ep_data["monitored"] = True
    put_resp = await arr.put(
        f"{SONARR_URL}/api/v3/episode/{episode_id}",
        json=ep_data,
        headers=SONARR_HEADERS,
    )
    put_resp.raise_for_status()

    command = {"name": "EpisodeSearch", "episodeIds": [episode_id]}
    resp = await arr.post(
        f"{SONARR_URL}/api/v3/command",
        json=command,
        headers=SONARR_HEADERS,
    )
    resp.raise_for_status()
    return (
        f"Buscando descarga para S{ep_data['seasonNumber']:02d}E{ep_data['episodeNumber']:02d} "
        f"'{ep_data.get('title', '')}'. Sonarr te notificará cuando esté listo."
    )


# ── Declaración de tools para Gemini (function calling) ────
# Usamos types.Schema con enums types.Type (formato canónico y el más
# compatible entre versiones del SDK) en lugar de parameters_json_schema.

FUNCTION_DECLARATIONS = [
    types.FunctionDeclaration(
        name="search_movie",
        description="Busca películas por nombre. Usar cuando el usuario quiere una película.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(
                    type=types.Type.STRING,
                    description="Nombre de la película a buscar",
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="add_movie",
        description="Agrega una película para descargar. Usar después de confirmar cuál quiere el usuario.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "tmdb_id": types.Schema(
                    type=types.Type.INTEGER,
                    description="TMDB ID de la película",
                ),
            },
            required=["tmdb_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_series",
        description="Busca series de TV por nombre.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(
                    type=types.Type.STRING,
                    description="Nombre de la serie a buscar",
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="add_series",
        description="Agrega una serie completa para descargar.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "tvdb_id": types.Schema(
                    type=types.Type.INTEGER,
                    description="TVDB ID de la serie",
                ),
            },
            required=["tvdb_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_episode",
        description=(
            "Busca un episodio específico de una serie que ya está en Sonarr. "
            "Usar cuando el usuario pide un capítulo puntual."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "tvdb_id": types.Schema(
                    type=types.Type.INTEGER, description="TVDB ID de la serie"
                ),
                "season": types.Schema(
                    type=types.Type.INTEGER, description="Número de temporada"
                ),
                "episode": types.Schema(
                    type=types.Type.INTEGER, description="Número de episodio"
                ),
            },
            required=["tvdb_id", "season", "episode"],
        ),
    ),
    types.FunctionDeclaration(
        name="download_episode",
        description="Descarga un episodio específico. Usar después de search_episode cuando el usuario confirme.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "episode_id": types.Schema(
                    type=types.Type.INTEGER,
                    description="ID del episodio en Sonarr (obtenido de search_episode)",
                ),
            },
            required=["episode_id"],
        ),
    ),
]

# El objeto Tool que agrupa todas las function declarations. Se inyecta en
# CADA petición a Gemini (dentro de config); sin esto el modelo no sabe que
# tiene herramientas y responde como un chatbot común.
MEDIA_TOOL = types.Tool(function_declarations=FUNCTION_DECLARATIONS)

SYSTEM_PROMPT = """Sos un asistente de media server. Ayudás al usuario a buscar y descargar
películas y series.

Flujo para películas:
1. El usuario pide algo (ej: "quiero ver Oppenheimer")
2. Usá search_movie para buscar
3. Mostrá los resultados y preguntá cuál quiere
4. Cuando confirme, usá add_movie

Flujo para series completas:
1. Usá search_series para buscar
2. Mostrá resultados y confirmá
3. Usá add_series para agregar toda la serie

Flujo para episodios específicos:
1. Si el usuario pide un capítulo puntual (ej: "S02E05 de Breaking Bad")
2. Primero buscá la serie con search_series
3. Si la serie no está agregada, usá add_series primero
4. Después usá search_episode con el tvdb_id, temporada y episodio
5. Confirmá con el usuario y usá download_episode

Reglas:
- Respondé siempre en español
- Sé conciso, esto es un chat de Telegram
- Si no estás seguro si es serie o película, preguntá
- Mostrá máximo 3-5 resultados para elegir
- Usá emojis para que sea amigable (🎬 películas, 📺 series, ✅ confirmado, etc.)
- Todo se descarga en 1080p automáticamente
"""

def build_config() -> types.GenerateContentConfig:
    """
    Construye un GenerateContentConfig nuevo por cada petición.

    Se crea fresco (en vez de reutilizar un singleton) para evitar cualquier
    mutación interna del SDK compartida entre peticiones concurrentes. Lo clave
    es que SIEMPRE incluye `tools=[MEDIA_TOOL]`: así el esquema de herramientas
    viaja en cada llamada a generate_content y Gemini emite function_call.
    """
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[MEDIA_TOOL],
        temperature=0.3,
        # Modo AUTO: el modelo decide cuándo llamar una tool (recomendado para
        # un flujo conversacional donde a veces solo hay que responder texto).
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="AUTO")
        ),
        # No pasamos callables de Python, así que Gemini nunca ejecuta nada por
        # su cuenta: siempre devuelve function_calls y el bot controla la ejecución.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


# ── Dispatch de tools ──────────────────────────────────────

async def dispatch_tool(name: str, args: dict):
    """Ejecuta la tool solicitada y devuelve un dict/list serializable."""
    try:
        if name == "search_movie":
            return await search_movie(args["query"])
        if name == "add_movie":
            return await add_movie(int(args["tmdb_id"]))
        if name == "search_series":
            return await search_series(args["query"])
        if name == "add_series":
            return await add_series(int(args["tvdb_id"]))
        if name == "search_episode":
            return await search_episode(
                int(args["tvdb_id"]), int(args["season"]), int(args["episode"])
            )
        if name == "download_episode":
            return await download_episode(int(args["episode_id"]))
        return {"error": f"Tool desconocido: {name}"}
    except httpx.HTTPStatusError as e:
        logger.error("HTTP %s en %s: %s", e.response.status_code, name, e)
        return {"error": f"El servicio respondió {e.response.status_code}."}
    except httpx.RequestError as e:
        # Timeouts, conexión rechazada, servicio caído, etc.
        logger.error("Fallo de red en %s: %s", name, e)
        return {"error": "No pude contactar el servicio (timeout o caído). Intentá más tarde."}
    except (KeyError, IndexError, ValueError) as e:
        logger.error("Datos inesperados en %s: %s", name, e)
        return {"error": f"Datos inválidos para {name}: {e}"}


def _wrap_response(result) -> dict:
    """Gemini exige que la respuesta de una tool sea un objeto JSON (dict)."""
    if isinstance(result, dict):
        return result
    return {"result": result}


# ── Historial de conversación por chat ─────────────────────
# Guardamos objetos types.Content (formato nativo del SDK de Gemini).
conversations: dict[int, list[types.Content]] = {}
chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
MAX_HISTORY = 20


def get_history(chat_id: int) -> list[types.Content]:
    return conversations.setdefault(chat_id, [])


def _is_user_text(content: types.Content) -> bool:
    """True si es un turno de usuario de texto (no una respuesta de tool)."""
    if getattr(content, "role", None) != "user":
        return False
    for part in content.parts or []:
        if getattr(part, "function_response", None) is not None:
            return False
    return True


def _trim_history(history: list[types.Content]) -> None:
    """
    Recorta el historial sin corromperlo: nunca lo deja empezando en un turno
    del modelo ni en una respuesta de tool huérfana (sin su function_call).
    Gemini requiere que la conversación arranque con un turno de usuario.
    """
    if len(history) > MAX_HISTORY:
        del history[:-MAX_HISTORY]
    while history and not _is_user_text(history[0]):
        history.pop(0)


# ── Procesar con Gemini ────────────────────────────────────

async def process_with_gemini(chat_id: int, user_message: str) -> str:
    history = get_history(chat_id)
    history.append(
        types.Content(role="user", parts=[types.Part.from_text(text=user_message)])
    )
    _trim_history(history)

    config = build_config()
    response = await gemini.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=history,
        config=config,
    )

    # Loop de function calling controlado por nosotros.
    guard = 0
    while response.function_calls and guard < 10:
        guard += 1

        # Turno del modelo (contiene los function_call): lo guardamos tal cual.
        model_content = response.candidates[0].content
        history.append(model_content)

        tool_parts = []
        for fc in response.function_calls:
            args = dict(fc.args or {})
            logger.info("Tool call: %s(%s)", fc.name, args)
            result = await dispatch_tool(fc.name, args)
            tool_parts.append(
                types.Part.from_function_response(
                    name=fc.name,
                    response=_wrap_response(result),
                )
            )

        history.append(types.Content(role="tool", parts=tool_parts))

        response = await gemini.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=history,
            config=config,
        )

    final_text = (response.text or "").strip()
    if response.candidates and response.candidates[0].content:
        history.append(response.candidates[0].content)
    _trim_history(history)

    return final_text or "No obtuve una respuesta. Probá de nuevo. 🤔"


# ── Handlers de Telegram ──────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 ¡Hola! Soy tu asistente de media.\n\n"
        "Pedime cualquier película o serie y la busco y pongo a descargar.\n\n"
        "Ejemplos:\n"
        '• "Quiero ver Oppenheimer"\n'
        '• "Descargá Breaking Bad"\n'
        '• "Buscá la última de Nolan"'
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_msg = update.message.text

    await update.message.chat.send_action("typing")

    # Un lock por chat: distintos chats corren en paralelo (concurrent_updates),
    # pero los mensajes de un mismo chat se serializan para no corromper su historial.
    lock = chat_locks[chat_id]
    async with lock:
        try:
            reply = await process_with_gemini(chat_id, user_msg)
            await update.message.reply_text(reply)
        except Exception as e:
            logger.exception("Error procesando mensaje de %s: %s", chat_id, e)
            await update.message.reply_text(
                "❌ Hubo un error procesando tu mensaje. Intentá de nuevo."
            )


async def _on_shutdown(app) -> None:
    await arr.aclose()
    logger.info("Cliente HTTP cerrado.")


# ── Main ──────────────────────────────────────────────────

def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)  # procesa varios mensajes a la vez
        .post_shutdown(_on_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot iniciado con modelo %s", GEMINI_MODEL)
    app.run_polling()


if __name__ == "__main__":
    main()
