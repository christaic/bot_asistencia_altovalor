import asyncio
import unicodedata, re
import os
import io
import json
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from google.oauth2 import service_account
from googleapiclient.discovery import build
from pytz import timezone

# ================== ZONA HORARIA ==================
LIMA_TZ = timezone("America/Lima")

# ================== CONFIGURACIÓN ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Token del bot
NOMBRE_CARPETA_DRIVE = "ASISTENCIA_SGA_ALTOVALOR"  # Carpeta principal en la unidad compartida
DRIVE_ID = "0AN8pG_lPt1dtUk9PVA"        # ID de la unidad compartida (Shared Drive)

# --- ÚNICO SPREADSHEET GLOBAL ---
GLOBAL_SHEET_NAME = "ASISTENCIA_CUADRILLAS_DISP_ALTO_VALOR"

# Carga de credenciales desde variable de entorno
CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]

# ================== LOGGING ==================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

async def log_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("[UNHANDLED] Excepción no controlada", exc_info=context.error)

# ================== GOOGLE APIs ==================
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

def get_services():
    creds_info = json.loads(CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES
    )
    drive = build("drive", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    return drive, sheets

drive_service, sheets_service = get_services()

# ================== HELPERS DRIVE ==================
def get_or_create_main_folder():
    """Busca la carpeta principal en la unidad compartida. Si no existe, la crea."""
    query = f"name='{NOMBRE_CARPETA_DRIVE}' and '{DRIVE_ID}' in parents and trashed=false"
    results = drive_service.files().list(
        q=query,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {
        "name": NOMBRE_CARPETA_DRIVE,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [DRIVE_ID],
    }
    folder = drive_service.files().create(
        body=metadata, fields="id", supportsAllDrives=True
    ).execute()
    return folder["id"]

MAIN_FOLDER_ID = get_or_create_main_folder()

def buscar_archivo_en_drive(nombre_archivo: str, mime: str | None = None):
    q = [
        f"name='{nombre_archivo}'",
        f"'{MAIN_FOLDER_ID}' in parents",
        "trashed=false",
    ]
    if mime:
        q.append(f"mimeType='{mime}'")
    query = " and ".join(q)
    results = drive_service.files().list(
        q=query,
        fields="files(id, name, mimeType)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    files = results.get("files", [])
    return files[0] if files else None

SHEET_MIME = "application/vnd.google-apps.spreadsheet"

def ensure_global_spreadsheet() -> str:
    """
    Garantiza que exista un único Google Sheet GLOBAL_SHEET_NAME en MAIN_FOLDER_ID.
    Devuelve su file_id.
    """
    archivo = buscar_archivo_en_drive(GLOBAL_SHEET_NAME, SHEET_MIME)
    if archivo:
        return archivo["id"]

    meta = {
        "name": GLOBAL_SHEET_NAME,
        "mimeType": SHEET_MIME,
        "parents": [MAIN_FOLDER_ID],
    }
    created = drive_service.files().create(
        body=meta, fields="id", supportsAllDrives=True
    ).execute()
    return created["id"]

# ================== GOOGLE SHEETS ==================
SHEET_TITLE = "Registros"

# Cabezera sin "MES"
HEADERS = [
    "FECHA",
    "CUADRILLA",
    "TIPO DE CUADRILLA",   # Disponibilidad | Regular
    "SELFIE CUADRILLA",    # OK / file_id opcional
    "LATITUD",
    "LONGITUD",
    "HORA INGRESO",
    "HORA BREAK OUT",
    "HORA BREAK IN",
    "HORA SALIDA",
    "SELFIE SALIDA",       # OK / file_id opcional
    "LATITUD SALIDA",
    "LONGITUD SALIDA",
]

COL = {
    "FECHA": "A",
    "CUADRILLA": "B",
    "TIPO DE CUADRILLA": "C",
    "SELFIE CUADRILLA": "D",
    "LATITUD": "E",
    "LONGITUD": "F",
    "HORA INGRESO": "G",
    "HORA BREAK OUT": "H",
    "HORA BREAK IN": "I",
    "HORA SALIDA": "J",
    "SELFIE SALIDA": "K",
    "LATITUD SALIDA": "L",
    "LONGITUD SALIDA": "M",
}

def ensure_sheet_and_headers(spreadsheet_id: str):
    """Asegura pestaña SHEET_TITLE y fila 1 con HEADERS (y congela fila 1)."""
    meta = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets = meta.get("sheets", [])
    sheet_id = None
    for s in sheets:
        if s["properties"]["title"] == SHEET_TITLE:
            sheet_id = s["properties"]["sheetId"]
            break

    if sheet_id is None:
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{
                "addSheet": {
                    "properties": {
                        "title": SHEET_TITLE,
                        "gridProperties": {"frozenRowCount": 1}
                    }
                }
            }]}
        ).execute()

    # Escribir headers si hacen falta
    vr = sheets_service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{SHEET_TITLE}!A1:M1"
    ).execute()
    row = vr.get("values", [])
    if not row or row[0] != HEADERS:
        sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{SHEET_TITLE}!A1:M1",
            valueInputOption="RAW",
            body={"values": [HEADERS]}
        ).execute()

def set_cell_value(spreadsheet_id: str, sheet_title: str, a1: str, value):
    body = {"values": [[value]]}
    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_title}!{a1}",
        valueInputOption="USER_ENTERED",
        body=body
    ).execute()

def update_single_cell(spreadsheet_id: str, sheet_title: str, col_letter: str, row: int, value):
    range_name = f"{sheet_title}!{col_letter}{row}"
    body = {"values": [[value]]}
    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        valueInputOption="USER_ENTERED",
        body=body
    ).execute()
    logger.info(f"[DEBUG] update_single_cell OK -> {range_name} = {value}")

def _parse_row_from_updated_range(updated_range: str) -> int:
    # Ej: "Registros!A2:M2" o "'Registros'!A2:M2"
    tail = updated_range.split("!")[1]
    a1 = tail.split(":")[0]  # "A2"
    return int(re.findall(r"\d+", a1)[0])

def append_base_row(spreadsheet_id: str, data: dict) -> int:
    """
    Inserta nueva fila base y devuelve el número de fila insertada.
    """
    ahora = datetime.now(LIMA_TZ)
    payload = {
        "FECHA": ahora.strftime("%Y-%m-%d"),
        "CUADRILLA": data.get("CUADRILLA", ""),
        "TIPO DE CUADRILLA": data.get("TIPO DE CUADRILLA", ""),
        "SELFIE CUADRILLA": "",
        "LATITUD": "",
        "LONGITUD": "",
        "HORA INGRESO": "",
        "HORA BREAK OUT": "",
        "HORA BREAK IN": "",
        "HORA SALIDA": "",
        "SELFIE SALIDA": "",
        "LATITUD SALIDA": "",
        "LONGITUD SALIDA": "",
    }
    row = [[payload.get(h, "") for h in HEADERS]]
    resp = sheets_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{SHEET_TITLE}!A:A",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": row}
    ).execute()
    return _parse_row_from_updated_range(resp["updates"]["updatedRange"])

def gs_set_by_header(spreadsheet_id: str, row: int, header: str, value):
    col = COL[header]
    set_cell_value(spreadsheet_id, SHEET_TITLE, f"{col}{row}", value)

# ================== ESTADO EN MEMORIA ==================
user_data = {}  # por chat_id (privado)

# ================== SOLO PRIVADO ==================
def es_chat_privado(update: Update) -> bool:
    msg = update.message or getattr(update, "callback_query", None)
    chat = (msg.chat if hasattr(msg, "chat") else None) if msg else None
    return bool(chat and chat.type == "private")

# ================== BOT INFO ==================
BOT_USERNAME = None
async def init_bot_info(app):
    global BOT_USERNAME
    bot_info = await app.bot.get_me()
    BOT_USERNAME = f"@{bot_info.username}"
    logger.info(f"Bot iniciado como {BOT_USERNAME}")

# ================== VALIDACIONES ==================
async def validar_contenido(update: Update, tipo: str):
    if tipo == "texto" and not update.message.text:
        await update.message.reply_text("⚠️ Debes enviar el *nombre de tu cuadrilla* en texto. ✍️")
        return False
    if tipo == "foto" and not update.message.photo:
        await update.message.reply_text("⚠️ Debes enviar una *foto*, no texto. 🤳")
        return False
    if tipo == "ubicacion" and not update.message.location:
        await update.message.reply_text("📍 Por favor, envíame tu *ubicación actual* desde el clip ➜ Ubicación.")
        return False
    return True

# ================== COMANDOS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    await update.message.reply_text(
        "👋 ¡Hola! Este bot funciona por chat privado.\n\n"
        "Para iniciar tu registro usa /ingreso."
    )

async def ingreso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    user_data[chat_id] = {"paso": 0}  # reinicia flujo
    await update.message.reply_text(
        "✍️ Escribe el *nombre de tu cuadrilla*.\n\n"
        "Ejemplo:\nT1: Juan Pérez\nT2: José Flores",
        parse_mode="Markdown"
    )

# ================== PASO 0: NOMBRE CUADRILLA ==================
async def nombre_cuadrilla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.setdefault(chat_id, {"paso": 0})
    if ud.get("paso") != 0:
        return
    if not await validar_contenido(update, "texto"):
        return

    ud["cuadrilla"] = update.message.text.strip()
    keyboard = [
        [InlineKeyboardButton("✅ Confirmar", callback_data="confirmar_nombre")],
        [InlineKeyboardButton("✏️ Corregir", callback_data="corregir_nombre")],
    ]
    await update.message.reply_text(
        f"Has ingresado la cuadrilla:\n*{ud['cuadrilla']}*\n\n¿Es correcto?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def handle_nombre_cuadrilla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not es_chat_privado(update):
        return
    await query.answer()
    chat_id = query.message.chat.id
    ud = user_data.setdefault(chat_id, {})

    if query.data == "corregir_nombre":
        ud["paso"] = 0
        ud["cuadrilla"] = ""
        await query.edit_message_text(
            "✍️ *Escribe el nombre de tu cuadrilla nuevamente.*",
            parse_mode="Markdown"
        )
        return

    if query.data == "confirmar_nombre":
        if not ud.get("cuadrilla"):
            ud["paso"] = 0
            await query.edit_message_text("⚠️ No encontré el nombre. Escríbelo y confirma.")
            return

        # Garantizar spreadsheet global y headers
        ssid = ensure_global_spreadsheet()
        ensure_sheet_and_headers(ssid)

        # Crear fila base una sola vez (idempotente)
        if not ud.get("spreadsheet_id") or not ud.get("row"):
            base = {"CUADRILLA": ud["cuadrilla"], "TIPO DE CUADRILLA": ""}
            row = append_base_row(ssid, base)
            ud["spreadsheet_id"] = ssid
            ud["row"] = row

        ud["paso"] = "tipo"
        keyboard = [
            [InlineKeyboardButton("🟢 Disponibilidad", callback_data="tipo_disp")],
            [InlineKeyboardButton("⚪ Regular", callback_data="tipo_reg")],
        ]
        await query.edit_message_text(
            "Selecciona el *tipo de cuadrilla*:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# ================== TIPO DE CUADRILLA ==================
async def handle_tipo_cuadrilla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not es_chat_privado(update):
        return
    await query.answer()
    chat_id = query.message.chat.id
    ud = user_data.setdefault(chat_id, {})
    data = query.data

    if data not in ("tipo_disp", "tipo_reg"):
        return

    tipo = "Disponibilidad" if data == "tipo_disp" else "Regular"
    ssid = ud.get("spreadsheet_id")
    row = ud.get("row")

    if not ssid or not row:
        # fallback (no debería)
        ssid = ensure_global_spreadsheet()
        ensure_sheet_and_headers(ssid)
        base = {"CUADRILLA": ud.get("cuadrilla", ""), "TIPO DE CUADRILLA": ""}
        row = append_base_row(ssid, base)
        ud["spreadsheet_id"] = ssid
        ud["row"] = row

    gs_set_by_header(ssid, row, "TIPO DE CUADRILLA", tipo)

    # Siguiente paso: selfie de cuadrilla (inicio) y hora de ingreso
    ud["paso"] = 1
    await query.edit_message_text(
        f"Tipo seleccionado: *{tipo}*\n\n📸 Envía la *selfie de la cuadrilla (inicio)*.",
        parse_mode="Markdown"
    )

# ================== FOTO INICIO + HORA INGRESO ==================
async def foto_ingreso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.get(chat_id) or {}
    if ud.get("paso") != 1:
        return
    if not await validar_contenido(update, "foto"):
        return

    ssid, row = ud.get("spreadsheet_id"), ud.get("row")
    if not ssid or not row:
        await update.message.reply_text("❌ No hay registro activo. Usa /ingreso para iniciar.")
        return

    # Registrar HORA INGRESO y marcar SELFIE CUADRILLA
    hora_ingreso = datetime.now(LIMA_TZ).strftime("%H:%M")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, update_single_cell, ssid, SHEET_TITLE, COL["HORA INGRESO"], row, hora_ingreso
    )
    gs_set_by_header(ssid, row, "SELFIE CUADRILLA", "OK")

    ud["hora_ingreso"] = hora_ingreso
    ud["paso"] = 2  # siguiente: ubicación
    await update.message.reply_text(
        f"⏱️ Hora de ingreso registrada: *{hora_ingreso}*.\n\n"
        "📍 Ahora comparte tu *ubicación actual* (clip ➜ Ubicación).",
        parse_mode="Markdown"
    )
    user_data[chat_id] = ud

# ================== UBICACIÓN INICIO ==================
async def manejar_ubicacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.get(chat_id) or {}
    if not update.message.location:
        return

    ssid, row = ud.get("spreadsheet_id"), ud.get("row")
    if not ssid or not row:
        return

    lat = update.message.location.latitude
    lng = update.message.location.longitude

    if ud.get("paso") == 2:
        # Ubicación de inicio
        gs_set_by_header(ssid, row, "LATITUD", f"{lat:.6f}")
        gs_set_by_header(ssid, row, "LONGITUD", f"{lng:.6f}")
        ud["paso"] = "en_jornada"
        await update.message.reply_text(
            "✅ Ubicación de inicio registrada.\n\n"
            "Puedes usar /breakout y /breakin durante el día. "
            "Cuando termines, usa /salida."
        )

    elif ud.get("paso") == "ubicacion_salida":
        # Ubicación de salida
        gs_set_by_header(ssid, row, "LATITUD SALIDA", f"{lat:.6f}")
        gs_set_by_header(ssid, row, "LONGITUD SALIDA", f"{lng:.6f}")
        ud["paso"] = None
        await update.message.reply_text(
            "🫡 *¡Registro finalizado!* Gracias por tu apoyo hoy.",
            parse_mode="Markdown"
        )

    user_data[chat_id] = ud

# ================== BREAK OUT / BREAK IN ==================
async def breakout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.setdefault(chat_id, {})
    ssid = ud.get("spreadsheet_id")
    row = ud.get("row")

    if not ssid or not row:
        await update.message.reply_text("⚠️ No hay jornada activa. Usa /ingreso para iniciar.")
        return

    hora = datetime.now(LIMA_TZ).strftime("%H:%M")
    set_cell_value(ssid, SHEET_TITLE, f"{COL['HORA BREAK OUT']}{row}", hora)
    await update.message.reply_text(f"🍽️ Break Out registrado a las {hora}.")

async def breakin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.setdefault(chat_id, {})
    ssid = ud.get("spreadsheet_id")
    row = ud.get("row")

    if not ssid or not row:
        await update.message.reply_text("⚠️ No hay jornada activa. Usa /ingreso para iniciar.")
        return

    hora = datetime.now(LIMA_TZ).strftime("%H:%M")
    set_cell_value(ssid, SHEET_TITLE, f"{COL['HORA BREAK IN']}{row}", hora)
    await update.message.reply_text(f"🚶 Regreso de Break registrado a las {hora}.")

# ================== SALIDA ==================
async def salida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.setdefault(chat_id, {})
    ssid = ud.get("spreadsheet_id")
    row = ud.get("row")

    if not ssid or not row:
        await update.message.reply_text("⚠️ No hay jornada activa. Usa /ingreso para iniciar.")
        return

    ud["paso"] = "selfie_salida"
    await update.message.reply_text("📸 Envía tu *selfie de salida* para finalizar.", parse_mode="Markdown")

async def selfie_salida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.get(chat_id) or {}
    if ud.get("paso") != "selfie_salida":
        return
    if not await validar_contenido(update, "foto"):
        return

    ssid, row = ud.get("spreadsheet_id"), ud.get("row")
    if not ssid or not row:
        await update.message.reply_text("❌ No hay registro activo.")
        return

    hora = datetime.now(LIMA_TZ).strftime("%H:%M")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, update_single_cell, ssid, SHEET_TITLE, COL["HORA SALIDA"], row, hora
    )
    gs_set_by_header(ssid, row, "SELFIE SALIDA", "OK")

    ud["paso"] = "ubicacion_salida"
    await update.message.reply_text(
        f"⏱️ Hora de salida registrada: *{hora}*.\n\n"
        "📍 Comparte tu *ubicación actual* para finalizar.",
        parse_mode="Markdown"
    )
    user_data[chat_id] = ud

# ================== ROUTER DE FOTOS ==================
async def manejar_fotos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    paso = user_data.get(chat_id, {}).get("paso")
    if paso == 1:
        await foto_ingreso(update, context)
    elif paso == "selfie_salida":
        await selfie_salida(update, context)
    else:
        await update.message.reply_text("⚠️ No es momento de enviar fotos. Usa /ingreso para comenzar.")

# ================== CALLBACKS REGISTRO ==================
async def manejar_repeticiones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # (reservado por si luego deseas botones de repetir foto, etc.)
    pass

# ================== MAIN ==================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = init_bot_info

    # Comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ingreso", ingreso))
    app.add_handler(CommandHandler("breakout", breakout))
    app.add_handler(CommandHandler("breakin", breakin))
    app.add_handler(CommandHandler("salida", salida))

    # Mensajes
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, nombre_cuadrilla))
    app.add_handler(MessageHandler(filters.PHOTO, manejar_fotos))
    app.add_handler(MessageHandler(filters.LOCATION, manejar_ubicacion))

    # Callbacks
    app.add_handler(CallbackQueryHandler(handle_nombre_cuadrilla, pattern="^(confirmar_nombre|corregir_nombre)$"))
    app.add_handler(CallbackQueryHandler(handle_tipo_cuadrilla, pattern="^tipo_(disp|reg)$"))
    app.add_handler(CallbackQueryHandler(manejar_repeticiones, pattern="^repetir_"))

    # Errores
    app.add_error_handler(log_error)

    print("🚀 Bot de Asistencia (privado) en ejecución...")
    app.run_polling()

if __name__ == "__main__":
    main()
Qué cambió (resumen rápido)
Solo chat privado (ignora grupos).

Un único Spreadsheet global: ASISTENCIA_CUADRILLAS_DISP_ALTO_VALOR (pestaña Registros).

HEADERS nuevos (sin “MES”): FECHA, CUADRILLA, TIPO DE CUADRILLA, SELFIE CUADRILLA, LATITUD, LONGITUD, HORA INGRESO, HORA BREAK OUT, HORA BREAK IN, HORA SALIDA, SELFIE SALIDA, LATITUD SALIDA, LONGITUD SALIDA.

Flujo actualizado con selfie de cuadrilla y ubicación al inicio; y selfie + ubicación al cerrar.

Tiempos en hora de Lima: ingreso, break out, break in, salida.

Si quieres que además guarde el file_id de las fotos en el Sheet (en vez de “OK”), te lo conecto en dos líneas. ¿Lo dejamos así o lo prefieres con file_id/URL?







Tú dijiste:
Me parece perfecto, quisiera añadir el file_id URL de las fotos en el sheet porfavor para poder visualizarlas haciendo click luego porfavor ..


ChatGPT Plus

Pensó durante 46s
¡De una, Chris! Guardé enlaces clicables a las fotos en tu Google Sheet subiendo cada selfie a Drive (carpeta ASISTENCIA_BOT/IMAGENES) y escribiendo el webViewLink en las columnas SELFIE CUADRILLA y SELFIE SALIDA. Así evitas exponer el token del bot (no uso la URL de Telegram con token). Si tu unidad compartida permite “cualquiera con el enlace”, quedará visible; si no, el link abrirá para los que tengan acceso a la unidad.

Aquí tienes el main.py completo con el cambio:

python
Copiar código
import asyncio
import unicodedata, re
import os
import io
import json
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from pytz import timezone

# ================== ZONA HORARIA ==================
LIMA_TZ = timezone("America/Lima")

# ================== CONFIGURACIÓN ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Token del bot
NOMBRE_CARPETA_DRIVE = "ASISTENCIA_BOT"  # Carpeta principal en la unidad compartida
DRIVE_ID = "0AOy_EhsaSY_HUk9PVA"        # ID de la unidad compartida (Shared Drive)

# --- ÚNICO SPREADSHEET GLOBAL ---
GLOBAL_SHEET_NAME = "ASISTENCIA_CUADRILLAS_DISP_ALTO_VALOR"

# Carga de credenciales desde variable de entorno
CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]

# ================== LOGGING ==================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

async def log_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("[UNHANDLED] Excepción no controlada", exc_info=context.error)

# ================== GOOGLE APIs ==================
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

def get_services():
    creds_info = json.loads(CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES
    )
    drive = build("drive", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    return drive, sheets

drive_service, sheets_service = get_services()

# ================== HELPERS DRIVE ==================
def get_or_create_main_folder():
    """Busca la carpeta principal en la unidad compartida. Si no existe, la crea."""
    query = f"name='{NOMBRE_CARPETA_DRIVE}' and '{DRIVE_ID}' in parents and trashed=false"
    results = drive_service.files().list(
        q=query,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {
        "name": NOMBRE_CARPETA_DRIVE,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [DRIVE_ID],
    }
    folder = drive_service.files().create(
        body=metadata, fields="id", supportsAllDrives=True
    ).execute()
    return folder["id"]

MAIN_FOLDER_ID = get_or_create_main_folder()

def get_or_create_images_folder():
    """Crea/obtiene subcarpeta IMAGENES dentro de la carpeta principal."""
    query = (
        f"name='IMAGENES' and '{MAIN_FOLDER_ID}' in parents and "
        f"mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    res = drive_service.files().list(
        q=query,
        fields="files(id,name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    meta = {
        "name": "IMAGENES",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [MAIN_FOLDER_ID],
    }
    f = drive_service.files().create(
        body=meta, fields="id", supportsAllDrives=True
    ).execute()
    return f["id"]

IMAGES_FOLDER_ID = get_or_create_images_folder()

def buscar_archivo_en_drive(nombre_archivo: str, mime: str | None = None):
    q = [
        f"name='{nombre_archivo}'",
        f"'{MAIN_FOLDER_ID}' in parents",
        "trashed=false",
    ]
    if mime:
        q.append(f"mimeType='{mime}'")
    query = " and ".join(q)
    results = drive_service.files().list(
        q=query,
        fields="files(id, name, mimeType)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    files = results.get("files", [])
    return files[0] if files else None

SHEET_MIME = "application/vnd.google-apps.spreadsheet"

def ensure_global_spreadsheet() -> str:
    """
    Garantiza que exista un único Google Sheet GLOBAL_SHEET_NAME en MAIN_FOLDER_ID.
    Devuelve su file_id.
    """
    archivo = buscar_archivo_en_drive(GLOBAL_SHEET_NAME, SHEET_MIME)
    if archivo:
        return archivo["id"]

    meta = {
        "name": GLOBAL_SHEET_NAME,
        "mimeType": SHEET_MIME,
        "parents": [MAIN_FOLDER_ID],
    }
    created = drive_service.files().create(
        body=meta, fields="id", supportsAllDrives=True
    ).execute()
    return created["id"]

# ---- Subida de imagen a Drive y enlace clicable ----
def upload_image_and_get_link(image_bytes: io.BytesIO, filename: str) -> str:
    """
    Sube una imagen a la carpeta IMAGENES y devuelve un enlace webViewLink.
    Intenta poner permiso 'anyone with the link' como lector (si la política lo permite).
    """
    media = MediaIoBaseUpload(image_bytes, mimetype="image/jpeg", resumable=False)
    metadata = {"name": filename, "parents": [IMAGES_FOLDER_ID]}
    file = drive_service.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True
    ).execute()
    file_id = file["id"]
    try:
        drive_service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            fields="id",
            supportsAllDrives=True
        ).execute()
    except Exception as e:
        logger.warning(f"[WARN] No se pudo abrir a 'cualquiera con el enlace': {e}. El link puede requerir acceso.")
    return file.get("webViewLink")

# ================== GOOGLE SHEETS ==================
SHEET_TITLE = "Registros"

# Cabezera sin "MES"; columnas para URL de selfies
HEADERS = [
    "FECHA",
    "CUADRILLA",
    "TIPO DE CUADRILLA",   # Disponibilidad | Regular
    "SELFIE CUADRILLA",    # URL clicable en Drive
    "LATITUD",
    "LONGITUD",
    "HORA INGRESO",
    "HORA BREAK OUT",
    "HORA BREAK IN",
    "HORA SALIDA",
    "SELFIE SALIDA",       # URL clicable en Drive
    "LATITUD SALIDA",
    "LONGITUD SALIDA",
]

COL = {
    "FECHA": "A",
    "CUADRILLA": "B",
    "TIPO DE CUADRILLA": "C",
    "SELFIE CUADRILLA": "D",
    "LATITUD": "E",
    "LONGITUD": "F",
    "HORA INGRESO": "G",
    "HORA BREAK OUT": "H",
    "HORA BREAK IN": "I",
    "HORA SALIDA": "J",
    "SELFIE SALIDA": "K",
    "LATITUD SALIDA": "L",
    "LONGITUD SALIDA": "M",
}

def ensure_sheet_and_headers(spreadsheet_id: str):
    """Asegura pestaña SHEET_TITLE y fila 1 con HEADERS (y congela fila 1)."""
    meta = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets = meta.get("sheets", [])
    sheet_id = None
    for s in sheets:
        if s["properties"]["title"] == SHEET_TITLE:
            sheet_id = s["properties"]["sheetId"]
            break

    if sheet_id is None:
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{
                "addSheet": {
                    "properties": {
                        "title": SHEET_TITLE,
                        "gridProperties": {"frozenRowCount": 1}
                    }
                }
            }]}
        ).execute()

    # Escribir headers si hacen falta
    vr = sheets_service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{SHEET_TITLE}!A1:M1"
    ).execute()
    row = vr.get("values", [])
    if not row or row[0] != HEADERS:
        sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{SHEET_TITLE}!A1:M1",
            valueInputOption="RAW",
            body={"values": [HEADERS]}
        ).execute()

def set_cell_value(spreadsheet_id: str, sheet_title: str, a1: str, value):
    body = {"values": [[value]]}
    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_title}!{a1}",
        valueInputOption="USER_ENTERED",
        body=body
    ).execute()

def update_single_cell(spreadsheet_id: str, sheet_title: str, col_letter: str, row: int, value):
    range_name = f"{sheet_title}!{col_letter}{row}"
    body = {"values": [[value]]}
    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        valueInputOption="USER_ENTERED",
        body=body
    ).execute()
    logger.info(f"[DEBUG] update_single_cell OK -> {range_name} = {value}")

def _parse_row_from_updated_range(updated_range: str) -> int:
    # Ej: "Registros!A2:M2" o "'Registros'!A2:M2"
    tail = updated_range.split("!")[1]
    a1 = tail.split(":")[0]  # "A2"
    return int(re.findall(r"\d+", a1)[0])

def append_base_row(spreadsheet_id: str, data: dict) -> int:
    """
    Inserta nueva fila base y devuelve el número de fila insertada.
    """
    ahora = datetime.now(LIMA_TZ)
    payload = {
        "FECHA": ahora.strftime("%Y-%m-%d"),
        "CUADRILLA": data.get("CUADRILLA", ""),
        "TIPO DE CUADRILLA": data.get("TIPO DE CUADRILLA", ""),
        "SELFIE CUADRILLA": "",
        "LATITUD": "",
        "LONGITUD": "",
        "HORA INGRESO": "",
        "HORA BREAK OUT": "",
        "HORA BREAK IN": "",
        "HORA SALIDA": "",
        "SELFIE SALIDA": "",
        "LATITUD SALIDA": "",
        "LONGITUD SALIDA": "",
    }
    row = [[payload.get(h, "") for h in HEADERS]]
    resp = sheets_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{SHEET_TITLE}!A:A",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": row}
    ).execute()
    return _parse_row_from_updated_range(resp["updates"]["updatedRange"])

def gs_set_by_header(spreadsheet_id: str, row: int, header: str, value):
    col = COL[header]
    set_cell_value(spreadsheet_id, SHEET_TITLE, f"{col}{row}", value)

# ================== ESTADO EN MEMORIA ==================
user_data = {}  # por chat_id (privado)

# ================== SOLO PRIVADO ==================
def es_chat_privado(update: Update) -> bool:
    msg = update.message or getattr(update, "callback_query", None)
    chat = (msg.chat if hasattr(msg, "chat") else None) if msg else None
    return bool(chat and chat.type == "private")

# ================== BOT INFO ==================
BOT_USERNAME = None
async def init_bot_info(app):
    global BOT_USERNAME
    bot_info = await app.bot.get_me()
    BOT_USERNAME = f"@{bot_info.username}"
    logger.info(f"Bot iniciado como {BOT_USERNAME}")

# ================== VALIDACIONES ==================
async def validar_contenido(update: Update, tipo: str):
    if tipo == "texto" and not update.message.text:
        await update.message.reply_text("⚠️ Debes enviar el *nombre de tu cuadrilla* en texto. ✍️")
        return False
    if tipo == "foto" and not update.message.photo:
        await update.message.reply_text("⚠️ Debes enviar una *foto*, no texto. 🤳")
        return False
    if tipo == "ubicacion" and not update.message.location:
        await update.message.reply_text("📍 Por favor, envíame tu *ubicación actual* desde el clip ➜ Ubicación.")
        return False
    return True

# ================== COMANDOS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    await update.message.reply_text(
        "👋 ¡Hola! Este bot funciona por chat privado.\n\n"
        "Para iniciar tu registro usa /ingreso."
    )

async def ingreso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    user_data[chat_id] = {"paso": 0}  # reinicia flujo
    await update.message.reply_text(
        "✍️ Escribe el *nombre de tu cuadrilla*.\n\n"
        "Ejemplo:\nT1: Juan Pérez\nT2: José Flores",
        parse_mode="Markdown"
    )

# ================== PASO 0: NOMBRE CUADRILLA ==================
async def nombre_cuadrilla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.setdefault(chat_id, {"paso": 0})
    if ud.get("paso") != 0:
        return
    if not await validar_contenido(update, "texto"):
        return

    ud["cuadrilla"] = update.message.text.strip()
    keyboard = [
        [InlineKeyboardButton("✅ Confirmar", callback_data="confirmar_nombre")],
        [InlineKeyboardButton("✏️ Corregir", callback_data="corregir_nombre")],
    ]
    await update.message.reply_text(
        f"Has ingresado la cuadrilla:\n*{ud['cuadrilla']}*\n\n¿Es correcto?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def handle_nombre_cuadrilla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not es_chat_privado(update):
        return
    await query.answer()
    chat_id = query.message.chat.id
    ud = user_data.setdefault(chat_id, {})

    if query.data == "corregir_nombre":
        ud["paso"] = 0
        ud["cuadrilla"] = ""
        await query.edit_message_text(
            "✍️ *Escribe el nombre de tu cuadrilla nuevamente.*",
            parse_mode="Markdown"
        )
        return

    if query.data == "confirmar_nombre":
        if not ud.get("cuadrilla"):
            ud["paso"] = 0
            await query.edit_message_text("⚠️ No encontré el nombre. Escríbelo y confirma.")
            return

        # Garantizar spreadsheet global y headers
        ssid = ensure_global_spreadsheet()
        ensure_sheet_and_headers(ssid)

        # Crear fila base (una sola vez)
        if not ud.get("spreadsheet_id") or not ud.get("row"):
            base = {"CUADRILLA": ud["cuadrilla"], "TIPO DE CUADRILLA": ""}
            row = append_base_row(ssid, base)
            ud["spreadsheet_id"] = ssid
            ud["row"] = row

        ud["paso"] = "tipo"
        keyboard = [
            [InlineKeyboardButton("🟢 Disponibilidad", callback_data="tipo_disp")],
            [InlineKeyboardButton("⚪ Regular", callback_data="tipo_reg")],
        ]
        await query.edit_message_text(
            "Selecciona el *tipo de cuadrilla*:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# ================== TIPO DE CUADRILLA ==================
async def handle_tipo_cuadrilla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not es_chat_privado(update):
        return
    await query.answer()
    chat_id = query.message.chat.id
    ud = user_data.setdefault(chat_id, {})
    data = query.data

    if data not in ("tipo_disp", "tipo_reg"):
        return

    tipo = "Disponibilidad" if data == "tipo_disp" else "Regular"
    ssid = ud.get("spreadsheet_id")
    row = ud.get("row")

    if not ssid or not row:
        ssid = ensure_global_spreadsheet()
        ensure_sheet_and_headers(ssid)
        base = {"CUADRILLA": ud.get("cuadrilla", ""), "TIPO DE CUADRILLA": ""}
        row = append_base_row(ssid, base)
        ud["spreadsheet_id"] = ssid
        ud["row"] = row

    gs_set_by_header(ssid, row, "TIPO DE CUADRILLA", tipo)

    ud["paso"] = 1
    await query.edit_message_text(
        f"Tipo seleccionado: *{tipo}*\n\n📸 Envía la *selfie de la cuadrilla (inicio)*.",
        parse_mode="Markdown"
    )

# ================== FOTO INICIO + HORA INGRESO ==================
async def foto_ingreso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.get(chat_id) or {}
    if ud.get("paso") != 1:
        return
    if not await validar_contenido(update, "foto"):
        return

    ssid, row = ud.get("spreadsheet_id"), ud.get("row")
    if not ssid or not row:
        await update.message.reply_text("❌ No hay registro activo. Usa /ingreso para iniciar.")
        return

    # ---- Subir selfie a Drive y obtener URL ----
    try:
        photo = update.message.photo[-1]  # mayor resolución
        tg_file = await context.bot.get_file(photo.file_id)
        buff = io.BytesIO()
        await tg_file.download(out=buff)
        buff.seek(0)
        filename = f"selfie_inicio_{datetime.now(LIMA_TZ).strftime('%Y%m%d_%H%M%S')}_{chat_id}_{row}.jpg"
        link = upload_image_and_get_link(buff, filename)
        gs_set_by_header(ssid, row, "SELFIE CUADRILLA", link)
    except Exception as e:
        logger.error(f"[ERROR] Subiendo selfie inicio a Drive: {e}")
        await update.message.reply_text("⚠️ No pude subir la foto a Drive. Igual continúa, puedes reenviarla si deseas.")

    # Registrar HORA INGRESO
    hora_ingreso = datetime.now(LIMA_TZ).strftime("%H:%M")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, update_single_cell, ssid, SHEET_TITLE, COL["HORA INGRESO"], row, hora_ingreso
    )

    ud["hora_ingreso"] = hora_ingreso
    ud["paso"] = 2  # siguiente: ubicación
    await update.message.reply_text(
        f"⏱️ Hora de ingreso registrada: *{hora_ingreso}*.\n\n"
        "📍 Ahora comparte tu *ubicación actual* (clip ➜ Ubicación).",
        parse_mode="Markdown"
    )
    user_data[chat_id] = ud

# ================== UBICACIÓN INICIO / SALIDA ==================
async def manejar_ubicacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.get(chat_id) or {}
    if not update.message.location:
        return

    ssid, row = ud.get("spreadsheet_id"), ud.get("row")
    if not ssid or not row:
        return

    lat = update.message.location.latitude
    lng = update.message.location.longitude

    if ud.get("paso") == 2:
        gs_set_by_header(ssid, row, "LATITUD", f"{lat:.6f}")
        gs_set_by_header(ssid, row, "LONGITUD", f"{lng:.6f}")
        ud["paso"] = "en_jornada"
        await update.message.reply_text(
            "✅ Ubicación de inicio registrada.\n\n"
            "Usa /breakout y /breakin durante el día. "
            "Cuando termines, usa /salida."
        )

    elif ud.get("paso") == "ubicacion_salida":
        gs_set_by_header(ssid, row, "LATITUD SALIDA", f"{lat:.6f}")
        gs_set_by_header(ssid, row, "LONGITUD SALIDA", f"{lng:.6f}")
        ud["paso"] = None
        await update.message.reply_text(
            "🫡 *¡Registro finalizado!* Gracias por tu apoyo hoy.",
            parse_mode="Markdown"
        )

    user_data[chat_id] = ud

# ================== BREAK OUT / BREAK IN ==================
async def breakout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.setdefault(chat_id, {})
    ssid = ud.get("spreadsheet_id")
    row = ud.get("row")

    if not ssid or not row:
        await update.message.reply_text("⚠️ No hay jornada activa. Usa /ingreso para iniciar.")
        return

    hora = datetime.now(LIMA_TZ).strftime("%H:%M")
    set_cell_value(ssid, SHEET_TITLE, f"{COL['HORA BREAK OUT']}{row}", hora)
    await update.message.reply_text(f"🍽️ Break Out registrado a las {hora}.")

async def breakin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.setdefault(chat_id, {})
    ssid = ud.get("spreadsheet_id")
    row = ud.get("row")

    if not ssid or not row:
        await update.message.reply_text("⚠️ No hay jornada activa. Usa /ingreso para iniciar.")
        return

    hora = datetime.now(LIMA_TZ).strftime("%H:%M")
    set_cell_value(ssid, SHEET_TITLE, f"{COL['HORA BREAK IN']}{row}", hora)
    await update.message.reply_text(f"🚶 Regreso de Break registrado a las {hora}.")

# ================== SALIDA ==================
async def salida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.setdefault(chat_id, {})
    ssid = ud.get("spreadsheet_id")
    row = ud.get("row")

    if not ssid or not row:
        await update.message.reply_text("⚠️ No hay jornada activa. Usa /ingreso para iniciar.")
        return

    ud["paso"] = "selfie_salida"
    await update.message.reply_text("📸 Envía tu *selfie de salida* para finalizar.", parse_mode="Markdown")

async def selfie_salida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.get(chat_id) or {}
    if ud.get("paso") != "selfie_salida":
        return
    if not await validar_contenido(update, "foto"):
        return

    ssid, row = ud.get("spreadsheet_id"), ud.get("row")
    if not ssid or not row:
        await update.message.reply_text("❌ No hay registro activo.")
        return

    # ---- Subir selfie de salida a Drive y escribir URL ----
    try:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        buff = io.BytesIO()
        await tg_file.download(out=buff)
        buff.seek(0)
        filename = f"selfie_salida_{datetime.now(LIMA_TZ).strftime('%Y%m%d_%H%M%S')}_{chat_id}_{row}.jpg"
        link = upload_image_and_get_link(buff, filename)
        gs_set_by_header(ssid, row, "SELFIE SALIDA", link)
    except Exception as e:
        logger.error(f"[ERROR] Subiendo selfie salida a Drive: {e}")
        await update.message.reply_text("⚠️ No pude subir la foto de salida a Drive. Puedes reenviarla si deseas.")

    # Registrar HORA SALIDA
    hora = datetime.now(LIMA_TZ).strftime("%H:%M")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, update_single_cell, ssid, SHEET_TITLE, COL["HORA SALIDA"], row, hora
    )

    ud["paso"] = "ubicacion_salida"
    await update.message.reply_text(
        f"⏱️ Hora de salida registrada: *{hora}*.\n\n"
        "📍 Comparte tu *ubicación actual* para finalizar.",
        parse_mode="Markdown"
    )
    user_data[chat_id] = ud

# ================== ROUTER DE FOTOS ==================
async def manejar_fotos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    paso = user_data.get(chat_id, {}).get("paso")
    if paso == 1:
        await foto_ingreso(update, context)
    elif paso == "selfie_salida":
        await selfie_salida(update, context)
    else:
        await update.message.reply_text("⚠️ No es momento de enviar fotos. Usa /ingreso para comenzar.")

# ================== CALLBACKS (placeholder) ==================
async def manejar_repeticiones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass

# ================== MAIN ==================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = init_bot_info

    # Comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ingreso", ingreso))
    app.add_handler(CommandHandler("breakout", breakout))
    app.add_handler(CommandHandler("breakin", breakin))
    app.add_handler(CommandHandler("salida", salida))

    # Mensajes
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, nombre_cuadrilla))
    app.add_handler(MessageHandler(filters.PHOTO, manejar_fotos))
    app.add_handler(MessageHandler(filters.LOCATION, manejar_ubicacion))

    # Callbacks
    app.add_handler(CallbackQueryHandler(handle_nombre_cuadrilla, pattern="^(confirmar_nombre|corregir_nombre)$"))
    app.add_handler(CallbackQueryHandler(handle_tipo_cuadrilla, pattern="^tipo_(disp|reg)$"))
    app.add_handler(CallbackQueryHandler(manejar_repeticiones, pattern="^repetir_"))

    # Errores
    app.add_error_handler(log_error)

    print("🚀 Bot de Asistencia (privado) en ejecución...")
    app.run_polling()

if __name__ == "__main__":
    main()
