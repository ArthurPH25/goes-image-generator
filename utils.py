import os
import time
from datetime import datetime, timezone

import matplotlib.colors as mcolors

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

GOES_TRANSITION_UTC = datetime(2025, 4, 7, 15, 10, tzinfo=timezone.utc)

LOCK_POLL_INTERVAL_S = 0.5
LOCK_STALE_TIMEOUT_S = 300

TEMP_NC_DIR = os.path.join(BASE_DIR, "satelite_temp_downloads")

LOG_COLLECTOR_STATE = {"collector": None}

def _record_log(level, message):
    collector = LOG_COLLECTOR_STATE["collector"]
    if collector is not None:
        try:
            collector.append((level, message))
        except Exception:
            pass

def log_error(message):
    print(f"❌ ERRO: {message}")
    _record_log("ERRO", message)

def log_warning(message):
    print(f"⚠️ AVISO: {message}")
    _record_log("AVISO", message)

def log_info(message):
    print(f"ℹ️ {message}")

def log_success(message):
    print(f"✅ {message}")

def get_satellite_info(target_dt):
    if target_dt < GOES_TRANSITION_UTC:
        return "noaa-goes16", "GOES-16"
    return "noaa-goes19", "GOES-19"

ABI_STEP_MINUTES = 10
GLM_STEP_SECONDS = 20
GLM_PRODUCT = "GLM-L2-LCFA"

def floor_to_abi_step(dt):
    floored_minute = (dt.minute // ABI_STEP_MINUTES) * ABI_STEP_MINUTES
    return dt.replace(minute=floored_minute, second=0, microsecond=0)

def read_map_geometry_config(config):
    projection = config.get("MAP", "projection").strip().upper()
    if projection not in ("P", "C"):
        raise ValueError(f"projection deve ser 'P' ou 'C', valor recebido: '{projection}'")
    coords_raw = config.get("MAP", "target_coordinates").split(",")
    if len(coords_raw) != 4:
        raise ValueError(
            "target_coordinates deve ter exatamente 4 valores "
            f"(oeste, leste, sul, norte), recebido: '{config.get('MAP', 'target_coordinates')}'"
        )
    target_coordinates = [float(value.strip()) for value in coords_raw]
    lon_w, lon_e, lat_s, lat_n = target_coordinates
    if not (-180.0 <= lon_w <= 180.0) or not (-180.0 <= lon_e <= 180.0):
        raise ValueError(
            "target_coordinates: oeste e leste devem estar entre -180 e 180, "
            f"recebido: oeste={lon_w}, leste={lon_e}"
        )
    if not (-90.0 <= lat_s <= 90.0) or not (-90.0 <= lat_n <= 90.0):
        raise ValueError(
            "target_coordinates: sul e norte devem estar entre -90 e 90, "
            f"recebido: sul={lat_s}, norte={lat_n}"
        )
    if lon_w >= lon_e:
        raise ValueError(f"target_coordinates: oeste ({lon_w}) deve ser menor que leste ({lon_e})")
    if lat_s >= lat_n:
        raise ValueError(f"target_coordinates: sul ({lat_s}) deve ser menor que norte ({lat_n})")

    return {
        "projection": projection,
        "target_coordinates": target_coordinates,
    }

def validate_color(key, raw_value):
    value = raw_value.strip()
    if not mcolors.is_color_like(value):
        raise ValueError(
            f"{key} não é uma cor reconhecida pelo matplotlib: '{value}' "
            "(use um hexadecimal #RRGGBB ou um nome válido, ex: 'gray' em vez de 'grey')"
        )
    return value

def read_style_config(config):
    watermark_size = config.getfloat("STYLE", "watermark_size")
    if watermark_size <= 0:
        raise ValueError(f"watermark_size deve ser um número positivo, recebido: {watermark_size}")

    figure_width = config.getfloat("STYLE", "figure_width")
    figure_height = config.getfloat("STYLE", "figure_height")
    if figure_width <= 0 or figure_height <= 0:
        raise ValueError(
            "figure_width e figure_height devem ser positivos, "
            f"recebido: width={figure_width}, height={figure_height}"
        )

    borders_line_width = config.getfloat("STYLE", "borders_line_width")
    states_line_width = config.getfloat("STYLE", "states_line_width")
    coast_line_width = config.getfloat("STYLE", "coast_line_width")
    if borders_line_width < 0 or states_line_width < 0 or coast_line_width < 0:
        raise ValueError(
            "borders_line_width, states_line_width e coast_line_width não podem ser negativos, "
            f"recebido: borders={borders_line_width}, states={states_line_width}, coast={coast_line_width}"
        )

    borders_color = validate_color("borders_color", config.get("STYLE", "borders_color"))
    states_color = validate_color("states_color", config.get("STYLE", "states_color"))
    coast_color = validate_color("coast_color", config.get("STYLE", "coast_color"))

    watermark_color = validate_color("watermark_color", config.get("STYLE", "watermark_color"))

    return {
        "watermark": config.get("STYLE", "watermark"),
        "watermark_color": watermark_color,
        "watermark_size": watermark_size,
        "clean_mode": config.getboolean("STYLE", "clean_mode"),
        "hide_map_lines": config.getboolean("STYLE", "hide_map_lines"),
        "borders_color": borders_color,
        "borders_line_width": borders_line_width,
        "states_color": states_color,
        "states_line_width": states_line_width,
        "coast_color": coast_color,
        "coast_line_width": coast_line_width,
        "figure_width": figure_width,
        "figure_height": figure_height,
    }

def acquire_file_lock(lock_path, stale_timeout_s=LOCK_STALE_TIMEOUT_S, max_wait_s=LOCK_STALE_TIMEOUT_S):
    start = time.time()
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(time.time()).encode("utf-8"))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                with open(lock_path, "r", encoding="utf-8") as f:
                    timestamp_str = f.read().strip()
                if timestamp_str:
                    lock_time = float(timestamp_str)
                    if time.time() - lock_time > stale_timeout_s:
                        log_warning(f"Removendo lock obsoleto '{os.path.basename(lock_path)}' (execução anterior travou sem limpar).")
                        os.remove(lock_path)
                        continue
            except Exception:
                pass
            if time.time() - start >= max_wait_s:
                return False
            time.sleep(LOCK_POLL_INTERVAL_S)

def release_file_lock(lock_path):
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except OSError:
        pass