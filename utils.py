import secrets
import string

from config import CODE_LENGTH


def generate_code(length: int = CODE_LENGTH) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def generate_unique_code(length: int = CODE_LENGTH) -> str:
    """Generate a code that doesn't exist in DB. Retries on collision."""
    import db
    for _ in range(10):
        code = generate_code(length)
        if not await db.code_exists(code):
            return code
    # Fallback: use longer code
    return generate_code(length + 4)
