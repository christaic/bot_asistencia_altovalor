import os
import json
import logging
import asyncio
import unicodedata, re
from datetime import datetime
from pytz import timezone
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
NOMBRE_CARPETA_DRIVE = "ASISTENCIA - SGA ALTO VALOR"
DRIVE_ID = "0AOy_EhsaSY_HUk9PVA"

# Zona horaria
LIMA_TZ = timezone("America/Lima")

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- GOOGLE SERVICES ----------------
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

# ---------------- SHEET CONFIG ----------------
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
SHEET_TITLE = "Registro Cuadrillas por Disponibilidad - Alto Valor"

HEADERS = [
    "FECHA Y HORA", "NOMBRE DE CUADRILLA", "TIPO DE CUADRILLA",
    "FOTO DE CUADRILLA", "COORDENADAS INGRESO", "HORA DE INICIO",
    "HORA DE SALIDA A BREAK", "HORA DE REGRESO DE BREAK",
    "HORA DE SALIDA", "FOTO DE SALIDA", "COORDENADAS SALIDA"
]

COL = {h: chr(65+i) for i, h in enumerate(HEADERS)}

def get_or_create_main_folder():
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
        "parents": [DRIVE_ID]
    }
    folder = drive_service.files().create(
        body=metadata,
        fields="id",
        supportsAllDrives=True
    ).execute()
    return folder["id"]

MAIN_FOLDER_ID = get_or_create_main_folder()

def buscar_archivo(nombre_archivo: str):
    q = [
        f"name='{nombre_archivo}'",
        f"'{MAIN_FOLDER_ID}' in parents",
        "trashed=false",
    ]
    query = " and ".join(q)
    results = drive_service.files().list(
        q=query,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    files = results.get("files", [])
    return files[0] if files else None

def ensure_spreadsheet(nombre: str) -> str:
    archivo = buscar_archivo(nombre)
    if archivo:
        return archivo["id"]

    meta = {
        "name": nombre,
        "mimeType": SHEET_MIME,
        "parents": [MAIN_FOLDER_ID],
    }
    created = drive_service.files().create(
        body=meta,
        fields="id",
        supportsAllDrives=True
    ).execute()
    ssid = created["id"]

    # Crear hoja y headers
    sheets_service.spreadsheets().values().update(
        spreadsheetId=ssid,
        range=f"{SHEET_TITLE}!A1:K1",
        valueInputOption="RAW",
        body={"values": [HEADERS]}
    ).execute()
    return ssid

def append_row(ssid: str, data: dict) -> int:
    row_vals = [[data.get(h, "") for h in HEADERS]]
    resp = sheets_service.spreadsheets().values().append(
        spreadsheetId=ssid,
        range=f"{SHEET_TITLE}!A:K",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": row_vals}
    ).execute()
    updated_range = resp["updates"]["updatedRange"]
    row = int("".join([c for c in updated_range.split("!")[1].split(":")[0] if c.isdigit()]))
    return row

def update_cell(ssid: str, row: int, header: str, value: str):
    col = COL[header]
    sheets_service.spreadsheets().values().update(
        spreadsheetId=ssid,
        range=f"{SHEET_TITLE}!{col}{row}",
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]}
    ).execute()

# ---------------- BOT DATA ----------------
user_data = {}

# ---------------- VALIDACIÓN ----------------
async def validar_contenido(update: Update, tipo: str):
    if tipo == "texto" and not update.message.text:
        await update.message.reply_text("⚠️ Debes enviar *texto*, no fotos ni ubicación.")
        return False
    if tipo == "foto" and not update.message.photo:
        await update.message.reply_text("⚠️ Debes enviar una *foto*, no texto ni ubicación.")
        return False
    if tipo == "ubicacion" and not update.message.location:
        await update.message.reply_text("⚠️ Debes enviar una *ubicación GPS*, no texto ni fotos.")
        return False
    return True

# ---------------- FLUJO ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_data[chat_id] = {"paso": 0}
    await update.message.reply_text("👋 Hola! Empecemos.\n\n✍️ Ingresa el *nombre de la cuadrilla*.")

async def manejar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    paso = user_data.get(chat_id, {}).get("paso")

    # Paso 0: nombre de cuadrilla
    if paso == 0:
        if not await validar_contenido(update, "texto"):
            return
        user_data[chat_id]["cuadrilla"] = update.message.text.strip()
        user_data[chat_id]["paso"] = 1
        keyboard = [
            [InlineKeyboardButton("📌 DISPONIBILIDAD", callback_data="tipo_disp")],
            [InlineKeyboardButton("👷 REGULAR", callback_data="tipo_reg")]
        ]
        await update.message.reply_text(
            f"Nombre de cuadrilla: *{user_data[chat_id]['cuadrilla']}*\n\nSelecciona el tipo:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def handle_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat.id
    await query.answer()

    tipo = "DISPONIBILIDAD" if query.data == "tipo_disp" else "REGULAR"
    user_data[chat_id]["tipo"] = tipo
    user_data[chat_id]["paso"] = 2

    # Crear fila base
    ssid = ensure_spreadsheet("ASISTENCIA_CUADRILLAS")
    user_data[chat_id]["spreadsheet_id"] = ssid
    ahora = datetime.now(LIMA_TZ).strftime("%Y-%m-%d %H:%M")
    fila = append_row(ssid, {
        "FECHA Y HORA": ahora,
        "NOMBRE DE CUADRILLA": user_data[chat_id]["cuadrilla"],
        "TIPO DE CUADRILLA": tipo
    })
    user_data[chat_id]["row"] = fila

    await query.edit_message_text(
        f"Tipo seleccionado: *{tipo}*\n\n📸 Ahora envía la *foto de la cuadrilla*.",
        parse_mode="Markdown"
    )

async def manejar_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    paso = user_data.get(chat_id, {}).get("paso")
    ssid = user_data.get(chat_id, {}).get("spreadsheet_id")
    row = user_data.get(chat_id, {}).get("row")

    # Paso 2: foto cuadrilla
    if paso == 2:
        if not await validar_contenido(update, "foto"):
            return
        user_data[chat_id]["paso"] = 3
        update_cell(ssid, row, "FOTO DE CUADRILLA", "OK")
        await update.message.reply_text("✅ Foto recibida.\n\n📍 Envía ahora la *ubicación de ingreso*.")

    # Paso 8: foto salida
    elif paso == 8:
        if not await validar_contenido(update, "foto"):
            return
        user_data[chat_id]["paso"] = 9
        update_cell(ssid, row, "FOTO DE SALIDA", "OK")
        await update.message.reply_text("✅ Foto de salida recibida.\n\n📍 Envía ahora la *ubicación de salida*.")

async def manejar_ubicacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    paso = user_data.get(chat_id, {}).get("paso")
    ssid = user_data.get(chat_id, {}).get("spreadsheet_id")
    row = user_data.get(chat_id, {}).get("row")

    if not await validar_contenido(update, "ubicacion"):
        return

    lat, lng = update.message.location.latitude, update.message.location.longitude

    # Paso 3: coords ingreso
    if paso == 3:
        user_data[chat_id]["paso"] = 4
        update_cell(ssid, row, "COORDENADAS INGRESO", f"{lat},{lng}")
        hora = datetime.now(LIMA_TZ).strftime("%H:%M")
        update_cell(ssid, row, "HORA DE INICIO", hora)
        await update.message.reply_text(f"✅ Coordenadas de ingreso guardadas.\n🕑 Inicio registrado {hora}.\n\nUsa /breakout cuando salgas a break.")

    # Paso 9: coords salida
    elif paso == 9:
        user_data[chat_id]["paso"] = 10
        update_cell(ssid, row, "COORDENADAS SALIDA", f"{lat},{lng}")
        hora = datetime.now(LIMA_TZ).strftime("%H:%M")
        update_cell(ssid, row, "HORA DE SALIDA", hora)
        await update.message.reply_text(
            f"✅ Coordenadas de salida guardadas.\n🕑 Salida registrada {hora}.\n\n🎉 Registro completado con éxito.\n👏 ¡Gracias cuadrilla!"
        )

# Break out / in
async def breakout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ssid = user_data.get(chat_id, {}).get("spreadsheet_id")
    row = user_data.get(chat_id, {}).get("row")
    hora = datetime.now(LIMA_TZ).strftime("%H:%M")
    update_cell(ssid, row, "HORA DE SALIDA A BREAK", hora)
    user_data[chat_id]["paso"] = 5
    await update.message.reply_text(f"🍽️ Salida a break registrado {hora}. Usa /breakin al volver.")

async def breakin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ssid = user_data.get(chat_id, {}).get("spreadsheet_id")
    row = user_data.get(chat_id, {}).get("row")
    hora = datetime.now(LIMA_TZ).strftime("%H:%M")
    update_cell(ssid, row, "HORA DE REGRESO DE BREAK", hora)
    user_data[chat_id]["paso"] = 7
    await update.message.reply_text(f"🚶 Regreso de break registrado {hora}.")

async def salida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_data[chat_id]["paso"] = 8
    await update.message.reply_text("📸 Envía tu foto de salida.")

# ---------------- MAIN ----------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("breakout", breakout))
    app.add_handler(CommandHandler("breakin", breakin))
    app.add_handler(CommandHandler("salida", salida))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_texto))
    app.add_handler(MessageHandler(filters.PHOTO, manejar_foto))
    app.add_handler(MessageHandler(filters.LOCATION, manejar_ubicacion))

    app.add_handler(CallbackQueryHandler(handle_tipo, pattern="^tipo_"))

    print("🚀 Bot de Asistencia privado en ejecución...")
    app.run_polling()

if __name__ == "__main__":

    main()

