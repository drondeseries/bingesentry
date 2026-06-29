import os
import configparser

class Config:
    def __init__(self, config_file_path=None):
        self.config = configparser.ConfigParser()
        
        # Locations to check for the configuration file
        config_locations = [
            config_file_path,
            os.getenv('CONFIG_LOCATION'),
            '/app/config/config.ini',
            'config.ini'
        ]
        
        self.loaded_file = None
        for path in config_locations:
            if path and os.path.exists(path):
                self.config.read(path)
                self.loaded_file = path
                break
                
    def _get_val(self, section, option, env_name, default=None, val_type=str):
        # 1. Check environment variable override first
        val = os.getenv(env_name)
        if val is not None:
            try:
                if val_type == bool:
                    return val.lower() in ('true', '1', 't', 'y', 'yes')
                val_res = val_type(val)
                return val_res.strip() if isinstance(val_res, str) else val_res
            except ValueError:
                pass
                
        # 2. Check the configuration file if loaded
        if self.loaded_file and self.config.has_section(section) and self.config.has_option(section, option):
            try:
                if val_type == bool:
                    return self.config.getboolean(section, option)
                elif val_type == int:
                    return self.config.getint(section, option)
                elif val_type == float:
                    return self.config.getfloat(section, option)
                val_res = self.config.get(section, option)
                return val_res.strip() if isinstance(val_res, str) else val_res
            except Exception:
                pass
                
        # 3. Fallback to default value
        return default.strip() if isinstance(default, str) else default

    @property
    def plex_url(self):
        return self._get_val('Plex', 'PLEX_URL', 'PLEX_URL')

    @property
    def plex_token(self):
        return self._get_val('Plex', 'PLEX_TOKEN', 'PLEX_TOKEN')

    @property
    def log_file(self):
        return self._get_val('Logging', 'LOG_FILE', 'LOG_FILE', './config/bingesentry.log')

    @property
    def log_level(self):
        return self._get_val('Logging', 'LOG_LEVEL', 'LOG_LEVEL', 'INFO')

    @property
    def log_to_console(self):
        return self._get_val('Logging', 'LOG_TO_CONSOLE', 'LOG_TO_CONSOLE', True, bool)

    @property
    def cache_command(self):
        return self._get_val('Cache', 'CACHE_COMMAND', 'CACHE_COMMAND', 'python cache_executor.py --file {file_path}')

    @property
    def min_free_space_gb(self):
        return self._get_val('Cache', 'MIN_FREE_SPACE_GB', 'MIN_FREE_SPACE_GB', 5.0, float)

    @property
    def episodes_to_cache(self):
        return self._get_val('Cache', 'EPISODES_TO_CACHE', 'EPISODES_TO_CACHE', 1, int)

    @property
    def cache_start_threshold_pct(self):
        return self._get_val('Cache', 'CACHE_START_THRESHOLD_PCT', 'CACHE_START_THRESHOLD_PCT', 50, int)

    @property
    def throttle_speed_active_mb(self):
        return self._get_val('Cache', 'THROTTLE_SPEED_ACTIVE_MB', 'THROTTLE_SPEED_ACTIVE_MB', 0.0, float)


    @property
    def path_map_from(self):
        return self._get_val('Cache', 'PATH_MAP_FROM', 'PATH_MAP_FROM', '')

    @property
    def path_map_to(self):
        return self._get_val('Cache', 'PATH_MAP_TO', 'PATH_MAP_TO', '')

    @property
    def rclone_cache_dir(self):
        return self._get_val('Cache', 'RCLONE_CACHE_DIR', 'RCLONE_CACHE_DIR', '')

    @property
    def rclone_remote_name(self):
        return self._get_val('Cache', 'RCLONE_REMOTE_NAME', 'RCLONE_REMOTE_NAME', '')

    @property
    def rclone_mount_dir(self):
        return self._get_val('Cache', 'RCLONE_MOUNT_DIR', 'RCLONE_MOUNT_DIR', '')


    @property
    def tui_mode(self):
        return self._get_val('Daemon', 'TUI_MODE', 'TUI_MODE', False, bool)

    @property
    def max_cache_total_gb(self):
        return self._get_val('Cache', 'MAX_CACHE_TOTAL_GB', 'MAX_CACHE_TOTAL_GB', 0.0, float)

    @property
    def max_concurrent_caches(self):
        return self._get_val('Cache', 'MAX_CONCURRENT_CACHES', 'MAX_CONCURRENT_CACHES', 1, int)

    def _get_list(self, section, option, env_name):
        val = self._get_val(section, option, env_name, '')
        if not val:
            return []
        return [item.strip() for item in val.split(',') if item.strip()]

    @property
    def user_whitelist(self):
        return self._get_list('Cache', 'USER_WHITELIST', 'USER_WHITELIST')

    @property
    def user_blacklist(self):
        return self._get_list('Cache', 'USER_BLACKLIST', 'USER_BLACKLIST')

    @property
    def library_whitelist(self):
        return self._get_list('Cache', 'LIBRARY_WHITELIST', 'LIBRARY_WHITELIST')

    @property
    def library_blacklist(self):
        return self._get_list('Cache', 'LIBRARY_BLACKLIST', 'LIBRARY_BLACKLIST')

    @property
    def queue_file(self):
        return self._get_val('Cache', 'QUEUE_FILE', 'QUEUE_FILE', './config/queue.json')

    @property
    def max_cpu_percent_limit(self):
        return self._get_val('Cache', 'MAX_CPU_PERCENT_LIMIT', 'MAX_CPU_PERCENT_LIMIT', 0.0, float)

    @property
    def max_mem_percent_limit(self):
        return self._get_val('Cache', 'MAX_MEM_PERCENT_LIMIT', 'MAX_MEM_PERCENT_LIMIT', 0.0, float)

    @property
    def max_history_count(self):
        return self._get_val('Cache', 'MAX_HISTORY_COUNT', 'MAX_HISTORY_COUNT', 50, int)

    @property
    def max_history_age_days(self):
        return self._get_val('Cache', 'MAX_HISTORY_AGE_DAYS', 'MAX_HISTORY_AGE_DAYS', 7.0, float)

