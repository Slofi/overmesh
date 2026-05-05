import os
import secrets

from db import get_auth_setting


def load_secret_key(data_dir):
    """Load or generate a persistent Flask secret key stored in DATA_DIR/secret.key."""
    key_path = os.path.join(data_dir, "secret.key")
    try:
        with open(key_path) as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    with open(key_path, "w") as f:
        f.write(key)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key


def is_auth_enabled():
    return get_auth_setting("auth_enabled", "0") == "1"


def check_credentials(username, password):
    from werkzeug.security import check_password_hash
    stored_user = get_auth_setting("auth_username", "")
    stored_hash = get_auth_setting("auth_password_hash", "")
    if not stored_user or not stored_hash:
        return False
    return username == stored_user and check_password_hash(stored_hash, password)
