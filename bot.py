from datetime import datetime
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import json
import os
import re
import zipfile

import requests
from telethon import TelegramClient, events
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import ChannelParticipantCreator

# Environment variables
API_ID = os.getenv('API_ID')  # Your API ID (from my.telegram.org)
API_HASH = os.getenv('API_HASH')  # Your API Hash (from my.telegram.org)
zoneId = os.getenv('CF_ZONE_ID')
apiKey = os.getenv('CF_API_KEY')
email = os.getenv('CF_EMAIL')
TOKEN = os.getenv("GITHUB_TOKEN")
MAX_FILE_SIZE = 200 * 1024 * 1024  # Max file size 200MB
DOWNLOAD_FOLDER = 'downloads'

channels = [2046444460, 2188783347, 1943841872, 1890409212, 1734222246]
PG_JAR_GROUP = 1943841872
VERSION_FILE = 'pg.version'
# monitored files in PG_JAR_GROUP: {downloaded filename: path inside the pg zip}
PACKAGE_FILES = {
    'pg.jar': 'pg.jar',
    'aliproxy.tar.xz': 'lib/aliproxy.tar.xz',
}
# Initialize the TelegramClient
client = TelegramClient("bot", API_ID, API_HASH)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
handler = RotatingFileHandler(
    'app.log',
    maxBytes=10 * 1024 * 1024,
    backupCount=7
)
logger.addHandler(handler)

# Create the download folder if it doesn't exist
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)


def read_version():
    try:
        with open(VERSION_FILE, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def write_version(version):
    with open(VERSION_FILE, "w") as f:
        f.write(version)
        f.flush()
        os.fsync(f.fileno())
        logger.info(f"update version: {version} for {VERSION_FILE}")


def save_latest(zip_path, new_version):
    """Keep zip_path as the latest local base package and update pg.version.

    Removes the previous versioned pg.<old>.zip so only the latest remains.
    """
    old = read_version()
    write_version(new_version)
    if old and old != new_version:
        old_zip = f"pg.{old}.zip"
        if os.path.exists(old_zip):
            os.remove(old_zip)
            logger.info(f"removed old base zip: {old_zip}")


def build_pg_zip(base_zip, out_zip, files):
    """Rebuild base_zip into out_zip, injecting/replacing the given files.

    files: list of (entry_name, local_path) — entry_name is the exact path
    inside the zip (e.g. 'pg.jar', 'lib/aliproxy.tar.xz'). A '<entry>.md5'
    sidecar is refreshed only if one already exists in the base zip.
    """
    payloads = {}
    for entry, local in files:
        with open(local, "rb") as f:
            payloads[entry] = f.read()

    with zipfile.ZipFile(base_zip, "r") as zin:
        base_names = set(zin.namelist())
    sidecars = {e + ".md5" for e in payloads if e + ".md5" in base_names}

    replaced = set()
    with zipfile.ZipFile(base_zip, "r") as zin, zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename in payloads:
                zout.writestr(item, payloads[item.filename])
                replaced.add(item.filename)
            elif item.filename in sidecars:
                zout.writestr(item, hashlib.md5(payloads[item.filename[:-4]]).hexdigest())
            else:
                zout.writestr(item, zin.read(item.filename))
        for entry in payloads:
            if entry not in replaced:
                zout.writestr(entry, payloads[entry])
                logger.info(f"{entry} not found in base zip; added")
    logger.info(f"built {out_zip} with {list(payloads)}")


async def is_owner(chat_id, user_id):
    owner_id = os.getenv("PG_OWNER_ID")
    if user_id is None:
        # Anonymous admin / channel post: sender is hidden (sender_id=None).
        # In this controlled group only the owner posts, so treat it as owner.
        logger.info("owner check: sender is anonymous (None); treating as owner")
        return True
    if owner_id:
        return str(user_id) == str(owner_id)
    try:
        res = await client(GetParticipantRequest(chat_id, user_id))
        return isinstance(res.participant, ChannelParticipantCreator)
    except Exception as e:
        logger.warning(f"owner check failed for {user_id} in {chat_id}: {e}")
        return False


def release(zip_file, new_version, repo, body=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {TOKEN}"
    }

    # 1️⃣ Create a release
    logger.info(f"Creating release {new_version}...")

    release_data = {
        "tag_name": new_version,
        "target_commitish": "main",
        "name": new_version,
        "body": body or new_version,
        "draft": False,
        "prerelease": False
    }

    r = requests.post(
        f"https://api.github.com/repos/power721/{repo}/releases",
        headers=headers,
        data=json.dumps(release_data)
    )

    if r.status_code not in (200, 201):
        logger.warning(f"❌ Failed to create release: {r.text}")
        return

    release = r.json()
    upload_url = release["upload_url"].split("{")[0]
    logger.info(f"✅ Created release: {release['html_url']}")

    # 2️⃣ Upload ZIP asset
    if not os.path.exists(zip_file):
        logger.warning(f"❌ File not found: {zip_file}")
        return

    logger.info(f"Uploading asset: {zip_file}...")
    with open(zip_file, "rb") as f:
        upload_headers = headers.copy()
        upload_headers["Content-Type"] = "application/zip"

        upload_url = f"{upload_url}?name={os.path.basename(zip_file)}"
        ur = requests.post(upload_url, headers=upload_headers, data=f)

    if ur.status_code not in (200, 201):
        logger.warning(f"❌ Failed to upload asset: {ur.text}")
        return

    logger.info("✅ Uploaded asset successfully.")
    logger.info(f"🔗 Asset URL: {ur.json()['browser_download_url']}")


@client.on(events.NewMessage(chats=channels))
async def downloader(event):
    message = event.message
    channel_name = message.chat.title
    channel_id = message.chat.id
    logger.info(f"From: {channel_id} {channel_name}")
    if message.document:
        file_name = message.file.name
        file_size = message.document.size

        logger.info(f"Received file: {file_name} with size: {file_size} bytes")

        # Check if file exceeds the size limit
        if file_size > MAX_FILE_SIZE:
            logger.info(f"File {file_name} exceeds size limit. Skipping download.")
            return

        match = re.match(r'pg\.(\d{8}-\d{4}).zip', file_name)
        if match:
            new_version = match.group(1)
            logger.info(f"New version: {new_version}")

            await client.download_media(message, file_name)
            body = (message.message or "").strip() or new_version
            release(file_name, new_version, "PG", body=body)
            save_latest(file_name, new_version)

        else:
            # 真心20250406-增量包.zip
            match = re.match(r'真心(\d{8})-?(\d)?-?增量包.zip', file_name)
            if match:
                new_version = datetime.now().strftime("%Y%m%d-%H%M")
                logger.info(f"New version: {new_version}")
                new_file = f"zx{new_version}.zip"

                await client.download_media(message, new_file)
                release(new_file, new_version, "ZX")
                os.remove(new_file)

            else:
                # 真心20250402-全量包.zip
                # match = re.match(r'真心(\d{8})-?(\d)?-(全量包|完整包).zip', file_name)
                # if match:
                #     new_version = datetime.now().strftime("%Y%m%d-%H%M")
                #     logger.info(f"New version: {new_version}")
                #     new_file = f"zx-{new_version}.zip"
                #
                #     await client.download_media(message, "zx.base.zip")
                #     commit(new_file, "zx.base.zip")
                #
                # else:
                #     logger.info(f"Ignoring file {file_name}, does not match version pattern.")
                logger.info(f"Ignoring file {file_name}, does not match version pattern.")


@client.on(events.NewMessage(chats=[PG_JAR_GROUP]))
async def package_updater(event):
    message = event.message
    if not message.document:
        return
    name = os.path.basename(message.file.name or "").lower()
    entry = PACKAGE_FILES.get(name)
    if entry is None:
        return
    if not await is_owner(event.chat_id, event.sender_id):
        logger.info(f"Ignoring {name} from non-owner {event.sender_id}")
        return

    logger.info(f"Received {name} from owner {event.sender_id} in {event.chat_id}")

    await client.download_media(message, name)

    base_version = read_version()
    base_zip = f"pg.{base_version}.zip"
    if not base_version or not os.path.exists(base_zip):
        logger.error(f"Base zip not found: {base_zip}; skipping {name}")
        if os.path.exists(name):
            os.remove(name)
        return

    new_version = datetime.now().strftime("%Y%m%d-%H%M")
    if new_version == base_version:
        logger.info(f"Same-minute collision with base version {new_version}; skipping")
        os.remove(name)
        return

    out_zip = f"pg.{new_version}.zip"
    build_pg_zip(base_zip, out_zip, [(entry, name)])

    body = (message.message or "").strip() or new_version
    release(out_zip, new_version, "PG", body=body)
    save_latest(out_zip, new_version)

    os.remove(name)


# Run the bot
if __name__ == '__main__':
    logger.info("Bot is running...")
    client.start()
    client.run_until_disconnected()

