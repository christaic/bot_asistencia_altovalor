import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ---------------- CONFIG ----------------
CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]

# ID de la unidad compartida
DRIVE_ID = "0AN8pG_lPt1dtUk9PVA"

# Carpeta dentro de la unidad compartida
MAIN_FOLDER_ID = "1OKL_s5Qs8VXbmhWFPDiJBqaaQArKQGG7"

# ---------------- GOOGLE SERVICES ----------------
SCOPES = ["https://www.googleapis.com/auth/drive"]
creds_info = json.loads(CREDENTIALS_JSON)
creds = service_account.Credentials.from_service_account_info(
    creds_info, scopes=SCOPES
)
drive_service = build("drive", "v3", credentials=creds)

# ---------------- TEST ----------------
try:
    # 1. Probar acceso a la unidad compartida
    resp_unit = drive_service.drives().get(driveId=DRIVE_ID).execute()
    print("✅ Acceso a la unidad compartida:", resp_unit)

    # 2. Probar acceso a la carpeta dentro de la unidad
    resp_folder = drive_service.files().get(
        fileId=MAIN_FOLDER_ID,
        fields="id, name",
        supportsAllDrives=True
    ).execute()
    print("✅ Acceso a la carpeta:", resp_folder)

    # 3. Intentar crear un archivo temporal dentro de la carpeta
    file_metadata = {
        "name": "prueba_bot.txt",
        "mimeType": "application/vnd.google-apps.document",
        "parents": [MAIN_FOLDER_ID],
    }
    temp_file = drive_service.files().create(
        body=file_metadata,
        fields="id, name",
        supportsAllDrives=True
    ).execute()
    print("✅ Archivo de prueba creado:", temp_file)

    # 4. Eliminar el archivo de prueba
    drive_service.files().delete(
        fileId=temp_file["id"],
        supportsAllDrives=True
    ).execute()
    print("🧹 Archivo de prueba eliminado correctamente.")

except Exception as e:
    print("❌ Error de acceso o permisos:", e)
