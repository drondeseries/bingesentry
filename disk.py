import os
import re
import logging
import time
import threading
import psutil

_cpu_cache_lock = threading.Lock()
_last_cpu_time = 0.0
_cached_cpu_pct = 0.0

def get_cpu_percent():
    """
    Thread-safe CPU percent getter that caches the result for 1.0 second.
    This prevents psutil.cpu_percent() from returning 0.0 due to thread interference.
    """
    global _last_cpu_time, _cached_cpu_pct
    with _cpu_cache_lock:
        current_time = time.time()
        if current_time - _last_cpu_time >= 1.0:
            _cached_cpu_pct = psutil.cpu_percent()
            _last_cpu_time = current_time
        return _cached_cpu_pct

def has_enough_disk_space(directory_path, required_space_gb=5.0):
    """
    Checks if the filesystem mount containing directory_path has enough free space in GB.
    If the path does not exist, checks the closest existing parent directory.
    """
    try:
        target_path = os.path.abspath(directory_path)
        while target_path and not os.path.exists(target_path):
            parent = os.path.dirname(target_path)
            if parent == target_path:
                break
            target_path = parent
            
        if not os.path.exists(target_path):
            target_path = '/'
            
        statvfs = os.statvfs(target_path)
        available_space_gb = (statvfs.f_frsize * statvfs.f_bavail) / (1024 ** 3)
        return available_space_gb >= required_space_gb, available_space_gb
    except Exception as e:
        logging.error(f"Error checking disk space for path '{directory_path}': {e}")
        return False, 0.0

def is_mount_responsive(mount_path, timeout=2.0):
    """
    Checks if a mount directory is responsive by running a command in a subprocess with a timeout.
    This prevents the daemon from hanging if a FUSE mount goes offline or locks up.
    """
    if not mount_path:
        return True
    import subprocess
    try:
        subprocess.run(
            ["stat", mount_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True
        )
        return True
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, Exception):
        return False


def get_file_size_gb(file_path):
    """
    Returns the file size in Gigabytes.
    """
    try:
        if os.path.exists(file_path):
            file_size_bytes = os.path.getsize(file_path)
            return file_size_bytes / (1024 ** 3)
        return 0.0
    except Exception as e:
        logging.error(f"Error getting file size for '{file_path}': {e}")
        return 0.0

def map_path(file_path, map_from, map_to):
    """
    Replaces a prefix path (map_from) with another (map_to) to map remote paths
    from Plex back to the cache client local mount.
    """
    if map_from and file_path.startswith(map_from):
        mapped = file_path.replace(map_from, map_to, 1)
        return os.path.normpath(mapped)
    return file_path

def resolve_existing_path(file_path):
    """
    Checks if the given file exists. If not, attempts to resolve it by looking for
    a file in the same directory that matches the season/episode code or year (for movies).
    """
    if os.path.exists(file_path):
        return file_path
        
    dir_name = os.path.dirname(file_path)
    if not os.path.exists(dir_name):
        return file_path
        
    base_name = os.path.basename(file_path)
    name_no_ext, ext = os.path.splitext(base_name)
    
    # Try matching TV Show season/episode pattern (e.g. S12E33 or S1E01)
    match_tv = re.search(r'S(\d{1,3})\s*E(\d{1,3})', name_no_ext, re.IGNORECASE)
    if match_tv:
        season_num = int(match_tv.group(1))
        episode_num = int(match_tv.group(2))
        try:
            for f in os.listdir(dir_name):
                f_match = re.search(r'S(\d{1,3})\s*E(\d{1,3})', f, re.IGNORECASE)
                if f_match and f.endswith(ext):
                    fs_val = int(f_match.group(1))
                    fe_val = int(f_match.group(2))
                    if season_num == fs_val and episode_num == fe_val:
                        resolved = os.path.join(dir_name, f)
                        logging.info(f"Path Resolver: Resolved missing TV path '{file_path}' to existing file '{resolved}'")
                        return resolved
        except Exception as e:
            logging.debug(f"Path Resolver: Error listdir for TV: {e}")
            
    # Try matching Movie year pattern (e.g. 2002 or (2002))
    match_movie = re.search(r'\(?(\d{4})\)?', name_no_ext)
    if match_movie:
        year_str = match_movie.group(1)
        try:
            for f in os.listdir(dir_name):
                # Search for 4-digit year inside filename (matches with or without parentheses)
                if year_str in f and f.endswith(ext):
                    resolved = os.path.join(dir_name, f)
                    logging.info(f"Path Resolver: Resolved missing Movie path '{file_path}' to existing file '{resolved}'")
                    return resolved
        except Exception as e:
            logging.debug(f"Path Resolver: Error listdir for Movie: {e}")
            
    return file_path

def get_cache_status(file_path, cache_dir, remote_name, mount_dir):
    """
    Checks rclone's vfsMeta directory to find the cached ranges
    and returns (is_fully_cached, percentage, cached_bytes, total_size).
    If metadata file is missing or config is incomplete, returns (False, 0.0, 0, 0).
    """
    if not cache_dir or not remote_name or not mount_dir:
        return False, 0.0, 0, 0
        
    import json
    try:
        from disk import is_file_in_mount; is_inside, relative_path = is_file_in_mount(file_path, mount_dir)
        if not is_inside:
            return False, 0.0, 0, 0
        
        # Construct path to vfsMeta JSON file
        meta_file_path = os.path.join(cache_dir, 'vfsMeta', remote_name, relative_path)
        
        if not os.path.exists(meta_file_path):
            return False, 0.0, 0, 0
            
        with open(meta_file_path, 'r') as f:
            meta_data = json.load(f)
            
        total_size = meta_data.get('Size', 0)
        if total_size <= 0:
            return False, 0.0, 0, 0
            
        ranges = meta_data.get('Rs', [])
        cached_bytes = sum(r.get('Size', 0) for r in ranges)
        
        # Calculate percentage
        percentage = (cached_bytes / total_size) * 100.0
        
        # Check if fully cached
        is_fully_cached = (cached_bytes >= total_size)
        
        return is_fully_cached, min(percentage, 100.0), cached_bytes, total_size
    except Exception as e:
        logging.debug(f"Error checking cache status for '{file_path}': {e}")
        return False, 0.0, 0, 0

def update_bandwidth_stats(saved_bytes, read_bytes, stats_file_path="./config/stats.json"):
    """
    Increments lifetime bandwidth savings and cached bytes counters in the stats file.
    """
    import json
    try:
        os.makedirs(os.path.dirname(os.path.abspath(stats_file_path)), exist_ok=True)
        stats = {"total_saved_bytes": 0, "total_read_bytes": 0, "files_cached": 0}
        if os.path.exists(stats_file_path):
            try:
                with open(stats_file_path, 'r') as f:
                    stats.update(json.load(f))
            except Exception:
                pass
        stats["total_saved_bytes"] += saved_bytes
        stats["total_read_bytes"] += read_bytes
        stats["files_cached"] += 1
        with open(stats_file_path, 'w') as f:
            json.dump(stats, f, indent=4)
    except Exception as e:
        logging.error(f"Failed to update bandwidth stats: {e}")

def get_bandwidth_stats(stats_file_path="./config/stats.json"):
    """
    Loads and returns the lifetime bandwidth statistics.
    """
    import json
    try:
        if os.path.exists(stats_file_path):
            with open(stats_file_path, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {"total_saved_bytes": 0, "total_read_bytes": 0, "files_cached": 0}


def is_file_in_mount(file_path, mount_dir):
    """Checks if a file is inside a mount directory."""
    file_path_real = os.path.realpath(file_path)
    mount_dir_real = os.path.realpath(mount_dir)
    file_path_abs = os.path.abspath(file_path_real)
    mount_dir_abs = os.path.abspath(mount_dir_real)

    try:
        relative_path = os.path.relpath(file_path_abs, mount_dir_abs)
        is_inside = not relative_path.startswith('..') and not os.path.isabs(relative_path)
    except ValueError:
        is_inside = False
        relative_path = ""
    return is_inside, relative_path
