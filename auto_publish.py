import os
import json
import argparse
from datetime import datetime, timezone, timedelta
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from instagrapi import Client as IGClient
from tiktok_uploader.upload import upload_video

# Переменные Google Drive
GDRIVE_CLIENT_ID = os.getenv("GDRIVE_CLIENT_ID")
GDRIVE_CLIENT_SECRET = os.getenv("GDRIVE_CLIENT_SECRET")
GDRIVE_REFRESH_TOKEN = os.getenv("GDRIVE_REFRESH_TOKEN")

# Переменные соцсетей
IG_SESSION_ID = os.getenv("IG_SESSION_ID")
IG_COOKIES_TEXT = os.getenv("IG_COOKIES_TEXT", "")
TT_COOKIES_TEXT = os.getenv("TT_COOKIES_TEXT")

def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=GDRIVE_REFRESH_TOKEN,
        client_id=GDRIVE_CLIENT_ID,
        client_secret=GDRIVE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token"
    )
    creds.refresh(Request())
    return build('drive', 'v3', credentials=creds)

def find_today_folder(drive, date_str):
    query = f"mimeType = 'application/vnd.google-apps.folder' and name contains '_{date_str}' and trashed = false"
    results = drive.files().list(q=query, fields="files(id, name)").execute()
    items = results.get('files', [])
    return items[0] if items else None

def download_file(drive, file_id, output_path):
    request = drive.files().get_media(fileId=file_id)
    with open(output_path, 'wb') as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

def get_sessionid_from_netscape(cookies_text):
    """Вытаскивает sessionid из текста куки Netscape, если он передан целиком."""
    for line in cookies_text.splitlines():
        if 'sessionid' in line and not line.startswith('#'):
            parts = line.strip().split()
            if len(parts) >= 7:
                return parts[-1]
    return None

def publish_to_instagram(video_path, meta_data):
    """Публикация в Instagram Reels по sessionid."""
    caption = meta_data.get('caption', meta_data.get('description', ''))
    
    # Ищем sessionid либо в отдельной переменной, либо извлекаем из куки-файла
    session_id = IG_SESSION_ID or get_sessionid_from_netscape(IG_COOKIES_TEXT)
    
    if not session_id:
        raise Exception("Ошибка: Не найден sessionid для Instagram в Secrets!")

    cl = IGClient()
    print("Авторизация в Instagram...")
    cl.login_by_sessionid(session_id)
    
    print("Загрузка Reels в Instagram...")
    media = cl.clip_upload(path=video_path, caption=caption)
    print(f"Успешно опубликовано в Instagram Reels! ID: {media.pk}")

def publish_to_tiktok(video_path, meta_data):
    """Публикация в TikTok через эмуляцию с куки."""
    description = meta_data.get('description', meta_data.get('title', ''))
    
    cookies_file = "tiktok_cookies.txt"
    with open(cookies_file, "w", encoding="utf-8") as f:
        f.write(TT_COOKIES_TEXT)

    print("Запуск загрузки в TikTok...")
    try:
        upload_video(
            filename=video_path,
            description=description,
            cookies=cookies_file,
            headless=True
        )
        print("Успешно опубликовано в TikTok!")
    finally:
        if os.path.exists(cookies_file):
            os.remove(cookies_file)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["tiktok", "instagram"], required=True)
    args = parser.parse_args()

    tz_plus_3 = timezone(timedelta(hours=3))
    today_str = datetime.now(tz_plus_3).strftime("%Y-%m-%d")
    print(f"Поиск материалов на дату: {today_str} для: {args.platform}")

    drive = get_drive_service()
    folder = find_today_folder(drive, today_str)

    if not folder:
        print(f"Папка на дату {today_str} не найдена на Google Диске.")
        return

    results = drive.files().list(q=f"'{folder['id']}' in parents and trashed = false", fields="files(id, name)").execute()
    files = results.get('files', [])

    video_file = next((f for f in files if f['name'].endswith(('.mp4', '.mov'))), None)
    meta_file = next((f for f in files if f['name'].endswith(f"_{args.platform}.json")), None)

    if not video_file or not meta_file:
        print(f"Файлы для {args.platform} не найдены в папке {folder['name']}")
        return

    video_path = f"video_{today_str}.mp4"
    meta_path = f"meta_{args.platform}.json"

    download_file(drive, video_file['id'], video_path)
    download_file(drive, meta_file['id'], meta_path)

    with open(meta_path, 'r', encoding='utf-8') as f:
        meta_data = json.load(f)

    if args.platform == "tiktok":
        publish_to_tiktok(video_path, meta_data)
    elif args.platform == "instagram":
        publish_to_instagram(video_path, meta_data)

if __name__ == "__main__":
    main()
