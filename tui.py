import os
import time
import logging
import psutil
import sys
import select
import termios
import tty
from datetime import datetime
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.console import Console
from rich.text import Text
from rich.box import ROUNDED
from disk import get_cache_status, is_mount_responsive

class KeyListener:
    """
    Non-blocking keyboard listener using select and termios.
    Works natively on UNIX-based systems.
    """
    def __init__(self):
        self.old_settings = None
        self.active = False
        
    def start(self):
        try:
            self.old_settings = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
            self.active = True
        except Exception:
            self.active = False
            
    def stop(self):
        if self.active and self.old_settings:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.old_settings)
            self.active = False
            
    def get_key(self):
        if not self.active:
            return None
        if select.select([sys.stdin], [], [], 0)[0]:
            return sys.stdin.read(1)
        return None




class MemoryLogHandler(logging.Handler):
    """
    In-memory logging handler to store the latest logs for real-time display in the TUI.
    """
    def __init__(self, capacity=10):
        super().__init__()
        self.capacity = capacity
        self.logs = []
        
    def emit(self, record):
        log_entry = self.format(record)
        self.logs.append(log_entry)
        if len(self.logs) > self.capacity:
            self.logs.pop(0)
            
    def get_logs(self):
        return "\n".join(self.logs)

class CachingTask:
    """
    Represents an active or completed caching process.
    """
    def __init__(self, file_path, size_gb, pid, cmd):
        self.file_path = file_path
        self.size_gb = size_gb
        self.pid = pid
        self.cmd = cmd
        self.start_time = time.time()
        self.status = "Caching"  # Options: Caching, Completed, Failed
        
    def get_elapsed_time(self):
        return int(time.time() - self.start_time)

class TUIDashboard:
    """
    Manages the Rich Live Terminal User Interface.
    """
    def __init__(self, plex_url, config, log_handler):
        self.plex_url = plex_url
        self.config = config
        self.log_handler = log_handler
        self.console = Console()
        self.start_time = time.time()
        
        # In-memory registry of caching tasks
        # keyed by file_path
        self.caching_tasks = {}
        
        # Last fetched session data
        self.active_sessions = []
        self.mount_healthy = True
        self.selected_task_index = 0
        
        self.layout = self._create_layout()
        
    def _create_layout(self) -> Layout:
        """
        Builds the 3-section layout: Header, Body (Split), and Footer (Logs).
        """
        layout = Layout()
        
        # Main split
        layout.split(
            Layout(name="header", size=4),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=12)
        )
        
        # Body split
        layout["body"].split_row(
            Layout(name="sessions", ratio=3),
            Layout(name="cache_queue", ratio=2)
        )
        
        return layout
        
    def add_cache_task(self, file_path, size_gb, pid, cmd):
        """
        Registers a new active caching task.
        """
        self.caching_tasks[file_path] = CachingTask(file_path, size_gb, pid, cmd)
        
    def update_task_statuses(self):
        """
        Scans registered tasks to see if their background process PIDs are still active.
        """
        for task in self.caching_tasks.values():
            if task.status == "Caching":
                try:
                    proc = psutil.Process(task.pid)
                    # Check if it has completed or turned into a zombie
                    if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                        task.status = "Completed"
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    # If process doesn't exist, assume completed
                    task.status = "Completed"
                    
    def update_sessions(self, sessions):
        """
        Updates the internal cache of active Plex sessions.
        """
        self.active_sessions = sessions
        
    def _render_header(self, is_paused=False) -> Panel:
        """
        Renders the TUI top banner.
        """
        uptime_secs = int(time.time() - self.start_time)
        hours, remainder = divmod(uptime_secs, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        
        # Gather quick system stats
        cpu_usage = psutil.cpu_percent()
        mem_usage = psutil.virtual_memory().percent
        
        grid = Table.grid(expand=True)
        grid.add_column(justify="left", ratio=2)
        grid.add_column(justify="center", ratio=1)
        grid.add_column(justify="right", ratio=2)
        
        status_text = "[bold yellow](PAUSED)[/bold yellow]" if is_paused else "[bold green](ONLINE)[/bold green]"
        if not getattr(self, 'mount_healthy', True):
            status_text = "[bold blink red](MOUNT OFFLINE)[/bold blink red]"
        elif not getattr(self, 'conn_manager', None) or not self.conn_manager.plex:
            status_text = "[bold blink red](PLEX OFFLINE)[/bold blink red]"
        grid.add_row(
            Text.from_markup(f"[bold magenta]BingeSentry[/bold magenta] ➔ [cyan]{self.plex_url}[/cyan] {status_text}"),
            Text.from_markup(f"[bold white]CPU:[/bold white] [yellow]{cpu_usage}%[/yellow] | [bold white]MEM:[/bold white] [yellow]{mem_usage}%[/yellow]"),
            Text.from_markup(f"[bold white]Uptime:[/bold white] [cyan]{uptime_str}[/cyan] | [bold white]Time:[/bold white] {datetime.now().strftime('%H:%M:%S')}")
        )
        
        return Panel(
            grid,
            box=ROUNDED,
            title="[bold yellow]System Status[/bold yellow]",
            border_style="magenta"
        )
        
    def _render_sessions(self, queue_manager) -> Panel:
        """
        Renders the active Plex session table.
        """
        table = Table(
            expand=True,
            box=ROUNDED,
            border_style="cyan"
        )
        
        table.add_column("User", style="cyan", width=15)
        table.add_column("Type", style="yellow", width=8)
        table.add_column("Title / Playing Media", style="white", ratio=3)
        table.add_column("Progress", style="magenta", width=12)
        table.add_column("Next Cache Target", style="green", ratio=3)
        
        if not self.active_sessions:
            table.add_row(
                Text("No sessions", style="dim italic"),
                "-",
                Text("No active playback sessions on Plex.", style="dim italic"),
                "-",
                "-"
            )
        else:
            for session in self.active_sessions:
                user = session.usernames[0] if session.usernames else "Unknown"
                
                # Check media type
                if session.type == "episode":
                    show = session.grandparentTitle
                    season_num = session.parentIndex
                    episode_num = session.index
                    title = f"{show} (S{season_num}E{episode_num})"
                    
                    # Try to fetch current playback offset/percentage
                    duration = getattr(session, 'duration', 0)
                    view_offset = getattr(session, 'viewOffset', 0)
                    pct = int((view_offset / duration) * 100) if duration > 0 else 0
                    progress = f"{pct}%" if duration > 0 else "Playing"
                        
                    # Find cached target
                    next_cache = "Resolving next..."
                    # Find if we cached or are caching any files for this show
                    # We can lookup by looking at what we mapped
                    # For TUI we will calculate this dynamically in the refresh cycle
                    if hasattr(session, '_next_cache_file'):
                        file_path = session._next_cache_file
                        filename = os.path.basename(file_path)
                        
                        if getattr(session, '_cache_threshold_waiting', False):
                            thresh = getattr(session, '_cache_threshold_val', 0)
                            next_cache = f"{filename} [bold yellow](deferred: {pct}%/{thresh}%)[/bold yellow]"
                        else:
                            # Get cache status
                            is_cached, cached_pct, _, _ = get_cache_status(
                                file_path,
                                self.config.rclone_cache_dir,
                                self.config.rclone_remote_name,
                                self.config.rclone_mount_dir or self.config.path_map_to
                            )
                            
                            if is_cached:
                                next_cache = f"{filename} [bold green](100%)[/bold green]"
                            elif cached_pct > 0:
                                next_cache = f"{filename} [bold yellow]({cached_pct:.1f}%)[/bold yellow]"
                            else:
                                if file_path in queue_manager.tasks:
                                    task = queue_manager.tasks[file_path]
                                    if task.status == "Pending":
                                        next_cache = f"{filename} [dim cyan](queued)[/dim cyan]"
                                    elif "Paused" in task.status or task.status == "Paused":
                                        reason = task.status.replace("Paused", "").strip(" ()")
                                        reason = reason if reason else "buffering"
                                        style = "red" if "Failed" in task.status else "yellow"
                                        next_cache = f"{filename} [bold {style}](paused: {reason})[/bold {style}]"
                                    else:
                                        next_cache = f"{filename} [bold green](caching...)[/bold green]"
                                else:
                                    next_cache = f"{filename} [red](not cached)[/red]"
                    else:
                        next_cache = "None / Finished"
                        
                    table.add_row(user, "TV Show", title, progress, next_cache)
                else:
                    movie_title = getattr(session, 'title', 'Unknown Movie')
                    progress = "Playing"
                    duration = getattr(session, 'duration', 0)
                    view_offset = getattr(session, 'viewOffset', 0)
                    if duration > 0:
                        pct = int((view_offset / duration) * 100)
                        progress = f"{pct}%"
                    table.add_row(user, "Movie", movie_title, progress, "[dim]- (No cache required)[/dim]")
                    
        return Panel(
            table,
            box=ROUNDED,
            title="[bold cyan]Active Plex Playback Sessions[/bold cyan]",
            border_style="cyan"
        )
        
    def _render_cache_queue(self, queue_manager) -> Panel:
        """
        Renders the sequential caching queue.
        """
        table = Table(
            expand=True,
            box=ROUNDED,
            border_style="green"
        )
        
        table.add_column("File Name", style="white", ratio=3)
        table.add_column("Size", style="yellow", width=8)
        table.add_column("PID", style="magenta", width=8)
        table.add_column("Status", width=18)
        table.add_column("Elapsed", style="cyan", width=10)
        
        # Sort queue manager tasks: Caching/Paused first, then pending by priority score descending
        sorted_tasks = sorted(
            queue_manager.tasks.values(),
            key=lambda t: (t.status in ("Caching", "Paused"), t.priority_score if t.status == "Pending" else -9999, t.start_time or 0),
            reverse=True
        )
        
        if not sorted_tasks:
            table.add_row(
                Text("No caching tasks", style="dim italic"),
                "-",
                "-",
                Text("Idle", style="dim green"),
                "-"
            )
        else:
            # Clamp selection index dynamically to prevent list errors
            self.selected_task_index = max(0, min(self.selected_task_index, len(sorted_tasks) - 1))
            
            for idx, task in enumerate(sorted_tasks[:10]):
                filename = os.path.basename(task.file_path)
                size_str = f"{task.size_gb:.2f} GB" if task.size_gb > 0 else "Unknown"
                
                is_selected = (idx == self.selected_task_index)
                prefix = "➔ " if is_selected else "  "
                filename_text = Text(f"{prefix}{filename}", style="bold cyan" if is_selected else "white")
                
                if task.status == "Caching":
                    is_cached, cached_pct, cached_bytes, total_bytes = get_cache_status(
                        task.file_path,
                        self.config.rclone_cache_dir,
                        self.config.rclone_remote_name,
                        self.config.rclone_mount_dir or self.config.path_map_to
                    )
                    if is_cached:
                        task.status = "Completed"
                        status_text = Text("Completed", style="bold green")
                        elapsed = "Finished"
                    else:
                        cached_gb = cached_bytes / (1024 ** 3)
                        total_gb = total_bytes / (1024 ** 3) if total_bytes > 0 else task.size_gb
                        status_text = Text(f"Caching ({cached_gb:.2f} GB / {total_gb:.2f} GB {cached_pct:.1f}%)", style="bold blink yellow" if cached_pct > 0 else "bold blink green")
                        elapsed = f"{task.get_elapsed_time()}s"
                elif task.status.startswith("Paused") or task.status == "Paused":
                    style = "bold red" if "Failed" in task.status else "bold yellow"
                    # Handle legacy Paused status representation
                    disp_status = "Paused (buffering)" if task.status == "Paused" else task.status
                    status_text = Text(disp_status, style=style)
                    elapsed = f"{task.get_elapsed_time()}s" if task.start_time else "-"
                elif task.status == "Pending":
                    offset_val = getattr(task, 'offset', 1)
                    status_text = Text(f"Pending (+{offset_val} ep, {task.progress_pct:.0f}%)", style="dim cyan")
                    elapsed = "-"
                elif task.status == "Completed":
                    status_text = Text("Completed", style="bold green")
                    elapsed = "Finished"
                else:
                    status_text = Text(task.status, style="bold red")
                    elapsed = "Finished"
                    
                table.add_row(filename_text, size_str, str(task.pid), status_text, elapsed)
                
        return Panel(
            table,
            box=ROUNDED,
            title="[bold green]Rclone Cache Queue (Recent 10 Tasks)[/bold green]",
            border_style="green"
        )
        
    def _render_logs(self) -> Panel:
        """
        Renders scrollable log buffer.
        """
        log_text = self.log_handler.get_logs()
        controls_text = (
            "\n[bold yellow]Global Controls:[/bold yellow] [bold white]Q[/bold white] Quit | [bold white]P[/bold white] Pause/Resume Scanner | [bold white]F[/bold white] Force Scan"
            "\n[bold yellow]Queue Controls:[/bold yellow] [bold white]W/S[/bold white] or [bold white]K/J[/bold white] Navigate | [bold white]U[/bold white] Bump Priority | [bold white]D[/bold white] Cancel Task | [bold white]R[/bold white] Retry Task"
        )
        return Panel(
            f"{log_text}\n{controls_text}",
            box=ROUNDED,
            title="[bold white]Live Application Logs[/bold white]",
            border_style="white"
        )
        
    def update_view(self, queue_manager, is_paused=False):
        """
        Updates the TUI widgets dynamically.
        """
        # status updates are handled by queue manager
        self.layout["header"].update(self._render_header(is_paused))
        self.layout["sessions"].update(self._render_sessions(queue_manager))
        self.layout["cache_queue"].update(self._render_cache_queue(queue_manager))
        self.layout["footer"].update(self._render_logs())
        
    def run_loop(self, conn_manager, queue_manager, fetch_sessions_func, scan_trigger_event=None):
        """
        Runs the main loop under Rich Live control.
        """
        self.conn_manager = conn_manager
        self.console.clear()
        
        is_paused = False
        initial_fetch = True
        
        # Start the non-blocking keyboard listener
        listener = KeyListener()
        listener.start()
        
        with Live(self.layout, console=self.console, screen=True, refresh_per_second=2) as live:
            try:
                while True:
                    current_time = time.time()
                    
                    # Fetch sorted tasks to allow navigation and selected operations
                    sorted_tasks = sorted(
                        queue_manager.tasks.values(),
                        key=lambda t: (t.status in ("Caching", "Paused"), t.priority_score if t.status == "Pending" else -9999, t.start_time or 0),
                        reverse=True
                    )
                    
                    # Non-blocking read of keyboard inputs
                    key = listener.get_key()
                    if key:
                        key = key.lower()
                        if key == 'q':
                            break
                        elif key == 'p':
                            is_paused = not is_paused
                            logging.info(f"TUI: Scanner {'PAUSED' if is_paused else 'RESUMED'}.")
                        elif key == 'f':
                            logging.info("TUI: Forcing immediate playback scan...")
                            if scan_trigger_event:
                                scan_trigger_event.set()
                        elif key in ('k', 'w'):
                            if sorted_tasks:
                                self.selected_task_index = max(0, self.selected_task_index - 1)
                        elif key in ('j', 's'):
                            if sorted_tasks:
                                self.selected_task_index = min(len(sorted_tasks) - 1, self.selected_task_index + 1)
                        elif key == 'd':
                            if sorted_tasks and 0 <= self.selected_task_index < len(sorted_tasks):
                                target_task = sorted_tasks[self.selected_task_index]
                                if target_task.process:
                                    try:
                                        target_task.process.terminate()
                                        target_task.process.wait(timeout=1)
                                    except Exception:
                                        pass
                                if queue_manager.active_task_path == target_task.file_path:
                                    queue_manager.active_task_path = None
                                    queue_manager.is_suspended = False
                                if target_task.file_path in queue_manager.tasks:
                                    del queue_manager.tasks[target_task.file_path]
                                logging.info(f"TUI Queue Control: Canceled caching for '{os.path.basename(target_task.file_path)}'")
                                self.selected_task_index = max(0, self.selected_task_index - 1)
                        elif key == 'u':
                            if sorted_tasks and 0 <= self.selected_task_index < len(sorted_tasks):
                                target_task = sorted_tasks[self.selected_task_index]
                                if target_task.status == "Pending":
                                    target_task.priority_score = 999999
                                    logging.info(f"TUI Queue Control: Prioritized '{os.path.basename(target_task.file_path)}' to the top of the queue.")
                        elif key == 'r':
                            if sorted_tasks and 0 <= self.selected_task_index < len(sorted_tasks):
                                target_task = sorted_tasks[self.selected_task_index]
                                target_task.status = "Pending"
                                target_task.retry_count = 0
                                target_task.process = None
                                target_task.start_time = None
                                target_task.pid = "-"
                                logging.info(f"TUI Queue Control: Reset and retried task '{os.path.basename(target_task.file_path)}'")
                    
                    # Check for event triggers (WebSocket Alerts)
                    event_triggered = False
                    if scan_trigger_event and scan_trigger_event.is_set():
                        event_triggered = True
                        scan_trigger_event.clear()
                        logging.info("TUI: WebSocket trigger event received. Fetching sessions immediately...")
                        
                    # Plex API Call (only if scanner is not paused, triggered by WebSocket events)
                    should_poll = False
                    if not is_paused:
                        if event_triggered or initial_fetch:
                            should_poll = True
                            initial_fetch = False
                            
                    if should_poll:
                        try:
                            # Verify mount health first
                            mount_dir = self.config.rclone_mount_dir or self.config.path_map_to
                            self.mount_healthy = is_mount_responsive(mount_dir) if mount_dir else True
                            
                            if self.mount_healthy:
                                # Execute standard caching flow (updates sessions inside the callback)
                                fetch_sessions_func(self.conn_manager, self.config, queue_manager, self)
                            else:
                                logging.error(f"Mount Health Guard: Mount directory '{mount_dir}' is unresponsive or disconnected! Suspending caching queue.")
                                queue_manager.handle_buffering_guard(any_user_buffering=True)
                        except Exception as e:
                            logging.error(f"Error querying Plex sessions in TUI: {e}")
                        
                    # Update screen
                    self.update_view(queue_manager, is_paused)
                    # Use smaller sleep interval for snappy keyboard input detection
                    time.sleep(0.1)
            except KeyboardInterrupt:
                pass
            finally:
                listener.stop()
                self.console.clear()
                self.console.print("[bold yellow]TUI Dashboard shut down. Good bye![/bold yellow]")
