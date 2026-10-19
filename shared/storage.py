import asyncio
from typing import TYPE_CHECKING
import boto3
import structlog
from botocore.exceptions import ClientError
from shared.config import BaseServiceSettings

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

logger = structlog.get_logger(__name__)

_clients: "dict[str, S3Client]" = {}


def _get_client(settings: BaseServiceSettings) -> "S3Client":
    key = settings.S3_ENDPOINT_URL
    if key not in _clients:
        _clients[key] = boto3.client(
            "s3", endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION, use_ssl=settings.S3_USE_SSL)
    return _clients[key]


async def ensure_bucket(settings: BaseServiceSettings, bucket: str) -> None:
    client = _get_client(settings)

    def _ensure() -> None:
        try:
            client.head_bucket(Bucket=bucket)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code not in ("404", "NoSuchBucket"):
                raise
            client.create_bucket(Bucket=bucket)

    await asyncio.to_thread(_ensure)
    logger.info("storage_bucket_ready", bucket=bucket)


async def upload_bytes(
        settings: BaseServiceSettings, *, bucket: str, key: str,
        content: bytes, content_type: str | None = None) -> None:
    client = _get_client(settings)

    def _upload() -> None:
        if content_type:
            client.put_object(
                Bucket=bucket, Key=key, Body=content, ContentType=content_type)
        else:
            client.put_object(Bucket=bucket, Key=key, Body=content)

    await asyncio.to_thread(_upload)


async def download_bytes(
        settings: BaseServiceSettings, *, bucket: str, key: str) -> bytes:
    client = _get_client(settings)

    def _download() -> bytes:
        response = client.get_object(Bucket=bucket, Key=key)
        body_bytes: bytes = response["Body"].read()
        return body_bytes

    return await asyncio.to_thread(_download)


async def delete_object(
        settings: BaseServiceSettings, *, bucket: str, key: str) -> None:
    client = _get_client(settings)

    def _delete() -> None:
        client.delete_object(Bucket=bucket, Key=key)

    await asyncio.to_thread(_delete)


async def list_object_keys(
        settings: BaseServiceSettings, *, bucket: str,
        prefix: str) -> list[str]:
    client = _get_client(settings)

    def _list() -> list[str]:
        paginator = client.get_paginator("list_objects_v2")
        return [
            item["Key"]
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
            for item in page.get("Contents", [])
        ]

    return await asyncio.to_thread(_list)


async def check_storage_connection(settings: BaseServiceSettings) -> bool:
    client = _get_client(settings)

    def _check() -> bool:
        client.list_buckets()
        return True

    try:
        return await asyncio.to_thread(_check)
    except Exception:
        return False


def close_storage_clients() -> None:
    _clients.clear()
