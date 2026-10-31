# ArtSafe

Keeps a copy of every saved version of your files, and keeps deleted files too.

## Install on her PC
1. Copy the whole `dist\ArtSafe` folder to her PC, e.g. to `C:\Program Files\ArtSafe` or `%LOCALAPPDATA%\Programs\ArtSafe`
   (it has to stay a folder: `ArtSafe.exe` needs the `_internal` folder next to it).
2. Run `ArtSafe.exe`. A short setup wizard finds folders with her art (Photoshop, Clip Studio, Illustrator files), asks about the cloud copy and Photoshop snapshots, and shows the one Photoshop setting to change. It can be run again from Settings → "Run setup again…".
3. Done. It starts with Windows and lives in the tray (shield icon near the clock).

Optional: right-click `ArtSafe.exe` → Show more options → Send to → Desktop (create shortcut).

## How it works
- Always backed up: Photoshop (.psd, .psb), Clip Studio (.clip) and Illustrator (.ai), all with previews. Common image formats (png, jpg, tiff…) are backed up too.
- Starts with Windows automatically. On every launch it re-registers itself, so this still works if the folder is moved or startup was disabled in Task Manager.
- A file is copied a few seconds after it's saved. Unchanged files aren't copied again, and identical copies are stored only once.
- Photoshop snapshots: every 5 minutes (adjustable), ArtSafe asks Photoshop to save a copy of the document she's working on into its own storage. Her real file is never touched. It only does this when the document has unsaved changes and she has used the mouse or keyboard since the last snapshot. It waits for an 8-second pause in her work, so Photoshop doesn't freeze mid-stroke (it gives up waiting after 5 minutes). Never-saved "Untitled" documents are covered too. **Snapshot Photoshop now** (in the tray menu and on the Unsaved work page) snapshots every open document immediately.
- Unsaved work: Photoshop secretly saves a recovery copy of each open document every few minutes (in `%APPDATA%\Adobe\Adobe Photoshop <version>\AutoRecover`), overwrites it each time, and deletes it when the document closes. ArtSafe keeps every one of those copies under **Unsaved work** for 14 days, so work that was never saved, or that was saved over, can be recovered. In Photoshop, set Edit → Preferences → File Handling → "Automatically Save Recovery Information Every" to **5 minutes**. Clip Studio's backup folder (`%APPDATA%\CELSYSUserData`) is kept the same way. These copies stay on the PC and aren't uploaded to the cloud.
- Deleted or renamed files are noticed. Deleted ones show up under **Deleted files**, and Windows shows a notification.
- **Restore** first saves the current file as a version, so a restore can always be undone.
- Old versions: every version is kept for 7 days, then one per day for 90 days. The newest copy of each file is always kept. All of this can be changed in Settings.
- Backups: `%LOCALAPPDATA%\ArtSafe\Vault`. Settings: `%APPDATA%\ArtSafe\config.json`.
- Cloud copy: if OneDrive is set up, the newest copy of every file is also placed in `OneDrive\ArtSafe Backup`, and OneDrive uploads it. Deletions are never copied there. Any other synced folder (Google Drive, Dropbox) can be chosen in Settings.
- Safety: ArtSafe only ever reads her files. The only times it writes to her folders are a Restore or "Save as a copy", and both back up whatever is there first. It pauses backups if fewer than 5 GB (or 5% of the disk) would be left free, so Photoshop can always save. It skips OneDrive "online-only" files, so nothing gets downloaded. It doesn't treat an unplugged drive as deleted files. Previews of huge files use the small preview stored inside the file, to save memory. If Photoshop ever fails to keep a snapshot as a separate copy, automatic snapshots switch off and she's told what to do.
- Same-disk backups protect against mistakes, not against the disk failing. That's what the cloud copy is for.

## Updates
ArtSafe checks GitHub Releases every 6 hours (and via Settings → "Check for updates"). New versions are
only installed if they're signed with the private key in `%USERPROFILE%\.artsafe\signing_key.pem`
(never commit or share it; without it you can't publish updates, so back it up somewhere safe).
Updates install while the window is closed, then ArtSafe restarts in the tray. If installing fails, the
old version is put back. Her backups and settings live in AppData and are never touched by updates.
Note: the ArtSafe folder must be somewhere she can write to (e.g. Documents), not Program Files.

To publish a new version:
```
powershell -ExecutionPolicy Bypass -File release.ps1 -Version 1.0.1 -Notes "What changed"
```

## Logs and error reports
- Log file on her PC: `%LOCALAPPDATA%\ArtSafe\logs\artsafe.log` (Settings → "Open log folder").
- Errors, crashes and notable events (update installed or failed, disk full, Photoshop safety stop)
  are posted to your Discord webhook, with her user name replaced by `~`. The same error is sent at
  most once every 6 hours, and at most 25 reports per day. She can turn this off in Settings.
- Settings → "Something wrong? Send a report…" sends her note plus the log file to the channel.
- The webhook URL lives in `%USERPROFILE%\.artsafe\webhook.txt`; `build.ps1` writes it into the
  build (`artsafe/_secrets.py`, git-ignored). It is never committed.

## Development
```
python -m venv .venv
.venv\Scripts\pip install PySide6 watchdog Pillow psd-tools pywin32 pyinstaller pytest
.venv\Scripts\python main.py          # run from source
.venv\Scripts\python -m pytest tests  # tests
.venv\Scripts\python tests\screenshots.py  # render screens with sample data
powershell -ExecutionPolicy Bypass -File build.ps1   # build dist\ArtSafe
```
