import os
import logging

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
        # Resolve symlink (e.g. follow /mnt/usenet-rclone to /mnt/altmount-rclone)
        file_path_real = os.path.realpath(file_path)
        mount_dir_real = os.path.realpath(mount_dir)
        
        file_path_abs = os.path.abspath(file_path_real)
        mount_dir_abs = os.path.abspath(mount_dir_real)
        
        if not file_path_abs.startswith(mount_dir_abs):
            return False, 0.0, 0, 0
            
        relative_path = os.path.relpath(file_path_abs, mount_dir_abs)
        
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

