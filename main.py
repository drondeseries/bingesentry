import os
import sys
import time
import logging
from plexapi.video import Episode

# Import modular helpers
from config import Config
from logger import setup_logger
from disk import has_enough_disk_space, get_file_size_gb, map_path, get_cache_status, is_mount_responsive
from rclone import is_file_being_cached, start_cache_process
from plex_utils import connect_to_plex, get_next_episodes_for_session


# Custom ANSI colors for formatted logging in the console
COLORS = {
    'user': '\033[38;5;33m',        # Vibrant Blue
    'movie': '\033[38;5;166m',      # Warm Orange
    'show': '\033[38;5;63m',        # Slate Purple/Blue
    'current': '\033[38;5;201m',    # Hot Pink
    'size': '\033[38;5;118m',       # Emerald Green
    'info': '\033[38;5;220m'        # Golden Yellow
}
RESET_COLOR = '\033[0m'

def colorize(message, color_key):
    """
    Applies escape codes to wrap console text with color.
    """
    return f"{COLORS.get(color_key, '')}{message}{RESET_COLOR}"

def check_sessions(conn_manager, config, queue_manager, tui_dashboard=None):
    """
    Scans active sessions, logs streaming activity, maps paths, and triggers caches.
    """
    # Mount responsiveness health guard
    mount_dir = config.rclone_mount_dir or config.path_map_to
    if mount_dir and not is_mount_responsive(mount_dir):
        logging.error(f"Mount Health Guard: Mount directory '{mount_dir}' is unresponsive or disconnected! Suspending caching queue.")
        queue_manager.handle_buffering_guard(any_user_buffering=True)
        return

    plex = conn_manager.plex
    if not plex:
        logging.warning("Plex connection is currently offline. Skipping session check.")
        queue_manager.update_task_statuses()
        return

    try:
        currently_playing = plex.sessions()
    except Exception as e:
        logging.error(f"Failed to query Plex active sessions: {e}")
        return

    if not currently_playing:
        logging.info("No active media sessions currently playing on Plex.")
        # Ensure active tasks finish status updating even if nothing is currently playing
        queue_manager.update_task_statuses()
        return

    # Check if any user session is in a buffering state
    any_user_buffering = False
    for session in currently_playing:
        state = getattr(session, 'state', '') or getattr(getattr(session, 'player', None), 'state', '')
        if state.lower() == 'buffering':
            any_user_buffering = True
            break
            
    # Trigger buffering guard to suspend/resume active downloads
    queue_manager.handle_buffering_guard(any_user_buffering)

    shows_list = []
    movies_list = []

    for session in currently_playing:
        try:
            if isinstance(session, Episode):
                show = session.grandparentTitle
                user = session.usernames[0] if session.usernames else "Unknown User"
                season_num = session.parentIndex
                episode_num = session.index
                
                # Calculate playback progress
                duration = getattr(session, 'duration', 0)
                view_offset = getattr(session, 'viewOffset', 0)
                progress_pct = (view_offset / duration) * 100.0 if duration > 0 else 0.0
                
                threshold = config.cache_start_threshold_pct
                is_below_threshold = False
                if threshold > 0 and progress_pct < threshold:
                    is_below_threshold = True
                
                log_message = (
                    f"Show: {colorize(show, 'show')} | "
                    f"User: {colorize(user, 'user')} | "
                    f"Season: {season_num} | "
                    f"Episode: {episode_num} | "
                    f"Progress: {progress_pct:.1f}%"
                )
                shows_list.append(log_message)
                logging.info(log_message)
                

                
                # Fetch up to configuration count of sequential upcoming episodes
                next_eps = get_next_episodes_for_session(plex, session, count=config.episodes_to_cache)
                if not next_eps:
                    logging.info(f"No upcoming episodes found for '{colorize(show, 'show')}' (reached end of show).")
                    continue
                    
                for offset, next_ep in enumerate(next_eps, start=1):
                    raw_file_path = next_ep.media[0].parts[0].file
                    
                    # Convert remote Plex media path to cache client path
                    mapped_file_path = map_path(raw_file_path, config.path_map_from, config.path_map_to)
                    
                    # Store resolved cache path and progress stats on session for TUI retrieval
                    session._next_cache_file = mapped_file_path
                    session._cache_threshold_waiting = is_below_threshold
                    session._cache_threshold_val = threshold
                    session._current_progress_pct = progress_pct
                    
                    if is_below_threshold:
                        logging.info(
                            f"Caching of S{next_ep.seasonNumber}E{next_ep.index} deferred: "
                            f"Progress is {progress_pct:.1f}% (requires {threshold}%)."
                        )
                        continue
                        
                    logging.info(
                        f"Target caching episode S{next_ep.seasonNumber}E{next_ep.index}: '{mapped_file_path}'" + 
                        (f" (mapped from '{raw_file_path}')" if mapped_file_path != raw_file_path else "")
                    )
                    
                    # Check if the file is already fully cached locally
                    is_cached, cached_pct, _, _ = get_cache_status(
                        mapped_file_path,
                        config.rclone_cache_dir,
                        config.rclone_remote_name,
                        config.rclone_mount_dir or config.path_map_to
                    )
                    if is_cached:
                        logging.info(f"Skipping cache process: '{mapped_file_path}' is already 100% cached locally.")
                        continue
                        
                    # Handle size validation and disk space checking
                    file_size_gb = get_file_size_gb(mapped_file_path)
                    required_space = max(config.min_free_space_gb, file_size_gb)
                    
                    has_space, free_space = has_enough_disk_space(os.path.dirname(mapped_file_path), required_space)
                    if not has_space:
                        logging.warning(
                            f"Cache skipped: insufficient disk space. "
                            f"Required: {required_space:.2f} GB, Available: {free_space:.2f} GB. "
                            f"Path: '{mapped_file_path}'"
                        )
                        continue
                        
                    if file_size_gb > 0:
                        logging.info(f"Estimated cache file size: {colorize(f'{file_size_gb:.2f} GB', 'size')}")
                    else:
                        logging.info("File size returned 0 GB (might not exist locally yet). Spawning cache process.")
                        
                    # Add task to sequential queue instead of executing immediately
                    queue_manager.add_or_update_task(
                        mapped_file_path,
                        file_size_gb,
                        progress_pct,
                        show,
                        f"S{next_ep.seasonNumber}E{next_ep.index}",
                        offset
                    )
            else:
                movie_name = getattr(session, 'title', 'Unknown Title')
                user = session.usernames[0] if session.usernames else "Unknown User"
                log_message = f"Movie: {colorize(movie_name, 'movie')} | User: {colorize(user, 'user')}"
                movies_list.append(log_message)
                logging.info(log_message)
                
        except Exception as e:
            logging.error(f"Error processing session item: {e}", exc_info=True)
            continue
            
    # Process sequential downloads queue
    queue_manager.process_queue()
    
    # Output playback session summaries
    if shows_list or movies_list:
        logging.info("--- Active Playback Summary ---")
        if shows_list:
            logging.info(f"Currently playing {len(shows_list)} show(s):")
            for show_log in shows_list:
                logging.info(f"  * {show_log}")
        if movies_list:
            logging.info(f"Currently playing {len(movies_list)} movie(s):")
            for movie_log in movies_list:
                logging.info(f"  * {movie_log}")
        logging.info("-------------------------------")



    # Update TUI dashboard with decorated session objects containing cached metadata attributes
    if tui_dashboard:
        tui_dashboard.update_sessions(currently_playing)


import threading

class PlexConnectionManager:
    """
    Manages a persistent connection to the Plex Media Server and handles the
    recreation of the WebSocket AlertListener on errors or disconnects.
    """
    def __init__(self, config, scan_trigger_event):
        self.config = config
        self.scan_trigger_event = scan_trigger_event
        self.plex = None
        self.alert_listener = None
        self.is_running = True

    def connect(self):
        """
        Attempts to connect to Plex indefinitely until successful.
        """
        while self.is_running:
            logging.info("Connecting to Plex Media Server...")
            try:
                self.plex = connect_to_plex(self.config.plex_url, self.config.plex_token)
                logging.info("Successfully connected to Plex Media Server.")
                self.start_listener()
                break
            except Exception as e:
                logging.warning(f"Could not connect to Plex Server: {e}. Retrying in 10 seconds...")
                time.sleep(10)

    def start_listener(self):
        """
        Starts the WebSocket alert listener. If starting fails, it spawns a background reconnect.
        """
        try:
            logging.info("Starting Plex WebSocket Alert Listener...")
            def alert_callback(data):
                if data.get('type') in ('playing', 'timeline'):
                    self.scan_trigger_event.set()

            def alert_error_callback(error):
                logging.error(f"Plex WebSocket Alert Listener error: {error}. Attempting to reconnect...")
                self.alert_listener = None
                self.plex = None
                self.scan_trigger_event.set()  # Wake up loop to update status
                # Trigger reconnection thread
                threading.Thread(target=self.connect, daemon=True).start()

            self.alert_listener = self.plex.startAlertListener(
                callback=alert_callback,
                callbackError=alert_error_callback
            )
            logging.info("Plex WebSocket Alert Listener started successfully.")
            # Wake up daemon loop to perform initial check
            self.scan_trigger_event.set()
        except Exception as e:
            logging.warning(f"Failed to start Plex WebSocket Alert Listener: {e}. Retrying in 10 seconds...")
            self.alert_listener = None
            self.plex = None
            threading.Thread(target=self.connect, daemon=True).start()

    def shutdown(self):
        """
        Stops the alert listener.
        """
        self.is_running = False
        if self.alert_listener:
            try:
                self.alert_listener.stop()
                logging.info("Plex WebSocket Alert Listener stopped.")
            except Exception:
                pass


def main():
    try:
        config = Config()
    except Exception as e:
        print(f"Error loading configuration: {e}")
        sys.exit(1)

    # Setup appropriate logging handlers
    if config.tui_mode:
        from tui import MemoryLogHandler, TUIDashboard
        # Disable direct standard console logging in TUI mode to avoid visual corruption
        setup_logger(config.log_file, config.log_level, log_to_console=False)
        
        # Add memory log handler for TUI live display
        root_logger = logging.getLogger()
        memory_log_handler = MemoryLogHandler(capacity=20)
        memory_log_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
        root_logger.addHandler(memory_log_handler)
    else:
        setup_logger(config.log_file, config.log_level, config.log_to_console)
    
    logging.info("========================================")
    logging.info("BingeSentry Service Initialized")
    logging.info(f"TUI Mode     : {config.tui_mode}")
    logging.info("========================================")
    
    from queue_manager import CachingQueueManager
    queue_manager = CachingQueueManager(config)
        
    import threading
    scan_trigger_event = threading.Event()
    
    # Initialize connection manager to handle connection & socket retrying/recreation
    conn_manager = PlexConnectionManager(config, scan_trigger_event)
    conn_manager.connect()
        
    try:
        if config.tui_mode:
            dashboard = TUIDashboard(config.plex_url, config, memory_log_handler)
            dashboard.run_loop(conn_manager, queue_manager, check_sessions, scan_trigger_event)
        else:
            logging.info("Entering service daemon loop (polling disabled, WebSocket event-driven only)...")
            try:
                while True:
                    scan_trigger_event.clear()
                    scan_trigger_event.wait()
                    check_sessions(conn_manager, config, queue_manager)
            except KeyboardInterrupt:
                logging.info("Daemon service stopped by user signal.")
    finally:
        # Gracefully shut down the alert listener
        conn_manager.shutdown()
        # Stop any active subprocesses safely
        queue_manager.cleanup()


if __name__ == "__main__":
    main()
