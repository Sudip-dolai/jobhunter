import os
import tempfile

# Must run before any app import so tests use a throwaway database.
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["SECRET_KEY"] = "test-secret"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
