#!/usr/bin/env python
import os
import sys
import time
import json
import logging
import argparse
from config import Config

# Set up logging specifically for the executor
# Log to the same log file configured in config.ini
def setup_executor_logging(config):
    log_format = '%(asctime)s %(levelname)s: [Executor] %(message)s'
    logging.basicConfig(
        filename=config.log_file,
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format=log_format
    )

def get_uncached_ranges(total_size, cached_ranges):
    """
    Given total_size of file and list of cached ranges [{'Pos': ..., 'Size': ...}],
    returns a list of uncached byte ranges as tuples (start, end) where end is exclusive.
    """
    if total_size <= 0:
        return []
        
    # Sort cached ranges by Pos
    sorted_ranges = sorted(cached_ranges, key=lambda r: r.get('Pos', 0))
    
    gaps = []
    current_pos = 0
    
    for r in sorted_ranges:
        pos = r.get('Pos', 0)
        size = r.get('Size', 0)
        
        if pos > current_pos:
            gaps.append((current_pos, pos))
            
        current_pos = max(current_pos, pos + size)
        
    if current_pos < total_size:
        gaps.append((current_pos, total_size))
        
    return gaps

class DynamicThrottler:
    def __init__(self, plex_url, plex_token, throttle_speed_mb):
        self.plex_url = plex_url
        self.plex_token = plex_token
        self.throttle_speed_bps = throttle_speed_mb * 1024 * 1024
        self.last_check_time = 0
        self.is_active_streams = False
        self.plex = None
        
    def check_throttle(self):
        """
        Polls Plex sessions at most once every 10 seconds.
        Returns the speed limit in bytes per second (0 means unlimited).
        """
        if self.throttle_speed_bps <= 0 or not self.plex_url or not self.plex_token:
            return 0
            
        current_time = time.time()
        if current_time - self.last_check_time >= 10:
            self.last_check_time = current_time
            try:
                if not self.plex:
                    from plexapi.server import PlexServer
                    self.plex = PlexServer(self.plex_url, self.plex_token, timeout=10.0)
                sessions = self.plex.sessions()
                self.is_active_streams = len(sessions) > 0
                if self.is_active_streams:
                    logging.info(f"Active streams detected. Throttling caching speed to {self.throttle_speed_bps / (1024*1024):.1f} MB/s.")
                else:
                    logging.debug("No active streams. Running cache at full speed.")
            except Exception as e:
                # If query fails, fallback to previous state to avoid interruption
                logging.debug(f"Plex session query failed: {e}")
                self.plex = None
                
        if self.is_active_streams:
            return self.throttle_speed_bps
        return 0

def main():
    parser = argparse.ArgumentParser(description="BingeSentry Smart Cache Executor")
    parser.add_argument("--file", required=True, help="Path of the file to cache")
    args = parser.parse_args()
    
    config = Config()
    setup_executor_logging(config)
    
    # Enforce mount health check before proceeding
    mount_dir = config.rclone_mount_dir or config.path_map_to
    if mount_dir:
        from disk import is_mount_responsive
        if not is_mount_responsive(mount_dir):
            logging.error(f"Mount Health Guard: Mount directory '{mount_dir}' is unresponsive or offline. Aborting executor.")
            sys.exit(2)
            
    file_path = args.file
    filename = os.path.basename(file_path)
    
    logging.info(f"Starting smart cache for: '{filename}'")
    
    # Follow symlinks to locate real path
    file_path_real = os.path.realpath(file_path)
    if not os.path.exists(file_path_real):
        logging.error(f"Target file does not exist: '{file_path_real}'")
        sys.exit(2)
        
    total_size = os.path.getsize(file_path_real)
    if total_size <= 0:
        logging.warning(f"File size is 0 bytes. Nothing to cache.")
        sys.exit(5)
        
    # Resolve vfsMeta path
    cache_dir = config.rclone_cache_dir
    remote_name = config.rclone_remote_name
    mount_dir = config.rclone_mount_dir or config.path_map_to
    
    gaps = []
    vfs_meta_found = False
    
    if cache_dir and remote_name and mount_dir:
        mount_dir_real = os.path.realpath(mount_dir)
        file_path_abs = os.path.abspath(file_path_real)
        mount_dir_abs = os.path.abspath(mount_dir_real)
        
        try:
            relative_path = os.path.relpath(file_path_abs, mount_dir_abs)
            is_inside = not relative_path.startswith('..') and not os.path.isabs(relative_path)
        except ValueError:
            is_inside = False

        if is_inside:
            meta_file_path = os.path.join(cache_dir, 'vfsMeta', remote_name, relative_path)
            
            if os.path.exists(meta_file_path):
                try:
                    with open(meta_file_path, 'r') as f:
                        meta_data = json.load(f)
                    
                    cached_ranges = meta_data.get('Rs', [])
                    gaps = get_uncached_ranges(total_size, cached_ranges)
                    vfs_meta_found = True
                    
                    cached_bytes = sum(r.get('Size', 0) for r in cached_ranges)
                    progress_pct = (cached_bytes / total_size) * 100.0 if total_size > 0 else 0
                    logging.info(f"VFS Metadata found. Currently cached: {progress_pct:.1f}%. Identified {len(gaps)} uncached range(s).")
                except Exception as e:
                    logging.warning(f"Failed to read vfsMeta file '{meta_file_path}': {e}")
            else:
                logging.debug(f"vfsMeta file not found at: '{meta_file_path}'")
        else:
            logging.debug(f"File '{file_path_abs}' is outside mount directory '{mount_dir_abs}'")
            
    if not vfs_meta_found:
        logging.info("VFS Metadata not available. Caching the entire file sequentially.")
        gaps = [(0, total_size)]
        
    # Remove empty gaps
    gaps = [g for g in gaps if g[1] > g[0]]
    if not gaps:
        logging.info(f"File '{filename}' is already 100% cached. Exiting.")
        try:
            from disk import update_bandwidth_stats
            update_bandwidth_stats(total_size, 0)
        except Exception:
            pass
        sys.exit(0)
        
    # Read chunk size in bytes
    chunk_size = int(config._get_val('Cache', 'CACHE_CHUNK_SIZE_MB', 'CACHE_CHUNK_SIZE_MB', 1.0, float) * 1024 * 1024)
    throttle_speed_mb = config.throttle_speed_active_mb
    
    throttler = DynamicThrottler(config.plex_url, config.plex_token, throttle_speed_mb)
    
    total_gaps_size = sum(end - start for start, end in gaps)
    total_read_bytes = 0
    
    logging.info(f"Beginning cache read of {total_gaps_size / (1024*1024):.2f} MB of missing data...")
    
    try:
        with open(file_path_real, 'rb') as f:
            for start, end in gaps:
                f.seek(start)
                bytes_remaining = end - start
                
                while bytes_remaining > 0:
                    speed_limit = throttler.check_throttle()
                    to_read = min(chunk_size, bytes_remaining)
                    
                    start_time = time.perf_counter()
                    data = f.read(to_read)
                    if not data:
                        # EOF reached prematurely
                        break
                        
                    read_len = len(data)
                    bytes_remaining -= read_len
                    total_read_bytes += read_len
                    
                    if speed_limit > 0:
                        target_duration = read_len / speed_limit
                        elapsed = time.perf_counter() - start_time
                        if elapsed < target_duration:
                            time.sleep(target_duration - elapsed)
                            
        logging.info(f"Smart cache completed. Read {total_read_bytes / (1024*1024):.2f} MB of uncached data.")
        try:
            from disk import update_bandwidth_stats
            update_bandwidth_stats(max(0, total_size - total_read_bytes), total_read_bytes)
        except Exception:
            pass
        sys.exit(0)
    except OSError as e:
        logging.error(f"I/O error during cache reading for '{filename}': {e}")
        sys.exit(3)
    except Exception as e:
        logging.error(f"Unexpected error during caching: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
