from pathlib import Path
import json
import shutil

# Create upload directory with subdirectories
UPLOAD_DIR = Path("C:/qapariksha_uploads")
UPLOADED_DIR = UPLOAD_DIR / "uploaded"
IN_PROGRESS_DIR = UPLOAD_DIR / "in_progress"
COMPLETED_DIR = UPLOAD_DIR / "completed"
FAILED_DIR = UPLOAD_DIR / "failed"

# Create all directories
for directory in [UPLOADED_DIR, IN_PROGRESS_DIR, COMPLETED_DIR, FAILED_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

# Create storage file
STORAGE_FILE = Path("file_path_storage.json")
if not STORAGE_FILE.exists():
    STORAGE_FILE.write_text(json.dumps({}, indent=4))

def load_storage() -> dict:
    if STORAGE_FILE.exists():
        try:
            return json.loads(STORAGE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}

def save_storage(data: dict) -> None:
    STORAGE_FILE.write_text(json.dumps(data, indent=4))

def set_file_record(uuid: str, data: dict) -> None:
    storage = load_storage()
    storage[uuid] = data
    save_storage(storage)

def get_file_record(uuid: str) -> dict | None:
    storage = load_storage()
    return storage.get(uuid)

def list_all_uuids() -> list:
    storage = load_storage()
    return list(storage.keys())

def delete_file_record(uuid: str) -> bool:
    storage = load_storage()
    if uuid in storage:
        del storage[uuid]
        save_storage(storage)
        return True
    return False

def clear_storage() -> None:
    save_storage({})

def move_file_to_status(uuid: str, target_dir: Path) -> str | None:
    """
    Move the file associated with a UUID to a target subdirectory.
    Updates the file_path in JSON storage to reflect new location.
    Returns the new file path as string, or None if not found.
    """
    storage = load_storage()
    record = storage.get(uuid)

    if not record:
        print(f"⚠️ move_file_to_status: UUID {uuid} not found in storage")
        return None
    current_path = Path(record["file_path"])

    if not current_path.exists():
        print(f"⚠️ move_file_to_status: File not found at {current_path}")
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    new_path = target_dir / current_path.name

    # Handle filename collision
    if new_path.exists() and new_path != current_path:
        stem = current_path.stem
        suffix = current_path.suffix
        new_path = target_dir / f"{stem}_{uuid[:8]}{suffix}"

    shutil.move(str(current_path), str(new_path))
    print(f"📁 Moved file: {current_path} → {new_path}")

    # Update stored path
    storage[uuid]["file_path"] = str(new_path)
    save_storage(storage)
    return str(new_path)
