import os
import signal
import psutil
import shlex
import subprocess
import logging

def is_file_being_cached(file_path):
    """
    Checks if there is already an active process caching this file.
    Matches if any part of the process's command line contains the target file path.

    If a matching process is found in STOPPED (SIGSTOP'd) state — e.g. left over from
    a previous service instance that used the buffering guard — we send SIGCONT to resume
    it so it makes progress, rather than silently treating it as healthy.
    """
    try:
        file_path_abs = os.path.abspath(file_path)
        current_pid = os.getpid()

        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'status']):
            try:
                if proc.pid == current_pid:
                    continue

                cmdline = proc.info.get('cmdline')
                if not cmdline:
                    continue

                for arg in cmdline:
                    try:
                        arg_abs = os.path.abspath(arg)
                    except Exception:
                        arg_abs = ""

                    # Check both exact match and substring match (useful if filepath is wrapped in a URL or custom arg string)
                    is_match = (file_path == arg or file_path_abs == arg or 
                                (arg_abs and file_path_abs == arg_abs) or 
                                (file_path in arg) or 
                                (file_path_abs in arg) or 
                                (arg_abs and file_path_abs in arg_abs))
                    
                    if is_match:
                        proc_status = proc.info.get('status', '')

                        if proc_status == psutil.STATUS_STOPPED:
                            # Process is SIGSTOP'd (likely frozen from a previous service instance).
                            # Resume it so caching continues rather than hanging forever.
                            try:
                                os.kill(proc.pid, signal.SIGCONT)
                                logging.info(
                                    f"Resumed stopped cache process PID {proc.pid} for '{file_path}' "
                                    f"(was SIGSTOP'd by a previous service instance)."
                                )
                            except Exception as e:
                                logging.warning(f"Failed to resume stopped cache process PID {proc.pid}: {e}")

                        logging.info(
                            f"Found active caching process: PID {proc.pid} ({proc.info['name']}) "
                            f"is handling '{file_path}'"
                        )
                        return proc.pid
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        return None
    except Exception as e:
        logging.debug(f"Error checking processes: {e}")
        return None

def start_cache_process(command_template, file_path):
    """
    Spawns a decoupled background process to cache the target file path.
    Replaces {file_path} in the command template.
    """
    try:
        cmd_str = command_template.format(file_path=file_path)
        cmd_args = shlex.split(cmd_str)

        logging.info(f"Launching cache process: {cmd_str}")
        # start_new_session=True decouples the process group so it lives on after the python script finishes
        process = subprocess.Popen(
            cmd_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        return process
    except Exception as e:
        logging.error(f"Error starting cache process for '{file_path}': {e}")
        return None
