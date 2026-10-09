"""Inert sample plugin for video downloads.

External downloading and uploading paths are completely disabled for security.
"""

ENABLED = False


def _upload(*args, **kwargs):
    raise NotImplementedError("Video uploading is completely disabled.")


def _download(*args, **kwargs):
    raise NotImplementedError("Video downloading is completely disabled.")


async def on_message(bot, msg):
    """Inert stub: video downloading and uploading paths are disabled."""
    pass
