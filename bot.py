import os
import re
import subprocess
from telegram.ext import Application, MessageHandler, filters

BOT_TOKEN = os.getenv('BOT_TOKEN')
MAX_FILE_SIZE = 50 * 1024 * 1024
DOWNLOAD_FOLDER = 'downloads'

async def downloader(update, context):
    message = update.message
    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name
        file_size = message.document.file_size

        print(f"Received file: {file_name} with file ID: {file_id} and size: {file_size} bytes")
        # Check if file exceeds the size limit
        if file_size > MAX_FILE_SIZE:
            await update.message.reply_text("Sorry, the file is too large to download (max 20MB).")
            print(f"File {file_name} exceeds size limit. Skipping download.")
        else:
            match = re.match(r'pg\.(\d{8})-(\d{4}).zip', file_name)
            if match:
                subprocess.call(["git", "pull"])
                f = open("version.txt", "r")
                old_version = int(f.read().replace("-", ""))
                new_version = int (match.group(1) + match.group(2))
                print(f"Old version: {old_version} New version: {new_version}")
                if new_version > old_version:
                    file = await context.bot.get_file(message.document)
                    await file.download_to_drive(os.path.join(DOWNLOAD_FOLDER, file_name))
                    with open("version.txt", "w") as text_file:
                        version = f"{match.group(1)}-{match.group(2)}"
                        text_file.write(version)
                        subprocess.call(["git", "commit", "-am", f"update {version}"])
                        subprocess.call(["git", "push", "origin", "main"])
                        subprocess.call(["git", "push", "lab", "main"])
            else:
                print(f'Ignore file {file_name}')

def main() -> None:
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(MessageHandler(filters.Document.ALL, downloader))

    print("run_polling")
    application.run_polling()

if __name__ == '__main__':
    main()
