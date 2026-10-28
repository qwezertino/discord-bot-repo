import logging
from urllib.parse import unquote


class OAuthQueryRedaction(logging.Filter):
    """Uvicorn access logs must not retain one-time OAuth codes or state."""
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            path = str(args[2]).split("?", 1)[0]
            if unquote(path).endswith("/auth/discord/callback"):
                args[2] = path
                record.args = tuple(args)
        return True


def install_oauth_log_filter() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, OAuthQueryRedaction) for item in logger.filters):
        logger.addFilter(OAuthQueryRedaction())
