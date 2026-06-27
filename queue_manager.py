import os
import signal
import psutil
import logging
import time
from rclone import start_cache_process, is_file_being_cached

class QueueTask:
    """
    Represents a caching task in the queue.
    """
    def __init__(self, file_path, size_gb, progress_pct, show_title, episode_info, offset=1):
        self.file_path = file_path
        self.size_gb = size_gb
        self.progress_pct = progress_pct
        self.show_title = show_title
        self.episode_info = episode_info
        self.status = "Pending"  # Options: Pending, Caching, Paused, Completed, Failed
        self.process = None
        self.start_time = None
        self.pid = "-"
        self.offset = offset
        self.priority_score = progress_pct - (offset * 100)
        self.retry_count = 0
        
    def get_elapsed_time(self):
        if self.start_time:
            return int(time.time() - self.start_time)
        return 0

class CachingQueueManager:
    """
    Manages the global sequential caching queue with priority sorting and buffering guards.
    """
    def __init__(self, config):
        self.config = config
        self.tasks = {}  # Keyed by file_path
        self.active_task_path = None
        self.is_suspended = False  # True if process is SIGSTOP'ed due to buffering guard
        
    def add_or_update_task(self, file_path, size_gb, progress_pct, show_title, episode_info, offset=1):
        """
        Adds a new caching task or updates playback progress for a pending task.
        """
        # Capacity guard check
        max_cache_gb = self.config.max_cache_total_gb
        if max_cache_gb > 0:
            total_active_size = sum(t.size_gb for t in self.tasks.values() if t.status in ("Caching", "Paused", "Pending"))
            if file_path not in self.tasks and (total_active_size + size_gb > max_cache_gb):
                logging.warning(
                    f"Queue Guard: Cache capacity limit reached! Adding '{os.path.basename(file_path)}' ({size_gb:.2f} GB) "
                    f"would exceed maximum limit of {max_cache_gb:.2f} GB (current: {total_active_size:.2f} GB). Skipping."
                )
                return

        if file_path in self.tasks:
            task = self.tasks[file_path]
            # Handle auto-retry for failed tasks
            if task.status in ("Failed", "Paused (Error)"):
                if getattr(task, 'retry_count', 0) < 3:
                    task.retry_count = getattr(task, 'retry_count', 0) + 1
                    logging.info(f"Queue: Retrying failed task '{os.path.basename(file_path)}' (Attempt {task.retry_count}/3) (Plex Progress: {progress_pct:.1f}%)")
                    task.status = "Pending"
                    task.progress_pct = progress_pct
                    task.priority_score = progress_pct - (offset * 100)
                    task.process = None
                    task.start_time = None
                    task.pid = "-"
                else:
                    logging.warning(f"Queue: Task '{os.path.basename(file_path)}' has failed too many times (3). Keeping paused.")
                    task.status = "Paused (Failed)"
            # Only update progress of pending items to dynamically adjust sorting
            elif task.status == "Pending":
                task.progress_pct = progress_pct
                task.priority_score = progress_pct - (offset * 100)
        else:
            self.tasks[file_path] = QueueTask(file_path, size_gb, progress_pct, show_title, episode_info, offset)
            logging.info(f"Queue: Added task '{os.path.basename(file_path)}' (Plex Progress: {progress_pct:.1f}%, Offset: {offset})")

    def update_task_statuses(self):
        """
        Monitors active running tasks to detect process completions or failures.
        """
        if not self.active_task_path:
            return
            
        task = self.tasks[self.active_task_path]
        if task.process:
            poll = task.process.poll()
            if poll is not None:
                # Process exited
                if poll == 0:
                    task.status = "Completed"
                    logging.info(f"Queue: Successfully cached '{os.path.basename(task.file_path)}'")
                else:
                    task.status = "Paused (Error)"
                    logging.warning(f"Queue: Caching process failed for '{os.path.basename(task.file_path)}' with exit code {poll}")
                
                self.active_task_path = None
                task.process = None
                task.pid = "-"
                self.is_suspended = False
            else:
                # Double-check zombie states
                try:
                    proc = psutil.Process(task.process.pid)
                    if proc.status() == psutil.STATUS_ZOMBIE:
                        task.status = "Completed"
                        self.active_task_path = None
                        task.process = None
                        task.pid = "-"
                        self.is_suspended = False
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    task.status = "Completed"
                    self.active_task_path = None
                    task.process = None
                    task.pid = "-"
                    self.is_suspended = False

    def handle_buffering_guard(self, any_user_buffering):
        """
        Sends SIGSTOP to pause downloads if Plex users are buffering,
        and SIGCONT to resume once buffering is cleared.
        """
        if not self.active_task_path:
            return
            
        task = self.tasks[self.active_task_path]
        if not task.process:
            return
            
        if any_user_buffering and not self.is_suspended:
            # Suspend the process immediately
            try:
                os.kill(task.process.pid, signal.SIGSTOP)
                self.is_suspended = True
                task.status = "Paused"
                logging.warning(f"Buffering Guard: Buffering detected on Plex! Suspending caching process PID {task.process.pid} for '{os.path.basename(task.file_path)}'.")
            except Exception as e:
                logging.error(f"Buffering Guard: Failed to suspend process PID {task.process.pid}: {e}")
                
        elif not any_user_buffering and self.is_suspended:
            # Resume the process
            try:
                os.kill(task.process.pid, signal.SIGCONT)
                self.is_suspended = False
                task.status = "Caching"
                logging.info(f"Buffering Guard: Buffering resolved. Resuming caching process PID {task.process.pid} for '{os.path.basename(task.file_path)}'.")
            except Exception as e:
                logging.error(f"Buffering Guard: Failed to resume process PID {task.process.pid}: {e}")

    def process_queue(self):
        """
        Schedules next task sequentially based on episode progress percentages.
        """
        self.update_task_statuses()
        
        # Concurrency limit = 1: if there is an active/paused task, wait for it
        if self.active_task_path:
            return
            
        pending_tasks = [t for t in self.tasks.values() if t.status == "Pending"]
        if not pending_tasks:
            return
            
        # Priority Sorting: sort by calculated priority score (prioritizing immediate next episodes, then closest to finishing)
        pending_tasks.sort(key=lambda t: t.priority_score, reverse=True)
        next_task = pending_tasks[0]
        
        # Check if another process is handling it already
        if is_file_being_cached(next_task.file_path):
            next_task.status = "Completed"
            logging.info(f"Queue: Skipping '{os.path.basename(next_task.file_path)}', already caching externally.")
            return
            
        # Launch task
        next_task.status = "Caching"
        next_task.start_time = time.time()
        self.active_task_path = next_task.file_path
        
        logging.info(f"Queue: Launching cache for '{os.path.basename(next_task.file_path)}' (Plex progress: {next_task.progress_pct:.1f}%)")
        next_task.process = start_cache_process(self.config.cache_command, next_task.file_path)
        if next_task.process:
            next_task.pid = next_task.process.pid

    def cleanup(self):
        """
        Cleans up any running/paused caching processes on shutdown.
        """
        if self.active_task_path:
            task = self.tasks[self.active_task_path]
            if task.process:
                try:
                    if self.is_suspended:
                        os.kill(task.process.pid, signal.SIGCONT)
                    task.process.terminate()
                    task.process.wait(timeout=2)
                    logging.info(f"Queue: Cleaned up cache process PID {task.process.pid} on shutdown.")
                except Exception:
                    pass
