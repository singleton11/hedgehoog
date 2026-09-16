"""Create local configuration without overwriting existing secrets."""
from pathlib import Path
import os
import secrets

root = Path(__file__).resolve().parent.parent
path = root / ".env"
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print("Using existing .env")
else:
    template = (root / ".env.example").read_text()
    with os.fdopen(fd, "w") as output:
        output.write(template.replace("GENERATE_WITH_MAKE_SETUP", secrets.token_urlsafe(48)))
    print("Created .env with a random JWT secret")
