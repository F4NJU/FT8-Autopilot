import logging
from pathlib import Path

from .adif import ImportResult, import_adif
from .service import WorkedTodayService

LOGGER = logging.getLogger(__name__)


def synchronize_adif(
    path: Path,
    service: WorkedTodayService,
    enabled: bool = True,
) -> ImportResult | None:
    if not enabled:
        LOGGER.info("[ADIF] startup synchronization disabled")
        return None
    if not path.is_file():
        LOGGER.warning("[ADIF] source=WSJTX_ADIF path=%s not found; continuing", path)
        return None
    try:
        result = import_adif(path, service.store, service.bands, source="WSJTX_ADIF")
    except OSError:
        LOGGER.exception("[ADIF] unable to read %s; continuing", path)
        return None
    service.refresh()
    return result
