import os
import json
import logging
import httpx
import anthropic
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Configuración ──────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
RADARR_URL = os.environ.get("RADARR_URL", "http://radarr:7878")
RADARR_API_KEY = os.environ["RADARR_API_KEY"]
SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989")
SONARR_API_KEY = os.environ["SONARR_API_KEY"]

client = anthropic.Anthropic(
    http_client=httpx.Client(
        proxy="socks5://warp:1080",
        timeout=30,
    )
)
http = httpx.Client(timeout=30)

# ── Funciones de Radarr / Sonarr ───────────────────────────

def search_movie(query: str) -> list[dict]:
    """Busca películas en Radarr vía TMDB."""
    resp = http.get(
        f"{RADARR_URL}/api/v3/movie/lookup",
        params={"term": query},
        headers={"X-Api-Key": RADARR_API_KEY},
    )
    resp.raise_for_status()
    results = resp.json()[:5]
    return [
        {
            "title": m["title"],
            "year": m.get("year"),
            "tmdbId": m["tmdbId"],
            "overview": m.get("overview", "")[:200],
        }
        for m in results
    ]


def add_movie(tmdb_id: int, quality_profile_id: int = 1) -> str:
    """Agrega una película a Radarr para descarga."""
    # Primero obtener datos completos
    lookup = http.get(
        f"{RADARR_URL}/api/v3/movie/lookup/tmdb",
        params={"tmdbId": tmdb_id},
        headers={"X-Api-Key": RADARR_API_KEY},
    )
    lookup.raise_for_status()
    movie_data = lookup.json()

    # Obtener root folder
    folders = http.get(
        f"{RADARR_URL}/api/v3/rootfolder",
        headers={"X-Api-Key": RADARR_API_KEY},
    )
    root_path = folders.json()[0]["path"]

    payload = {
        "title": movie_data["title"],
        "tmdbId": tmdb_id,
        "year": movie_data.get("year"),
        "qualityProfileId": quality_profile_id,
        "rootFolderPath": root_path,
        "monitored": True,
        "addOptions": {"searchForMovie": True},  # Busca torrent de inmediato
    }

    resp = http.post(
        f"{RADARR_URL}/api/v3/movie",
        json=payload,
        headers={"X-Api-Key": RADARR_API_KEY},
    )
    if resp.status_code == 400 and "already been added" in resp.text.lower():
        return f"'{movie_data['title']}' ya está en tu biblioteca."
    resp.raise_for_status()
    return f"'{movie_data['title']}' agregada. Radarr buscará y descargará automáticamente."


def search_series(query: str) -> list[dict]:
    """Busca series en Sonarr vía TVDB."""
    resp = http.get(
        f"{SONARR_URL}/api/v3/series/lookup",
        params={"term": query},
        headers={"X-Api-Key": SONARR_API_KEY},
    )
    resp.raise_for_status()
    results = resp.json()[:5]
    return [
        {
            "title": s["title"],
            "year": s.get("year"),
            "tvdbId": s["tvdbId"],
            "overview": s.get("overview", "")[:200],
        }
        for s in results
    ]


def add_series(tvdb_id: int, quality_profile_id: int = 1) -> str:
    """Agrega una serie a Sonarr para descarga."""
    lookup = http.get(
        f"{SONARR_URL}/api/v3/series/lookup",
        params={"term": f"tvdb:{tvdb_id}"},
        headers={"X-Api-Key": SONARR_API_KEY},
    )
    lookup.raise_for_status()
    series_data = lookup.json()[0]

    folders = http.get(
        f"{SONARR_URL}/api/v3/rootfolder",
        headers={"X-Api-Key": SONARR_API_KEY},
    )
    root_path = folders.json()[0]["path"]

    payload = {
        "title": series_data["title"],
        "tvdbId": tvdb_id,
        "qualityProfileId": quality_profile_id,
        "rootFolderPath": root_path,
        "monitored": True,
        "seasonFolder": True,
        "addOptions": {"searchForMissingEpisodes": True},
    }

    resp = http.post(
        f"{SONARR_URL}/api/v3/series",
        json=payload,
        headers={"X-Api-Key": SONARR_API_KEY},
    )
    if resp.status_code == 400 and "already been added" in resp.text.lower():
        return f"'{series_data['title']}' ya está en tu biblioteca."
    resp.raise_for_status()
    return f"'{series_data['title']}' agregada. Sonarr buscará y descargará automáticamente."

def search_episode(tvdb_id: int, season: int, episode: int) -> dict:
    """Busca un episodio específico de una serie en Sonarr."""
    # Primero verificar si la serie ya está en Sonarr
    resp = http.get(
        f"{SONARR_URL}/api/v3/series",
        headers={"X-Api-Key": SONARR_API_KEY},
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

    # Buscar el episodio
    resp = http.get(
        f"{SONARR_URL}/api/v3/episode",
        params={"seriesId": series_id},
        headers={"X-Api-Key": SONARR_API_KEY},
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


def download_episode(episode_id: int) -> str:
    """Fuerza la búsqueda y descarga de un episodio específico."""
    # Marcar como monitoreado
    ep_resp = http.get(
        f"{SONARR_URL}/api/v3/episode/{episode_id}",
        headers={"X-Api-Key": SONARR_API_KEY},
    )
    ep_resp.raise_for_status()
    ep_data = ep_resp.json()

    if ep_data.get("hasFile"):
        return f"'{ep_data.get('title', '')}' (S{ep_data['seasonNumber']:02d}E{ep_data['episodeNumber']:02d}) ya está descargado."

    # Activar monitoreo del episodio
    ep_data["monitored"] = True
    http.put(
        f"{SONARR_URL}/api/v3/episode/{episode_id}",
        json=ep_data,
        headers={"X-Api-Key": SONARR_API_KEY},
    )

    # Lanzar búsqueda del episodio
    command = {
        "name": "EpisodeSearch",
        "episodeIds": [episode_id],
    }
    resp = http.post(
        f"{SONARR_URL}/api/v3/command",
        json=command,
        headers={"X-Api-Key": SONARR_API_KEY},
    )
    resp.raise_for_status()
    return f"Buscando descarga para S{ep_data['seasonNumber']:02d}E{ep_data['episodeNumber']:02d} '{ep_data.get('title', '')}'. Sonarr te notificará cuando esté listo."


# ── Definición de tools para Claude ────────────────────────

TOOLS = [
    {
        "name": "search_movie",
        "description": "Busca películas por nombre. Usar cuando el usuario quiere una película.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Nombre de la película a buscar",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "add_movie",
        "description": "Agrega una película para descargar. Usar después de confirmar cuál quiere el usuario.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tmdb_id": {
                    "type": "integer",
                    "description": "TMDB ID de la película",
                }
            },
            "required": ["tmdb_id"],
        },
    },
    {
        "name": "search_series",
        "description": "Busca series de TV por nombre.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Nombre de la serie a buscar",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "add_series",
        "description": "Agrega una serie para descargar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tvdb_id": {
                    "type": "integer",
                    "description": "TVDB ID de la serie",
                }
            },
            "required": ["tvdb_id"],
        },
    },
    {
        "name": "search_episode",
        "description": "Busca un episodio específico de una serie que ya está en Sonarr. Usar cuando el usuario pide un capítulo puntual.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tvdb_id": {
                    "type": "integer",
                    "description": "TVDB ID de la serie",
                },
                "season": {
                    "type": "integer",
                    "description": "Número de temporada",
                },
                "episode": {
                    "type": "integer",
                    "description": "Número de episodio",
                },
            },
            "required": ["tvdb_id", "season", "episode"],
        },
    },
    {
        "name": "download_episode",
        "description": "Descarga un episodio específico. Usar después de search_episode cuando el usuario confirme.",
        "input_schema": {
            "type": "object",
            "properties": {
                "episode_id": {
                    "type": "integer",
                    "description": "ID del episodio en Sonarr (obtenido de search_episode)",
                }
            },
            "required": ["episode_id"],
        },
    },
]

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

# ── Historial de conversación por chat ─────────────────────
conversations: dict[int, list] = {}
MAX_HISTORY = 20


def get_history(chat_id: int) -> list:
    if chat_id not in conversations:
        conversations[chat_id] = []
    return conversations[chat_id]


# ── Procesar con Claude ───────────────────────────────────

def process_with_claude(chat_id: int, user_message: str) -> str:
    history = get_history(chat_id)
    history.append({"role": "user", "content": user_message})

    # Mantener historial limitado
    if len(history) > MAX_HISTORY:
        history[:] = history[-MAX_HISTORY:]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=history,
    )

    # Procesar tool calls en loop
    while response.stop_reason == "tool_use":
        # Obtener el bloque de tool use
        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        text_blocks = [b for b in response.content if b.type == "text"]

        # Agregar respuesta del asistente al historial
        history.append({"role": "assistant", "content": response.content})

        # Ejecutar cada tool call
        tool_results = []
        for tool_block in tool_blocks:
            tool_name = tool_block.name
            tool_input = tool_block.input

            logger.info(f"Tool call: {tool_name}({tool_input})")

            try:
                if tool_name == "search_movie":
                    result = search_movie(tool_input["query"])
                elif tool_name == "add_movie":
                    result = add_movie(tool_input["tmdb_id"])
                elif tool_name == "search_series":
                    result = search_series(tool_input["query"])
                elif tool_name == "add_series":
                    result = add_series(tool_input["tvdb_id"])
                elif tool_name == "search_episode":
                    result = search_episode(
                        tool_input["tvdb_id"],
                        tool_input["season"],
                        tool_input["episode"],
                    )
                elif tool_name == "download_episode":
                    result = download_episode(tool_input["episode_id"])
                else:
                    result = {"error": f"Tool desconocido: {tool_name}"}
            except Exception as e:
                logger.error(f"Error en {tool_name}: {e}")
                result = {"error": str(e)}

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

        history.append({"role": "user", "content": tool_results})

        # Siguiente iteración con Claude
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=history,
        )

    # Extraer texto final
    final_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            final_text += block.text

    history.append({"role": "assistant", "content": response.content})
    return final_text


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

    # Indicador de "escribiendo..."
    await update.message.chat.send_action("typing")

    try:
        reply = process_with_claude(chat_id, user_msg)
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(
            "❌ Hubo un error procesando tu mensaje. Intentá de nuevo."
        )


# ── Main ──────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot iniciado!")
    app.run_polling()


if __name__ == "__main__":
    main()