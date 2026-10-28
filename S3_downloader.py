from __future__ import annotations

import asyncio
import os
import random

import s3fs
from botocore.exceptions import ClientError
from netCDF4 import Dataset

from utils import log_error, log_warning, GLM_PRODUCT

CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 30
DEFAULT_MAX_CONCURRENT_DOWNLOADS = 16
MIN_VALID_FILE_SIZE_BYTES = 8 * 1024
MAX_RETRIES = 5
BASE_BACKOFF_S = 1.5
MAX_BACKOFF_S = 30.0

def build_hour_path(sat_bucket, dt, product_code):
    return f"{sat_bucket}/{product_code}/{dt.year}/{dt.timetuple().tm_yday:03d}/{dt.hour:02d}/"


def build_file_prefix(dt, product_code):
    is_glm = product_code == GLM_PRODUCT
    prefix = f"s{dt.year:04d}{dt.timetuple().tm_yday:03d}{dt.hour:02d}{dt.minute:02d}"
    return f"{prefix}{dt.second:02d}" if is_glm else prefix


def create_async_s3_fs():
    return s3fs.S3FileSystem(
        anon=True,
        asynchronous=True,
        config_kwargs={
            "connect_timeout": CONNECT_TIMEOUT_S,
            "read_timeout": READ_TIMEOUT_S,
            "retries": {"max_attempts": 0},
        },
    )


def _is_not_found_error(exc: Exception) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    if isinstance(exc, ClientError):
        error_code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return error_code in ("404", "NoSuchKey", "NotFound") or status == 404
    return False


def validate_nc_file(local_path: str, required_variables: list[str] | None = None) -> tuple[bool, str]:
    if not os.path.exists(local_path):
        return False, "file does not exist on disk after download"
    file_size = os.path.getsize(local_path)
    if file_size < MIN_VALID_FILE_SIZE_BYTES:
        return False, f"file looks truncated/empty ({file_size} bytes)"
    try:
        with Dataset(local_path, "r") as ds:
            if required_variables:
                missing = [v for v in required_variables if v not in ds.variables]
                if missing:
                    return False, f"variables missing from the NetCDF: {missing}"
                for var_name in required_variables:
                    _ = ds.variables[var_name][:1]
        return True, "ok"
    except Exception as e:
        return False, f"corrupted or unreadable NetCDF: {e}"


async def _download_one(
    fs: s3fs.S3FileSystem,
    semaphore: asyncio.Semaphore,
    remote_path: str,
    local_path: str,
    required_variables: list[str] | None,
    max_retries: int,
    label: str,
) -> tuple[str, bool, str]:
    last_error_msg = "unknown reason"
    async with semaphore:
        for attempt in range(1, max_retries + 1):
            try:
                await fs._get(remote_path, local_path)
                is_valid, reason = validate_nc_file(local_path, required_variables)
                if is_valid:
                    return remote_path, True, "ok"
                last_error_msg = reason
                if os.path.exists(local_path):
                    os.remove(local_path)
            except Exception as e:
                if os.path.exists(local_path):
                    os.remove(local_path)
                if _is_not_found_error(e):
                    return remote_path, False, f"file not found in the bucket (404): {e}"
                if isinstance(e, (PermissionError, IsADirectoryError)):
                    return remote_path, False, f"local permission/path error, not retrying: {e}"
                last_error_msg = f"{type(e).__name__}: {e}"
            if attempt < max_retries:
                backoff = min(BASE_BACKOFF_S * (2 ** (attempt - 1)), MAX_BACKOFF_S)
                jitter = random.uniform(0, backoff * 0.5)
                wait_time = backoff + jitter
                log_warning(f"[{label}] Attempt {attempt}/{max_retries} failed ({last_error_msg}). Retrying in {wait_time:.1f}s...")
                await asyncio.sleep(wait_time)
    return remote_path, False, f"exhausted {max_retries} attempts -- last error: {last_error_msg}"


async def download_batch_async(
    downloads: list[dict],
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    max_retries: int = MAX_RETRIES,
) -> dict[str, tuple[bool, str]]:
    if not downloads:
        return {}
    fs = create_async_s3_fs()
    session = await fs.set_session()
    try:
        semaphore = asyncio.Semaphore(max_concurrent)
        tasks = [
            _download_one(
                fs=fs,
                semaphore=semaphore,
                remote_path=item["remote_path"],
                local_path=item["local_path"],
                required_variables=item.get("required_variables"),
                max_retries=max_retries,
                label=item.get("label", item["remote_path"].split("/")[-1]),
            )
            for item in downloads
        ]
        results = await asyncio.gather(*tasks)
        return {remote_path: (ok, msg) for remote_path, ok, msg in results}
    finally:
        try:
            await session.close()
        except Exception as e:
            log_warning(f"Failed to close the async S3 session: {e}")


def download_batch(
    downloads: list[dict],
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    max_retries: int = MAX_RETRIES,
) -> dict[str, tuple[bool, str]]:
    return asyncio.run(download_batch_async(downloads, max_concurrent, max_retries))


def download_sources(remote_paths, local_dir, name_prefix, label_prefix):
    missing = [source_id for source_id, path in remote_paths.items() if not path]
    if missing:
        log_error(f"{label_prefix}: source(s) not found in the bucket: {', '.join(missing)}.")
        return None

    batch, local_paths = [], {}
    for source_id, remote_path in remote_paths.items():
        local_path = os.path.join(
            local_dir, f"{name_prefix}{source_id}_{os.getpid()}_{os.path.basename(remote_path)}"
        )
        local_paths[source_id] = local_path
        batch.append({
            "remote_path": remote_path,
            "local_path": local_path,
            "label": f"{label_prefix} {source_id}",
        })

    results = download_batch(batch)
    for item in batch:
        ok, msg = results[item["remote_path"]]
        if not ok:
            log_error(f"{label_prefix}: failed to download '{item['remote_path']}': {msg}")
            for path in local_paths.values():
                if os.path.exists(path):
                    os.remove(path)
            return None
    return local_paths