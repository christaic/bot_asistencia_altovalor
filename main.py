import asyncio
import unicodedata, re
import os
import io
import json
import logging
from datetime import datetime
from datetime import date
from PIL import Image
import gc
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

#== COMPRIMIR IMAGEN VARIABLE==

def comprimir_y_subir(buff: io.BytesIO, filename: str, ssid: str, row: int, header: str) -> str:
    """
    Comprime la imagen al 80%, la sube a Drive y guarda el link en Google Sheets.
    """
    try:
        compressed = io.BytesIO()
        img = Image.open(buff)
        img.save(compressed, format="JPEG", quality=80, optimize=True, progressive=True)
        compressed.seek(0)

        # Liberar RAM del buffer original
        buff.close()
        del buff
        gc.collect()

        # Subir a Drive
        link = upload_image_and_get_link(compressed, filename)
        gs_set_by_header(ssid, row, header, link)

        # Liberar RAM del comprimido
        compressed.close()
        del compressed, img
        gc.collect()

        return link
    except Exception as e:
        logger.error(f"[ERROR] comprimir_y_subir: {e}")
        raise

# Control de registros diarios (chat_id -> fecha último registro finalizado)

registro_diario = {}

def ya_registro_hoy(chat_id: int) -> bool:
    """Verifica si el usuario ya completó un registro hoy"""
    return registro_diario.get(chat_id) == date.today().isoformat()

def marcar_registro_completo(chat_id: int):
    """Marca que el usuario completó su registro hoy"""
    registro_diario[chat_id] = date.today().isoformat()


# ================== ZONA HORARIA ==================
LIMA_TZ = timezone("America/Lima")

# ================== CONFIGURACIÓN ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Token del bot
NOMBRE_CARPETA_DRIVE = "ASISTENCIA_SGA_ALTOVALOR"
DRIVE_ID = "0AN8pG_lPt1dtUk9PVA"
GLOBAL_SHEET_NAME = "ASISTENCIA_CUADRILLAS_DISP_ALTO_VALOR"
USUARIOS_TEST = {7175478712, 7286377190}

# Carga de credenciales desde variable de entorno
CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

# 🔎 Verificación temprana de variables críticas
if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN no definido en Render")

if not CREDENTIALS_JSON:
    raise RuntimeError("❌ GOOGLE_CREDENTIALS_JSON no definido en Render")


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
    image_bytes.seek(0)
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
    "FOTO INICIO CUADRILLA",    # URL clicable en Drive
    "LATITUD",
    "LONGITUD",
    "HORA INGRESO",
    "HORA SALIDA",
    "FOTO FIN CUADRILLA",       # URL clicable en Drive
    "LATITUD SALIDA",
    "LONGITUD SALIDA",
]

COL = {
    "FECHA": "A",
    "CUADRILLA": "B",
    "TIPO DE CUADRILLA": "C",
    "FOTO INICIO CUADRILLA": "D",
    "LATITUD": "E",
    "LONGITUD": "F",
    "HORA INGRESO": "G",
    "HORA SALIDA": "H",
    "FOTO FIN CUADRILLA": "I",
    "LATITUD SALIDA": "J",
    "LONGITUD SALIDA": "K",
}

PASOS = {
    "esperando_cuadrilla": {
        "mensaje": "✍️ Te falta escribir el <b>nombre de tu cuadrilla</b>."
    },
    "esperando_selfie_inicio": {
        "mensaje": "📸 Te falta tu <b>foto de inicio</b>."
    },
    "esperando_live_inicio": {
        "mensaje": "📍 Te falta tu <b>ubicación en tiempo real</b> de inicio."
    },
    "esperando_selfie_salida": {
        "mensaje": "📸 Te falta tu <b>foto de salida</b>."
    },
    "esperando_live_salida": {
        "mensaje": "📍 Te falta tu <b>ubicación en tiempo real</b> de salida."
    },
    "cerrado": {
        "mensaje": "✅ Registro completado. No puedes iniciar otro hasta mañana."
    }
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
        "FOTO INICIO CUADRILLA": "",
        "LATITUD": "",
        "LONGITUD": "",
        "HORA INGRESO": "",
        "HORA SALIDA": "",
        "FOTO FIN CUADRILLA": "",
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
    """
    True si el update proviene de un chat privado.
    Soporta mensajes y callbacks.
    """
    chat = None
    # Mensaje normal
    if getattr(update, "message", None):
        chat = update.message.chat
    # Callback (botones inline)
    elif getattr(update, "callback_query", None) and update.callback_query.message:
        chat = update.callback_query.message.chat
    # Mensaje editado (por si acaso)
    elif getattr(update, "edited_message", None):
        chat = update.edited_message.chat

    return bool(chat and chat.type == "private")

# ================== BOT INFO ==================
BOT_USERNAME = None

async def init_bot_info(app):
    global BOT_USERNAME
    bot_info = await app.bot.get_me()
    BOT_USERNAME = f"@{bot_info.username}"

    # Si había webhook, elimínalo para evitar conflictos con polling
    w = await app.bot.get_webhook_info()
    if w.url:
        logging.info(f"[BOOT] Webhook activo en {w.url}. Eliminando para usar polling…")
        await app.bot.delete_webhook(drop_pending_updates=True)

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
        await update.message.reply_text("📍 Por favor, envíame tu *ubicación actual en tiempo real* desde el clip ➜ Ubicación.")
        return False
    return True

#========== validar flujo =====

async def validar_flujo(update: Update, chat_id: int) -> bool:
    ud = user_data.get(chat_id, {})
    paso = ud.get("paso")

    if paso == 0 and not update.message.text:
        await update.message.reply_text("⚠️ Aquí solo debes escribir el *nombre de la cuadrilla*. ✍️")
        return False
    
    if paso == "esperando_selfie_inicio" and not update.message.photo:
        await update.message.reply_text("📸 Aquí solo debes enviar tu foto de inicio. 🤳")
        return False
    
    if paso == "esperando_live_inicio":
        if not update.message.location or not getattr(update.message.location, "live_period", None):
            await update.message.reply_text("📍 Debes compartir tu ubicación en tiempo real.")
            return False

    if paso == "esperando_selfie_salida" and not update.message.photo:
        await update.message.reply_text("📸 Aquí solo debes enviar tu foto de salida. 🤳")
        return False
    
    if paso == "esperando_live_salida":
        if not update.message.location or not getattr(update.message.location, "live_period", None):
            await update.message.reply_text("📍 Aquí solo debes compartir tu ubicación en tiempo real. 🔴")
            return False

    # 🚦 Ajuste aquí:
    if paso is None or paso == "finalizado":
        # Ya terminó, no mostrar error
        return False

    if paso not in (0, "esperando_selfie_inicio", "esperando_live_inicio",
                    "esperando_selfie_salida", "esperando_live_salida"):
        await update.message.reply_text(
            "⚠️ Este contenido no corresponde al paso actual.\n"
            "Usa <b>/ayuda</b> si necesitas orientación.",
            parse_mode="HTML"
        )
        return False
    
    return True


# ================== COMANDOS ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.get(chat_id, {})

    if ud.get("paso") and ud.get("paso") not in (None, "finalizado"):
        paso = ud.get("paso")
        msg = PASOS.get(paso, {}).get(
            "mensaje", "⚠️ Ya tienes un registro en curso. Complétalo con /salida."
        )
        await update.message.reply_text(msg, parse_mode="HTML")
        return

    comandos = """
📌 Funciones disponibles:

/ingreso – Iniciar registro de asistencia 📝
/salida – Registrar salida final 📸
/ayuda – Mostrar instrucciones ℹ️
"""

    keyboard = []

    await update.message.reply_text(
        "👋👋 ¡Hola! Bienvenido al bot asistencia SGA - WIN 👷‍♂️👷‍♂️.\n\n" + comandos,
        parse_mode="HTML",
    )

async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return

    texto = """
👋 👋 ¡Hola! Bienvenido al bot asistencia SGA - WIN 👷‍♂️👷‍♂️\n\n
ℹ️ Instrucciones para uso del bot:

1️⃣ Usa /ingreso para registrar tu Inicio de jornada laboral 👷‍♂️ .  
   - Envía el nombre de tu cuadrilla  
   - Luego la foto de inicio de actividades 📸
   - Ubicación en tiempo real 📍  

2️⃣ Usa /salida para tu Fin de jornada laboral 👷‍♂️:  
   - Envia la foto de fin de actividades 📸  
   - Ubicación en tiempo real 📍  

‼️ El flujo es estricto, no puedes saltarte pasos. 🧐\n

"""

    await update.message.reply_text(texto, parse_mode="HTML")

# ================== INGRESO ==================
async def ingreso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.get(chat_id, {})

    # 🚦 1. Si ya está en medio de un registro y no ha hecho salida, no permitir nuevo ingreso
    if ud.get("paso") not in (None, "finalizado"):
        paso = ud.get("paso")
        msg = PASOS.get(paso, {}).get(
            "mensaje", "⚠️ Ya tienes un registro en curso. Complétalo con /salida."
        )
        await update.message.reply_text(msg, parse_mode="HTML")
        return


    # 🚦 2. Si es usuario normal (no test) y ya registró hoy, bloquear
    if chat_id not in USUARIOS_TEST and ya_registro_hoy(chat_id):
        await update.message.reply_text(
            "⚠️ Ya completaste tu registro de hoy.\n\nDebes esperar hasta mañana para iniciar uno nuevo."
        )
        return

    # ✅ 3. Caso válido: iniciar nuevo flujo
    user_data[chat_id] = {"paso": 0}
    await update.message.reply_text(
        "✍️ Escribe el <b>nombre de tu cuadrilla</b>.👷‍♂️👷‍♀️\n\n"
        "✏️ Recuerda ingresarlo como aparece en <b>PHOENIX</b>.\n\n"
        "Ejemplo:\n\n <b>D 1 WIN SGA CHRISTOPHER INGA CONTRERAS</b>\n <b>D 2 TRASLADO WIN SGA RICHARD PINEDO PALLARTA</b>",
        parse_mode="HTML"
    )


# ================== PASO 0: NOMBRE CUADRILLA ==================
async def nombre_cuadrilla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    
    # 🚦 Validación: solo aceptar TEXTO en este paso
    if not await validar_flujo(update, chat_id):
        return    
        
    ud = user_data.setdefault(chat_id, {"paso": 0})
    if ud.get("paso") != 0:
        return

    if not await validar_contenido(update, "texto"):
        return

    ud["cuadrilla"] = update.message.text.strip()
    keyboard = [
        [InlineKeyboardButton("✅ Confirmar", callback_data="confirmar_nombre")],
        [InlineKeyboardButton("✏️ Corregir ", callback_data="corregir_nombre")],
    ]
    await update.message.reply_text(
        f"¿Has ingresado correctamente el nombre de tu cuadrilla 👷‍♂️? 🤔🤔\n\n<b>{ud['cuadrilla']}</b>\n\n¿Es correcto?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def handle_nombre_cuadrilla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not es_chat_privado(update):
        return

    chat_id = query.message.chat.id
    ud = user_data.setdefault(chat_id, {})

    try:
        # feedback para confirmar que el callback llegó
        await query.answer("Creando registro…")

        if query.data == "corregir_nombre":
            ud["paso"] = 0
            ud["cuadrilla"] = ""
            await query.edit_message_text(
                "✍️ <b>Escribe el nombre de tu cuadrilla 👷‍♂️ nuevamente.</b>",
                parse_mode="HTML"
            )

        if query.data == "confirmar_nombre":
            if not ud.get("cuadrilla"):
                ud["paso"] = 0
                await query.edit_message_text("⚠️ No encontré el nombre. Escríbelo y confirma.")
                return

            # 1) Sheet global + headers
            ssid = ensure_global_spreadsheet()
            ensure_sheet_and_headers(ssid)
            logger.info(f"[FLOW] usando ssid={ssid}")

            # 2) Fila base (solo una vez)
            if not ud.get("spreadsheet_id") or not ud.get("row"):
                base = {"CUADRILLA": ud["cuadrilla"], "TIPO DE CUADRILLA": ""}
                row = append_base_row(ssid, base)
                ud["spreadsheet_id"] = ssid
                ud["row"] = row
                logger.info(f"[OK] Fila base creada: row={row}, cuadrilla='{ud['cuadrilla']}'")

            # 3) Avanza a tipo de cuadrilla
            ud["paso"] = "tipo"
            keyboard = [
                [InlineKeyboardButton("🟠 DISPONIBILIDAD", callback_data="tipo_disp")],
                [InlineKeyboardButton("⚪ REGULAR", callback_data="tipo_reg")],
            ]
            await query.edit_message_text(
                "Selecciona el *tipo de cuadrilla*:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    except Exception:
        logger.exception("[handle_nombre_cuadrilla] Error")
        try:
            await query.message.reply_text(
                "❌ Ocurrió un error creando el registro en Google Drive/Sheets.\n"
                "Vuelve a intentar con /ingreso. Revisa los logs en Render para más detalle."
            )
        except Exception:
            pass

async def debug_callback_catcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = update.callback_query.data if update.callback_query else None
        logger.info(f"[DEBUG] Callback recibido: {data}")
        # responde algo breve para confirmar que llegó el callback:
        await update.callback_query.answer("✅ Recibido")
    except Exception:
        logger.exception("[DEBUG] error en debug_callback_catcher")



# ================== TIPO DE CUADRILLA ==================
async def handle_tipo_cuadrilla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not es_chat_privado(update):
        return

    chat_id = query.message.chat.id
    ud = user_data.setdefault(chat_id, {})
    data = query.data

    try:
        await query.answer()
    except Exception:
        pass

    if data not in ("tipo_disp", "tipo_reg"):
        return

    # Guarda selección provisional (sin escribir aún en el Sheet)
    seleccion = "DISPONIBILIDAD" if data == "tipo_disp" else "REGULAR"
    ud["tipo_seleccionado"] = seleccion
    ud["paso"] = "confirmar_tipo"

    k = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirmar", callback_data="confirmar_tipo")],
        [InlineKeyboardButton("✏️ Corregir", callback_data="corregir_tipo")],
    ])
    await query.edit_message_text(
        f"Seleccionaste: <b>{seleccion}</b>.\n\n¿Es correcto?",
        parse_mode="HTML",
        reply_markup=k
    )

# ====================== CORREGIR TIPO O CONFIRMAR ===========

async def handle_confirmar_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not es_chat_privado(update):
        return

    chat_id = query.message.chat.id
    ud = user_data.setdefault(chat_id, {})
    data = query.data

    try:
        await query.answer()
    except Exception:
        pass

    if ud.get("paso") != "confirmar_tipo":
        return

    if data == "corregir_tipo":
        # Volver a elegir
        k = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟠 DISPONIBILIDAD", callback_data="tipo_disp")],
            [InlineKeyboardButton("⚪ REGULAR", callback_data="tipo_reg")],
        ])
        await query.edit_message_text("Selecciona el *tipo de cuadrilla*:", parse_mode="Markdown", reply_markup=k)
        return

    if data == "confirmar_tipo":
        ssid = ud.get("spreadsheet_id")
        row  = ud.get("row")
        if not ssid or not row:
            await query.edit_message_text("❌ No hay registro activo. Usa /ingreso para iniciar.")
            return

        tipo = ud.get("tipo_seleccionado", "")
        if not tipo:
            await query.edit_message_text("⚠️ No encontré la selección. Vuelve a elegir el tipo.")
            return

        # Escribe en Sheet y avanza a pedir selfie de inicio
        
        update_single_cell(ssid, SHEET_TITLE, COL["TIPO DE CUADRILLA"], row, tipo)
        ud["tipo"] = tipo
        ud["paso"] = "esperando_selfie_inicio"

        await query.edit_message_text(
            f"Tipificación de cuadrilla confirmada: <b>{tipo}</b>.\n\n📸 Envía tu foto de <b>Inicio con tus EPPs completos</b>.",
            parse_mode="HTML"
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

    # 1) Descargar la foto de Telegram a memoria
    photo = update.message.photo[-1]  # mayor resolución
    buff = io.BytesIO()
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        for attempt in range(3):
            try:
                await tg_file.download_to_memory(out=buff)
                break
            except Exception as e:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 * (attempt + 1))
        buff.seek(0)
    except Exception as e:
        logger.error(f"[ERROR] Descargando foto TG (inicio): {e}")
        await update.message.reply_text("⚠️ No pude leer la foto desde Telegram. Reenvíala, por favor.")
        return

    # 2) Comprimir, subir y guardar link en el Sheet
    try:
        filename = f"selfie_inicio_{datetime.now(LIMA_TZ).strftime('%Y%m%d_%H%M%S')}_{chat_id}_{row}.jpg"
        link = comprimir_y_subir(buff, filename, ssid, row, "FOTO INICIO CUADRILLA")
    except Exception:
        await update.message.reply_text("⚠️ No pude registar tu foto. Porfavor, intenta otra vez. 📸📸")
        return

    # 3) Registrar hora de ingreso
    hora = datetime.now(LIMA_TZ).strftime("%H:%M")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        update_single_cell,
        ssid,
        SHEET_TITLE,
        COL["HORA INGRESO"],
        row,
        hora
    )
    ud["hora_ingreso"] = hora
    ud["paso"] = 2
    user_data[chat_id] = ud

    await update.message.reply_text(
        f"⏱️ Hora de ingreso registrada: <b>{hora}</b>.\n\n"
        "📍 Ahora comparte tu <b>ubicación actual</b> (clip ➜ Ubicación).",
        parse_mode="HTML"
    )


# ================== UBICACIÓN INICIO / SALIDA ==================
async def manejar_ubicacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Solo chat privado y mensajes con location
    if not es_chat_privado(update) or not update.message or not update.message.location:
        return
    chat_id = update.effective_chat.id

    # 🚦 Validación: solo aceptar UBICACIÓN en este paso
    if not await validar_flujo(update, chat_id):
        return
    
    ud = user_data.setdefault(chat_id, {})
    ssid, row = ud.get("spreadsheet_id"), ud.get("row")
    if not ssid or not row:
        return

    loc = update.message.location
    lat, lon = loc.latitude, loc.longitude
    is_live = bool(getattr(loc, "live_period", None))

    # Exigir live-location (no aceptar ubicación estática)
    if not is_live:
        await update.message.reply_text(
            "⚠️ Por favor, comparte tu *ubicación en tiempo real*.\n\n"
            "Toca el clip ➜ Ubicación ➜ *Compartir ubicación en tiempo real*."
        )
        return

    # Ubicación de INICIO
    if ud.get("paso") in ("esperando_live_inicio", 2):
        update_single_cell(ssid, SHEET_TITLE, COL["LATITUD"], row, f"{lat:.6f}")
        update_single_cell(ssid, SHEET_TITLE, COL["LONGITUD"], row, f"{lon:.6f}")
        ud["paso"] = "en_jornada"   # jornada abierta hasta /salida
        user_data[chat_id] = ud

        await update.message.reply_text(
            "✅ Ubicación de inicio registrada.\n\n"
            "Recuerda que para concluir tu jornada debes usar /salida."
        )
        return

    # Ubicación de SALIDA
    if ud.get("paso") == "esperando_live_salida":
        update_single_cell(ssid, SHEET_TITLE, COL["LATITUD SALIDA"], row, f"{lat:.6f}")
        update_single_cell(ssid, SHEET_TITLE, COL["LONGITUD SALIDA"], row, f"{lon:.6f}")
        ud["paso"] = None
        user_data[chat_id] = ud

        # ✅ Marcar registro como completo (excepto usuarios de prueba)
        if chat_id not in USUARIOS_TEST:
            marcar_registro_completo(chat_id)
        
        await update.message.reply_text(
            "✅ Ubicación de salida registrada.\n\n"
            "<b> 👷‍♂️🦺 Salida registrada. Que tengas un buen regreso a casa. 🏠 </b>",
            parse_mode="HTML"
        )



# ================== SALIDA ==================

async def salida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.setdefault(chat_id, {})

    # 🚦 Validar pasos obligatorios antes de permitir salida
    if not ud.get("cuadrilla"):
        await update.message.reply_text("⚠️ No puedes registrar salida todavía. Te falta escribir el <b>nombre de la cuadrilla ✍️</b>", parse_mode="HTML")
        return 

    if not ud.get("hora_ingreso"):
        await update.message.reply_text("⚠️ No puedes registrar salida todavía. Te falta tu <b>selfie de inicio 📸</b>", parse_mode="HTML")
        return
    
    if ud.get("paso") in ("esperando_live_inicio", 0, "confirmar_selfie_inicio"):
        await update.message.reply_text("⚠️ No puedes registrar salida todavía. Te falta tu <b>ubicación en tiempo real 📍</b>", parse_mode="HTML")
        return
    
    # 🚦 Si ya está finalizado, bloquear
    if ud.get("paso") == "finalizado":
        await update.message.reply_text("✅ Ya completaste tu registro hoy. No puedes registrar otra salida.")
        return
    
    # ✅ Si cumplió con lo mínimo → permitir selfie de salida
    ssid, row = ud.get("spreadsheet_id"), ud.get("row")
    if not ssid or not row:
        await update.message.reply_text("⚠️ No hay jornada activa. Usa /ingreso para iniciar.")
        return
    
    ud["paso"] = "esperando_selfie_salida"
    await update.message.reply_text("📸 Envía tu foto de <b>fin de labores con tus EPPs completos</b>.\n Para finalizar tu jornada. 🏠", parse_mode="HTML")


async def selfie_salida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ud = user_data.get(chat_id) or {}
    if ud.get("paso") != "selfie_salida":
        return
    if not await validar_contenido(update, "foto"):
        return

    ssid, row = ud.get("spreadsheet_id"), ud.get("row")
    if not ssid or not row:
        await update.message.reply_text("❌ No hay registro activo. Usa /ingreso para iniciar.")
        return

    # 1) Descargar a memoria
    photo = update.message.photo[-1]
    buff = io.BytesIO()
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        for attempt in range(3):
            try:
                await tg_file.download_to_memory(out=buff)
                break
            except Exception as e:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 * (attempt + 1))
        buff.seek(0)
    except Exception as e:
        logger.error(f"[ERROR] Descargando foto TG (salida): {e}")
        await update.message.reply_text("⚠️ No pude leer la foto desde Telegram. Reenvíala, por favor.")
        return

    # 2) Comprimir, subir y guardar link
    try:
        filename = f"selfie_salida_{datetime.now(LIMA_TZ).strftime('%Y%m%d_%H%M%S')}_{chat_id}_{row}.jpg"
        link = comprimir_y_subir(buff, filename, ssid, row, "FOTO FIN CUADRILLA")
    except Exception:
        await update.message.reply_text("⚠️ No pude subir la foto de salida a Drive. Reenvíala para continuar.")
        return

    # 3) Registrar hora salida y pedir ubicación final
    hora = datetime.now(LIMA_TZ).strftime("%H:%M")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        update_single_cell,
        ssid,
        SHEET_TITLE,
        COL["HORA SALIDA"],
        row,
        hora
    )
    ud["paso"] = "ubicacion_salida"
    user_data[chat_id] = ud

    await update.message.reply_text(
        f"⏱️ Hora de salida registrada: <b>{hora}</b>.\n\n"
        "📍 Comparte tu <b>ubicación actual</b> para finalizar.",
        parse_mode="HTML"
    )


# ================== ROUTER DE FOTOS ==================

async def manejar_fotos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.effective_chat.id
        paso = user_data.get(chat_id, {}).get("paso")
        
    # 🚦 Validación: solo aceptar FOTO en este paso
        if not await validar_flujo(update, chat_id):
            return
        if update.message.reply_to_message:
            if update.message.reply_to_message.message_id == user_data.get(chat_id, {}).get("msg_id_motivador"):
                return

        # Selfie de INICIO -> capturamos y pedimos confirmación
        if paso == "esperando_selfie_inicio":
            photo = update.message.photo[-1]
            ud = user_data.setdefault(chat_id, {})
            ud["pending_selfie_inicio_file_id"] = photo.file_id
            ud["paso"] = "confirmar_selfie_inicio"
            k = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmar", callback_data="confirmar_selfie_inicio")],
                [InlineKeyboardButton("🔄 Corregir", callback_data="repetir_selfie_inicio")],
            ])
            await update.message.reply_text("¿Usamos esta foto para iniciar actividades?\n\n ⚠️ Importante: Despues de brindar la confirmación.\n\n ⏳ Debemos esperar como minimo 8 seg. para continuar.", reply_markup=k)
            return

        # Selfie de SALIDA -> capturamos y pedimos confirmación
        if paso == "esperando_selfie_salida":
            photo = update.message.photo[-1]
            ud = user_data.setdefault(chat_id, {})
            ud["pending_selfie_salida_file_id"] = photo.file_id
            ud["paso"] = "confirmar_selfie_salida"
            k = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmar", callback_data="confirmar_selfie_salida")],
                [InlineKeyboardButton("🔄 Corregir", callback_data="repetir_selfie_salida")],
            ])
            await update.message.reply_text("¿Usamos esta foto para finalizar actividades?\n\n ⚠️ Importante: Despues de brindar la confirmación.\n\n ⏳ Debemos esperar como minimo 8 seg. para finalizar tu registro.", reply_markup=k)
            return

        # Flujo viejo (por si llega foto fuera de lugar)
        if paso == 1:
            await foto_ingreso(update, context)  # si aún usas este camino
        elif paso == 2:
            await foto_ats(update, context)      # si aún usas ATS en tu versión
        elif paso == "selfie_salida":
            await selfie_salida(update, context)
        else:
            await update.message.reply_text("⚠️ No es momento de enviar fotos.\n\n Usa /ingreso para comenzar.")
    except Exception as e:
        logger.error(f"[ERROR] manejar_fotos: {e}")

#============= FUERA DE LUGAR ===========================

async def filtro_comandos_fuera_de_lugar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
        
    # --- Otros comandos bloqueados ---
    await update.message.reply_text(
        "⚠️ Comando no permitido en este momento.\n"
        "Usa <b>/ayuda</b> para más información.",
        parse_mode="HTML"
    )


# ============= CONFIRMAR SELFIE INICIO & SALIDA =========

async def handle_confirmar_selfie_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not es_chat_privado(update):
        return
    chat_id = query.message.chat.id
    ud = user_data.setdefault(chat_id, {})
    try:
        await query.answer()
    except Exception:
        pass

    if query.data == "repetir_selfie_inicio":
        ud["pending_selfie_inicio_file_id"] = None
        ud["paso"] = "esperando_selfie_inicio"
        await query.edit_message_text("🔄 *Envía nuevamente tu foto de inicio de actividades.*", parse_mode="Markdown")
        return

    if query.data == "confirmar_selfie_inicio":
        ssid, row = ud.get("spreadsheet_id"), ud.get("row")
        fid = ud.get("pending_selfie_inicio_file_id")
        if not (ssid and row and fid):
            await query.edit_message_text("❌ *Falta foto de inicio de actividades.*")
            return
        try:
            filename = f"selfie_inicio_{datetime.now(LIMA_TZ).strftime('%Y%m%d_%H%M%S')}_{chat_id}_{row}.jpg"
            tg_file = await context.bot.get_file(fid)
            buff = io.BytesIO()
            await tg_file.download_to_memory(out=buff)
            buff.seek(0)

            loop = asyncio.get_running_loop()
            link = await loop.run_in_executor(
                None,
                lambda: comprimir_y_subir(buff, filename, ssid, row, "FOTO INICIO CUADRILLA")
            )

            # Hora de ingreso
            hora = datetime.now(LIMA_TZ).strftime("%H:%M")
            update_single_cell(ssid, SHEET_TITLE, COL["HORA INGRESO"], row, hora)
            ud["hora_ingreso"] = hora

            # Pedir ubicación en tiempo real
            ud["paso"] = "esperando_live_inicio"
            ud["pending_selfie_inicio_file_id"] = None

            await query.edit_message_text(
                f"✅ Fotografía registrada. ⏱️ Hora de inicio: <b>{hora}</b>.\n\n"
                "📍 Ahora envía tu <b>ubicación en tiempo real</b>\n\n(Elige “Compartir ubicación en tiempo real” 📍).",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"[ERROR] confirm_selfie_inicio upload: {e}")
            await query.edit_message_text("⚠️ No pude registra tu foto.\n Reintenta enviando tu foto nuevamente.")

async def handle_confirmar_selfie_salida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not es_chat_privado(update):
        return

    chat_id = query.message.chat.id
    ud = user_data.setdefault(chat_id, {})

    try:
        await query.answer()
    except Exception:
        pass

    # --- Caso: repetir selfie ---
    if query.data == "repetir_selfie_salida":
        ud["pending_selfie_salida_file_id"] = None
        ud["paso"] = "esperando_selfie_salida"
        await query.edit_message_text(
            "🔄 Envía nuevamente tu <b>selfie de salida</b>.",
            parse_mode="HTML"
        )
        return

    # --- Caso: confirmar selfie ---
    if query.data == "confirmar_selfie_salida":
        ssid, row = ud.get("spreadsheet_id"), ud.get("row")
        fid = ud.get("pending_selfie_salida_file_id")

        if not (ssid and row and fid):
            await query.edit_message_text("❌ Faltan fotos o registro. Usa /salida para iniciar cierre.")
            return

        try:
            filename = f"selfie_salida_{datetime.now(LIMA_TZ).strftime('%Y%m%d_%H%M%S')}_{chat_id}_{row}.jpg"

            # Descargar de Telegram
            tg_file = await context.bot.get_file(fid)
            buff = io.BytesIO()
            await tg_file.download_to_memory(out=buff)
            buff.seek(0)

            # Procesar (comprimir + subir a Drive) en un executor
            loop = asyncio.get_running_loop()
            link = await loop.run_in_executor(
                None,
                lambda: comprimir_y_subir(buff, filename, ssid, row, "FOTO FIN CUADRILLA")
            )

            # Registrar hora de salida
            hora = datetime.now(LIMA_TZ).strftime("%H:%M")
            update_single_cell(ssid, SHEET_TITLE, COL["HORA SALIDA"], row, hora)

            # Avanzar paso
            ud["paso"] = "esperando_live_salida"
            ud["pending_selfie_salida_file_id"] = None

            await query.edit_message_text(
                f"✅ Fotografía registrada. ⏱️ Hora de salida: <b>{hora}</b>.\n\n"
                "📍 Ahora envía tu <b>ubicación en tiempo real</b>\n\n"
                "(Elige “Compartir ubicación en tiempo real” 📍).",
                parse_mode="HTML"
            )

        except Exception as e:
            logger.error(f"[ERROR] confirm_selfie_salida upload: {e}")
            await query.edit_message_text(
                "⚠️ No pude subir la foto a Drive.\n"
                "Reenvíala, por favor."
            )

# ================== CALLBACKS / AYUDA (placeholder) ==================
async def manejar_repeticiones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass

async def handle_ayuda_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not es_chat_privado(update):
        return

    await query.answer()
    await query.edit_message_text(
        "⚠️⚠️ <b>¡Usa los comandos o botones para registrar tu asistencia paso a paso!</b>\n\n"
        "Comienza con /ingreso y sigue la secuencia para que tu asistencia se registre correctamente. ✅✅",
        parse_mode="HTML"
    )


# ================== MAIN ==================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = init_bot_info

    # --- DEBUG: atrapa cualquier callback primero ---
    app.add_handler(CallbackQueryHandler(debug_callback_catcher, block=False), group=-1)

    # --- COMANDOS ---
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(CommandHandler("ingreso", ingreso))
    app.add_handler(CommandHandler("salida", salida))
    app.add_handler(MessageHandler(filters.COMMAND, filtro_comandos_fuera_de_lugar),group=1)

    # --- MENSAJES ---
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, nombre_cuadrilla))
    app.add_handler(MessageHandler(filters.PHOTO, manejar_fotos))
    app.add_handler(MessageHandler(filters.LOCATION, manejar_ubicacion))

    # --- CALLBACKS REALES ---
    app.add_handler(CallbackQueryHandler(handle_ayuda_callback, pattern="^ayuda$"))
    app.add_handler(CallbackQueryHandler(handle_confirmar_selfie_inicio, pattern="^(confirmar_selfie_inicio|repetir_selfie_inicio)$"))
    app.add_handler(CallbackQueryHandler(handle_confirmar_selfie_salida, pattern="^(confirmar_selfie_salida|repetir_selfie_salida)$"))
    app.add_handler(CallbackQueryHandler(handle_confirmar_tipo, pattern="^(confirmar_tipo|corregir_tipo)$"))
    app.add_handler(CallbackQueryHandler(handle_nombre_cuadrilla, pattern="^(confirmar_nombre|corregir_nombre)$"))
    app.add_handler(CallbackQueryHandler(handle_tipo_cuadrilla, pattern="^tipo_(disp|reg)$"))
    app.add_handler(CallbackQueryHandler(manejar_repeticiones, pattern="^repetir_"))

    # --- ERRORES ---
    app.add_error_handler(log_error)

    # --- ARRANQUE EN POLLING ---
    logger.info("🚀 Bot de Asistencia (privado) en ejecución...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
