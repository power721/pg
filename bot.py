import asyncio
from datetime import datetime
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import json
import os
import re
import time
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
UPLOAD_RETRIES = 3
ALBUM_DEBOUNCE = 3.0  # seconds to wait for all files in an album before building one combined release
DOWNLOAD_FOLDER = 'downloads'

channels = [2046444460, 2188783347, 1943841872, 1890409212, 1734222246]
PG_JAR_GROUP = 1943841872
VERSION_FILE = 'pg.version'
# monitored files in PG_JAR_GROUP: {downloaded filename: path inside the pg zip}
PACKAGE_FILES = {
    'pg.jar': 'pg.jar',
    'aliproxy.tar.xz': 'lib/aliproxy.tar.xz',
    'allinone.tar.xz': 'lib/allinone.tar.xz',
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


def _asset_exists(repo, release_id, asset_name, headers):
    """Return True if the release already carries an asset named asset_name."""
    try:
        resp = requests.get(
            f"https://api.github.com/repos/power721/{repo}/releases/{release_id}/assets",
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 200:
            return any(a.get("name") == asset_name for a in resp.json())
    except requests.exceptions.RequestException as e:
        logger.warning(f"asset existence check failed: {e}")
    return False


def _delete_release_and_tag(repo, release_id, tag, headers):
    """Delete a release and the tag created with it.

    Deleting a release does not delete its tag, so an orphan tag would otherwise
    block recreating the same version (422). Call only when rolling back a
    release this bot just created.
    """
    try:
        requests.delete(
            f"https://api.github.com/repos/power721/{repo}/releases/{release_id}",
            headers=headers,
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        logger.warning(f"❌ Failed to delete release {release_id}: {e}")
    try:
        requests.delete(
            f"https://api.github.com/repos/power721/{repo}/git/refs/tags/{tag}",
            headers=headers,
            timeout=30,
        )
        logger.info(f"removed orphan tag {tag}")
    except requests.exceptions.RequestException as e:
        logger.warning(f"❌ Failed to delete tag {tag}: {e}")


def release(zip_file, new_version, repo, body=None, asset_name=None):
    """Create a GitHub release and upload zip_file as an asset.

    Returns True only when the asset is confirmed present on the release.
    Upload retries are idempotent: a 422 already_exists means a prior attempt
    already uploaded the asset (we just missed the success response) and is
    treated as success; and we never roll back a release that actually carries
    the asset. On a genuine failure the release and its generated tag are both
    removed so the same version can be retried.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {TOKEN}"
    }
    asset_name = asset_name or os.path.basename(zip_file)

    if not os.path.exists(zip_file):
        logger.error(f"❌ File not found, aborting release: {zip_file}")
        return False

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

    try:
        r = requests.post(
            f"https://api.github.com/repos/power721/{repo}/releases",
            headers=headers,
            data=json.dumps(release_data),
            timeout=30
        )
    except requests.exceptions.RequestException as e:
        logger.warning(f"❌ Failed to create release (network): {e}")
        return False

    if r.status_code not in (200, 201):
        logger.warning(f"❌ Failed to create release: {r.text}")
        return False

    rel = r.json()
    rel_id = rel["id"]
    upload_base = rel["upload_url"].split("{")[0]
    logger.info(f"✅ Created release: {rel['html_url']}")

    # 2️⃣ Upload ZIP asset. Idempotent: a prior attempt may have uploaded the
    # asset while we missed the success response, in which case GitHub returns
    # 422 already_exists — treat that as success rather than a retry failure.
    upload_headers = headers.copy()
    upload_headers["Content-Type"] = "application/zip"
    asset_url = f"{upload_base}?name={asset_name}"

    logger.info(f"Uploading asset: {asset_name}...")
    uploaded = False
    for attempt in range(UPLOAD_RETRIES):
        try:
            with open(zip_file, "rb") as f:
                ur = requests.post(asset_url, headers=upload_headers, data=f, timeout=300)
            if ur.status_code in (200, 201):
                uploaded = True
                logger.info("✅ Uploaded asset successfully.")
                logger.info(f"🔗 Asset URL: {ur.json().get('browser_download_url')}")
                break
            if ur.status_code == 422 and "already_exists" in ur.text:
                uploaded = True
                logger.info("✅ Asset already present (422 already_exists); treating as uploaded.")
                break
            logger.warning(f"❌ Upload attempt {attempt + 1}/{UPLOAD_RETRIES} failed: {ur.status_code} {ur.text[:200]}")
        except requests.exceptions.RequestException as e:
            logger.warning(f"❌ Upload attempt {attempt + 1}/{UPLOAD_RETRIES} error: {e}")
        if attempt < UPLOAD_RETRIES - 1:
            time.sleep(2 ** attempt)

    # 3️⃣ Never roll back a release that actually has the asset (covers a
    # success whose response was lost without a subsequent 422 reaching us).
    if not uploaded and _asset_exists(repo, rel_id, asset_name, headers):
        logger.info("✅ Asset found on release despite upload errors; keeping release.")
        return True

    # 4️⃣ Genuine failure: remove the release AND its tag so the version is retriable.
    if not uploaded:
        logger.error(f"❌ Asset upload failed after {UPLOAD_RETRIES} attempts; rolling back release {rel_id} and tag {new_version}")
        _delete_release_and_tag(repo, rel_id, new_version, headers)
        return False

    return True


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

            # Download to a temp path so a failed or duplicate release can never
            # overwrite or delete the active base package (pg.<base version>.zip).
            tmp_path = f"{file_name}.part"
            await client.download_media(message, tmp_path)
            body = (message.message or "").strip() or new_version
            if release(tmp_path, new_version, "PG", body=body, asset_name=file_name):
                os.replace(tmp_path, file_name)
                save_latest(file_name, new_version)
            elif os.path.exists(tmp_path):
                os.remove(tmp_path)
            return

        # 真心20250406-增量包.zip
        match = re.match(r'真心(\d{8})-?(\d)?-?增量包.zip', file_name)
        if match:
            new_version = datetime.now().strftime("%Y%m%d-%H%M")
            logger.info(f"New version: {new_version}")
            new_file = f"zx{new_version}.zip"

            await client.download_media(message, new_file)
            release(new_file, new_version, "ZX")
            os.remove(new_file)
            return

        # zx20250406.zip
        match = re.match(r'zx(\d{8})-?(\d)?.zip', file_name)
        if match:
            new_version = datetime.now().strftime("%Y%m%d-%H%M")
            logger.info(f"New version: {new_version}")
            new_file = f"zx{new_version}.zip"

            await client.download_media(message, new_file)
            release(new_file, new_version, "ZX")
            os.remove(new_file)
            return

        logger.info(f"Ignoring file {file_name}, does not match version pattern.")


# Album buffering: collect files posted together into one combined release.
_album_data = {}   # grouped_id -> {"chat_id": int, "entries": [(message, name, entry)]}
_album_tasks = {}  # grouped_id -> asyncio.Task


async def resolve_caption(chat_id, entries, grouped_id):
    """Best-effort caption for the package(s) being released.

    Albums carry the caption on a single message (often a non-file item like a
    photo), so for albums we search the surrounding messages for one with the
    same grouped_id. Re-fetching also reflects edits made before we read.
    Returns "" when no caption is found.
    """
    ref = entries[0][0]
    if grouped_id is not None:
        try:
            msgs = await client.get_messages(chat_id, min_id=ref.id - 20, max_id=ref.id + 20)
        except Exception as e:
            logger.warning(f"album caption fetch failed: {e}")
            msgs = []
        for m in msgs or []:
            if m and m.grouped_id == grouped_id and (m.message or "").strip():
                return m.message.strip()
        return ""
    try:
        fresh = await client.get_messages(chat_id, ids=ref.id)
    except Exception as e:
        logger.warning(f"caption fetch failed: {e}")
        return ""
    return (fresh.message or "").strip() if fresh else ""


async def process_package_files(entries, chat_id, grouped_id=None):
    """Build one release from the given package files.

    entries: list of (message, name, entry). Downloads each, rebuilds the base
    zip injecting/replacing every entry, then releases once with the album
    caption (if any) as the body. save_latest only runs on a successful upload.
    """
    names = [name for (_, name, _) in entries]

    for message, name, _ in entries:
        await client.download_media(message, name)

    def cleanup():
        for name in names:
            if os.path.exists(name):
                os.remove(name)

    base_version = read_version()
    base_zip = f"pg.{base_version}.zip"
    if not base_version or not os.path.exists(base_zip):
        logger.error(f"Base zip not found: {base_zip}; skipping {names}")
        cleanup()
        return

    new_version = datetime.now().strftime("%Y%m%d-%H%M")
    if new_version == base_version:
        logger.info(f"Same-minute collision with base version {new_version}; skipping")
        cleanup()
        return

    out_zip = f"pg.{new_version}.zip"
    build_pg_zip(base_zip, out_zip, [(entry, name) for (_, name, entry) in entries])

    body = await resolve_caption(chat_id, entries, grouped_id) or new_version
    if release(out_zip, new_version, "PG", body=body):
        save_latest(out_zip, new_version)
    elif os.path.exists(out_zip):
        os.remove(out_zip)

    cleanup()


async def _flush_album(grouped_id):
    """Wait for an album to finish arriving, then build one combined release."""
    try:
        await asyncio.sleep(ALBUM_DEBOUNCE)
    except asyncio.CancelledError:
        return  # a later file in the album reset the debounce timer
    data = _album_data.pop(grouped_id, None)
    _album_tasks.pop(grouped_id, None)
    if not data:
        return
    try:
        logger.info(f"Flushing album grouped_id={grouped_id} with {[n for (_, n, _) in data['entries']]}")
        await process_package_files(data["entries"], data["chat_id"], grouped_id=grouped_id)
    except Exception:
        logger.exception(f"album flush failed for grouped_id={grouped_id}")
        for (_, name, _) in data["entries"]:
            if os.path.exists(name):
                os.remove(name)


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

    grouped_id = message.grouped_id
    if grouped_id is None:
        # Single package file: build and release immediately.
        await process_package_files([(message, name, entry)], event.chat_id)
        return

    # Album: buffer all files, debounce, then one combined release.
    data = _album_data.setdefault(grouped_id, {"chat_id": event.chat_id, "entries": []})
    data["entries"].append((message, name, entry))
    existing = _album_tasks.get(grouped_id)
    if existing:
        existing.cancel()
    _album_tasks[grouped_id] = asyncio.create_task(_flush_album(grouped_id))


# Run the bot
if __name__ == '__main__':
    logger.info("Bot is running...")
    client.start()
    client.run_until_disconnected()

