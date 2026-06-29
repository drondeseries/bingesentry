import os
import json
import signal
import psutil
import logging
import time
import threading
import functools
from rclone import start_cache_process, is_file_being_cached
from disk import get_cpu_percent

def synchronized(method):
    """
    Decorator that acquires the CachingQueueManager reentrant lock
    before calling the decorated method.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapper
class AdoptedProcess:
    """
    Wraps an already running process so the queue manager can monitor and control it
    just like a subprocess.Popen object.
    """
    def __init__(self, pid):
        self.pid = pid
        try:
            self._proc = psutil.Process(pid)
        except Exception:
            self._proc = None

    def poll(self):
        if not self._proc:
            return 0  # If process doesn't exist, assume it finished
        try:
            if not self._proc.is_running() or self._proc.status() == psutil.STATUS_ZOMBIE:
                return 0
            return None  # Still running
        except Exception:
            return 0

    def terminate(self):
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def wait(self, timeout=None):
        if self._proc:
            try:
                self._proc.wait(timeout=timeout)
            except Exception:
                pass


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
        self.last_cached_bytes = None
        self.last_speed_check_time = None
        self.current_speed_bps = 0.0
        self.finished_time = None
        
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
        self.lock = threading.RLock()
        self.tasks = {}  # Keyed by file_path
        self.active_task_path = None
        self.is_suspended = False  # True if process is SIGSTOP'ed due to buffering guard
        with self.lock:
            self.load_tasks()

    def get_all_tasks(self):
        """
        Thread-safe getter returning a snapshot list of current tasks.
        """
        with self.lock:
            return list(self.tasks.values())

    def get_task(self, file_path):
        """
        Thread-safe getter returning a single task or None.
        """
        with self.lock:
            return self.tasks.get(file_path)

    @synchronized
    def save_tasks(self):
        """
        Saves current tasks (excluding process objects) to a JSON file.
        """
        try:
            data = {}
            for path, task in self.tasks.items():
                data[path] = {
                    "file_path": task.file_path,
                    "size_gb": task.size_gb,
                    "progress_pct": task.progress_pct,
                    "show_title": task.show_title,
                    "episode_info": task.episode_info,
                    "status": task.status if task.status != "Caching" else "Pending",
                    "offset": task.offset,
                    "priority_score": task.priority_score,
                    "retry_count": task.retry_count,
                    "finished_time": getattr(task, "finished_time", None)
                }
            
            queue_file = self.config.queue_file
            os.makedirs(os.path.dirname(os.path.abspath(queue_file)), exist_ok=True)
            
            with open(queue_file, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logging.error(f"Queue Persistence: Failed to save queue: {e}")

    @synchronized
    def load_tasks(self):
        """
        Loads saved tasks from the JSON file.
        """
        try:
            queue_file = self.config.queue_file
            if not os.path.exists(queue_file):
                return
                
            with open(queue_file, 'r') as f:
                data = json.load(f)
                
            for path, tdata in data.items():
                task = QueueTask(
                    tdata["file_path"],
                    tdata["size_gb"],
                    tdata["progress_pct"],
                    tdata["show_title"],
                    tdata["episode_info"],
                    tdata["offset"]
                )
                task.status = tdata["status"]
                task.priority_score = tdata["priority_score"]
                task.retry_count = tdata["retry_count"]
                task.finished_time = tdata.get("finished_time")
                self.tasks[path] = task
            logging.info(f"Queue Persistence: Loaded {len(self.tasks)} task(s) from persistence file.")
        except Exception as e:
            logging.error(f"Queue Persistence: Failed to load queue: {e}")

    @synchronized
    def cleanup_completed_tasks(self, max_age_seconds=604800, max_history_count=50):
        """
        Prunes completed or failed tasks from history. Retains up to max_history_count
        of the most recent tasks for up to max_age_seconds (7 days by default).
        """
        finished_tasks = [
            task for task in self.tasks.values()
            if task.status in ("Completed", "Paused (Error)", "Paused (Failed)")
        ]
        
        # Sort by finished time, newest first
        finished_tasks.sort(key=lambda t: t.finished_time or 0, reverse=True)
        
        current_time = time.time()
        paths_to_prune = []
        for i, task in enumerate(finished_tasks):
            finished_at = task.finished_time or 0
            if i >= max_history_count or (current_time - finished_at) > max_age_seconds:
                paths_to_prune.append(task.file_path)
                
        if paths_to_prune:
            for path in paths_to_prune:
                if path in self.tasks:
                    del self.tasks[path]
            logging.info(f"Queue: Pruned {len(paths_to_prune)} old finished task(s) from history.")
            self.save_tasks()

    @synchronized
    def cancel_task(self, file_path):
        if file_path in self.tasks:
            task = self.tasks[file_path]
            if task.process:
                try:
                    task.process.terminate()
                    task.process.wait(timeout=1)
                except Exception:
                    pass
            del self.tasks[file_path]
            self.save_tasks()
            return True
        return False

    @synchronized
    def prioritize_task(self, file_path):
        if file_path in self.tasks:
            task = self.tasks[file_path]
            if task.status == "Pending":
                task.priority_score = 999999
                self.save_tasks()
                return True
        return False

    @synchronized
    def retry_task(self, file_path):
        if file_path in self.tasks:
            task = self.tasks[file_path]
            task.status = "Pending"
            task.retry_count = 0
            task.process = None
            task.start_time = None
            task.pid = "-"
            task.last_cached_bytes = None
            task.last_speed_check_time = None
            task.current_speed_bps = 0.0
            task.finished_time = None
            self.save_tasks()
            return True
        return False

    @synchronized
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
            # If the task was previously completed, but is requested again,
            # it means the file is no longer fully cached (e.g. evicted). Reset to Pending.
            if task.status == "Completed":
                logging.info(f"Queue: Re-adding previously completed task '{os.path.basename(file_path)}' (likely evicted or incomplete on disk)")
                task.status = "Pending"
                task.progress_pct = progress_pct
                task.priority_score = progress_pct - (offset * 100)
                task.process = None
                task.start_time = None
                task.pid = "-"
                task.finished_time = None
                self.save_tasks()
            # Handle auto-retry for failed tasks
            elif task.status in ("Failed", "Paused (Error)"):
                if getattr(task, 'retry_count', 0) < 3:
                    task.retry_count = getattr(task, 'retry_count', 0) + 1
                    logging.info(f"Queue: Retrying failed task '{os.path.basename(file_path)}' (Attempt {task.retry_count}/3) (Plex Progress: {progress_pct:.1f}%)")
                    task.status = "Pending"
                    task.progress_pct = progress_pct
                    task.priority_score = progress_pct - (offset * 100)
                    task.process = None
                    task.start_time = None
                    task.pid = "-"
                    task.finished_time = None
                else:
                    logging.warning(f"Queue: Task '{os.path.basename(file_path)}' has failed too many times (3). Keeping paused.")
                    task.status = "Paused (Failed)"
                    task.finished_time = time.time()
                self.save_tasks()
            # Only update progress of pending items if the new progress is higher,
            # to dynamically adjust sorting based on the user furthest along
            elif task.status == "Pending":
                if progress_pct > task.progress_pct:
                    task.progress_pct = progress_pct
                    task.priority_score = progress_pct - (offset * 100)
                    self.save_tasks()
        else:
            self.tasks[file_path] = QueueTask(file_path, size_gb, progress_pct, show_title, episode_info, offset)
            logging.info(f"Queue: Added task '{os.path.basename(file_path)}' (Plex Progress: {progress_pct:.1f}%, Offset: {offset})")
            self.save_tasks()

    @synchronized
    def update_task_statuses(self):
        """
        Monitors active running tasks to detect process completions or failures,
        and checks if active tasks have finished caching on disk early.
        """
        from disk import get_cache_status
        status_changed = False
        
        # 1. Early cache check: check if any active tasks are 100% cached on disk early
        active_tasks = [task for task in self.tasks.values() if task.status in ("Caching", "Paused") or "Paused" in task.status]
        for task in active_tasks:
            is_cached, _, _, _ = get_cache_status(
                task.file_path,
                self.config.rclone_cache_dir,
                self.config.rclone_remote_name,
                self.config.rclone_mount_dir or self.config.path_map_to
            )
            if is_cached:
                task.status = "Completed"
                task.finished_time = time.time()
                logging.info(f"Queue: Detected '{os.path.basename(task.file_path)}' is 100% cached on disk.")
                if task.process:
                    try:
                        task.process.terminate()
                    except Exception:
                        pass
                task.process = None
                task.pid = "-"
                status_changed = True

        # 2. Process check: monitor active running tasks
        active_paths = [path for path, task in self.tasks.items() if task.process is not None]
        for path in active_paths:
            task = self.tasks[path]
            poll = task.process.poll()
            if poll is not None:
                # Process exited
                if poll == 0:
                    task.status = "Completed"
                    task.finished_time = time.time()
                    logging.info(f"Queue: Successfully cached '{os.path.basename(task.file_path)}'")
                    status_changed = True
                elif poll == 2:
                    # Exit code 2 = file does not exist on local mount yet (still downloading)
                    # Remove from queue so it gets re-added naturally on next poll cycle
                    logging.info(
                        f"Queue: File not yet available locally for '{os.path.basename(task.file_path)}'. "
                        f"Removing from queue; will retry when file appears."
                    )
                    del self.tasks[path]
                    status_changed = True
                    continue
                else:
                    task.status = "Paused (Error)"
                    task.finished_time = time.time()
                    logging.warning(f"Queue: Caching process failed for '{os.path.basename(task.file_path)}' with exit code {poll}")
                    status_changed = True
                
                task.process = None
                task.pid = "-"
            else:
                # Double-check zombie states
                try:
                    proc = psutil.Process(task.process.pid)
                    if proc.status() == psutil.STATUS_ZOMBIE:
                        task.status = "Completed"
                        task.finished_time = time.time()
                        task.process = None
                        task.pid = "-"
                        status_changed = True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    task.status = "Completed"
                    task.finished_time = time.time()
                    task.process = None
                    task.pid = "-"
                    status_changed = True
                    
        if status_changed:
            self.save_tasks()

    @synchronized
    def handle_buffering_guard(self, any_user_buffering):
        """
        Sends SIGSTOP to pause downloads if Plex users are buffering or system limits are exceeded,
        and SIGCONT to resume once limits/buffering are cleared.
        """
        cpu_limit = self.config.max_cpu_percent_limit
        mem_limit = self.config.max_mem_percent_limit
        system_overloaded = False
        overload_reason = ""
        
        if cpu_limit > 0:
            cpu_usage = get_cpu_percent()
            if cpu_usage > cpu_limit:
                system_overloaded = True
                overload_reason = f"CPU usage ({cpu_usage:.1f}%) exceeds limit ({cpu_limit:.1f}%)"
                
        if mem_limit > 0:
            mem_usage = psutil.virtual_memory().percent
            if mem_usage > mem_limit:
                system_overloaded = True
                overload_reason = f"Memory usage ({mem_usage:.1f}%) exceeds limit ({mem_limit:.1f}%)"

        should_suspend = any_user_buffering or system_overloaded
        
        active_tasks = [t for t in self.tasks.values() if t.process is not None]
        if not active_tasks:
            return
            
        status_changed = False
        for task in active_tasks:
            if should_suspend:
                if task.status == "Caching":
                    if task.process.poll() is not None:
                        continue
                    try:
                        os.kill(task.process.pid, signal.SIGSTOP)
                        reason = "buffering" if any_user_buffering else "overload"
                        task.status = f"Paused ({reason})"
                        if any_user_buffering:
                            logging.warning(f"Buffering Guard: Buffering detected on Plex! Suspending caching process PID {task.process.pid} for '{os.path.basename(task.file_path)}'.")
                        else:
                            logging.warning(f"System Guard: {overload_reason}! Suspending caching process PID {task.process.pid} for '{os.path.basename(task.file_path)}'.")
                        status_changed = True
                    except Exception as e:
                        logging.error(f"Buffering/System Guard: Failed to suspend process PID {task.process.pid}: {e}")
            else:
                if task.status in ("Paused", "Paused (buffering)", "Paused (overload)"):
                    try:
                        os.kill(task.process.pid, signal.SIGCONT)
                        task.status = "Caching"
                        logging.info(f"Buffering/System Guard: Limits cleared. Resuming caching process PID {task.process.pid} for '{os.path.basename(task.file_path)}'.")
                        status_changed = True
                    except Exception as e:
                        logging.error(f"Buffering/System Guard: Failed to resume process PID {task.process.pid}: {e}")
                        
        if status_changed:
            self.save_tasks()

    @synchronized
    def process_queue(self):
        """
        Schedules next tasks up to concurrency limit based on episode progress percentages.
        """
        self.update_task_statuses()
        self.cleanup_completed_tasks(
            max_age_seconds=int(self.config.max_history_age_days * 86400),
            max_history_count=self.config.max_history_count
        )
        
        running_tasks = [t for t in self.tasks.values() if t.process is not None]
        max_concurrent = self.config.max_concurrent_caches
        
        if len(running_tasks) >= max_concurrent:
            return
            
        pending_tasks = [t for t in self.tasks.values() if t.status == "Pending"]
        if not pending_tasks:
            return
            
        # Priority Sorting: sort by calculated priority score (prioritizing immediate next episodes, then closest to finishing)
        pending_tasks.sort(key=lambda t: t.priority_score, reverse=True)
        
        slots_available = max_concurrent - len(running_tasks)
        queue_mutated = False
        for i in range(min(slots_available, len(pending_tasks))):
            next_task = pending_tasks[i]
            
            # Check if another process is handling it already
            existing_pid = is_file_being_cached(next_task.file_path)
            if existing_pid:
                # Adopt the running process
                next_task.status = "Caching"
                next_task.start_time = time.time()
                next_task.process = AdoptedProcess(existing_pid)
                next_task.pid = existing_pid
                logging.info(f"Queue: Adopting existing cache process PID {existing_pid} for '{os.path.basename(next_task.file_path)}'")
                queue_mutated = True
                continue
                
            # Launch task
            next_task.status = "Caching"
            next_task.start_time = time.time()
            
            logging.info(f"Queue: Launching cache for '{os.path.basename(next_task.file_path)}' (Plex progress: {next_task.progress_pct:.1f}%)")
            next_task.process = start_cache_process(self.config.cache_command, next_task.file_path)
            if next_task.process:
                next_task.pid = next_task.process.pid
                queue_mutated = True
                
        if queue_mutated:
            self.save_tasks()

    @synchronized
    def cleanup(self):
        """
        Cleans up any running/paused caching processes on shutdown.
        """
        for task in self.tasks.values():
            if task.process:
                try:
                    # Resume process if it was stopped (SIGSTOP'd) so it can receive SIGTERM and exit
                    try:
                        os.kill(task.process.pid, signal.SIGCONT)
                    except Exception:
                        pass
                    task.process.terminate()
                    task.process.wait(timeout=2)
                    logging.info(f"Queue: Cleaned up cache process PID {task.process.pid} on shutdown.")
                except Exception:
                    pass
