from __future__ import annotations

import asyncio
import os
import random
import time

import s3fs
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, EndpointConnectionError
from netCDF4 import Dataset

from utils import log_warning

CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 30
DEFAULT_MAX_CONCURRENT_DOWNLOADS = 16
MIN_VALID_FILE_SIZE_BYTES = 8 * 1024
MAX_RETRIES = 5
BASE_BACKOFF_S = 1.5
MAX_BACKOFF_S = 30.0

class DownloadError(Exception):
    pass

def _is_not_found_error(exc: Exception) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    if isinstance(exc, ClientError):
        error_code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return error_code in ("404", "NoSuchKey", "NotFound") or status == 404
    msg = str(exc).lower()
    return "404" in msg or "no such key" in msg or "not found" in msg

def _is_transient_network_error(exc: Exception) -> bool:
    transient_types = (
        asyncio.TimeoutError,
        TimeoutError,
        ConnectionError,
        ConnectionResetError,
        EndpointConnectionError,
        OSError,
    )
    if isinstance(exc, transient_types):
        return True
    if isinstance(exc, ClientError):
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        error_code = exc.response.get("Error", {}).get("Code", "")
        return status in (500, 502, 503, 504) or error_code in ("SlowDown", "RequestTimeout", "InternalError")
    return False

def validate_nc_file(local_path: str, required_variables: list[str] | None = None) -> tuple[bool, str]:
    if not os.path.exists(local_path):
        return False, "arquivo não existe em disco após o download"
    file_size = os.path.getsize(local_path)
    if file_size < MIN_VALID_FILE_SIZE_BYTES:
        return False, f"arquivo suspeito de estar truncado/vazio ({file_size} bytes)"
    try:
        with Dataset(local_path, "r") as ds:
            if required_variables:
                missing = [v for v in required_variables if v not in ds.variables]
                if missing:
                    return False, f"variáveis ausentes no NetCDF: {missing}"
            if required_variables:
                for var_name in required_variables:
                    _ = ds.variables[var_name][:1]
        return True, "ok"
    except Exception as e:
        return False, f"NetCDF corrompido ou ilegível: {e}"

async def _download_one(
    fs: s3fs.S3FileSystem,
    semaphore: asyncio.Semaphore,
    remote_path: str,
    local_path: str,
    required_variables: list[str] | None,
    max_retries: int,
    label: str,
) -> tuple[str, bool, str]:
    last_error_msg = "motivo desconhecido"
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
                    return remote_path, False, f"arquivo não encontrado no bucket (404): {e}"
                if _is_transient_network_error(e):
                    last_error_msg = f"erro transitório de rede/timeout: {e}"
                else:
                    last_error_msg = f"erro inesperado: {e}"
            if attempt < max_retries:
                backoff = min(BASE_BACKOFF_S * (2 ** (attempt - 1)), MAX_BACKOFF_S)
                jitter = random.uniform(0, backoff * 0.5)
                wait_time = backoff + jitter
                log_warning(f"[{label}] Tentativa {attempt}/{max_retries} falhou ({last_error_msg}). Nova tentativa em {wait_time:.1f}s...")
                await asyncio.sleep(wait_time)
    return remote_path, False, f"esgotou {max_retries} tentativas -- último erro: {last_error_msg}"

async def download_batch_async(
    downloads: list[dict],
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    max_retries: int = MAX_RETRIES,
) -> dict[str, tuple[bool, str]]:
    if not downloads:
        return {}
    fs = s3fs.S3FileSystem(
        anon=True,
        asynchronous=True,
        config_kwargs={
            "connect_timeout": CONNECT_TIMEOUT_S,
            "read_timeout": READ_TIMEOUT_S,
            "retries": {"max_attempts": 0},
        },
    )
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
            log_warning(f"Falha ao fechar sessão S3 assíncrona: {e}")

def download_batch(
    downloads: list[dict],
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    max_retries: int = MAX_RETRIES,
) -> dict[str, tuple[bool, str]]:
    return asyncio.run(download_batch_async(downloads, max_concurrent, max_retries))

if __name__ == "__main__":
    demo_bucket = "noaa-goes19/GLM-L2-LCFA/2024/200/12/"
    fs_sync = s3fs.S3FileSystem(anon=True)
    try:
        files = fs_sync.ls(demo_bucket)[:3]
    except Exception as e:
        print(f"Não foi possível listar bucket de demonstração: {e}")
        files = []
    if files:
        os.makedirs("/tmp/glm_test", exist_ok=True)
        batch = [
            {
                "remote_path": f,
                "local_path": f"/tmp/glm_test/{f.split('/')[-1]}",
                "required_variables": ["flash_lon", "flash_lat"],
                "label": f.split("/")[-1],
            }
            for f in files
        ]
        t0 = time.time()
        results = download_batch(batch, max_concurrent=8)
        elapsed = time.time() - t0
        print(f"\nBaixados {len(files)} arquivos concorrentemente em {elapsed:.2f}s")
        for remote, (ok, msg) in results.items():
            status = "✅" if ok else "❌"
            print(f"{status} {remote.split('/')[-1]}: {msg}")