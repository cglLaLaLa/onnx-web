from io import BytesIO
from logging import getLogger
from typing import List, Optional

from boto3 import Session
from PIL import Image

from ..params import ImageParams, StageParams
from ..server import ServerContext
from ..worker import WorkerContext
from .stage import BaseStage

logger = getLogger(__name__)


class PersistS3Stage(BaseStage):
    def run(
        self,
        _job: WorkerContext,
        server: ServerContext,
        _stage: StageParams,
        _params: ImageParams,
        sources: List[Image.Image],
        *,
        output: str,
        bucket: str,
        endpoint_url: Optional[str] = None,
        profile_name: Optional[str] = None,
        stage_source: Optional[Image.Image] = None,
        **kwargs,
    ) -> List[Image.Image]:
        session = Session(profile_name=profile_name)
        s3 = session.client("s3", endpoint_url=endpoint_url)

        for source in sources:
            data = BytesIO()
            source.save(data, format=server.image_format)
            data.seek(0)

            try:
                s3.upload_fileobj(data, bucket, output)
                logger.info("saved image to s3://%s/%s", bucket, output)
            except Exception:
                logger.exception("error saving image to S3")

        return sources
