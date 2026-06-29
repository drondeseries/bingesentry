import os
import re
import time
import logging
from datetime import datetime
import psutil
from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Header, Footer, DataTable, RichLog, Label, Input, Button
from textual.containers import Grid, Vertical, Container, Horizontal
from textual.coordinate import Coordinate
from textual import work, events
from disk import get_cache_status, is_mount_responsive, get_cpu_percent

def truncate_text(text, max_len=25):
    if len(text) <= max_len:
        return text
    return text[:max_len-3] + "..."

def truncate_path(file_path, max_len=30):
    if not file_path:
        return "-"
    filename = os.path.basename(file_path)
    if len(filename) <= max_len:
        return filename
    name, ext = os.path.splitext(filename)
    if len(ext) > 6:
        ext = ""
    ext_len = len(ext)
    avail = max_len - 3 - ext_len
    if avail > 5:
        return name[:avail] + "..." + ext
    else:
        return filename[:max_len-3] + "..."

def get_row_key(table, row_index):
    try:
        if row_index is not None:
            if hasattr(table, "check_row_index"):
                row_key, _ = table.check_row_index(row_index)
                return row_key
            if hasattr(table, "_row_locations") and hasattr(table._row_locations, "get_key"):
                return table._row_locations.get_key(row_index)
    except Exception:
        pass
    return None

class HoverDataTable(DataTable):
    """
    DataTable subclass that dynamically updates its tooltip with the full path of the row under mouse hover.
    """
    def _on_mouse_move(self, event: events.MouseMove) -> None:
        super()._on_mouse_move(event)
        try:
            meta = event.style.meta
            if meta and "row" in meta:
                row_idx = meta["row"]
                row_key = get_row_key(self, row_idx)
                if row_key and row_key.value:
                    self.tooltip = f"Full Path:\n{row_key.value}"
                    return
        except Exception:
            pass
        self.tooltip = None

    def _on_leave(self, event: events.Leave) -> None:
        super()._on_leave(event)
        self.tooltip = None

# Regular expression to strip ANSI color escape codes
ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

# Custom formatter for colorized log output inside Textual RichLog
class TUILogFormatter(logging.Formatter):
    """
    Custom log formatter for the TUI that strips year/date/milliseconds and formats
    log levels with beautiful Rich markup colors to optimize horizontal space.
    """
    def __init__(self):
        super().__init__('%(asctime)s', datefmt='%H:%M:%S')

    def format(self, record):
        asctime = self.formatTime(record, self.datefmt)
        level = record.levelname
        if level == "INFO":
            level_formatted = "[bold green]INFO[/bold green]"
        elif level == "WARNING":
            level_formatted = "[bold yellow]WARN[/bold yellow]"
        elif level == "ERROR":
            level_formatted = "[bold red]ERR [/bold red]"
        elif level == "CRITICAL":
            level_formatted = "[bold blink red]CRIT[/bold blink red]"
        elif level == "DEBUG":
            level_formatted = "[bold blue]DBG [/bold blue]"
        else:
            level_formatted = f"[bold]{level}[/bold]"
            
        message = record.getMessage()
        # Strip raw ANSI escape color codes from message to prevent garbled text in the TUI
        message_clean = ANSI_ESCAPE.sub('', message)
        return f"[dim]{asctime}[/dim] [{level_formatted}] {message_clean}"


class MemoryLogHandler(logging.Handler):
    """
    In-memory logging handler to store early startup logs before Textual starts.
    """
    def __init__(self, capacity=20):
        super().__init__()
        self.capacity = capacity
        self.logs = []
        self.setFormatter(TUILogFormatter())
        
    def emit(self, record):
        log_entry = self.format(record)
        self.logs.append(log_entry)
        if len(self.logs) > self.capacity:
            self.logs.pop(0)


class TextualLogHandler(logging.Handler):
    """
    Direct handler to stream python logging messages thread-safely into the Textual Log widget.
    """
    def __init__(self, log_widget):
        super().__init__()
        self.log_widget = log_widget
        self.setFormatter(TUILogFormatter())

    def emit(self, record):
        try:
            log_entry = self.format(record)
            self.log_widget.app.call_from_thread(self.log_widget.write, log_entry)
        except Exception:
            pass


class ConfigEditScreen(ModalScreen):
    """
    Modal screen for editing BingeSentry configurations.
    """
    CSS = """
    ConfigEditScreen {
        align: center middle;
    }
    
    #config_dialog {
        width: 80;
        height: auto;
        max-height: 85%;
        overflow-y: auto;
        border: thick #5a547a;
        background: #18142c;
        padding: 1 2;
    }
    
    #config_title {
        text-align: center;
        text-style: bold;
        color: #ff007f;
        margin-bottom: 1;
    }
    
    .config_label {
        color: #f1ecff;
        text-style: bold;
        margin-top: 1;
    }
    
    Input {
        background: #131023;
        color: #ffffff;
        border: solid #5a547a;
        height: 3;
    }
    
    #config_buttons {
        margin-top: 2;
        align: right middle;
    }
    
    Button {
        margin-left: 2;
    }
    """
    
    def __init__(self, config):
        super().__init__()
        self.config = config
        
    def compose(self) -> ComposeResult:
        with Vertical(id="config_dialog"):
            yield Label("🔧 BingeSentry Configuration Editor", id="config_title")
            
            yield Label("Max Concurrent Caches:", classes="config_label")
            yield Input(str(self.config.max_concurrent_caches), id="input_concurrent")
            
            yield Label("Episodes to Cache Ahead:", classes="config_label")
            yield Input(str(self.config.episodes_to_cache), id="input_episodes")
            
            yield Label("Cache Start Progress Threshold (%):", classes="config_label")
            yield Input(str(self.config.cache_start_threshold_pct), id="input_threshold")

            yield Label("Max CPU Load Limit (%): (0 to disable)", classes="config_label")
            yield Input(str(self.config.max_cpu_percent_limit), id="input_cpu_limit")
            
            yield Label("Max Memory Load Limit (%): (0 to disable)", classes="config_label")
            yield Input(str(self.config.max_mem_percent_limit), id="input_mem_limit")

            yield Label("Max History Count:", classes="config_label")
            yield Input(str(self.config.max_history_count), id="input_history_count")

            yield Label("Max History Age (Days):", classes="config_label")
            yield Input(str(self.config.max_history_age_days), id="input_history_age")
            
            yield Label("User Whitelist (comma-separated):", classes="config_label")
            yield Input(", ".join(self.config.user_whitelist), id="input_user_whitelist")
            
            yield Label("User Blacklist (comma-separated):", classes="config_label")
            yield Input(", ".join(self.config.user_blacklist), id="input_user_blacklist")
            
            yield Label("Library Whitelist (comma-separated):", classes="config_label")
            yield Input(", ".join(self.config.library_whitelist), id="input_library_whitelist")
            
            yield Label("Library Blacklist (comma-separated):", classes="config_label")
            yield Input(", ".join(self.config.library_blacklist), id="input_library_blacklist")
            
            with Horizontal(id="config_buttons"):
                yield Button("Save Settings", variant="success", id="save_btn")
                yield Button("Cancel", variant="error", id="cancel_btn")
                
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save_btn":
            try:
                max_concurrent = int(self.query_one("#input_concurrent", Input).value)
                episodes = int(self.query_one("#input_episodes", Input).value)
                threshold = int(self.query_one("#input_threshold", Input).value)
                cpu_limit = float(self.query_one("#input_cpu_limit", Input).value)
                mem_limit = float(self.query_one("#input_mem_limit", Input).value)
                history_count = int(self.query_one("#input_history_count", Input).value)
                history_age = float(self.query_one("#input_history_age", Input).value)
                user_wl = self.query_one("#input_user_whitelist", Input).value
                user_bl = self.query_one("#input_user_blacklist", Input).value
                lib_wl = self.query_one("#input_library_whitelist", Input).value
                lib_bl = self.query_one("#input_library_blacklist", Input).value
                
                config_file = self.config.loaded_file or "config.ini"
                import configparser
                parser = configparser.ConfigParser()
                parser.read(config_file)
                
                if not parser.has_section("Cache"):
                    parser.add_section("Cache")
                    
                parser.set("Cache", "MAX_CONCURRENT_CACHES", str(max_concurrent))
                parser.set("Cache", "EPISODES_TO_CACHE", str(episodes))
                parser.set("Cache", "CACHE_START_THRESHOLD_PCT", str(threshold))
                parser.set("Cache", "MAX_CPU_PERCENT_LIMIT", str(cpu_limit))
                parser.set("Cache", "MAX_MEM_PERCENT_LIMIT", str(mem_limit))
                parser.set("Cache", "MAX_HISTORY_COUNT", str(history_count))
                parser.set("Cache", "MAX_HISTORY_AGE_DAYS", str(history_age))
                parser.set("Cache", "USER_WHITELIST", user_wl)
                parser.set("Cache", "USER_BLACKLIST", user_bl)
                parser.set("Cache", "LIBRARY_WHITELIST", lib_wl)
                parser.set("Cache", "LIBRARY_BLACKLIST", lib_bl)
                
                with open(config_file, 'w') as f:
                    parser.write(f)
                    
                self.config.config.read(config_file)
                logging.info("Configuration updated and reloaded successfully.")
                self.dismiss(True)
            except ValueError:
                logging.error("Configuration editor: Failed to save. Numeric inputs must be valid numbers.")
                self.dismiss(False)
            except Exception as e:
                logging.error(f"Configuration editor: Failed to save: {e}")
                self.dismiss(False)
        elif event.button.id == "cancel_btn":
            self.dismiss(False)


class TUIDashboard(App):
    """
    Modern terminal dashboard powered by Textualize.
    """
    # Sleek dark mode palette with neon highlights
    CSS = """
    Screen {
        background: #0d0b18;
        overflow-x: hidden;
    }
    
    #header {
        background: #18142c;
        color: #f1ecff;
        height: 3;
        content-align: center middle;
        border-bottom: solid magenta;
        padding: 0 1;
        text-style: bold;
    }
    
    #body {
        height: 1fr;
        layout: grid;
        grid-size: 2;
        grid-columns: 3fr 2fr;
        grid-gutter: 1;
        padding: 1 1;
    }
    
    DataTable {
        background: #131023;
        color: #ded9eb;
        border: round #5a547a;
        height: 100%;
        overflow-x: hidden;
    }
    
    DataTable > .datatable--header {
        background: #201a3b;
        color: #ffffff;
        text-style: bold;
    }
    
    #logs_panel {
        height: 9;
        border: round #5a547a;
        background: #0f0d1d;
        padding: 0 1;
        margin: 0 1;
    }
    
    #controls {
        height: 2;
        content-align: center middle;
        background: #18142c;
        color: #c9bfdf;
        border-top: solid #5a547a;
        text-style: bold;
        overflow-x: hidden;
    }
    """
    
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("p", "toggle_pause", "Pause/Resume"),
        ("f", "force_scan", "Force Scan"),
        ("c", "edit_config", "Edit Config"),
        ("h", "toggle_history", "Toggle History"),
        ("d", "cancel_task", "Cancel Selected"),
        ("u", "prioritize_task", "Prioritize Selected"),
        ("r", "retry_task", "Retry Selected"),
    ]
    
    def __init__(self, plex_url, config, log_handler, view_only=False):
        super().__init__()
        self.plex_url = plex_url
        self.config = config
        self.log_handler = log_handler
        self.start_time = time.time()
        self.mount_healthy = True
        self.show_history = False
        
        self.active_sessions = []
        self.view_only = view_only
        
    def compose(self) -> ComposeResult:
        yield Label("BingeSentry Status Dashboard Loading...", id="header")
        with Container(id="body"):
            yield HoverDataTable(id="sessions_table")
            yield HoverDataTable(id="queue_table")
        yield RichLog(id="logs_panel", markup=True, highlight=True, max_lines=500)
        yield Label("Controls ➔ Q: Quit | P: Pause/Resume Scanner | F: Force Scan | C: Edit Config | H: Toggle History | D: Cancel Task | U: Prioritize | R: Retry", id="controls")

    def on_mount(self):
        # Initialize tables
        sessions_table = self.query_one("#sessions_table", HoverDataTable)
        sessions_table.border_title = "Active Plex Playback Sessions"
        sessions_table.add_columns("User", "Type", "Title / Playing Media", "Progress", "Next Cache Target")
        
        queue_table = self.query_one("#queue_table", HoverDataTable)
        queue_table.border_title = "Rclone Cache Queue"
        queue_table.add_columns("File Name", "Size", "PID", "Status", "Elapsed")
        queue_table.cursor_type = "row"
        queue_table.show_cursor = True
        queue_table.focus()
        
        if self.view_only:
            self.last_log_offset = 0
            self.set_interval(1.0, self.tail_log_file)
            self.set_interval(1.0, self.refresh_dashboard_view_only)
            self.set_interval(1.0, self.update_system_stats_view_only)
        else:
            # Populate early logs from handler
            log_widget = self.query_one("#logs_panel", RichLog)
            for log in self.log_handler.logs:
                log_widget.write(log)
                
            # Bind log widget to textual log handler
            self.textual_log_handler = TextualLogHandler(log_widget)
            logging.getLogger().addHandler(self.textual_log_handler)
            
            # Remove the early memory log handler to prevent duplicate formatting overhead
            logging.getLogger().removeHandler(self.log_handler)
            
            # Start intervals
            self.set_interval(0.5, self.check_trigger_event)
            self.set_interval(1.0, self.refresh_dashboard)
            self.set_interval(1.0, self.update_system_stats)
            self.set_interval(10.0, self.trigger_periodic_scan)

    def trigger_periodic_scan(self):
        """
        Periodically wakes up the scanner to check queue status and processes even when Plex is silent.
        """
        if self.scan_trigger_event:
            self._is_periodic_scan = True
            self.scan_trigger_event.set()
    def update_sessions(self, sessions):
        self.active_sessions = sessions

    def check_trigger_event(self):
        if self.scan_trigger_event and self.scan_trigger_event.is_set():
            self.scan_trigger_event.clear()
            is_periodic = getattr(self, '_is_periodic_scan', False)
            self._is_periodic_scan = False
            self.run_plex_scan(force=not is_periodic)

    @work(thread=True)
    def run_plex_scan(self, force=False):
        """
        Runs the Plex scan in a background thread to prevent UI freezing.
        """
        try:
            mount_dir = self.config.rclone_mount_dir or self.config.path_map_to
            self.mount_healthy = is_mount_responsive(mount_dir) if mount_dir else True
            
            if self.mount_healthy:
                self.fetch_sessions_func(self.conn_manager, self.config, self.queue_manager, self, force=force)
            else:
                logging.error(f"Mount Health Guard: Mount directory '{mount_dir}' is unresponsive or disconnected! Suspending caching queue.")
                self.queue_manager.handle_buffering_guard(any_user_buffering=True)
        except Exception as e:
            logging.error(f"Error querying Plex sessions in TUI: {e}")

    def update_system_stats(self):
        uptime_secs = int(time.time() - self.start_time)
        hours, remainder = divmod(uptime_secs, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        
        cpu_usage = get_cpu_percent()
        mem_usage = psutil.virtual_memory().percent
        
        status_text = "[bold yellow](PAUSED)[/bold yellow]" if getattr(self, 'is_paused', False) else "[bold green](ONLINE)[/bold green]"
        if not self.mount_healthy:
            status_text = "[bold blink red](MOUNT OFFLINE)[/bold blink red]"
        elif not self.conn_manager or not self.conn_manager.plex:
            status_text = "[bold blink red](PLEX OFFLINE)[/bold blink red]"
            
        # Get mount free space if possible
        mount_dir = self.config.rclone_mount_dir or self.config.path_map_to
        disk_str = ""
        if self.mount_healthy and mount_dir:
            try:
                from disk import has_enough_disk_space
                _, free_space_gb = has_enough_disk_space(mount_dir, 0.0)
                if free_space_gb > 0:
                    disk_str = f" | Disk: {free_space_gb:.1f} GB free"
            except Exception:
                pass

        # Get bandwidth stats
        saved_str = ""
        try:
            from disk import get_bandwidth_stats
            stats = get_bandwidth_stats()
            saved_gb = stats.get("total_saved_bytes", 0) / (1024 ** 3)
            if saved_gb > 0:
                saved_str = f" | Saved: {saved_gb:.1f} GB"
        except Exception:
            pass

        header = self.query_one("#header", Label)
        header.update(
            f"BingeSentry ➔ {self.plex_url} {status_text} | "
            f"CPU: {cpu_usage}% | MEM: {mem_usage}%{disk_str}{saved_str} | "
            f"Uptime: {uptime_str} | Time: {datetime.now().strftime('%H:%M:%S')}"
        )

    def refresh_dashboard(self):
        self.refresh_sessions_table()
        self.refresh_queue_table()

    def refresh_sessions_table(self):
        table = self.query_one("#sessions_table", HoverDataTable)
        table.clear(columns=True)
        table.add_columns("User", "Type", "Title / Playing Media", "Progress", "Next Cache Target")
        
        if not self.active_sessions:
            table.add_row("No active sessions on Plex.", "-", "-", "-", "-")
            return
            
        from main import map_path, resolve_existing_path
        for session in self.active_sessions:
            user = session.usernames[0] if session.usernames else "Unknown"
            if session.type == "episode":
                show = session.grandparentTitle
                season_num = session.parentIndex
                episode_num = session.index
                title = f"{show} (S{season_num}E{episode_num})"
                title_truncated = truncate_text(title, 32)
                
                duration = getattr(session, 'duration', 0)
                view_offset = getattr(session, 'viewOffset', 0)
                pct = int((view_offset / duration) * 100) if duration > 0 else 0
                progress = f"{pct}%" if duration > 0 else "Playing"
                
                next_cache = "Resolving..."
                file_path = getattr(session, '_next_cache_file', None)
                if getattr(session, '_cache_filtered', False):
                    next_cache = "[yellow]Filtered (User/Library)[/yellow]"
                elif getattr(session, '_end_of_show', False):
                    next_cache = "[green]End of Show[/green]"
                elif getattr(session, '_next_cache_file', None) is not None:
                    filename = truncate_path(file_path, 32)
                    
                    if getattr(session, '_cache_threshold_waiting', False):
                        thresh = getattr(session, '_cache_threshold_val', 0)
                        next_cache = f"{filename} [yellow](deferred: {pct}%/{thresh}%)[/yellow]"
                    else:
                        is_cached, cached_pct, _, _ = get_cache_status(
                            file_path,
                            self.config.rclone_cache_dir,
                            self.config.rclone_remote_name,
                            self.config.rclone_mount_dir or self.config.path_map_to
                        )
                        if is_cached:
                            next_cache = f"{filename} [green](100%)[/green]"
                        elif cached_pct > 0:
                            next_cache = f"{filename} [yellow]({cached_pct:.1f}%)[/yellow]"
                        else:
                            task = self.queue_manager.get_task(file_path)
                            if task:
                                if task.status == "Pending":
                                    next_cache = f"{filename} [cyan](queued)[/cyan]"
                                elif "Paused" in task.status or task.status == "Paused":
                                    reason = task.status.replace("Paused", "").strip(" ()")
                                    reason = reason if reason else "buffering"
                                    style = "red" if "Failed" in task.status else "yellow"
                                    next_cache = f"{filename} [{style}](paused: {reason})[/{style}]"
                                else:
                                    next_cache = f"{filename} [green](caching...)[/green]"
                            else:
                                next_cache = f"{filename} [red](not cached)[/red]"
                else:
                    next_cache = "None / Finished"
                
                playing_file = None
                try:
                    raw_path = session.media[0].parts[0].file
                    playing_file = map_path(raw_path, self.config.path_map_from, self.config.path_map_to)
                    playing_file = resolve_existing_path(playing_file)
                except Exception:
                    pass
                row_key = file_path or playing_file
                table.add_row(user, "TV Show", title_truncated, progress, next_cache, key=row_key)
            else:
                movie_title = getattr(session, 'title', 'Unknown Movie')
                movie_title_truncated = truncate_text(movie_title, 32)
                progress = "Playing"
                duration = getattr(session, 'duration', 0)
                view_offset = getattr(session, 'viewOffset', 0)
                if duration > 0:
                    pct = int((view_offset / duration) * 100)
                    progress = f"{pct}%"
                    
                movie_file = None
                try:
                    raw_path = session.media[0].parts[0].file
                    movie_file = map_path(raw_path, self.config.path_map_from, self.config.path_map_to)
                    movie_file = resolve_existing_path(movie_file)
                except Exception:
                    pass
                table.add_row(user, "Movie", movie_title_truncated, progress, "- (No cache required)", key=movie_file)

    def refresh_queue_table(self):
        table = self.query_one("#queue_table", HoverDataTable)
        all_tasks = self.queue_manager.get_all_tasks()
        
        saved_key = None
        if table.cursor_row is not None:
            try:
                saved_key = get_row_key(table, table.cursor_row)
            except Exception:
                pass
                
        table.clear(columns=True)
        
        if getattr(self, 'show_history', False):
            table.add_columns("File Name", "Size", "Status", "Completed At")
            
            history_tasks = [t for t in all_tasks if t.status in ("Completed", "Paused (Error)", "Paused (Failed)")]
            history_tasks.sort(key=lambda t: t.finished_time or 0, reverse=True)
            
            if not history_tasks:
                table.add_row("No caching history found.", "-", "-", "-")
                return
                
            for task in history_tasks[:15]:
                filename = truncate_path(task.file_path, 32)
                size_str = f"{task.size_gb:.2f} GB" if task.size_gb > 0 else "Unknown"
                
                status_style = "green" if task.status == "Completed" else "red"
                status_text = f"[{status_style}]{task.status}[/{status_style}]"
                
                finished_at = "-"
                if task.finished_time:
                    finished_at = datetime.fromtimestamp(task.finished_time).strftime('%H:%M:%S')
                    
                table.add_row(filename, size_str, status_text, finished_at, key=task.file_path)
        else:
            table.add_columns("File Name", "Size", "PID", "Status", "Elapsed")
            
            active_tasks = [t for t in all_tasks if t.status not in ("Completed", "Paused (Error)", "Paused (Failed)")]
            
            sorted_tasks = sorted(
                active_tasks,
                key=lambda t: (t.status in ("Caching", "Paused") or t.status.startswith("Paused"), t.priority_score if t.status == "Pending" else -9999, t.start_time or 0),
                reverse=True
            )
            
            if not sorted_tasks:
                table.add_row("No caching tasks in queue.", "-", "-", "Idle", "-")
                return
                
            for task in sorted_tasks[:10]:
                filename = truncate_path(task.file_path, 32)
                size_str = f"{task.size_gb:.2f} GB" if task.size_gb > 0 else "Unknown"
                
                if task.status == "Caching":
                    is_cached, cached_pct, cached_bytes, total_bytes = get_cache_status(
                        task.file_path,
                        self.config.rclone_cache_dir,
                        self.config.rclone_remote_name,
                        self.config.rclone_mount_dir or self.config.path_map_to
                    )
                    if is_cached:
                        status_text = "[green]Completed[/green]"
                        elapsed = "Finished"
                    else:
                        # Calculate caching speed and ETA
                        current_time = time.time()
                        if task.last_cached_bytes is not None and task.last_speed_check_time is not None:
                            elapsed_time = current_time - task.last_speed_check_time
                            if elapsed_time >= 0.5:
                                bytes_diff = cached_bytes - task.last_cached_bytes
                                if bytes_diff >= 0:
                                    instant_speed = bytes_diff / elapsed_time
                                    # Smooth speed updates using Exponential Moving Average (EMA)
                                    task.current_speed_bps = 0.7 * task.current_speed_bps + 0.3 * instant_speed
                                task.last_cached_bytes = cached_bytes
                                task.last_speed_check_time = current_time
                        else:
                            task.last_cached_bytes = cached_bytes
                            task.last_speed_check_time = current_time
                            task.current_speed_bps = 0.0

                        speed_mb = task.current_speed_bps / (1024 * 1024)
                        speed_str = f"{speed_mb:.1f} MB/s" if speed_mb > 0.05 else "0.0 MB/s"
                        
                        if task.current_speed_bps > 1024:
                            eta_secs = int((total_bytes - cached_bytes) / task.current_speed_bps)
                            if eta_secs > 3600:
                                eta_str = f"{eta_secs // 3600}h {(eta_secs % 3600) // 60}m"
                            elif eta_secs > 60:
                                eta_str = f"{eta_secs // 60}m {eta_secs % 60}s"
                            else:
                                eta_str = f"{eta_secs}s"
                        else:
                            eta_str = "--"

                        status_text = f"[yellow]Caching {cached_pct:.1f}%[/yellow] ({speed_str}, ETA: {eta_str})"
                        elapsed = f"{task.get_elapsed_time()}s"
                elif task.status.startswith("Paused") or task.status == "Paused":
                    task.last_cached_bytes = None
                    task.last_speed_check_time = None
                    task.current_speed_bps = 0.0
                    style = "red" if "Failed" in task.status or "Error" in task.status else "yellow"
                    disp_status = "Paused (buffering)" if task.status == "Paused" else task.status
                    status_text = f"[{style}]{disp_status}[/{style}]"
                    elapsed = f"{task.get_elapsed_time()}s" if task.start_time else "-"
                elif task.status == "Pending":
                    offset_val = getattr(task, 'offset', 1)
                    status_text = f"[cyan]Pending (+{offset_val} ep, {task.progress_pct:.0f}%)[/cyan]"
                    elapsed = "-"
                elif task.status == "Completed":
                    status_text = "[green]Completed[/green]"
                    elapsed = "Finished"
                else:
                    status_text = f"[red]{task.status}[/red]"
                    elapsed = "Finished"
                    
                table.add_row(filename, size_str, str(task.pid), status_text, elapsed, key=task.file_path)
            
        if saved_key:
            try:
                table.cursor_coordinate = Coordinate(table.get_row_index(saved_key), 0)
            except Exception:
                pass

    def action_toggle_history(self):
        self.show_history = not getattr(self, 'show_history', False)
        # Update table border title
        queue_table = self.query_one("#queue_table", HoverDataTable)
        if self.show_history:
            queue_table.border_title = "Completed Caching History"
        else:
            queue_table.border_title = "Rclone Cache Queue"
        logging.info(f"TUI: Toggled view to {'History' if self.show_history else 'Queue'}.")
        self.refresh_dashboard()

    def action_edit_config(self):
        def handle_config_edit_result(result):
            if result:
                self.refresh_dashboard()
        self.push_screen(ConfigEditScreen(self.config), handle_config_edit_result)

    def action_toggle_pause(self):
        self.is_paused = not getattr(self, 'is_paused', False)
        logging.info(f"TUI: Scanner {'PAUSED' if self.is_paused else 'RESUMED'}.")

    def action_force_scan(self):
        logging.info("TUI: Forcing immediate playback scan...")
        if self.scan_trigger_event:
            self._is_periodic_scan = False
            self.scan_trigger_event.set()

    def action_cancel_task(self):
        table = self.query_one("#queue_table", HoverDataTable)
        if table.cursor_row is not None:
            try:
                row_key = get_row_key(table, table.cursor_row)
                if row_key and row_key.value:
                    file_path = row_key.value
                    if getattr(self, 'view_only', False):
                        self.modify_task_status_external(file_path, delete=True)
                    else:
                        if self.queue_manager.cancel_task(file_path):
                            logging.info(f"TUI Queue Control: Canceled caching for '{os.path.basename(file_path)}'")
                            self.refresh_dashboard()
            except Exception as e:
                logging.debug(f"TUI Queue Control Error: {e}")

    def action_prioritize_task(self):
        table = self.query_one("#queue_table", HoverDataTable)
        if table.cursor_row is not None:
            try:
                row_key = get_row_key(table, table.cursor_row)
                if row_key and row_key.value:
                    file_path = row_key.value
                    if getattr(self, 'view_only', False):
                        self.modify_task_status_external(file_path, priority=True)
                    else:
                        if self.queue_manager.prioritize_task(file_path):
                            logging.info(f"TUI Queue Control: Prioritized '{os.path.basename(file_path)}' to the top of the queue.")
                            self.refresh_dashboard()
            except Exception as e:
                logging.debug(f"TUI Queue Control Error: {e}")

    def action_retry_task(self):
        table = self.query_one("#queue_table", HoverDataTable)
        if table.cursor_row is not None:
            try:
                row_key = get_row_key(table, table.cursor_row)
                if row_key and row_key.value:
                    file_path = row_key.value
                    if getattr(self, 'view_only', False):
                        self.modify_task_status_external(file_path, retry=True)
                    else:
                        if self.queue_manager.retry_task(file_path):
                            logging.info(f"TUI Queue Control: Reset and retried task '{os.path.basename(file_path)}'")
                            self.refresh_dashboard()
            except Exception as e:
                logging.debug(f"TUI Queue Control Error: {e}")

    def modify_task_status_external(self, file_path, delete=False, priority=False, retry=False):
        import json
        queue_file = self.config.queue_file
        if not os.path.exists(queue_file):
            return
        try:
            with open(queue_file, 'r') as f:
                queue_data = json.load(f)
                
            if file_path in queue_data:
                if delete:
                    del queue_data[file_path]
                elif priority:
                    queue_data[file_path]["priority_score"] = 999999
                elif retry:
                    queue_data[file_path]["status"] = "Pending"
                    queue_data[file_path]["retry_count"] = 0
                    queue_data[file_path]["finished_time"] = None
                    queue_data[file_path]["pid"] = "-"
                    
                with open(queue_file, 'w') as f:
                    json.dump(queue_data, f, indent=4)
                    
                action_name = "canceled" if delete else ("prioritized" if priority else "retried")
                filename = os.path.basename(file_path)
                log_file = self.config.log_file or "./config/bingesentry.log"
                if os.path.exists(log_file):
                    with open(log_file, 'a') as f:
                        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} INFO: External Control: {action_name.capitalize()} task '{filename}' via TUI View.\n")
                self.refresh_dashboard_view_only()
        except Exception as e:
            logging.error(f"External TUI Control: Failed to modify task: {e}")

    def tail_log_file(self):
        log_widget = self.query_one("#logs_panel", RichLog)
        log_file = self.config.log_file or "./config/bingesentry.log"
        if not os.path.exists(log_file):
            return
        try:
            with open(log_file, 'r') as f:
                if getattr(self, 'last_log_offset', 0) == 0:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 10240))
                    f.readline()
                else:
                    f.seek(self.last_log_offset)
                    
                lines = f.readlines()
                self.last_log_offset = f.tell()
                
                for line in lines:
                    log_widget.write(line.strip())
        except Exception:
            pass

    def refresh_dashboard_view_only(self):
        self.refresh_sessions_table_view_only()
        self.refresh_queue_table_view_only()

    def refresh_sessions_table_view_only(self):
        table = self.query_one("#sessions_table", HoverDataTable)
        table.clear(columns=True)
        table.add_columns("User", "Type", "Title / Playing Media", "Progress", "Next Cache Target")
        
        sessions_file = os.path.join(os.path.dirname(self.config.queue_file), 'sessions.json')
        if not os.path.exists(sessions_file):
            table.add_row("No active sessions on Plex.", "-", "-", "-", "-")
            return
            
        try:
            with open(sessions_file, 'r') as f:
                sessions_data = json.load(f)
        except Exception:
            sessions_data = []
            
        if not sessions_data:
            table.add_row("No active sessions on Plex.", "-", "-", "-", "-")
            return
            
        for s in sessions_data:
            table.add_row(
                s.get("user", "Unknown"),
                s.get("type", "TV Show"),
                s.get("title", ""),
                s.get("progress", ""),
                s.get("next_cache", "")
            )

    def refresh_queue_table_view_only(self):
        import json
        table = self.query_one("#queue_table", HoverDataTable)
        
        saved_key = None
        if table.cursor_row is not None:
            try:
                saved_key = get_row_key(table, table.cursor_row)
            except Exception:
                pass
                
        table.clear(columns=True)
        
        queue_file = self.config.queue_file
        if not os.path.exists(queue_file):
            table.add_row("No caching tasks in queue.", "-", "-", "Idle", "-")
            return
            
        try:
            with open(queue_file, 'r') as f:
                queue_data = json.load(f)
        except Exception:
            queue_data = {}
            
        all_tasks = list(queue_data.values())
        
        if self.show_history:
            table.add_columns("File Name", "Size", "Status", "Completed At")
            history_tasks = [t for t in all_tasks if t.get("status") in ("Completed", "Paused (Error)", "Paused (Failed)")]
            history_tasks.sort(key=lambda t: t.get("finished_time") or 0, reverse=True)
            
            if not history_tasks:
                table.add_row("No caching history found.", "-", "-", "-")
                return
                
            for task in history_tasks[:15]:
                filename = truncate_path(task.get("file_path", ""), 32)
                size_str = f"{task.get('size_gb', 0.0):.2f} GB" if task.get("size_gb", 0) > 0 else "Unknown"
                status_style = "green" if task.get("status") == "Completed" else "red"
                status_text = f"[{status_style}]{task.get('status')}[/{status_style}]"
                finished_at = "-"
                if task.get("finished_time"):
                    finished_at = datetime.fromtimestamp(task.get("finished_time")).strftime('%H:%M:%S')
                table.add_row(filename, size_str, status_text, finished_at, key=task.get("file_path"))
        else:
            table.add_columns("File Name", "Size", "PID", "Status", "Elapsed")
            active_tasks = [t for t in all_tasks if t.get("status") not in ("Completed", "Paused (Error)", "Paused (Failed)")]
            
            active_tasks.sort(
                key=lambda t: (t.get("status") in ("Caching", "Paused") or t.get("status", "").startswith("Paused"), t.get("priority_score", 0) if t.get("status") == "Pending" else -9999),
                reverse=True
            )
            
            if not active_tasks:
                table.add_row("No caching tasks in queue.", "-", "-", "Idle", "-")
                return
                
            for task in active_tasks[:10]:
                filename = truncate_path(task.get("file_path", ""), 32)
                size_str = f"{task.get('size_gb', 0.0):.2f} GB" if task.get("size_gb", 0) > 0 else "Unknown"
                pid_str = str(task.get("pid", "-"))
                
                status = task.get("status", "Pending")
                if status == "Caching":
                    status_text = "[yellow]Caching[/yellow]"
                elif "Paused" in status:
                    style = "red" if "Failed" in status or "Error" in status else "yellow"
                    status_text = f"[{style}]{status}[/{style}]"
                elif status == "Pending":
                    offset_val = task.get("offset", 1)
                    status_text = f"[cyan]Pending (+{offset_val} ep, {task.get('progress_pct', 0.0):.0f}%)[/cyan]"
                else:
                    status_text = status
                    
                elapsed = "-"
                table.add_row(filename, size_str, pid_str, status_text, elapsed, key=task.get("file_path"))
                
        if saved_key:
            try:
                table.cursor_coordinate = Coordinate(table.get_row_index(saved_key), 0)
            except Exception:
                pass

    def update_system_stats_view_only(self):
        uptime_secs = int(time.time() - self.start_time)
        hours, remainder = divmod(uptime_secs, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        
        cpu_usage = get_cpu_percent()
        mem_usage = psutil.virtual_memory().percent
        
        status_text = "[bold green](VIEW MODE)[/bold green]"
        
        # Get mount free space if possible
        mount_dir = self.config.rclone_mount_dir or self.config.path_map_to
        disk_str = ""
        if mount_dir and os.path.exists(mount_dir):
            try:
                from disk import has_enough_disk_space
                _, free_space_gb = has_enough_disk_space(mount_dir, 0.0)
                if free_space_gb > 0:
                    disk_str = f" | Disk: {free_space_gb:.1f} GB free"
            except Exception:
                pass
                
        # Get bandwidth stats
        saved_str = ""
        try:
            from disk import get_bandwidth_stats
            stats = get_bandwidth_stats()
            saved_gb = stats.get("total_saved_bytes", 0) / (1024 ** 3)
            if saved_gb > 0:
                saved_str = f" | Saved: {saved_gb:.1f} GB"
        except Exception:
            pass
            
        header = self.query_one("#header", Label)
        header.update(
            f"BingeSentry ➔ {self.plex_url} {status_text} | "
            f"CPU: {cpu_usage}% | MEM: {mem_usage}%{disk_str}{saved_str} | "
            f"Uptime: {uptime_str} | Time: {datetime.now().strftime('%H:%M:%S')}"
        )

    def run_loop(self, conn_manager, queue_manager, fetch_sessions_func, scan_trigger_event=None):
        self.conn_manager = conn_manager
        self.queue_manager = queue_manager
        self.fetch_sessions_func = fetch_sessions_func
        self.scan_trigger_event = scan_trigger_event
        
        # Start Textual Event Loop
        self.run()

if __name__ == "__main__":
    from config import Config
    config = Config()
    dashboard = TUIDashboard(config.plex_url, config, None, view_only=True)
    dashboard.run()
