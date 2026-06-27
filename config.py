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
                return val_type(val)
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
                return self.config.get(section, option)
            except Exception:
                pass
                
        # 3. Fallback to default value
        return default

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
        return self._get_val('Cache', 'CACHE_COMMAND', 'CACHE_COMMAND', 'rclone md5sum {file_path}')

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

