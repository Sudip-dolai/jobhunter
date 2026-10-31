"""Resume storage: always local; also uploads to a private Supabase bucket when configured."""
import requests

from app import config


def save_resume(user_id: int, data: bytes) -> str:
    folder = config.RESUME_DIR / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "resume.pdf"
    path.write_bytes(data)
    if config.SUPABASE_URL and config.SUPABASE_SERVICE_KEY:
        try:
            requests.post(
                f"{config.SUPABASE_URL}/storage/v1/object/{config.SUPABASE_BUCKET}/{user_id}/resume.pdf",
                headers={
                    "Authorization": f"Bearer {config.SUPABASE_SERVICE_KEY}",
                    "apikey": config.SUPABASE_SERVICE_KEY,
                    "Content-Type": "application/pdf",
                    "x-upsert": "true",
                },
                data=data,
                timeout=30,
            ).raise_for_status()
        except requests.RequestException as e:  # local copy is still saved
            print(f"Supabase upload failed: {e}")
    return str(path)
