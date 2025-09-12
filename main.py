import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ---------------- CONFIG ----------------
CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
MAIN_FOLDER_ID = "1OKL_s5Qs8VXbmhWFPDUiJ8qQaQArKQGG7"  # tu carpeta en unidad compartida

# ---------------- GOOGLE SERVICES ----------------
SCOPES = ["https://www.googleapis.com/auth/drive"]

creds_info = json.loads(CREDENTIALS_JSON)
creds = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
drive_service = build("drive", "v3", credentials=creds)

# ---------------- TEST ----------------
try:
    resp = drive_service.files().get(
        fileId=MAIN_FOLDER_ID,
        fields="id, name",
        supportsAllDrives=True
    ).execute()
    print("✅ Acceso correcto a la carpeta:", resp)
except Exception as e:
    print("❌ Error de acceso:", e)
