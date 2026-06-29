import os
import sys
import time
import logging
from plexapi.video import Episode

# Import modular helpers
from config import Config
from logger import setup_logger
from disk import has_enough_disk_space, get_file_size_gb, map_path, get_cache_status, is_mount_responsive, resolve_existing_path
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

_last_scan_time = 0.0

def save_sessions_json(currently_playing, config, queue_manager):
    import json
    sessions_data = []
    if currently_playing:
        for session in currently_playing:
            user = session.usernames[0] if session.usernames else "Unknown User"
            if isinstance(session, Episode):
                show = session.grandparentTitle
                season_num = session.parentIndex
                episode_num = session.index
                title = f"{show} (S{season_num}E{episode_num})"
                
                duration = getattr(session, 'duration', 0)
                view_offset = getattr(session, 'viewOffset', 0)
                pct = int((view_offset / duration) * 100) if duration > 0 else 0
                progress = f"{pct}%" if duration > 0 else "Playing"
                
                next_cache = "None"
                file_path = getattr(session, '_next_cache_file', None)
                if getattr(session, '_cache_filtered', False):
                    next_cache = "Filtered (User/Library)"
                elif getattr(session, '_end_of_show', False):
                    next_cache = "End of Show"
                elif file_path is not None:
                    is_cached, cached_pct, _, _ = get_cache_status(
                        file_path,
                        config.rclone_cache_dir,
                        config.rclone_remote_name,
                        config.rclone_mount_dir or config.path_map_to
                    )
                    filename = os.path.basename(file_path)
                    if is_cached:
                        next_cache = f"{filename} (100%)"
                    elif cached_pct > 0:
                        next_cache = f"{filename} ({cached_pct:.1f}%)"
                    else:
                        task = queue_manager.get_task(file_path)
                        if task:
                            if task.status == "Pending":
                                next_cache = f"{filename} (queued)"
                            elif "Paused" in task.status:
                                next_cache = f"{filename} (paused)"
                            else:
                                next_cache = f"{filename} (caching...)"
                        else:
                            next_cache = f"{filename} (not cached)"
                
                sessions_data.append({
                    "user": user,
                    "type": "TV Show",
                    "title": title,
                    "progress": progress,
                    "next_cache": next_cache
                })
            else:
                movie_title = getattr(session, 'title', 'Unknown Movie')
                progress = "Playing"
                duration = getattr(session, 'duration', 0)
                view_offset = getattr(session, 'viewOffset', 0)
                if duration > 0:
                    pct = int((view_offset / duration) * 100)
                    progress = f"{pct}%"
                sessions_data.append({
                    "user": user,
                    "type": "Movie",
                    "title": movie_title,
                    "progress": progress,
                    "next_cache": "- (No cache required)"
                })
    try:
        sessions_file = os.path.join(os.path.dirname(config.queue_file), 'sessions.json')
        with open(sessions_file, 'w') as f:
            json.dump(sessions_data, f, indent=4)
    except Exception as e:
        logging.error(f"Failed to write sessions.json: {e}")

def check_sessions(conn_manager, config, queue_manager, tui_dashboard=None, force=False):
    """
    Scans active sessions, logs streaming activity, maps paths, and triggers caches.
    """
    global _last_scan_time
    current_time = time.time()

    # Mount responsiveness health guard
    mount_dir = config.rclone_mount_dir or config.path_map_to
    if mount_dir and not is_mount_responsive(mount_dir):
        logging.error(f"Mount Health Guard: Mount directory '{mount_dir}' is unresponsive or disconnected! Suspending caching queue.")
        queue_manager.handle_buffering_guard(any_user_buffering=True)
        return

    # Rate-limit Plex sessions query to once every 5 seconds to prevent request storms.
    # We still process queue updates locally on rate-limited events.
    if not force and (current_time - _last_scan_time < 5.0):
        queue_manager.process_queue()
        return

    _last_scan_time = current_time

    plex = conn_manager.plex
    if not plex:
        logging.warning("Plex connection is currently offline. Skipping session check.")
        queue_manager.process_queue()
        return

    try:
        currently_playing = plex.sessions()
    except Exception as e:
        logging.error(f"Failed to query Plex active sessions: {e}")
        queue_manager.process_queue()
        return

    if not currently_playing:
        logging.info("No active media sessions currently playing on Plex.")
        save_sessions_json([], config, queue_manager)
        # Process queue to check for completion and launch next tasks when Plex is idle
        queue_manager.process_queue()
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
                
                # Check user & library whitelists/blacklists
                library = getattr(session, 'librarySectionTitle', '')
                user_allowed = True
                if config.user_whitelist and user not in config.user_whitelist:
                    user_allowed = False
                if config.user_blacklist and user in config.user_blacklist:
                    user_allowed = False
                    
                lib_allowed = True
                if config.library_whitelist and library not in config.library_whitelist:
                    lib_allowed = False
                if config.library_blacklist and library in config.library_blacklist:
                    lib_allowed = False
                    
                if not (user_allowed and lib_allowed):
                    session._cache_filtered = True
                    logging.debug(f"Filtering: Caching skipped for show '{show}' (User: '{user}', Library: '{library}').")
                    continue

                
                # Fetch up to configuration count of sequential upcoming episodes
                next_eps = get_next_episodes_for_session(plex, session, count=config.episodes_to_cache)
                if not next_eps:
                    session._end_of_show = True
                    logging.info(f"No upcoming episodes found for '{colorize(show, 'show')}' (reached end of show).")
                    continue
                    
                for offset, next_ep in enumerate(next_eps, start=1):
                    # Reload episode individually to get accurate file path.
                    # show.episodes() bulk fetch returns stale/lite cached data
                    # that may not match the actual current path on disk.
                    try:
                        next_ep.reload()
                    except Exception as e:
                        logging.debug(f"Could not reload episode metadata: {e}")
                    if not next_ep.media or not next_ep.media[0].parts:
                        logging.warning(f"No media files found for upcoming episode S{next_ep.seasonNumber}E{next_ep.index}")
                        continue
                    raw_file_path = next_ep.media[0].parts[0].file
                    if not raw_file_path:
                        logging.warning(f"Empty media path found for upcoming episode S{next_ep.seasonNumber}E{next_ep.index}")
                        continue
                    
                    # Convert remote Plex media path to cache client path
                    mapped_file_path = map_path(raw_file_path, config.path_map_from, config.path_map_to)
                    mapped_file_path = resolve_existing_path(mapped_file_path)
                    
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
                        logging.debug(
                            f"Skipping cache queue for S{next_ep.seasonNumber}E{next_ep.index}: "
                            f"file not available locally yet (size=0). Will retry on next poll."
                        )
                        continue
                        
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
    save_sessions_json(currently_playing, config, queue_manager)
    
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
        self.is_connecting = False
        self._connection_lock = threading.Lock()

    def connect(self):
        """
        Attempts to connect to Plex indefinitely until successful.
        """
        with self._connection_lock:
            if self.is_connecting:
                logging.debug("Reconnection already in progress. Skipping duplicate thread.")
                return
            self.is_connecting = True

        try:
            while self.is_running:
                logging.info("Connecting to Plex Media Server...")
                try:
                    self.plex = connect_to_plex(self.config.plex_url, self.config.plex_token)
                    logging.info("Successfully connected to Plex Media Server.")
                    self.start_listener()
                    break
                except Exception as e:
                    logging.warning(f"Could not connect to Plex Server: {e}. Retrying in 10 seconds...")
                    # Sleep in small chunks to react immediately to shutdown signals
                    for _ in range(20):
                        if not self.is_running:
                            break
                        time.sleep(0.5)
        finally:
            with self._connection_lock:
                self.is_connecting = False

    def start_listener(self):
        """
        Starts the WebSocket alert listener. If starting fails, it propagates the exception to the caller.
        """
        if self.alert_listener:
            try:
                logging.debug("Stopping existing Plex WebSocket Alert Listener...")
                self.alert_listener.stop()
            except Exception as e:
                logging.debug(f"Error stopping existing Alert Listener: {e}")
            self.alert_listener = None

        logging.info("Starting Plex WebSocket Alert Listener...")
        def alert_callback(data):
            if isinstance(data, dict) and data.get('type') in ('playing', 'timeline', 'activity', 'library-changed'):
                self.scan_trigger_event.set()

        def alert_error_callback(error):
            logging.error(f"Plex WebSocket Alert Listener error: {error}. Attempting to reconnect...")
            self.alert_listener = None
            self.plex = None
            self.scan_trigger_event.set()  # Wake up loop to update status
            # Trigger reconnection thread from async error handler
            threading.Thread(target=self.connect, daemon=True).start()

        self.alert_listener = self.plex.startAlertListener(
            callback=alert_callback,
            callbackError=alert_error_callback
        )
        logging.info("Plex WebSocket Alert Listener started successfully.")
        # Wake up daemon loop to perform initial check
        self.scan_trigger_event.set()

    def shutdown(self):
        """
        Stops the alert listener.
        """
        self.is_running = False
        self.scan_trigger_event.set()  # Wake up scanning loop instantly to allow immediate exit
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

    tui_enabled = config.tui_mode
    fallback_warning = False
    if tui_enabled and not sys.stdout.isatty():
        fallback_warning = True
        tui_enabled = False

    # Setup appropriate logging handlers
    if tui_enabled:
        from tui import MemoryLogHandler, TUIDashboard
        # Disable direct standard console logging in TUI mode to avoid visual corruption
        setup_logger(config.log_file, config.log_level, log_to_console=False)
        
        # Add memory log handler for TUI live display
        root_logger = logging.getLogger()
        memory_log_handler = MemoryLogHandler(capacity=20)
        root_logger.addHandler(memory_log_handler)
    else:
        setup_logger(config.log_file, config.log_level, config.log_to_console)
    
    if fallback_warning:
        logging.warning("TUI Mode is enabled in configuration, but stdout is not a TTY. Falling back to background daemon mode.")

    logging.info("========================================")
    logging.info("BingeSentry Service Initialized")
    logging.info(f"TUI Mode     : {tui_enabled}")
    logging.info("========================================")

    from queue_manager import CachingQueueManager
    queue_manager = CachingQueueManager(config)
        
    import threading
    scan_trigger_event = threading.Event()

    # Initialize connection manager to handle connection & socket retrying/recreation
    conn_manager = PlexConnectionManager(config, scan_trigger_event)
    if not tui_enabled:
        conn_manager.connect()
    else:
        # Run connection manager asynchronously in background thread for TUI mode
        threading.Thread(target=conn_manager.connect, daemon=True).start()
        
    try:
        if tui_enabled:
            dashboard = TUIDashboard(config.plex_url, config, memory_log_handler)
            dashboard.run_loop(conn_manager, queue_manager, check_sessions, scan_trigger_event)
        else:
            logging.info("Entering service daemon loop (polling disabled, WebSocket event-driven only)...")
            try:
                while True:
                    scan_trigger_event.clear()
                    # Wait for WebSocket event or wake up periodically (every 10 seconds) to process the queue
                    scan_trigger_event.wait(timeout=10.0)
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
