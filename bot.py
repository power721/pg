import os
import re
import subprocess
import shutil
from telethon import TelegramClient, events

# Environment variables
API_ID = os.getenv('API_ID')  # Your API ID (from my.telegram.org)
API_HASH = os.getenv('API_HASH')  # Your API Hash (from my.telegram.org)
MAX_FILE_SIZE = 100 * 1024 * 1024  # Max file size 100MB
DOWNLOAD_FOLDER = 'downloads'

# Initialize the TelegramClient
client = TelegramClient("bot", API_ID, API_HASH)

# Create the download folder if it doesn't exist
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)


@client.on(events.NewMessage)
async def downloader(event):
    message = event.message
    if message.chat is None:
        return
    channel_name = message.chat.title
    channel_id = message.chat.id
    print(f"From: {channel_id} {channel_name}")
    if channel_id == 2046444460 and message.document:
        file_name = message.file.name
        mime_type = message.document.mime_type
        file_size = message.document.size

        print(f"Received file: {mime_type} {file_name} with size: {file_size} bytes")

        # Check if file exceeds the size limit
        if file_size > MAX_FILE_SIZE:
            print(f"File {file_name} exceeds size limit. Skipping download.")
        else:
            match = re.match(r'pg\.(\d{8})-(\d{4}).zip', file_name)
            if match:
                subprocess.call(["git", "pull"])
                with open("version.txt", "r") as f:
                    old_version = int(f.read().replace("-", ""))
                new_version = int(match.group(1) + match.group(2))

                print(f"Old version: {old_version} New version: {new_version}")

                if new_version > old_version:
                    # Download the file to the specified folder
                    file_path = os.path.join(DOWNLOAD_FOLDER, file_name)
                    await client.download_media(message, file_path)
                    shutil.move(file_path, "./pg.zip")

                    with open("version.txt", "w") as text_file:
                        version = f"{match.group(1)}-{match.group(2)}"
                        text_file.write(version)

                        # Git operations: commit, push
                        subprocess.call(["git", "commit", "-am", f"update {version}"])
                        subprocess.call(["git", "push", "origin", "main"])
                        subprocess.call(["git", "push", "lab", "main"])
                else:
                    print(f"Ignoring file {file_name}, new version is not greater than old version.")
            else:
                print(f"Ignoring file {file_name}, does not match version pattern.")


# Run the bot
if __name__ == '__main__':
    print("Bot is running...")
    client.start()
    client.run_until_disconnected()
