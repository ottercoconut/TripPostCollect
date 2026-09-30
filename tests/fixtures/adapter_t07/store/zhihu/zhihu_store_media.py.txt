"""TripPostCollect Zhihu body-image staging and manifest storage."""

from __future__ import annotations

from trippostcollect.core.paths import MEDIACRAWLER_DIR

from pathlib import Path
from typing import Dict, List

import config
from base.base_crawler import AbstractStoreImage
from tools import utils
from tools.image_manifest import zhihu_source_asset_key
from trippostcollect.artifacts.image_staging import PostImageStager


class ZhihuStoreImage(PostImageStager, AbstractStoreImage):
    def __init__(self):
        super().__init__(
            save_data_root=Path(config.SAVE_DATA_PATH) if config.SAVE_DATA_PATH else MEDIACRAWLER_DIR / "data",
            platform="zhihu",
            source_key="image_list",
            source_asset_key=lambda item: zhihu_source_asset_key(item["url"]),
            log_saved=lambda count, content_id: utils.logger.info(
                f"[ZhihuStoreImage.store_post_images] saved {count} "
                f"body images for content {content_id}"
            ),
        )

    async def store_post_images(self, content_id: str, image_content_items: List[Dict]):
        return await super().store_post_images(content_id, image_content_items)

    async def record_failure(self, content_id: str, image_content_item: Dict):
        return await super().record_failure(content_id, image_content_item)
