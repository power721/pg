import os
from telegram.ext import Application, MessageHandler, filters

BOT_TOKEN = os.getenv('BOT_TOKEN')
MAX_FILE_SIZE = 20 * 1024 * 1024
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
            # Download the file if within size limits
            file = await context.bot.get_file(message.document)
            await file.download_to_drive(os.path.join(DOWNLOAD_FOLDER, file_name))

def main() -> None:
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(MessageHandler(filters.Document.ALL, downloader))

    print("run_polling")
    application.run_polling()

if __name__ == '__main__':
    main()
