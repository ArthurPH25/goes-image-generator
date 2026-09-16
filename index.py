import configparser
import gc
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from multiprocessing import Manager

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from PIL import Image
import imageio.v2 as imageio
import numpy as np
import s3fs

import goes_bands
import GLM
from utils import (
    get_satellite_info,
    log_error,
    log_warning,
    log_info,
    glm_nc_cache_paths,
    acquire_background_lock,
    release_background_lock,
    LOG_COLLECTOR_STATE,
    floor_to_abi_step,
    read_map_geometry_config,
    read_style_config,
    TEMP_NC_DIR,
    GLM_NC_CACHE_DIR,
    ABI_REQUIRED_VARS,
)
from S3_downloader import download_batch

warnings.filterwarnings("ignore")

GLM_REQUIRED_VARS = ["flash_lon", "flash_lat"]
INSTANCE_LOCK_STALE_TIMEOUT_S = 24 * 60 * 60

def configure_log_collector(shared_list):
    LOG_COLLECTOR_STATE["collector"] = shared_list

def log_fatal(message):
    print(f"☠️ ERRO FATAL: {message}")

def exit_fatal(message):
    log_fatal(message)
    sys.exit(1)

def log_success(message):
    print(f"✅ {message}")

def format_elapsed_time(seconds):
    if seconds < 1:
        return f"{seconds:.2f}s"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"

def parse_datetime(date_str, time_str, require_seconds):
    full_str = f"{date_str} {time_str}"
    has_seconds = time_str.count(":") == 2
    if require_seconds and not has_seconds:
        raise ValueError(
            f"horário '{time_str}' sem segundos: canal GLM exige o formato HH:MM:SS"
        )
    if not require_seconds and has_seconds:
        raise ValueError(
            f"horário '{time_str}' com segundos: canal ABI exige o formato HH:MM, sem segundos"
        )
    time_format = "%H:%M:%S" if has_seconds else "%H:%M"
    return datetime.strptime(full_str, f"%Y-%m-%d {time_format}").replace(tzinfo=timezone.utc)

def get_unique_path(desired_path):
    if not os.path.exists(desired_path):
        return desired_path
    base, ext = os.path.splitext(desired_path)
    counter = 2
    while True:
        candidate = f"{base} ({counter}){ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1

def acquire_instance_lock(lock_path):
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.write(fd, str(time.time()).encode("utf-8"))
        os.close(fd)
        return True
    except FileExistsError:
        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                lock_time = float(f.read().strip())
            if time.time() - lock_time > INSTANCE_LOCK_STALE_TIMEOUT_S:
                log_warning("Lock de instância obsoleto encontrado (execução anterior travou sem limpar). Assumindo e continuando.")
                os.remove(lock_path)
                return acquire_instance_lock(lock_path)
        except Exception:
            pass
        return False

def release_instance_lock(lock_path):
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except OSError:
        pass

def compute_render_signature(config, channel_id, is_glm, glm_background_band, dpi):
    map_geo = read_map_geometry_config(config)
    style = read_style_config(config)

    bands_for_palette = set()
    if is_glm:
        if glm_background_band:
            bands_for_palette.add(int(glm_background_band))
    elif not (1 <= channel_id <= 6):
        bands_for_palette.add(channel_id)

    palette_data = {}
    if is_glm:
        if config.has_section("PALETTE_GLM"):
            palette_data["GLM"] = sorted(config.items("PALETTE_GLM"))
    for band_id in sorted(bands_for_palette):
        section = f"PALETTE_BAND_{band_id:02d}"
        if config.has_section(section):
            palette_data[section] = sorted(config.items(section))

    signature_payload = {
        "channel": "GLM" if is_glm else channel_id,
        "glm_background_band": glm_background_band if is_glm else None,
        "dpi": dpi,
        "map_geo": map_geo,
        "style": style,
        "palette_data": palette_data,
    }
    payload_json = json.dumps(signature_payload, sort_keys=True, default=str)
    digest = hashlib.sha1(payload_json.encode("utf-8")).hexdigest()[:12]
    return digest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.ini")
COLORS_PATH = os.path.join(SCRIPT_DIR, "colors.ini")

IMAGES_DIR = "satelite_images"
VIDEOS_DIR = "satelite_videos"
TEMP_IMAGES_DIR = "satelite_temp_images"
INSTANCE_LOCK_PATH = os.path.join(TEMP_NC_DIR, "instance.lock")

config = configparser.ConfigParser()
if len(config.read([COLORS_PATH, CONFIG_PATH])) < 2:
    exit_fatal("Arquivo 'colors.ini' e/ou 'config.ini' não encontrado no diretório do script.")

try:
    channel_raw = config.get("GENERAL", "channel").strip().upper()
    is_glm = (channel_raw == "GLM")
    channel_id = "GLM" if is_glm else int(channel_raw)

    if not is_glm and not goes_bands.is_valid_band(channel_id):
        exit_fatal(f"Canal '{channel_id}' inválido. Os canais válidos vão de 1 a 16, ou 'GLM'.")

    gen_type = config.get("GENERAL", "generation_type").strip().upper()
    if gen_type not in ["I", "V"]:
        exit_fatal(f"generation_type '{gen_type}' inválido. Os valores válidos são 'I' ou 'V'.")

    num_workers = config.getint("PROCESSING", "num_workers")
    delete_temp = config.getboolean("PROCESSING", "delete_temp_images")
    max_concurrent_downloads = config.getint("PROCESSING", "max_concurrent_downloads")
    dpi = config.getint("PROCESSING", "dpi")

    video_scale = config.get("FFMPEG", "video_scale")
    crf_value = config.get("FFMPEG", "crf")
    ffmpeg_preset = config.get("FFMPEG", "preset")

    glm_background_band = config.get("MAP", "glm_background_band").strip()
    clean_mode_active = config.getboolean("STYLE", "clean_mode")

    render_signature = compute_render_signature(config, channel_id, is_glm, glm_background_band, dpi)
    run_temp_dir = os.path.join(TEMP_IMAGES_DIR, render_signature)
    output_dir = IMAGES_DIR if gen_type == "I" else run_temp_dir

    target_dates = []

    if gen_type == "I":
        date_str = config.get("IMAGE_TIME", "image_date")
        time_str = config.get("IMAGE_TIME", "image_time")
        target_dt = parse_datetime(date_str, time_str, require_seconds=is_glm)

        if is_glm:
            if target_dt.second % 20 != 0:
                raise ValueError("Canal GLM exige segundos múltiplos de 20 (00, 20 ou 40).")
        else:
            if target_dt.minute % 10 != 0:
                raise ValueError("Canal ABI exige minutos múltiplos de 10 (imagem a cada 10 minutos).")

        target_dates.append(target_dt)
        video_fps = 10
    else:
        start_date_str = config.get("VIDEO_TIME", "start_date")
        start_time_str = config.get("VIDEO_TIME", "start_time")
        end_date_str = config.get("VIDEO_TIME", "end_date")
        end_time_str = config.get("VIDEO_TIME", "end_time")
        video_fps = config.getint("VIDEO_TIME", "fps")

        start_dt = parse_datetime(start_date_str, start_time_str, require_seconds=is_glm)
        end_dt = parse_datetime(end_date_str, end_time_str, require_seconds=is_glm)

        if is_glm:
            if start_dt.second % 20 != 0 or end_dt.second % 20 != 0:
                raise ValueError("Início e fim do vídeo GLM exigem segundos múltiplos de 20 (00, 20 ou 40).")
        else:
            if start_dt.minute % 10 != 0 or end_dt.minute % 10 != 0:
                raise ValueError("Início e fim do vídeo ABI exigem minutos múltiplos de 10.")

        if end_dt <= start_dt:
            start_dt, end_dt = end_dt, start_dt

        current_dt = start_dt
        step = timedelta(seconds=20) if is_glm else timedelta(minutes=10)
        while current_dt <= end_dt:
            target_dates.append(current_dt)
            current_dt += step

except Exception as e:
    exit_fatal(f"Falha ao validar 'config.ini': {e}")

def build_png_name(remote_file):
    png_name = remote_file.split("/")[-1].replace(".nc", ".png")
    if is_glm and glm_background_band:
        png_name = png_name.replace(".png", f"_C{int(glm_background_band):02d}.png")
    if gen_type == "I" and clean_mode_active:
        png_name = png_name.replace(".png", "_clean.png")
    return png_name

def _is_valid_png(path):
    try:
        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:
        return False

def _init_worker_log_collector(shared_log_list):
    configure_log_collector(shared_log_list)

def process_file_worker(args):
    remote_file, remote_abi_file, output_path, sat_name, target_ts, current_gen_type = args
    target_dt = datetime.fromtimestamp(target_ts, tz=timezone.utc)

    glm_cache_lock_path = None
    got_glm_lock = False
    try:
        file_name = remote_file.split("/")[-1]
        png_path = os.path.join(output_path, build_png_name(remote_file))

        if current_gen_type == "I":
            png_path = get_unique_path(png_path)

        if is_glm:
            pretty_time = target_dt.strftime("%Y%j%H%M%S")
        else:
            pretty_time = target_dt.strftime("%Y%j%H%M")

        log_time = pretty_time

        if current_gen_type == "V" and os.path.exists(png_path):
            if _is_valid_png(png_path):
                log_info(f"[{log_time} UTC] Frame já existe no cache. Pulando.")
                return True
            log_warning(f"[{log_time} UTC] Frame em cache estava corrompido (execução anterior interrompida?). Regenerando.")
            os.remove(png_path)

        if is_glm:
            os.makedirs(GLM_NC_CACHE_DIR, exist_ok=True)
            cache_path, glm_cache_lock_path = glm_nc_cache_paths(GLM_NC_CACHE_DIR, remote_file)
            local_nc_path = cache_path
            if os.path.exists(cache_path):
                log_info(f"[{log_time} UTC] NetCDF principal já em cache (reaproveitado de outro frame).")
            else:
                got_glm_lock = acquire_background_lock(glm_cache_lock_path)
                try:
                    if os.path.exists(cache_path):
                        log_info(f"[{log_time} UTC] NetCDF principal já em cache (reaproveitado de outro frame).")
                    else:
                        batch = [{
                            "remote_path": remote_file,
                            "local_path": cache_path,
                            "required_variables": GLM_REQUIRED_VARS,
                            "label": f"{log_time} principal",
                        }]
                        log_info(f"[{log_time} UTC] Baixando {len(batch)} arquivo(s) concorrentemente.")
                        results = download_batch(batch, max_concurrent=max_concurrent_downloads)
                        main_ok, main_msg = results[remote_file]
                        if not main_ok:
                            raise Exception(f"Falha ao baixar o NetCDF principal: {main_msg}")
                finally:
                    if got_glm_lock:
                        release_background_lock(glm_cache_lock_path)
                        got_glm_lock = False
        else:
            local_nc_path = os.path.join(TEMP_NC_DIR, f"temp_{file_name}")
            batch = [{
                "remote_path": remote_file,
                "local_path": local_nc_path,
                "required_variables": ABI_REQUIRED_VARS,
                "label": f"{log_time} principal",
            }]
            log_info(f"[{log_time} UTC] Baixando {len(batch)} arquivo(s) concorrentemente.")
            results = download_batch(batch, max_concurrent=max_concurrent_downloads)
            main_ok, main_msg = results[remote_file]
            if not main_ok:
                raise Exception(f"Falha ao baixar o NetCDF principal: {main_msg}")

        log_info(f"[{log_time} UTC] Processando dados e salvando PNG.")

        if is_glm:
            GLM.generate_image(local_nc_path, png_path, sat_name, pretty_time, config,
                                 target_dt=target_dt, remote_abi_file=remote_abi_file,
                                 current_gen_type=current_gen_type)
        else:
            goes_bands.generate_image(local_nc_path, png_path, sat_name, pretty_time, config, channel_id)

        if not os.path.exists(png_path):
            raise Exception("O módulo do canal não gerou o PNG esperado (falha silenciosa).")

        if not is_glm and os.path.exists(local_nc_path):
            os.remove(local_nc_path)

        gc.collect()
        log_success(f"[{log_time} UTC] Frame concluído.")
        return True
    except Exception as e:
        log_error(f"[WORKER] Falha ao gerar a imagem do timestamp {target_ts}: {e}")
        return False
    finally:
        if got_glm_lock and glm_cache_lock_path:
            release_background_lock(glm_cache_lock_path)

def process_satellite():
    t_start_total = time.time()
    t_start_search, t_end_search = None, None
    t_start_images, t_end_images = None, None
    t_start_video, t_end_video = None, None

    def calc_time(start, end):
        if start is None or end is None:
            return "não medido (execução interrompida antes desta etapa)"
        return format_elapsed_time(end - start)

    log_manager = Manager()
    shared_log_list = log_manager.list()
    configure_log_collector(shared_log_list)
    got_instance_lock = False

    try:
        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(TEMP_NC_DIR, exist_ok=True)
        if gen_type == "V":
            os.makedirs(VIDEOS_DIR, exist_ok=True)

        got_instance_lock = acquire_instance_lock(INSTANCE_LOCK_PATH)
        if not got_instance_lock:
            exit_fatal(
                "Já existe outra execução deste script em andamento nesta pasta "
                f"(lock ativo em '{INSTANCE_LOCK_PATH}'). Rodar duas instâncias ao mesmo tempo no "
                "mesmo diretório corrompe os caches compartilhados de download (TEMP_NC_DIR/GLM_NC_CACHE_DIR), "
                "que são apagados por inteiro ao final de cada execução. Espere a outra terminar ou, se "
                "tiver certeza de que não há nenhuma rodando, apague o arquivo de lock manualmente."
            )

        if is_glm and glm_background_band:
            channel_label = f"GLM (fundo {glm_background_band})"
        else:
            channel_label = str(channel_id)

        print("=" * 70)
        print("🛰️ GERADOR DE IMAGENS DE SATÉLITE 🛰️")
        print(f"Modo: {'Vídeo' if gen_type == 'V' else 'Imagem Única'} | Canal Ativo: {channel_label}")
        print(f"Alvo: {config.get('MAP', 'target_coordinates')}")
        if gen_type == "V":
            print(f"Pasta de frames desta execução: {run_temp_dir}")
        time_fmt = "%d/%m/%Y %H:%M:%S" if is_glm else "%d/%m/%Y %H:%M"
        if gen_type == "I":
            print(f"Instante: {target_dates[0].strftime(time_fmt)} UTC")
        else:
            print(f"Período: {target_dates[0].strftime(time_fmt)} a {target_dates[-1].strftime(time_fmt)} UTC")
        print("=" * 70)
        print()

        S3_fs = s3fs.S3FileSystem(anon=True)
        tasks = []

        log_info("Buscando os arquivos no bucket S3 da NOAA...")
        print()
        t_start_search = time.time()
        for target in target_dates:
            year = target.year
            day_of_year = target.timetuple().tm_yday
            hour = target.hour
            minute = target.minute
            second = target.second

            sat_bucket, sat_name = get_satellite_info(target)

            found_abi_file = None

            if is_glm:
                bucket_path = f"{sat_bucket}/GLM-L2-LCFA/{year}/{day_of_year:03d}/{hour:02d}/"
                prefix = f"s{year:04d}{day_of_year:03d}{hour:02d}{minute:02d}{second:02d}"
                channel_str = "GLM-L2-LCFA"

                if glm_background_band:
                    abi_minute = floor_to_abi_step(target).minute
                    abi_bucket_path = f"{sat_bucket}/ABI-L2-CMIPF/{year}/{day_of_year:03d}/{hour:02d}/"
                    abi_prefix = f"s{year:04d}{day_of_year:03d}{hour:02d}{abi_minute:02d}"
                    abi_ch_str = f"M6C{int(glm_background_band):02d}"
                    try:
                        abi_list = S3_fs.ls(abi_bucket_path)
                        found_abi_file = next((f for f in abi_list if abi_ch_str in f and abi_prefix in f), None)
                        if found_abi_file is None:
                            log_warning(
                                f"Fundo ABI {abi_ch_str} das {abi_minute:02d}min não encontrado no bucket; frame sairá sem fundo."
                            )
                    except Exception as e:
                        log_warning(f"Falha ao buscar o fundo ABI {abi_ch_str} no bucket: {e}")
            else:
                bucket_path = f"{sat_bucket}/ABI-L2-CMIPF/{year}/{day_of_year:03d}/{hour:02d}/"
                prefix = f"s{year:04d}{day_of_year:03d}{hour:02d}{minute:02d}"
                channel_str = f"M6C{channel_id:02d}"

            try:
                files_list = S3_fs.ls(bucket_path)
                found_file = next((f for f in files_list if channel_str in f and prefix in f), None)
                if found_file:
                    tasks.append((found_file, found_abi_file, output_dir, sat_name, target.replace(tzinfo=timezone.utc).timestamp(), gen_type))
                    log_success(f"Encontrado: {found_file.split('/')[-1]}")
                else:
                    log_warning(f"Arquivo das {hour:02d}:{minute:02d}:{second:02d} não encontrado no bucket.")
            except Exception as e:
                log_error(f"Falha ao listar {bucket_path}: {e}")
        t_end_search = time.time()

        if not tasks:
            exit_fatal("Nenhum arquivo encontrado para as datas configuradas. Revise o 'config.ini'.")

        print()
        log_info(f"Iniciando {num_workers} worker(s) (até {max_concurrent_downloads} downloads concorrentes cada)...")
        print()

        success_count = 0
        fail_count = 0
        total_tasks = len(tasks)

        t_start_images = time.time()
        with ProcessPoolExecutor(
            max_workers=num_workers,
            initializer=_init_worker_log_collector,
            initargs=(shared_log_list,),
        ) as executor:
            futures = {executor.submit(process_file_worker, t): t for t in tasks}

            for future in as_completed(futures):
                is_success = future.result()
                if is_success:
                    success_count += 1
                else:
                    fail_count += 1

                tasks_done = success_count + fail_count
                if tasks_done % 5 == 0 or tasks_done == total_tasks:
                    print()
                    log_info(f"{tasks_done}/{total_tasks} imagens processadas ({success_count} OK | {fail_count} falha(s)).")
                    print()
        t_end_images = time.time()

        if fail_count > 0:
            log_warning(f"{fail_count} imagem(ns) falharam durante o processamento. Revise os erros acima.")
        else:
            log_success("Etapa de imagens concluída sem falhas.")

        if gen_type == "V":
            if success_count > 0:
                t_start_video = time.time()
                log_info("Iniciando a montagem do vídeo.")

                try:
                    video_frames = []
                    skipped_corrupt = 0
                    for task in tasks:
                        expected_name = build_png_name(task[0])
                        frame_path = os.path.join(output_dir, expected_name)
                        if not os.path.exists(frame_path):
                            continue
                        if _is_valid_png(frame_path):
                            video_frames.append(frame_path)
                        else:
                            skipped_corrupt += 1

                    if skipped_corrupt:
                        log_warning(f"{skipped_corrupt} frame(s) em disco estavam corrompidos e foram excluídos do vídeo.")

                    if video_frames:
                        time_fmt_out = "%Y%m%d_%H%M%S" if is_glm else "%Y%m%d_%H%M"
                        video_filename = os.path.join(VIDEOS_DIR, f"satelite_{target_dates[0].strftime(time_fmt_out)}_ate_{target_dates[-1].strftime(time_fmt_out)}_fps{video_fps}.mp4")
                        video_filename = get_unique_path(video_filename)
                        ffmpeg_args = ['-crf', str(crf_value), '-preset', ffmpeg_preset, '-vf', video_scale, '-loglevel', 'error']

                        log_info(f"Codificando com FFmpeg (CRF {crf_value}, scale {video_scale}).")

                        first_frame_img = Image.open(video_frames[0])
                        target_dimensions = first_frame_img.size
                        first_frame_img.close()

                        with imageio.get_writer(video_filename, format='FFMPEG', fps=video_fps, macro_block_size=None, ffmpeg_params=ffmpeg_args) as video_writer:
                            total_frames = len(video_frames)
                            for idx, frame_path in enumerate(video_frames):
                                current_frame = idx + 1

                                if current_frame == 1 or current_frame % 10 == 0 or current_frame == total_frames:
                                    log_info(f"Adicionando frame {current_frame}/{total_frames} ao vídeo.")

                                img_object = Image.open(frame_path)
                                if img_object.size != target_dimensions:
                                    try:
                                        resampling_filter = Image.Resampling.LANCZOS
                                    except AttributeError:
                                        resampling_filter = Image.LANCZOS
                                    img_object = img_object.resize(target_dimensions, resampling_filter)
                                video_writer.append_data(np.array(img_object))
                                img_object.close()

                        log_success(f"Vídeo exportado em: {video_filename}")

                        if delete_temp:
                            log_info(f"Removendo a pasta temporária desta execução '{run_temp_dir}'.")
                            shutil.rmtree(run_temp_dir, ignore_errors=True)
                        else:
                            log_info(f"Os frames usados foram mantidos em '{run_temp_dir}'.")

                        try:
                            if platform.system() == 'Windows':
                                os.startfile(video_filename)
                            elif platform.system() == 'Darwin':
                                subprocess.run(['open', video_filename], check=True,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            else:
                                subprocess.run(['xdg-open', video_filename], check=True,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            log_warning(f"Não foi possível abrir o player de vídeo automaticamente (ambiente sem suporte gráfico?). Arquivo salvo em: {video_filename}")
                    else:
                        log_error("Nenhum frame PNG válido foi encontrado em disco para montar o vídeo, apesar de sucessos reportados. Verifique a pasta de frames.")
                except Exception as e:
                    log_error(f"Falha ao montar o vídeo final: {e}")
                finally:
                    t_end_video = time.time()
            else:
                exit_fatal("O modo configurado é de vídeo, mas nenhuma imagem foi gerada. Não é possível montar o vídeo.")

    except Exception as e:
        exit_fatal(f"Execução interrompida por um erro não tratado: {e}")
    finally:
        if is_glm and glm_background_band and os.path.isdir(TEMP_NC_DIR):
            leftover = [f for f in os.listdir(TEMP_NC_DIR) if f.startswith("bg_") or f.startswith("temp_bg_")]
            if leftover:
                log_info(f"Limpando {len(leftover)} arquivo(s) de cache de fundo GLM em '{TEMP_NC_DIR}'...")
                for name in leftover:
                    try:
                        os.remove(os.path.join(TEMP_NC_DIR, name))
                    except OSError:
                        pass

        if is_glm and os.path.isdir(GLM_NC_CACHE_DIR):
            nc_cache_files = [f for f in os.listdir(GLM_NC_CACHE_DIR) if f.endswith(".nc")]
            if nc_cache_files:
                log_info(
                    f"Limpando {len(nc_cache_files)} arquivo(s) .nc do cache compartilhado GLM "
                    f"em '{GLM_NC_CACHE_DIR}'..."
                )
                for name in nc_cache_files:
                    try:
                        os.remove(os.path.join(GLM_NC_CACHE_DIR, name))
                    except OSError:
                        pass
        t_end_total = time.time()
        print()
        print("=" * 70)
        print("RELATÓRIO DE TEMPO DE EXECUÇÃO")
        print("=" * 70)

        if gen_type == "V":
            print(f"Tempo buscando arquivos na NOAA: {calc_time(t_start_search, t_end_search)}")
            print(f"Tempo baixando e gerando as imagens: {calc_time(t_start_images, t_end_images)}")
            print(f"Tempo montando o vídeo: {calc_time(t_start_video, t_end_video)}")
            print("-" * 70)

        print(f"TEMPO TOTAL DE EXECUÇÃO: {calc_time(t_start_total, t_end_total)}")
        print("=" * 70)

        try:
            collected_logs = list(shared_log_list)
        except Exception:
            collected_logs = []

        print()
        print("=" * 70)
        print("RELATÓRIO DE AVISOS E ERROS")
        print("=" * 70)
        if not collected_logs:
            print("Nenhum aviso ou erro registrado durante a execução.")
        else:
            warning_count = sum(1 for level, _ in collected_logs if level == "AVISO")
            error_count = sum(1 for level, _ in collected_logs if level == "ERRO")
            print(f"Total: {warning_count} aviso(s), {error_count} erro(s).")
            print("-" * 70)
            for level, message in collected_logs:
                icon = "⚠️" if level == "AVISO" else "❌"
                print(f"{icon} [{level}] {message}")
        print("=" * 70)

        try:
            log_manager.shutdown()
        except Exception:
            pass

        if got_instance_lock:
            release_instance_lock(INSTANCE_LOCK_PATH)

if __name__ == "__main__":
    process_satellite()
    if sys.stdin.isatty():
        input("\nPressione Enter para sair...")