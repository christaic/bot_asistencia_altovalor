import uuid
import asyncio
import re
import os
import io
import gc
import json
import logging
from datetime import datetime
from datetime import date
from PIL import Image
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
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone



#== RESET REGISTRO 00:00==

async def resetear_registros():
    """Limpia user_data y registro_diario cada día a las 00:00"""
    global user_data, registro_diario
    user_data.clear()
    registro_diario.clear()
    logger.info("🧹 Limpieza diaria ejecutada: user_data y registro_diario reiniciados.")

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
        col = COL.get(header)
        if col:
            update_single_cell(ssid, SHEET_TITLE, col, row, link)
        else:
            logger.error(f"[ERROR] Header '{header}' no encontrado en COL")


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


load_dotenv()

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

# ================== GOOGLE MAPS API ==================
GOOGLE_MAPS_API_KEY = "AIzaSyCLcEElUO_4khY4DmNeOLpqutk-yVFHF7c"


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

MAIN_FOLDER_ID = "1OKL_s5Qs8VXbmhWFPDiJBqaaQArKQGG7"

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

def ensure_asistencia_cuadrillas_v1():
    """
    Verifica que el archivo 'ASISTENCIA_CUADRILLAS_DISP_ALTO_VALOR' exista dentro de la carpeta principal.
    Devuelve su file_id listo para escribir con los HEADERS globales.
    Si no existe, lanza advertencia y no crea nada nuevo.
    """
    nombre_archivo = "ASISTENCIA_CUADRILLAS_DISP_ALTO_VALOR"
    query = (
        f"name='{nombre_archivo}' and '{MAIN_FOLDER_ID}' in parents and "
        f"mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
    )

    res = drive_service.files().list(
        q=query,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()

    files = res.get("files", [])

    if not files:
        logger.error("❌ No se encontró el archivo 'ASISTENCIA_CUADRILLAS_DISP_ALTO_VALOR' en Drive. Verifica que exista.")
        return None

    ssid = files[0]["id"]
    logger.info(f"📄 Archivo '{nombre_archivo}' encontrado en Drive (ID={ssid}).")

    # ✅ Verificar encabezados (solo si quieres asegurarte que están correctos)
    try:
        sheets_service.spreadsheets().values().update(
            spreadsheetId=ssid,
            range="A1:V1",
            valueInputOption="RAW",
            body={"values": [HEADERS]},
        ).execute()
        logger.info(f"🧾 Encabezados verificados/actualizados en '{nombre_archivo}'.")
    except Exception as e:
        logger.warning(f"⚠️ No se pudieron escribir encabezados en '{nombre_archivo}': {e}")

    return ssid


# ---- Subida de imagen a Drive y enlace clicable ----

def upload_image_and_get_link(image_bytes: io.BytesIO, filename: str, max_retries: int = 3) -> str:
    """
    Sube una imagen a la carpeta IMAGENES y devuelve un enlace webViewLink.
    Usa subida fragmentada (resumable) con reintentos para evitar timeouts en Render.
    """
    image_bytes.seek(0)

    for intento in range(max_retries):
        try:
            # Subida en chunks de 256 KB
            media = MediaIoBaseUpload(
                image_bytes,
                mimetype="image/jpeg",
                resumable=True,
                chunksize=256 * 1024
            )
            metadata = {"name": filename, "parents": [IMAGES_FOLDER_ID]}

            request = drive_service.files().create(
                body=metadata,
                media_body=media,
                fields="id, webViewLink",
                supportsAllDrives=True
            )

            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    logger.info(f"[UPLOAD] Progreso: {int(status.progress() * 100)}%")

            file_id = response["id"]
            try:
                drive_service.permissions().create(
                    fileId=file_id,
                    body={"type": "anyone", "role": "reader"},
                    fields="id",
                    supportsAllDrives=True
                ).execute()
            except Exception as e:
                logger.warning(f"[WARN] No se pudo abrir a 'cualquiera con el enlace': {e}. El link puede requerir acceso.")

            return response.get("webViewLink")

        except Exception as e:
            logger.error(f"[UPLOAD] Error intento {intento+1}/{max_retries}: {e}")
            if intento == max_retries - 1:
                raise
            import time; time.sleep(2 * (intento + 1))  # backoff exponencial

def buscar_datos_cuadrilla(codigo: str):
    """
    Busca el código en la hoja CUADRILLAS ACTIVAS y devuelve un dict con
    CUADRILLA, PROVEEDOR, ZONA si lo encuentra. Caso contrario, None.
    """
    try:
        archivo = buscar_archivo_en_drive("CUADRILLAS ACTIVAS", SHEET_MIME)
        if not archivo:
            logger.error("❌ No se encontró el archivo 'CUADRILLAS ACTIVAS' en Drive.")
            return None

        ssid = archivo["id"]
        rango = "A:W"  # buscamos hasta la columna W
        data = sheets_service.spreadsheets().values().get(
            spreadsheetId=ssid, range=rango
        ).execute()

        values = data.get("values", [])
        for fila in values:
            if len(fila) < 23 or not fila[0].strip():  # asegurar que llega hasta W
                continue
            if fila[0].strip() == str(codigo).strip():  # Columna A: código
                cuadrilla = fila[1] if len(fila) > 1 else ""
                proveedor = fila[11] if len(fila) > 11 else ""
                zona = fila[22] if len(fila) > 22 else ""
                return {"CUADRILLA": cuadrilla, "PROVEEDOR": proveedor, "ZONA": zona}

        logger.warning(f"[CUADRILLAS] Código {codigo} no encontrado.")
        return None
    except Exception as e:
        logger.error(f"[ERROR] buscar_datos_cuadrilla: {e}")
        return None

import requests

def obtener_ubicacion_detallada(lat, lon):
    """
    Devuelve un dict con departamento, provincia y distrito usando Google Geocoding API.
    """
    try:
        url = f"https://maps.googleapis.com/maps/api/geocode/json?latlng={lat},{lon}&key={GOOGLE_MAPS_API_KEY}&language=es"
        resp = requests.get(url)
        data = resp.json()

        if not data.get("results"):
            return {"departamento": "", "provincia": "", "distrito": ""}

        # Buscar componentes administrativos
        components = data["results"][0]["address_components"]
        departamento = provincia = distrito = ""

        for comp in components:
            if "administrative_area_level_1" in comp["types"]:
                departamento = comp["long_name"]
            elif "administrative_area_level_2" in comp["types"]:
                provincia = comp["long_name"]
            elif "locality" in comp["types"] or "sublocality" in comp["types"]:
                distrito = comp["long_name"]

        return {
            "departamento": departamento,
            "provincia": provincia,
            "distrito": distrito
        }

    except Exception as e:
        logger.error(f"❌ Error obteniendo ubicación detallada: {e}")
        return {"departamento": "", "provincia": "", "distrito": ""}

# ================== GOOGLE SHEETS ==================
SHEET_TITLE = "Registros"

# ================== CABECERAS PRINCIPALES ==================
HEADERS = [
    "ID_REGISTRO",
    "USER_ID",
    "FECHA",
    "ID_PHOENIX",
    "CUADRILLA",
    "PROVEEDOR",
    "ZONA",
    "TIPO DE CUADRILLA",
    "FOTO INICIO CUADRILLA",
    "LATITUD",
    "LONGITUD",
    "DEPARTAMENTO",
    "PROVINCIA",
    "DISTRITO",
    "HORA INGRESO",
    "HORA SALIDA",
    "FOTO FIN CUADRILLA",
    "LATITUD SALIDA",
    "LONGITUD SALIDA",
    "DEPARTAMENTO SALIDA",
    "PROVINCIA SALIDA",
    "DISTRITO SALIDA"
]


# ================== MAPA DE COLUMNAS ==================
COL = {
    "ID_REGISTRO": "A",
    "USER_ID": "B",
    "FECHA": "C",
    "ID_PHOENIX": "D",
    "CUADRILLA": "E",
    "PROVEEDOR": "F",
    "ZONA": "G",
    "TIPO DE CUADRILLA": "H",
    "FOTO INICIO CUADRILLA": "I",
    "LATITUD": "J",
    "LONGITUD": "K",
    "DEPARTAMENTO": "L",
    "PROVINCIA": "M",
    "DISTRITO": "N",
    "HORA INGRESO": "O",
    "HORA SALIDA": "P",
    "FOTO FIN CUADRILLA": "Q",
    "LATITUD SALIDA": "R",
    "LONGITUD SALIDA": "S",
    "DEPARTAMENTO SALIDA": "T",
    "PROVINCIA SALIDA": "U",
    "DISTRITO SALIDA": "V",
}


PASOS = {
    "esperando_cuadrilla": {
        "mensaje": "🧐🧐 Aquí debes escribir el ID Phoenix de tu cuadrilla.👷‍♂️👷‍♀️\n\n""✏️ Recuerda ingresar tu ID PHOENIX.\n\n"
        "Ejemplo:\n\n0\n11\n9999"
    },
    "confirmar_nombre": {
        "mensaje": "👉 Confirma o corrige el ID Phoenix de tu cuadrilla usando los botones. 👇"
    },
    "confirmar_tipo": {
        "mensaje": "👉 Confirma o corrige el <b>tipo de cuadrilla</b> usando los botones. 👇 "
    },
    "tipo": {
        "mensaje": "📌 Selecciona el <b>tipo de cuadrilla</b> usando los botones. 👇"
    },
    "esperando_selfie_inicio": {
        "mensaje": "📸 Aquí solo debes enviar tu foto de inicio con tus EPPs completos. 👷‍♂️👷‍♀️"
    },
    "confirmar_selfie_inicio": {
        "mensaje": "👉 Confirma o corrige la foto de inicio usando los botones. 👇"
    },
    "esperando_live_inicio": {
        "mensaje": "📍 Comparte tu ubicación en tiempo real para continuar. 💪"
    },
    "en_jornada": {
        "mensaje": "🚀 Estás en jornada. Usa /salida para registrar tu fin de labores. 🔥"
    },
    "esperando_selfie_salida": {
        "mensaje": "📸 Aquí solo debes enviar tu foto de salida con tus EPPs completos. 👷‍♂️👷‍♀️"
    },
    "confirmar_selfie_salida": {
        "mensaje": "👉 Confirma o corrige la foto de salida usando los botones. 👇"
    },
    "esperando_live_salida": {
        "mensaje": "📍 Comparte tu ubicación en tiempo real para finalizar tu jornada. 💪"
    },
    "finalizado": {
        "mensaje": "✅✅ Registro completado.\nNos vemos mañana crack. 🤝🤝👷‍♂️👷‍♀️"
    }
}


def dentro_horario_laboral() -> bool:
    """True si la hora actual está dentro de 07:00 - 23:59 Lima."""
    ahora = datetime.now(LIMA_TZ).time()
    return datetime.strptime("07:00", "%H:%M").time() <= ahora <= datetime.strptime("23:59", "%H:%M").time()


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
        range=f"{SHEET_TITLE}!A1:V1"
    ).execute()
    row = vr.get("values", [])
    if not row or row[0] != HEADERS:
        sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{SHEET_TITLE}!A1:V1",
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

def append_base_row(spreadsheet_id: str, data: dict, chat_id: int) -> int:
    """
    Inserta nueva fila base y devuelve el número de fila insertada.
    """
    ahora = datetime.now(LIMA_TZ)
    id_registro = str(uuid.uuid4())
    
    payload = {
        "ID_REGISTRO": id_registro,
        "USER_ID": str(chat_id),
        "FECHA": ahora.strftime("%Y-%m-%d"),
        "ID_PHOENIX": data.get("ID_PHOENIX", ""),
        "CUADRILLA": data.get("CUADRILLA", ""),
        "PROVEEDOR": data.get("PROVEEDOR", ""),
        "ZONA": data.get("ZONA", ""),
        "TIPO DE CUADRILLA": data.get("TIPO DE CUADRILLA", ""),
        "FOTO INICIO CUADRILLA": "",
        "LATITUD": "",
        "LONGITUD": "",
        "HORA INGRESO": data.get ("HORA INGRESO", ""),
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

    row_num = _parse_row_from_updated_range(resp["updates"]["updatedRange"])

    # Guardar en memoria
    ud = user_data.setdefault(chat_id, {})
    ud["id_registro"] = id_registro
    ud["row"] = row_num
    ud["spreadsheet_id"] = spreadsheet_id

    return row_num


#=============== ID USUARIO ==================

def find_active_row(spreadsheet_id: str, id_registro: str) -> int | None:
    """
    Busca en Google Sheets la fila que contiene el ID_REGISTRO dado.
    Devuelve el número de fila (int) o None si no existe.
    """
    try:
        resp = sheets_service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{SHEET_TITLE}!A:A",  # Columna A: donde está ID_REGISTRO
        ).execute()

        values = resp.get("values", [])
        for idx, row in enumerate(values, start=1):
            if row and row[0] == id_registro:  # Col A contiene ID_REGISTRO
                return idx

    except Exception as e:
        logger.error(f"[ERROR] find_active_row({id_registro}): {e}")

    return None




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


#================= MUESTRA BOTONERA SEGUN PASO ===============

def mostrar_botonera(paso: str):

    if paso == "confirmar_nombre":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirmar", callback_data="confirmar_nombre")],
            [InlineKeyboardButton("✏️ Corregir", callback_data="corregir_nombre")]
        ])
    
    if paso == "tipo":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🟠 DISPONIBILIDAD", callback_data="tipo_disp")],
            [InlineKeyboardButton("⚪ REGULAR", callback_data="tipo_reg")]
        ])    
    
    if paso == "confirmar_tipo":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirmar", callback_data="confirmar_tipo")],
            [InlineKeyboardButton("🔄 Corregir", callback_data="corregir_tipo")]
        ])
    
    if paso == "confirmar_selfie_inicio":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirmar", callback_data="confirmar_selfie_inicio")],
            [InlineKeyboardButton("🔄 Repetir", callback_data="repetir_selfie_inicio")]
        ])
    
    if paso == "confirmar_selfie_salida":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirmar", callback_data="confirmar_selfie_salida")],
            [InlineKeyboardButton("🔄 Repetir", callback_data="repetir_selfie_salida")]
        ])
    
    return None


#====================== ESTADO =================
async def estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ud = user_data.get(chat_id, {})
    paso = ud.get("paso")

    if paso is None or paso == "finalizado":
        await update.message.reply_text(
            "⚠️ No tienes un registro activo.\nUsa /ingreso para iniciar tu jornada.",
            parse_mode="HTML"
        )
        return

    # ✅ Recuperar mensaje del paso actual
    if paso in PASOS:
        msg = PASOS[paso]["mensaje"]
    else:
        logger.warning(f"[ESTADO] Paso desconocido: {paso}")
        msg = f"⚠️ Estás en un paso no reconocido: <b>{paso}</b>.\nUsa /ingreso para reiniciar el flujo."

    # ✅ Mostrar botonera si corresponde
    kb = mostrar_botonera(paso)
    if kb:
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=kb)
    else:
        await update.message.reply_text(msg, parse_mode="HTML")


# ================== VALIDACIONES ==================
async def validar_contenido(update: Update, tipo: str):
    if tipo == "texto" and not update.message.text:
        await update.message.reply_text("⚠️ Debes enviar el ID Phoenix de tu cuadrilla en texto. ✍️")
        return False
    if tipo == "foto" and not update.message.photo:
        await update.message.reply_text("⚠️ Debes enviar una foto, no texto. 🤳")
        return False
    if tipo == "ubicacion" and not update.message.location:
        await update.message.reply_text("📍 Por favor, envíame tu ubicación actual en tiempo real desde el clip ➜ Ubicación.")
        return False
    return True

#========== validar flujo =====

async def validar_flujo(update: Update, chat_id: int) -> bool:
    ud = user_data.get(chat_id, {})
    paso = ud.get("paso")

        # 🚦 Si ya terminó
    if paso is None or paso == "finalizado":
        await update.message.reply_text(
            "✅ Ya completaste tu registro hoy. \n\nMañana podrás iniciar uno nuevo con /ingreso.💪💪",
            parse_mode="HTML"
        )
        return False

    # Paso 0 → solo texto
    if paso == "esperando_cuadrilla" and not update.message.text:
        await update.message.reply_text(
            PASOS["esperando_cuadrilla"]["mensaje"], parse_mode="HTML"
        )
        return False
    
    # Selfie inicio → solo foto
    if paso == "esperando_selfie_inicio" and not update.message.photo:
        await update.message.reply_text(
            PASOS["esperando_selfie_inicio"]["mensaje"], parse_mode="HTML"
        )
        return False

    # Ubicación inicio → solo live location
    if paso == "esperando_live_inicio":
        if not update.message.location or not getattr(update.message.location, "live_period", None):
            await update.message.reply_text(
                PASOS["esperando_live_inicio"]["mensaje"], parse_mode="HTML"
            )
            return False

    # Selfie salida → solo foto
    if paso == "esperando_selfie_salida" and not update.message.photo:
        await update.message.reply_text(
            PASOS["esperando_selfie_salida"]["mensaje"], parse_mode="HTML"
        )
        return False

    # Ubicación salida → solo live location
    if paso == "esperando_live_salida":
        if not update.message.location or not getattr(update.message.location, "live_period", None):
            await update.message.reply_text(
                PASOS["esperando_live_salida"]["mensaje"], parse_mode="HTML"
            )
            return False

     # 🔒 Si el paso requiere botones → bloquear texto/fotos/ubicación hasta que responda
    if paso in ("confirmar_nombre","tipo", "confirmar_tipo", "confirmar_selfie_inicio", "confirmar_selfie_salida"):
        kb = mostrar_botonera(paso)
        if kb:
            await update.message.reply_text(
                "⚠️ Usa los botones para confirmar o corregir. 👇",
                reply_markup=kb,
                parse_mode="HTML"
            )
        return False  

    # Cualquier otro contenido fuera de lugar
    if paso not in ("esperando_cuadrilla", "esperando_selfie_inicio", "esperando_live_inicio",
                    "esperando_selfie_salida", "esperando_live_salida",
                    "confirmar_nombre", "confirmar_tipo",
                    "confirmar_selfie_inicio", "confirmar_selfie_salida"):
        await update.message.reply_text(
            f"⚠️ Este contenido no corresponde al paso actual.\n\n"
            f"📍 Paso en curso: <b>{PASOS.get(paso, {}).get('mensaje', paso)}</b>",
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
    paso = ud.get("paso")

    # 🚦 Caso: ya está en un flujo activo
    if paso and paso not in (None, "finalizado"):
        msg = PASOS.get(paso, {}).get(
            "mensaje",
            "⚠️ Ya tienes un registro en curso.\n\n"
            "Para ver el estado de tu registro presiona:\n"
            "🆘 /estado para ayudarte en qué paso te encuentras. \n"
            "🛫 /salida para finalizar jornada."
        )

        kb = mostrar_botonera(paso)
        if kb:
            await update.message.reply_text(msg, parse_mode="HTML", reply_markup=kb)
        else:
            await update.message.reply_text(msg, parse_mode="HTML")
        return

    # 🚀 Bienvenida general
    comandos = (
        "👋 ¡Hola, técnico WIN SGA! Bienvenido al bot de asistencia 👷‍♂️👷‍♀️\n\n"
        "🧾 <b>Cómo funciona:</b>\n\n"
        "1️⃣ Usa /ingreso para registrar tu <b>Inicio de jornada laboral</b>.\n"
        "   - Ingresa tu <b>ID_PHOENIX</b> 🪪 (código de 1 a 4 dígitos de tu cuadrilla).\n"
        "   - El bot cargará automáticamente tus datos (cuadrilla, proveedor, zona).\n\n"
        "2️⃣ Luego selecciona tu <b>tipo de cuadrilla</b> 🟠 DISPONIBILIDAD o ⚪ REGULAR.\n\n"
        "3️⃣ Envía tus fotos y ubicación en tiempo real cuando se te indique 📸📍.\n\n"
        "4️⃣ Usa /salida para registrar el <b>Fin de jornada laboral</b> 🏁.\n\n"
        "📌 Puedes usar /estado en cualquier momento para saber en qué paso estás 💪.\n\n"
        "⚙️ El flujo es automático, no te preocupes. Yo te iré guiando paso a paso 😉"
    )

    await update.message.reply_text(comandos, parse_mode="HTML")


# ================== COMANDO /AYUDA ==================
async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return

    texto = (
        "🧾 <b>ASISTENCIA DIGITAL - WIN SGA</b> 👷‍♂️👷‍♀️\n\n"
        "💡 <b>Guía rápida de uso del bot:</b>\n\n"
        "1️⃣ Usa /ingreso para registrar tu <b>Inicio de jornada laboral</b>.\n"
        "   ➤ Ingresa tu <b>ID_PHOENIX</b> 🪪 (código de 1 a 4 dígitos de tu cuadrilla).\n"
        "   ➤ El bot cargará automáticamente tus datos: cuadrilla, proveedor y zona.\n\n"
        "2️⃣ Luego selecciona el <b>tipo de cuadrilla</b> 🟠 DISPONIBILIDAD o ⚪ REGULAR.\n\n"
        "3️⃣ Envía tus fotos y ubicación en tiempo real cuando se te indique 📸📍.\n\n"
        "4️⃣ Usa /salida para registrar tu <b>Fin de jornada</b> 🏁.\n\n"
        "📌 En cualquier momento puedes usar /estado para ver en qué paso estás 💪.\n\n"
        "⚙️ Todo el flujo es automático, no puedes saltar pasos — solo sigue las indicaciones 😉.\n\n"
        "📢 Si necesitas soporte adicional, contacta con el área de supervisión técnica WIN."
    )

    await update.message.reply_text(texto, parse_mode="HTML")



# ================== INGRESO ==================

async def ingreso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return

    chat_id = update.effective_chat.id
    ud = user_data.get(chat_id, {})

    # 🚦 Validar horario laboral
    if chat_id not in USUARIOS_TEST and not dentro_horario_laboral():
        await update.message.reply_text(
            "⚠️ Solo puedes registrar tu asistencia entre las <b>07:00 AM y 11:59 PM</b>.",
            parse_mode="HTML"
        )
        return

    # 🚦 Si ya está en medio de un registro
    if ud.get("paso") not in (None, "finalizado"):
        paso = ud.get("paso")
        msg = PASOS.get(paso, {}).get(
            "mensaje",
            "⚠️ Ya tienes un registro en curso.\n\n"
            "Usa /estado para saber en qué paso estás 💪 o /salida para finalizar tu jornada 🏁"
        )
        await update.message.reply_text(msg, parse_mode="HTML")
        return

    # 🚦 Si ya registró hoy (y no es usuario test)
    if chat_id not in USUARIOS_TEST and ya_registro_hoy(chat_id):
        await update.message.reply_text(
            "⚠️ Ya completaste tu registro de hoy.\n\nDebes esperar hasta mañana para iniciar uno nuevo. 🌅"
        )
        return

    # ✅ Inicio de flujo: pedir ID_PHOENIX
    user_data[chat_id] = {"paso": "esperando_cuadrilla"}

    await update.message.reply_text(
        "✍️ Hola, comencemos con tu registro 👷‍♂️👷‍♀️\n\n"
        "Por favor, ingresa tu <b>ID PHOENIX</b> 🪪 (código de 1 a 4 dígitos asignado a tu cuadrilla).\n\n"
        "Ejemplo:\n\n<b>0</b>\n<b>11</b>\n<b>9999</b>\n\n"
        "✏️ Con este código tendré tus datos registrados 📘",
        parse_mode="HTML"
    )

# ================== PASO 0: ID_PHOENIX ==================
async def nombre_cuadrilla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id

    texto = update.message.text.strip()

    # 🚦 Validar que sea un código de 4 dígitos
    if not re.fullmatch(r"\d{1,4}", texto):
        await update.message.reply_text(
            "⚠️ Debes ingresar tu <b>ID PHOENIX</b> (código de 1 a 4 dígitos numéricos).",
            parse_mode="HTML"
        )
        return

    # 🔍 Buscar el código en la hoja CUADRILLAS ACTIVAS
    datos = buscar_datos_cuadrilla(texto)
    if not datos:
        await update.message.reply_text(
            "❌ No encontré ese ID_PHOENIX en el registro de cuadrillas activas.\n"
            "Verifica el código y vuelve a intentarlo.",
            parse_mode="HTML"
        )
        return

    # ✅ Guardar datos obtenidos en memoria
    ud = user_data.setdefault(chat_id, {})
    ud.update({
        "id_phoenix": texto,
        "cuadrilla": datos["CUADRILLA"],
        "proveedor": datos["PROVEEDOR"],
        "zona": datos["ZONA"],
        "paso": "confirmar_nombre",
        "botones_activos": ["confirmar_nombre", "corregir_nombre"]
    })

    await update.message.reply_text(
        f"✅ <b>ID Phoenix:</b> {texto}\n"
        f"<b>Cuadrilla:</b> {datos['CUADRILLA']}\n"
        f"<b>Proveedor:</b> {datos['PROVEEDOR']}\n"
        f"<b>Zona:</b> {datos['ZONA']}\n\n"
        "¿Son correctos estos datos?",
        parse_mode="HTML",
        reply_markup=mostrar_botonera('confirmar_nombre')
    )

# ================== FILTRO DE MENSAJES DE TEXTO ==================
async def manejar_texto_fuera_de_lugar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Solo acepta ID_PHOENIX (1–4 dígitos) cuando el paso es 'esperando_cuadrilla'."""
    if not es_chat_privado(update):
        return

    chat_id = update.effective_chat.id
    ud = user_data.get(chat_id, {})
    paso = ud.get("paso")
    texto = (update.message.text or "").strip()

    # 🚫 Si el flujo ya terminó
    if paso == "finalizado":
        await update.message.reply_text(
            "✅ Ya completaste tu registro hoy.\n\n"
            "Mañana podrás iniciar uno nuevo con /ingreso 💪💪"
        )
        return

    # ✅ SOLO aquí permitimos escribir el ID
    if paso == "esperando_cuadrilla":
        if not re.fullmatch(r"\d{1,4}", texto):
            await update.message.reply_text(
                "⚠️ Debes ingresar tu <b>ID PHOENIX</b> de 1 a 4 dígitos.",
                parse_mode="HTML"
            )
            return
        await nombre_cuadrilla(update, context)
        return

    # ⛔ En cualquier otro paso NO aceptamos texto como ID
    if paso in ("confirmar_nombre", "tipo", "confirmar_tipo",
                "confirmar_selfie_inicio", "confirmar_selfie_salida"):
        kb = mostrar_botonera(paso)
        if kb:
            await update.message.reply_text(
                "⚠️ Usa los botones para continuar. 👇",
                reply_markup=kb,
                parse_mode="HTML"
            )
        return

    # Si no hay flujo o aún no inició
    if paso is None:
        await update.message.reply_text(
            "👷‍♂️ Usa el comando /ingreso para iniciar tu registro de asistencia."
        )
        return

    # Si el paso acepta texto (por ejemplo, campos personalizados)
    msg = PASOS.get(paso, {}).get("mensaje")
    if msg:
        await update.message.reply_text(msg, parse_mode="HTML")

# ===================== BOTONES CONFIRMAR/CORREGIR ID_PHOENIX =====================
async def handle_nombre_cuadrilla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not es_chat_privado(update):
        return

    chat_id = query.message.chat.id
    ud = user_data.setdefault(chat_id, {})

    # ⚡ Solo aceptar si está en los botones activos
    if query.data not in ud.get("botones_activos", []):
        await query.answer("⚠️ Este botón ya no es válido.")
        return

    try:
        await query.answer("Procesando… ⏳")

        # === CORREGIR ===
        if query.data == "corregir_nombre":
            ud["paso"] = "esperando_cuadrilla"
            ud.pop("id_phoenix", None)
            ud.pop("cuadrilla", None)
            ud.pop("proveedor", None)
            ud.pop("zona", None)
            ud.pop("botones_activos", None)

            await query.edit_message_text(
                "✍️ Ingresa nuevamente tu <b>ID_PHOENIX</b> (código de 1 a 4 dígitos numéricos).",
                parse_mode="HTML"
            )
            return

        # === CONFIRMAR ===
        if query.data == "confirmar_nombre":
            if not ud.get("id_phoenix"):
                ud["paso"] = "esperando_cuadrilla"
                ud.pop("botones_activos", None)
                await query.edit_message_text("⚠️ No encontré el ID_PHOENIX. Escríbelo nuevamente por favor.")
                return

            # 1️⃣ Garantizar el nuevo sheet
            ssid = ensure_asistencia_cuadrillas_v1()
            ensure_sheet_and_headers(ssid)
            logger.info(f"[FLOW] usando ssid={ssid}")

            # 2️⃣ Crear fila base con los datos del ID Phoenix
            if not ud.get("spreadsheet_id") or not ud.get("row"):
                base = {
                    "ID_PHOENIX": ud.get("id_phoenix", ""),
                    "CUADRILLA": ud.get("cuadrilla", ""),
                    "PROVEEDOR": ud.get("proveedor", ""),
                    "ZONA": ud.get("zona", ""),
                    "TIPO DE CUADRILLA": "",
                }
                row = append_base_row(ssid, base, chat_id)
                ud["spreadsheet_id"] = ssid
                ud["row"] = row
                logger.info(f"[OK] Registro base creado: row={row}, ID_PHOENIX={ud['id_phoenix']}")

            # 3️⃣ Avanzar directamente al paso "tipo de cuadrilla"
            ud["paso"] = "tipo"
            ud.pop("botones_activos", None)

            keyboard = [
                [InlineKeyboardButton("🟠 DISPONIBILIDAD", callback_data="tipo_disp")],
                [InlineKeyboardButton("⚪ REGULAR", callback_data="tipo_reg")],
            ]

            await query.edit_message_text(
                "Selecciona el <b>tipo de cuadrilla</b>: 👇",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    except Exception as e:
        logger.exception("[handle_nombre_cuadrilla] Error")
        try:
            await query.message.reply_text(
                "❌ Ocurrió un error inesperado.\n"
                "Usa /estado para que te indique en qué paso estás. 😊"
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

    # ⚡ Solo aceptar botones válidos
    if query.data not in ("tipo_disp", "tipo_reg"):
        await query.answer("⚠️ Opción no válida.")
        return
    

    try:
        await query.answer("Procesando tipo de cuadrila.. ⏳")
    except Exception:
        pass


    try:
    # Guarda selección provisional (sin escribir aún en el Sheet)
        seleccion = "DISPONIBILIDAD" if query.data == "tipo_disp" else "REGULAR"
        ud["tipo_seleccionado"] = seleccion
        ud["paso"] = "confirmar_tipo"
        # Guardamos los botones activos válidos en este estado
        ud["botones_activos"] = ["confirmar_tipo", "corregir_tipo"]

        kb = mostrar_botonera("confirmar_tipo")
        logger.info(f"[FLOW] Usuario {chat_id} seleccionó tipo de cuadrilla: {seleccion}")

        # 🚦 Evitamos error de "Message is not modified"
        try:
            await query.edit_message_text(
                f"Seleccionaste: <b>{seleccion}</b>.\n\n¿Es correcto?",
                parse_mode="HTML",
                reply_markup=kb
            )
        except Exception as e:
            if "Message is not modified" in str(e):
                logger.warning(f"[handle_tipo_cuadrilla] Botón repetido ignorado (chat_id={chat_id})")
            else:
                raise

    except Exception:
        logger.exception("[handle_tipo_cuadrilla] Error")
        try:
            await query.message.reply_text(
                "❌ Ocurrió un error.\nEscribe /estado para poder indicarte en qué paso estás. 😊"
            )
        except Exception:
            pass  

# ====================== CORREGIR TIPO O CONFIRMAR ===========

async def handle_confirmar_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not es_chat_privado(update):
        return

    chat_id = query.message.chat.id
    ud = user_data.setdefault(chat_id, {})

    # ⚡ Solo aceptar si está en los botones activos
    if query.data not in ud.get("botones_activos", []):
        try:
            await query.answer("⚠️ Este botón ya no es válido.")
        except Exception:
            pass
        return

    try:
        try:
            await query.answer("Procesando… ⏳")
        except Exception:
            pass

        if ud.get("paso") != "confirmar_tipo":
            return

        if query.data == "corregir_tipo":
            # Volver a elegir
            k = InlineKeyboardMarkup([
                [InlineKeyboardButton("🟠 DISPONIBILIDAD", callback_data="tipo_disp")],
                [InlineKeyboardButton("⚪ REGULAR", callback_data="tipo_reg")],
            ])
            ud["paso"] = "tipo"
            ud.pop("botones_activos", None)

            try:
                await query.edit_message_text(
                    "Selecciona el <b>tipo de cuadrilla</b>:",
                    parse_mode="HTML",
                    reply_markup=k
                )
            except Exception as e:
                if "Message is not modified" in str(e):
                    logger.warning(f"[handle_confirmar_tipo] Botón repetido ignorado (chat_id={chat_id})")
                else:
                    raise
            return

        if query.data == "confirmar_tipo":
            ssid = ud.get("spreadsheet_id")
            row = ud.get("row")
            if not ssid or not row:
                await query.edit_message_text("❌ No hay registro activo. Usa /ingreso para iniciar.")
                return

            tipo = ud.get("tipo_seleccionado", "")
            if not tipo:
                await query.edit_message_text("⚠️ No encontré la selección. Vuelve a elegir el tipo.")
                return

            try:
                # ✅ Corrección: usar columna + fila para el rango
                col = COL["TIPO DE CUADRILLA"]
                rango = f"{SHEET_TITLE}!{col}{row}"

                sheets_service.spreadsheets().values().update(
                    spreadsheetId=ssid,
                    range=rango,
                    valueInputOption="USER_ENTERED",
                    body={"values": [[tipo]]}
                ).execute()

                logger.info(
                    f"[EVIDENCIA] USER_ID={chat_id} | ID_REGISTRO={ud.get('id_registro')} "
                    f"| Paso=Tipo Cuadrilla | Tipo='{tipo}' | Row={row} | Rango={rango}"
                )

                ud["tipo"] = tipo
                ud["paso"] = "esperando_selfie_inicio"
                ud.pop("botones_activos", None)

                try:
                    await query.edit_message_text(
                        f"Tipificación de cuadrilla confirmada: <b>{tipo}</b>.\n\n📸 Envía tu foto de <b>Inicio con tus EPPs completos</b>.",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    if "Message is not modified" in str(e):
                        logger.warning(f"[handle_confirmar_tipo] Mensaje repetido ignorado (chat_id={chat_id})")
                    else:
                        raise

            except Exception as e:
                logger.error(f"[ERROR] confirm_tipo: {e}")
                await query.edit_message_text(
                    "⚠️ No pude registrar tu selección.\nEscribe /estado para continuar."
                )

    except Exception:
        logger.exception("[handle_confirmar_tipo] Error inesperado")
        try:
            await query.message.reply_text(
                "❌ Ocurrió un error inesperado.\n"
                "Escribe /estado para poder indicarte en qué paso estás. 😊"
            )
        except Exception:
            pass
    

# ================== FOTO INICIO + HORA INGRESO ==================

async def foto_ingreso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_chat_privado(update):
        return
    chat_id = update.effective_chat.id
    ud = user_data.get(chat_id) or {}

    if ud.get("paso") != "esperando_selfie_inicio":
        return

    if not await validar_contenido(update, "foto"):
        return

    ssid, id_registro = ud.get("spreadsheet_id"), ud.get("id_registro")
    if not ssid or not id_registro:
        await update.message.reply_text("❌ No hay registro activo. Usa /ingreso para iniciar.")
        return

    # ✅ Buscar la fila por ID_REGISTRO
    row = find_active_row(ssid, id_registro)
    if not row:
        await update.message.reply_text("⚠️ No encontré tu registro activo. Usa /ingreso para comenzar de nuevo.")
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
        await update.message.reply_text("⚠️ No pude procesar tu fotografía.\nReenvíala, por favor.")
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
    ud["paso"] = "esperando_live_inicio"
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
    user = update.effective_user

    # 🚦 Validación: solo aceptar UBICACIÓN en este paso
    if not await validar_flujo(update, chat_id):
        return
    
    ud = user_data.setdefault(chat_id, {})
    ssid = ud.get("spreadsheet_id")
    id_registro = ud.get("id_registro")
    if not ssid or not id_registro:
        await update.message.reply_text("⚠️ No encontré tu registro activo. Usa /ingreso para iniciar de nuevo.")
        logger.warning(f"[UBICACIÓN] {user.username} ({chat_id}) intentó sin registro activo")
        return

    # Buscar la fila activa en Sheets (más robusto que confiar solo en RAM)
    row = find_active_row(ssid, id_registro)
    if not row:
        await update.message.reply_text("⚠️ Tuvimos un problema. Usa /ingreso para iniciar de nuevo.")
        logger.error(f"[UBICACIÓN] No se encontró row activo para {chat_id}")
        return

    loc = update.message.location
    lat, lon = loc.latitude, loc.longitude
    is_live = bool(getattr(loc, "live_period", None))

    # Exigir live-location (no aceptar ubicación estática)
    if not is_live:
        await update.message.reply_text(
            "⚠️ Por favor, comparte tu *ubicación en tiempo real*.\n\n"
            "📎 Toca el clip ➜ Ubicación ➜ *Compartir ubicación en tiempo real*."
        )
        return

    try:
        # ================== UBICACIÓN DETALLADA ==================
        ubic = obtener_ubicacion_detallada(lat, lon)
        dep = ubic["departamento"]
        prov = ubic["provincia"]
        dist = ubic["distrito"]

        # ================== UBICACIÓN DE INICIO ==================
        if ud.get("paso") == "esperando_live_inicio":
            update_single_cell(ssid, SHEET_TITLE, COL["LATITUD"], row, f"{lat:.6f}")
            update_single_cell(ssid, SHEET_TITLE, COL["LONGITUD"], row, f"{lon:.6f}")
            update_single_cell(ssid, SHEET_TITLE, COL["DEPARTAMENTO"], row, dep)
            update_single_cell(ssid, SHEET_TITLE, COL["PROVINCIA"], row, prov)
            update_single_cell(ssid, SHEET_TITLE, COL["DISTRITO"], row, dist)

            logger.info(
                f"[EVIDENCIA] USER_ID={chat_id} | ID_REGISTRO={ud.get('id_registro')} "
                f"| Paso=Ubicación INICIO | Lat={lat:.6f}, Lon={lon:.6f} | "
                f"Dep={dep}, Prov={prov}, Dist={dist} | Row={row}"
            )

            ud["paso"] = "en_jornada"   # jornada abierta hasta /salida
            user_data[chat_id] = ud

            await update.message.reply_text(
                f"✅ Ubicación de inicio registrada.\n"
                f"🗺️ {dist}, {prov}, {dep}\n\n"
                "💭 Recuerda que para concluir tu jornada debes usar /salida."
            )
            return

        # ================== UBICACIÓN DE SALIDA ==================
        if ud.get("paso") == "esperando_live_salida":
            update_single_cell(ssid, SHEET_TITLE, COL["LATITUD SALIDA"], row, f"{lat:.6f}")
            update_single_cell(ssid, SHEET_TITLE, COL["LONGITUD SALIDA"], row, f"{lon:.6f}")
            update_single_cell(ssid, SHEET_TITLE, COL["DEPARTAMENTO SALIDA"], row, dep)
            update_single_cell(ssid, SHEET_TITLE, COL["PROVINCIA SALIDA"], row, prov)
            update_single_cell(ssid, SHEET_TITLE, COL["DISTRITO SALIDA"], row, dist)

            logger.info(
                f"[EVIDENCIA] USER_ID={chat_id} | ID_REGISTRO={ud.get('id_registro')} "
                f"| Paso=Ubicación SALIDA | Lat={lat:.6f}, Lon={lon:.6f} | "
                f"Dep={dep}, Prov={prov}, Dist={dist} | Row={row}"
            )

            # 🚦 Marcar finalización aquí
            ud["paso"] = "finalizado"
            user_data[chat_id] = ud
            if chat_id not in USUARIOS_TEST:
                marcar_registro_completo(chat_id)
            logger.info(f"[FINALIZADO] Registro cerrado para {chat_id} en row {row}")

            await update.message.reply_text(
                f"✅ Ubicación de salida registrada.\n"
                f"🗺️ {dist}, {prov}, {dep}\n\n"
                "👷‍♂️ Jornada finalizada.\n"
                "🏠 Buen regreso a casa. Nos vemos mañana 💪",
                parse_mode="HTML"
            )

    except Exception:
        logger.exception("[manejar_ubicacion] Error inesperado")
        try:
            await update.message.reply_text(
                "❌ Ocurrió un error registrando tu ubicación.\n"
                "Escribe /estado para continuar correctamente."
            )
        except Exception:
            pass


# ================== SALIDA ==================

async def salida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    # 📌 Log inicial → confirma que el comando llegó al bot
    logger.info(f"📌 /salida recibido de {user.username} ({user.id}) en chat {chat_id} a las {datetime.now()}")

    if not es_chat_privado(update):
        return
    
    chat_id = update.effective_chat.id
    ud = user_data.get(chat_id)   # 👈 usamos get, no setdefault

    # 🚫 Si no hay jornada activa → bloquear directo
    if not ud or not ud.get("spreadsheet_id") or not ud.get("id_registro"):
        await update.message.reply_text(
            "⚠️ No puedes usar <b>/salida</b> sin antes haber completado tu registro de <b>/ingreso</b>.",
            parse_mode="HTML"
        )
        logger.warning(f"[SALIDA BLOQUEADA] Usuario {user.id} intentó sin ingreso previo")
        return

    # 🚦 Validar horario laboral (excepto usuarios de prueba)
    if chat_id not in USUARIOS_TEST and not dentro_horario_laboral():
        await update.message.reply_text(
            "⚠️ Solo puedes registrar tu <b>asistencia</b> entre las <b>07:00 AM y 11:59 PM</b>.",
            parse_mode="HTML"
        )
        logger.warning(f"[SALIDA BLOQUEADA] Usuario {user.id} fuera de horario")
        return 

    # 🚦 Validar pasos obligatorios antes de permitir salida
    if not ud.get("cuadrilla"):
        await update.message.reply_text("⚠️ No puedes registrar salida todavía.\n""Te falta escribir el <b>ID Phoenix de tu cuadrilla ✍️</b>", parse_mode="HTML")
        return 

    if not ud.get("hora_ingreso"):
        await update.message.reply_text("⚠️ No puedes registrar salida todavía.\n""Te falta tu <b>foto de inicio 📸</b>", parse_mode="HTML")
        return
    
    if ud.get("paso") in ("esperando_live_inicio", "confirmar_selfie_inicio"):
        await update.message.reply_text("⚠️ No puedes registrar salida todavía.\n""Te falta compartir tu <b>ubicación en tiempo real 📍</b>", parse_mode="HTML")
        return
    
    # 🚦 Si ya está finalizado, bloquear
    if ud.get("paso") == "finalizado":
        await update.message.reply_text("✅ Ya completaste tu registro hoy. No puedes registrar otra salida hasta mañana.")
        logger.info(f"[SALIDA YA FINALIZADA] Usuario {user.id}")
        return
    
    # ✅ Si cumplió con lo mínimo → permitir selfie de salida
    ssid = ud.get("spreadsheet_id")
    row = find_active_row(ssid, ud.get("id_registro"))
    if not row:
        await update.message.reply_text("⚠️ No encontré tu registro activo. ¿Seguro que hiciste /ingreso?")
        logger.error(f"[SALIDA ERROR] No encontré fila activa para {user.id}")
        return

    # 🔐 Actualizar estado
    ud["row"] = row
    ud["paso"] = "esperando_selfie_salida"
    ud["botones_activos"] = ["confirmar_selfie_salida", "repetir_selfie_salida"]

    logger.info(f"[SALIDA] USER_ID={chat_id} | Row={row} | Cuadrilla={ud.get('cuadrilla')}")

    try:
        await update.message.reply_text(
            "📸 Envía tu foto de <b>fin de labores con tus EPPs completos</b>.\n"
            "👉 Con esta foto iniciaremos el cierre de tu jornada. 🏠",
            parse_mode="HTML"
        )   
    except Exception as e:
        logger.error(f"[ERROR] salida mensaje: {e}")
        await update.message.reply_text(
            "⚠️ Ocurrió un problema mostrando el mensaje de salida.\n"
            "Usa /estado para continuar en el flujo.",
            parse_mode="HTML"
        )


# ================== ROUTER DE FOTOS ==================

async def manejar_fotos(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:
        chat_id = update.effective_chat.id
        ud = user_data.setdefault(chat_id, {})
        paso = ud.get("paso")
        
        # 🚦 Validación: solo aceptar FOTO en este paso
        if not await validar_flujo(update, chat_id):
            return
        
        ssid, id_registro = ud.get("spreadsheet_id"), ud.get("id_registro")
        if not ssid or not id_registro:
            await update.message.reply_text("⚠️ No hay registro activo. Usa /ingreso para iniciar.")
            return

        # ✅ Buscar fila activa en Sheets
        row = find_active_row(ssid, id_registro)
        if not row:
            await update.message.reply_text("⚠️ No encontré tu registro activo. Usa /ingreso para iniciar de nuevo.")
            return

        # Selfie de INICIO -> capturamos y pedimos confirmación
        if paso == "esperando_selfie_inicio":
            photo = update.message.photo[-1]
            ud["pending_selfie_inicio_file_id"] = photo.file_id
            ud["row"] = row  # ✅ Guardamos la fila real
            ud["paso"] = "confirmar_selfie_inicio"
            ud["botones_activos"] = ["confirmar_selfie_inicio", "repetir_selfie_inicio"]

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmar", callback_data="confirmar_selfie_inicio")],
                [InlineKeyboardButton("🔄 Repetir", callback_data="repetir_selfie_inicio")]
            ])
            await update.message.reply_text("¿📸Usamos esta foto para iniciar actividades?", reply_markup=kb)
            return

        # Selfie de SALIDA -> capturamos y pedimos confirmación
        if paso == "esperando_selfie_salida":
            photo = update.message.photo[-1]
            ud["pending_selfie_salida_file_id"] = photo.file_id
            ud["row"] = row  # ✅ Guardamos la fila real
            ud["paso"] = "confirmar_selfie_salida"
            ud["botones_activos"] = ["confirmar_selfie_salida", "repetir_selfie_salida"]

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmar", callback_data="confirmar_selfie_salida")],
                [InlineKeyboardButton("🔄 Repetir", callback_data="repetir_selfie_salida")]
            ])
            await update.message.reply_text("¿📸Usamos esta foto para finalizar actividades?", reply_markup=kb)
            return

        # Caso: foto fuera de lugar
        await update.message.reply_text(
            "⚠️ No es momento de enviar fotos.\n Usa /estado para ver en qué paso estás."
        )

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

    # ⚡ Solo aceptar botones activos
    if query.data not in ud.get("botones_activos", []):
        try:
            await query.answer("⚠️ Este botón ya no es válido.")
        except Exception:
            pass
        return

    # ⚡ Contestamos de inmediato el callback
    try:
        # Confirmación inmediata de callback
        try:
            await query.answer("Procesando foto de ingreso... ⏳")
        except Exception:
            pass

        if query.data == "repetir_selfie_inicio":
            ud["pending_selfie_inicio_file_id"] = None
            ud["paso"] = "esperando_selfie_inicio"
            ud.pop("botones_activos", None)

            try:
                await query.edit_message_text("🔄 Envía nuevamente tu foto de inicio de actividades.\n""📸 Recuerda que debe ser con tus <b>EPPs completos</b>.", parse_mode="HTML")
            
            except Exception as e:
                if "Message is not modified" in str(e):
                    logger.warning(f"[handle_confirmar_selfie_inicio] Botón repetido ignorado (chat_id={chat_id})")
                else:
                    raise
            return
                
        if query.data == "confirmar_selfie_inicio":
            ssid, id_registro = ud.get("spreadsheet_id"), ud.get("id_registro")
            fid = ud.get("pending_selfie_inicio_file_id")
            if not (ssid and id_registro and fid):
                ud.pop("botones_activos", None)
                await query.edit_message_text("❌ Falta foto de inicio de actividades.")
                return

            # ✅ Buscar la fila por ID_REGISTRO
            row = find_active_row(ssid, id_registro)
            if not row:
                await query.edit_message_text("⚠️ No encontré tu registro activo.")
                return

            try:
                # Descargar de Telegram
                tg_file = await context.bot.get_file(fid)
                buff = io.BytesIO()
                await tg_file.download_to_memory(out=buff)
                buff.seek(0)

                filename = f"selfie_inicio_{datetime.now(LIMA_TZ).strftime('%Y%m%d_%H%M%S')}_{chat_id}_{row}.jpg"
                loop = asyncio.get_running_loop()
                link = await loop.run_in_executor(
                    None,
                    lambda: comprimir_y_subir(buff, filename, ssid, row, "FOTO INICIO CUADRILLA")
                )

                # Hora de ingreso
                hora = datetime.now(LIMA_TZ).strftime("%H:%M")
                update_single_cell(ssid, SHEET_TITLE, COL["HORA INGRESO"], row, hora)
                ud["hora_ingreso"] = hora

                logger.info(
                    f"[EVIDENCIA] USER_ID={chat_id} | ID_REGISTRO={ud.get('id_registro')} "
                    f"| Paso=Selfie INICIO | Hora={hora} | Row={row} | file_id={fid}"
                )

                # Pedir ubicación en tiempo real
                ud["paso"] = "esperando_live_inicio"
                ud.pop("botones_activos", None)  # limpiar botones activos
                ud.pop("pending_selfie_inicio_file_id", None)

                gc.collect()
                log_memoria("Después de confirmar Foto INICIO")

                try:
                    await query.edit_message_text(
                        f"✅ Fotografía registrada. ⏱️ Hora de salida: <b>{hora}</b>.\n\n"
                        "📍 Ahora envía tu <b>ubicación en tiempo real</b>\n\n"
                        "(Clip ➜ Ubicación ➜ Compartir ubicación en tiempo real 📍).",
                    parse_mode="HTML"
                    )

                except Exception as e:
                    if "Message is not modified" in str(e):
                        logger.warning(f"[handle_confirmar_selfie_inicio] Mensaje repetido ignorado (chat_id={chat_id})")
                    else:
                        raise  

            except Exception as e:
                logger.error(f"[ERROR] confirm_selfie_inicio upload: {e}")
                await query.edit_message_text("⚠️ No pude registra tu foto.\n""Reenvíala nuevamente con tus EPPs completos.")

    except Exception:
        logger.exception("[handle_confirmar_selfie_inicio] Error inesperado")
        try:
            await query.message.reply_text(
                "❌ Ocurrió un error inesperado.\n"
                "Escribe /estado para que te indique en qué paso estás."
            )
        except Exception:
            pass     


async def handle_confirmar_selfie_salida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not es_chat_privado(update):
        return

    chat_id = query.message.chat.id
    ud = user_data.setdefault(chat_id, {})

    # ⚡ Solo aceptar botones activos
    if query.data not in ud.get("botones_activos", []):
        try:
            await query.answer("⚠️ Este botón ya no es válido.")
        except Exception:
            pass
        return

    try:
        try:
            await query.answer("Procesando foto de salida... ⏳")
        except Exception:
            pass

        # --- Caso: repetir selfie ---
        if query.data == "repetir_selfie_salida":
            ud["pending_selfie_salida_file_id"] = None
            ud["paso"] = "esperando_selfie_salida"
            ud.pop("botones_activos", None)

            try:
                await query.edit_message_text(
                    "🔄 Envía nuevamente tu <b>foto de salida</b> 📸",
                    parse_mode="HTML"
                )
            except Exception as e:
                if "Message is not modified" in str(e):
                    logger.warning(f"[handle_confirmar_selfie_salida] Botón repetido ignorado (chat_id={chat_id})")
                else:
                    raise
            return

        # --- Caso: confirmar selfie ---
        if query.data == "confirmar_selfie_salida":
            ssid, id_registro = ud.get("spreadsheet_id"), ud.get("id_registro")
            fid = ud.get("pending_selfie_salida_file_id")
            if not (ssid and id_registro and fid):
                await query.edit_message_text("❌ Falta tu foto de salida 👀")
                return

        # ✅ Buscar la fila real por ID_REGISTRO

            row = find_active_row(ssid, id_registro)
            if not row:
                await query.edit_message_text("⚠️ No encontré tu registro activo.")
                return
        
            try:
                # Descargar de Telegram
                tg_file = await context.bot.get_file(fid)
                buff = io.BytesIO()
                await tg_file.download_to_memory(out=buff)
                buff.seek(0)

                filename = f"selfie_salida_{datetime.now(LIMA_TZ).strftime('%Y%m%d_%H%M%S')}_{chat_id}_{id_registro}.jpg"
            
            # ✅ Subir con row correcto Procesar (comprimir + subir a Drive) en un executor
                logger.info(f"[SELFIE] Procesando selfie de salida de {chat_id} (row={row})")

            # ✅ Subir con row correcto Procesar (comprimir + subir a Drive) en un executor
                loop = asyncio.get_running_loop()
                link = await loop.run_in_executor(
                    None,
                    lambda: comprimir_y_subir(buff, filename, ssid, row, "FOTO FIN CUADRILLA"))
                if link:
                    logger.info(f"[DRIVE] Foto de salida subida OK para {chat_id} | Link={link}")
                else:
                    logger.error(f"[DRIVE] Falló subida de selfie salida para {chat_id}")

                try:
                # Registrar hora de salida
                    hora = datetime.now(LIMA_TZ).strftime("%H:%M")
                    row = find_active_row(ssid, ud["id_registro"])
                    update_single_cell(ssid, SHEET_TITLE, COL["HORA SALIDA"], row, hora)
                    ud["hora_salida"] = hora
                    logger.info(f"[EXCEL] Hora de salida registrada {hora} en row {row} para {chat_id}")
                except Exception as e:
                    logger.error(f"[ERROR] No se pudo actualizar hora salida en Excel: {e}", exc_info=True)

                # Siempre log de evidencia, aunque falle Excel
                logger.info(
                    f"[EVIDENCIA] USER_ID={chat_id} | ID_REGISTRO={ud.get('id_registro')} "
                    f"| Paso=Selfie SALIDA | Hora={hora} | Row={row} | file_id={fid}"
                    )

                # Avanzar paso
                ud["paso"] = "esperando_live_salida"
                ud.pop("botones_activos", None)  # limpiar botones activos
                ud.pop("pending_selfie_salida_file_id", None)

                gc.collect()
                log_memoria("Después de confirmar selfie SALIDA")

                try:
                    await query.edit_message_text(
                        f"✅ Fotografía registrada. ⏱️ Hora de salida: <b>{hora}</b>.\n\n"
                        "📍 Ahora envía tu <b>ubicación en tiempo real</b>\n\n"
                        "(Clip ➜ Ubicación ➜ Compartir ubicación en tiempo real 📍).",
                        parse_mode="HTML"
                    )
                
                except Exception as e:
                    if "Message is not modified" in str(e):
                        logger.warning(f"[handle_confirmar_selfie_salida] Mensaje repetido ignorado (chat_id={chat_id})")
                    else:
                        raise

            except Exception as e:
                logger.error(f"[ERROR] confirm_selfie_salida upload: {e}")
                await query.edit_message_text(
                    "⚠️ No pude registrar tu foto de salida.\n""Reenvíala nuevamente con tus EPPs completos."
                )
    except Exception:
        logger.exception("[handle_confirmar_selfie_salida] Error inesperado")
        try:
            await query.message.reply_text(
                "❌ Ocurrió un error inesperado.\n"
                "Escribe /estado para que te indique en qué paso estás."
            )
        except Exception:
            pass

#==================LOG RAM===========

def log_memoria(contexto=""):
    logger.info(f"[MEMORIA] {contexto}")

# ================== CALLBACKS / AYUDA (placeholder) ==================

async def handle_ayuda_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not es_chat_privado(update):
        return

    await query.answer("Procesando… ⏳")
    await query.edit_message_text(
        "⚠️⚠️ <b>¡Usa los comandos o botones para registrar tu asistencia paso a paso!</b>\n\n"
        "Comienza con /ingreso y sigue la secuencia para que tu asistencia se registre correctamente. ✅✅",
        parse_mode="HTML"
    )

async def subir_con_reintentos(buff, filename, ssid, row, header, intentos=3):
    loop = asyncio.get_running_loop()
    for i in range(intentos):
        try:
            return await loop.run_in_executor(
                None,
                lambda: comprimir_y_subir(buff, filename, ssid, row, header)
            )
        except Exception as e:
            logger.warning(f"[WARN] Falló intento {i+1}/{intentos} al subir {filename}: {e}")
            if i == intentos - 1:
                raise
            await asyncio.sleep(2 * (i+1))  # backoff exponencial

# ================== MAIN ==================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = init_bot_info

    # --- DEBUG: atrapa cualquier callback primero ---
    app.add_handler(CallbackQueryHandler(debug_callback_catcher), group=-1)

    # --- COMANDOS válidos ---
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(CommandHandler("ingreso", ingreso))
    app.add_handler(CommandHandler("salida", salida))

    # --- COMANDOS inválidos (filtro general) ---
    app.add_handler(
        MessageHandler(
            filters.COMMAND & ~filters.Command(["start", "ingreso", "salida", "ayuda"]),
            filtro_comandos_fuera_de_lugar,
        ),
        group=1
    )

    # --- MENSAJES ---
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_texto_fuera_de_lugar))
    app.add_handler(MessageHandler(filters.PHOTO, manejar_fotos))
    app.add_handler(MessageHandler(filters.LOCATION, manejar_ubicacion))
    app.add_handler(CommandHandler("estado", estado))

    # --- CALLBACKS REALES ---
    app.add_handler(CallbackQueryHandler(handle_ayuda_callback, pattern="^ayuda$"))
    app.add_handler(CallbackQueryHandler(handle_confirmar_selfie_inicio, pattern="^(confirmar_selfie_inicio|repetir_selfie_inicio)$"))
    app.add_handler(CallbackQueryHandler(handle_confirmar_selfie_salida, pattern="^(confirmar_selfie_salida|repetir_selfie_salida)$"))
    app.add_handler(CallbackQueryHandler(handle_confirmar_tipo, pattern="^(confirmar_tipo|corregir_tipo)$"))
    app.add_handler(CallbackQueryHandler(handle_nombre_cuadrilla, pattern="^(confirmar_nombre|corregir_nombre)$"))
    app.add_handler(CallbackQueryHandler(handle_tipo_cuadrilla, pattern="^tipo_(disp|reg)$"))

    # --- ERRORES ---
    app.add_error_handler(log_error)

    # --- JOB DIARIO: reset a medianoche ---
    scheduler = AsyncIOScheduler(timezone=str(LIMA_TZ))
    scheduler.add_job(resetear_registros, "cron", hour=0, minute=0)
    scheduler.start()
    logger.info("⏰ Job diario programado para resetear registros a las 00:00.")
    
    # --- ARRANQUE EN POLLING ---
    logger.info("🚀 Bot de Asistencia (privado) en ejecución...")
    gc.collect()
    logger.info("🧠 Memoria optimizada antes de iniciar polling.")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

def verificar_recursos_iniciales():
    """
    Valida que las carpetas y archivos esenciales existan en Google Drive antes de iniciar el bot.
    Crea los faltantes automáticamente.
    """
    logger.info("🔎 Verificando estructura base en Google Drive...")

    try:
        # 1️⃣ Verificar acceso a carpeta principal
        meta = drive_service.files().get(
            fileId=MAIN_FOLDER_ID,
            fields="id, name, driveId",
            supportsAllDrives=True
        ).execute()
        logger.info(f"✅ Carpeta principal detectada: {meta['name']} ({meta['id']})")

    except Exception as e:
        logger.error(f"❌ No se puede acceder a la carpeta principal. Error: {e}")
        raise SystemExit("⛔ La cuenta de servicio no tiene acceso a la carpeta principal en Drive.")

    # 2️⃣ Verificar / crear carpeta IMAGENES
    try:
        query_img = (
            f"name='IMAGENES' and '{MAIN_FOLDER_ID}' in parents "
            "and mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        res_img = drive_service.files().list(
            q=query_img,
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()

        if res_img.get("files"):
            img_folder_id = res_img["files"][0]["id"]
            logger.info(f"📂 Carpeta IMAGENES OK → ID={img_folder_id}")
        else:
            meta_img = {
                "name": "IMAGENES",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [MAIN_FOLDER_ID],
            }
            new_img = drive_service.files().create(
                body=meta_img, fields="id", supportsAllDrives=True
            ).execute()
            logger.info(f"🆕 Carpeta IMAGENES creada → ID={new_img['id']}")

    except Exception as e:
        logger.error(f"❌ Error creando/verificando carpeta IMAGENES: {e}")
        raise SystemExit("⛔ Error al crear/verificar la carpeta IMAGENES.")

    # 3️⃣ Verificar / crear archivo ASISTENCIA_CUADRILLAS_DISP_ALTO_VALOR
    try:
        query_ass = (
            f"name='ASISTENCIA_CUADRILLAS_DISP_ALTO_VALOR' and '{MAIN_FOLDER_ID}' in parents "
            "and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
        )
        res_ass = drive_service.files().list(
            q=query_ass,
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()

        if res_ass.get("files"):
            asistencia_id = res_ass["files"][0]["id"]
            logger.info(f"📄 Archivo ASISTENCIA_CUADRILLAS_DISP_ALTO_VALOR OK → ID={asistencia_id}")
        else:
            meta_ass = {
                "name": "ASISTENCIA_CUADRILLAS_DISP_ALTO_VALOR",
                "mimeType": "application/vnd.google-apps.spreadsheet",
                "parents": [MAIN_FOLDER_ID],
            }
            new_ass = drive_service.files().create(
                body=meta_ass, fields="id", supportsAllDrives=True
            ).execute()
            asistencia_id = new_ass["id"]

            # Crear cabeceras en la nueva hoja
            sheets_service.spreadsheets().values().update(
                spreadsheetId=asistencia_id,
                range="A1:V1",
                valueInputOption="RAW",
                body={"values": [HEADERS]},
            ).execute()
            logger.info(f"🧾 Archivo ASISTENCIA_CUADRILLAS_DISP_ALTO_VALOR creado con encabezados OK → ID={asistencia_id}")

    except Exception as e:
        logger.error(f"❌ Error creando/verificando archivo ASISTENCIA_CUADRILLAS_DISP_ALTO_VALOR: {e}")
        raise SystemExit("⛔ Error al crear/verificar el archivo ASISTENCIA_CUADRILLAS_DISP_ALTO_VALOR.")

    # 4️⃣ Verificar que exista el archivo CUADRILLAS ACTIVAS
    try:
        res_cuad = drive_service.files().list(
            q=f"name='CUADRILLAS ACTIVAS' and '{MAIN_FOLDER_ID}' in parents and trashed=false",
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()

        if res_cuad.get("files"):
            logger.info(f"📘 Archivo CUADRILLAS ACTIVAS OK → ID={res_cuad['files'][0]['id']}")
        else:
            logger.warning("⚠️ No se encontró el archivo 'CUADRILLAS ACTIVAS' dentro de la carpeta principal.")
            logger.warning("⚠️ Este archivo debe cargarse manualmente desde tu Google Drive.")
    except Exception as e:
        logger.error(f"❌ Error buscando archivo CUADRILLAS ACTIVAS: {e}")

    logger.info("✅ Todos los recursos esenciales están listos.")


if __name__ == "__main__":
    verificar_recursos_iniciales()  # <-- NUEVA VALIDACIÓN
    main()
